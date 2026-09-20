"""Code-search backend — ripgrep-accelerated with a pure-Python fallback.

Why this module exists
======================
The original ``grep`` / ``glob`` tools walked the tree with ``os.walk`` and
searched byte-by-byte in Python, from inside an ``async`` method. That makes
search slow in every project (O(bytes of the whole tree)), blind to
``.gitignore`` (so ``dist/``, ``target/``, ``.next/`` … all get scanned), and —
because the work is synchronous — it blocks the event loop, freezing the TUI.

Like Claude Code's Grep/Glob tools (built on ripgrep) and Codex's
``file-search`` (the ripgrep ``ignore`` crate), the fix is to push the actual
work to a fast, ignore-aware engine and keep a correct fallback:

* **Tier 1 — ripgrep** (``rg``): parallel, respects ``.gitignore``/``.ignore``,
  skips binaries. Auto-detected; never a hard dependency.
* **Tier 2 — pure Python**: an expanded prune set, ``git``-aware file listing
  (authoritative ignore semantics without a ``.gitignore`` parser), and a
  thread pool so a search never blocks the event loop.

Both tiers return matches in the same order with the same shape, so tool output
is identical no matter which engine ran.
"""

from __future__ import annotations

# ── 独立调试支持：允许直接运行本文件（python openx/.../xxx.py）──────
if __name__ == "__main__" and not __package__:
    import sys as _sys
    from pathlib import Path as _Path
    _file = _Path(__file__).resolve()
    _root = _file.parent
    while _root != _root.parent and not (_root / "pyproject.toml").exists():
        _root = _root.parent
    _sys.path.insert(0, str(_root))
    __package__ = ".".join(_file.relative_to(_root).parts[:-1])

import asyncio
import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "Match",
    "find_ripgrep",
    "grep_files",
    "is_git_repo",
    "iter_files",
    "list_matching_files",
    "resolve_backend",
]

# 目录剪枝集：原 GrepTool._skip_dirs 的超集——把各语言生态里体积大、几乎
# 不含源码的输出目录一并剪掉。rg 路径用同名 glob 排除，保证两引擎一致。
_SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".eggs", "site-packages",
    "target", ".next", ".nuxt", ".gradle", "out", ".idea", ".vscode",
    ".cache", "coverage", ".pnpm-store", ".yarn", "vendor", "Pods",
    ".terraform", ".serverless", "bower_components",
})

# 二进制/资源后缀：搜索它们既慢又无意义（read_text 会把二进制解成乱码行）。
_BINARY_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe", ".bin", ".o", ".a",
    ".class", ".jar", ".war", ".wasm",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".ico", ".svg", ".webp",
    ".mp3", ".mp4", ".avi", ".mov", ".mkv", ".wav", ".ogg", ".flac",
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".lock", ".pack", ".idx",
})


@dataclass(frozen=True)
class Match:
    """一条命中：文件路径（绝对）、1-based 行号、该行原文（不含换行）。"""

    path: Path
    lineno: int
    line: str


# ── ripgrep 探测 ─────────────────────────────────────────────────


_rg_cache: str | None = None


def find_ripgrep() -> str | None:
    """返回可用的 ``rg`` 可执行路径，找不到返回 ``None``（结果缓存）。

    探测优先级：``OPENX_RIPGREP`` 环境变量（绝对路径，或 ``"none"``/``""``
    显式关闭）> ``PATH`` 上的 ``rg``。探测只做一次并缓存——避免每次搜索都
    付一次 ``shutil.which`` + 文件系统遍历。
    """
    global _rg_cache
    if _rg_cache is not None:
        return _rg_cache or None

    override = os.environ.get("OPENX_RIPGREP")
    if override is not None:
        override = override.strip()
        if override and override.lower() != "none":
            _rg_cache = override
            return override
        _rg_cache = ""  # 显式关闭
        return None

    found = shutil.which("rg")
    _rg_cache = found or ""
    return found


def resolve_backend(backend: str) -> str:
    """把用户的 ``backend`` 偏好解析成实际引擎：``"ripgrep"`` 或 ``"python"``。"""
    backend = (backend or "auto").lower()
    if backend == "python":
        return "python"
    if backend == "ripgrep":
        return "ripgrep" if find_ripgrep() else "python"
    # auto（默认）：有 rg 就用 rg，否则纯 Python 兜底。
    return "ripgrep" if find_ripgrep() else "python"


# ── git 感知的文件枚举 ───────────────────────────────────────────


def is_git_repo(root: Path) -> bool:
    """``root`` 是否位于某个 git 工作区内（向上找 ``.git``；兼容 worktree 文件）。"""
    p = root if root.is_dir() else root.parent
    while True:
        if (p / ".git").exists():
            return True
        if p.parent == p:
            return False
        p = p.parent


def _git_listed_relpaths(root: Path) -> list[str] | None:
    """用 ``git ls-files`` 取「git 认为相关」的文件（相对 ``root`` 的 posix 路径）。

    ``--cached --others --exclude-standard`` = 已跟踪 + 未跟踪且未被忽略 =
    权威的 ``.gitignore`` 语义（含 ``.git/info/exclude`` 与全局 ignore），
    无需自写 ``.gitignore`` 解析器。失败/非 git 仓库返回 ``None``（调用方回落
    ``os.walk``）。
    """
    if not is_git_repo(root):
        return None
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=str(root),
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out: list[str] = []
    for chunk in proc.stdout.split(b"\x00"):
        if chunk:
            out.append(chunk.decode("utf-8", "surrogateescape"))
    return out


def _accept(path: Path, include: str | None) -> bool:
    """文件级过滤：非二进制 + 命中 ``include`` glob。"""
    if path.suffix in _BINARY_SUFFIXES:
        return False
    return not (include and not path.match(include))


def _walk_files(root: Path, include: str | None) -> list[Path]:
    """纯 Python 遍历：原地剪枝 ``_SKIP_DIRS``（只列文件，不读内容）。"""
    files: list[Path] = []
    for dirpath, dirs, filenames in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            if _accept(p, include):
                files.append(p)
    return files


def iter_files(
    root: Path, include: str | None = None, respect_gitignore: bool = True
) -> list[Path]:
    """枚举待搜索的文件（绝对路径，已排序）。

    - ``respect_gitignore`` 且位于 git 仓库 → 走 ``git ls-files``（权威 ignore
      语义，一次子进程）；否则回落到 ``os.walk`` + 扩展剪枝集。两条路径都过滤
      二进制后缀、``_SKIP_DIRS`` 与 ``include``。
    """
    if root.is_file():
        return [root]

    if respect_gitignore:
        rels = _git_listed_relpaths(root)
        if rels is not None:
            files: list[Path] = []
            for rel in rels:
                parts = Path(rel).parts
                if _SKIP_DIRS.intersection(parts):
                    continue
                p = root / rel
                if p.is_file() and _accept(p, include):
                    files.append(p)
            return sorted(files, key=str)

    return sorted(_walk_files(root, include), key=str)


# ── 纯 Python 搜索（兜底引擎）────────────────────────────────────


def _compile(pattern: str, is_regex: bool, case_sensitive: bool) -> re.Pattern:
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(pattern if is_regex else re.escape(pattern), flags)


def _scan_file(path: Path, compiled: re.Pattern, max_matches: int) -> list[Match]:
    """单文件逐行搜索；返回该文件的命中（用于线程池）。"""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001  单文件读失败（权限/编码）跳过即可
        return []
    hits: list[Match] = []
    for i, line in enumerate(content.splitlines()):
        if compiled.search(line):
            hits.append(Match(path=path, lineno=i + 1, line=line))
            if len(hits) >= max_matches:
                break
    return hits


def _grep_python_sync(
    root: Path,
    pattern: str,
    is_regex: bool,
    case_sensitive: bool,
    include: str | None,
    respect_gitignore: bool,
    max_matches: int,
) -> list[Match]:
    compiled = _compile(pattern, is_regex, case_sensitive)

    if root.is_file():
        return _scan_file(root, compiled, max_matches)[:max_matches]

    files = iter_files(root, include, respect_gitignore)
    if not files:
        return []

    workers = min(8, max(1, (os.cpu_count() or 2)))
    batch = max(workers * 4, 16)
    out: list[Match] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i in range(0, len(files), batch):
            chunk = files[i:i + batch]
            # 逐批提交（而非 ``pool.map``，后者会一次性提交全部任务，毁掉早停）
            futures = [pool.submit(_scan_file, f, compiled, max_matches) for f in chunk]
            for fut in futures:
                out.extend(fut.result())
            if len(out) >= max_matches:
                break

    out.sort(key=lambda m: (str(m.path), m.lineno))
    return out[:max_matches]


# ── ripgrep 搜索（首选引擎）─────────────────────────────────────


def parse_rg_json_stream(
    lines: Iterable[str], root: Path, max_matches: int
) -> tuple[list[Match], bool]:
    """解析 ``rg --json`` 输出流 → ``(matches, truncated)``。

    纯函数（无 IO），便于单测。``--json`` 每行一个事件对象；只关心
    ``type == "match"``，取 ``data.path.text`` / ``data.line_number`` /
    ``data.lines.text``。非法/非匹配行忽略。命中数达 ``max_matches`` 即停。
    """
    matches: list[Match] = []
    for raw in lines:
        if not raw:
            continue
        try:
            evt = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(evt, dict) or evt.get("type") != "match":
            continue
        m = match_from_event(evt, root)
        if m is None:
            continue
        matches.append(m)
        if len(matches) >= max_matches:
            return matches, True
    return matches, False


def match_from_event(evt: dict, root: Path) -> Match | None:
    """从一条 rg ``match`` 事件构造 :class:`Match`（不可用时返回 ``None``）。"""
    data = evt.get("data") or {}
    path_info = data.get("path") or {}
    text = path_info.get("text")
    if not isinstance(text, str):
        return None  # 非 UTF-8 路径（rg 给 base64 bytes）——跳过
    lines_info = data.get("lines") or {}
    line_text = lines_info.get("text")
    if not isinstance(line_text, str):
        line_text = ""
    lineno = data.get("line_number")
    try:
        lineno = int(lineno)
    except (TypeError, ValueError):
        return None
    return Match(
        path=root / text,
        lineno=lineno,
        line=line_text.rstrip("\n").rstrip("\r"),
    )


def _rg_base_args(respect_gitignore: bool) -> list[str]:
    args = ["--no-config", "--color=never", "--json", "--hidden", "--no-messages"]
    if not respect_gitignore:
        args.append("--no-ignore")
    # 显式排除剪枝目录（--hidden 会让 .git 也可见，--no-ignore 也不会剪）。
    # 放在 include 之后 → 排除优先。
    for d in sorted(_SKIP_DIRS):
        args += ["-g", f"!**/{d}/**"]
    return args


async def _grep_ripgrep(
    rg: str,
    root: Path,
    pattern: str,
    is_regex: bool,
    case_sensitive: bool,
    include: str | None,
    respect_gitignore: bool,
    max_matches: int,
) -> list[Match] | None:
    """跑 ``rg --json`` 流式解析；命中达上限即终止进程早停。

    返回 ``None`` 表示 rg 不可用/出错（调用方回落到纯 Python）。
    """
    args = [rg, *_rg_base_args(respect_gitignore)]
    args.append("--case-sensitive" if case_sensitive else "--ignore-case")
    if include:
        args += ["-g", include]
    if is_regex:
        args += ["-e", pattern]
    else:
        args += ["--fixed-strings", "-e", pattern]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError):
        return None

    matches: list[Match] = []
    truncated = False
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            break
        text = raw.decode("utf-8", "replace")
        try:
            evt = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(evt, dict) or evt.get("type") != "match":
            continue
        m = match_from_event(evt, root)
        if m is None:
            continue
        matches.append(m)
        if len(matches) >= max_matches:
            truncated = True
            break

    # 命中达上限：先终止进程再排空 stderr——否则子进程可能正阻塞在已满的
    # stdout 管道上，永不关闭 stderr，排空读取会死锁。
    if truncated:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.stderr.read(), timeout=5)
    except Exception:  # noqa: BLE001,S110  stderr 排空尽力而为，绝不因它失败
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()

    rc = proc.returncode
    if not truncated and rc is not None and rc >= 2:
        # rg 报错（未知 flag / 无效 pattern）→ 交给纯 Python 兜底，绝不假装"无命中"
        return None

    matches.sort(key=lambda m: (str(m.path), m.lineno))
    return matches[:max_matches]


# ── 公开 API（异步）─────────────────────────────────────────────


async def grep_files(
    root: Path,
    pattern: str,
    *,
    is_regex: bool = False,
    case_sensitive: bool = True,
    include: str | None = None,
    respect_gitignore: bool = True,
    backend: str = "auto",
    max_matches: int = 500,
) -> list[Match]:
    """搜索 ``root``，返回按 ``(路径, 行号)`` 排序的命中列表。

    rg 路径走真异步子进程，Python 路径走 ``asyncio.to_thread``——两者都不
    阻塞事件循环，因此 TUI 不冻结、执行器的并行 ``gather`` 真正并发。
    """
    root = Path(root)
    if not root.is_file() and resolve_backend(backend) == "ripgrep":
        rg = find_ripgrep()
        if rg:
            res = await _grep_ripgrep(
                rg, root, pattern, is_regex, case_sensitive,
                include, respect_gitignore, max_matches,
            )
            if res is not None:
                return res

    return await asyncio.to_thread(
        _grep_python_sync, root, pattern, is_regex, case_sensitive,
        include, respect_gitignore, max_matches,
    )


# ── glob 支持（pathlib 语义）────────────────────────────────────


def _glob_python_sync(root: Path, pattern: str, respect_gitignore: bool) -> list[str]:
    """``root.glob(pattern)`` 语义 + 剪枝 + 可选 gitignore 过滤。

    刻意保留 pathlib 的 glob 语义（``*.py`` 只匹配顶层、``**/*.py`` 递归）
    ——它与 ``fnmatch``/``PurePath.match`` 不同，不能替换。性能收益来自
    「线程卸载 + 扩展剪枝 + gitignore 过滤」，而非替换匹配引擎。
    """
    try:
        raw = list(root.glob(pattern))
    except Exception:  # noqa: BLE001  非法 pattern / IO 失败 → 视为无匹配
        return []

    allowed: set[str] | None = None
    if respect_gitignore:
        rels = _git_listed_relpaths(root)
        if rels is not None:
            allowed = set(rels)

    out: set[str] = set()
    for p in raw:
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            continue
        if _SKIP_DIRS.intersection(Path(rel).parts):
            continue
        if p.is_file():
            if p.suffix in _BINARY_SUFFIXES:
                continue
            if allowed is not None and rel not in allowed:
                continue
        out.add(rel)
    return sorted(out)


async def list_matching_files(
    root: Path, pattern: str, *, respect_gitignore: bool = True
) -> list[str]:
    """返回匹配 ``pattern`` 的相对路径（排序）；线程卸载以免阻塞事件循环。"""
    return await asyncio.to_thread(_glob_python_sync, Path(root), pattern, respect_gitignore)


if __name__ == "__main__":
    # 独立调试：不依赖 rg/git，验证纯 Python 引擎 + JSON 解析器 + 探测逻辑
    import asyncio
    import tempfile

    async def _self_check():
        with tempfile.TemporaryDirectory() as ws:
            ws_path = Path(ws)
            (ws_path / "sub").mkdir()
            (ws_path / "sub" / "a.py").write_text("needle here\nplain\n")
            (ws_path / "b.py").write_text("nothing\nneedle again\n")
            (ws_path / "node_modules").mkdir()
            (ws_path / "node_modules" / "c.py").write_text("needle hidden\n")

            # 纯 Python 引擎：跳过 node_modules，命中两处
            hits = await grep_files(
                ws_path, "needle", respect_gitignore=False, backend="python"
            )
            rels = [f"{m.path.relative_to(ws_path)}:{m.lineno}" for m in hits]
            assert rels == ["b.py:2", "sub/a.py:1"], rels
            print("python grep:", rels)

            # rg JSON 流解析（喂固定事件，无需真 rg）
            stream = [
                json.dumps({"type": "begin", "data": {"path": {"text": "b.py"}}}),
                json.dumps({"type": "match", "data": {
                    "path": {"text": "b.py"}, "line_number": 2,
                    "lines": {"text": "needle again\n"}}}),
                "not json",
                json.dumps({"type": "end", "data": {}}),
            ]
            parsed, truncated = parse_rg_json_stream(stream, ws_path, 10)
            assert len(parsed) == 1 and not truncated, parsed
            assert parsed[0].lineno == 2 and parsed[0].line == "needle again", parsed
            print("rg json parse:", parsed)

            # 上限截断
            many = [json.dumps({"type": "match", "data": {
                "path": {"text": f"f{i}.py"}, "line_number": 1,
                "lines": {"text": "x\n"}}}) for i in range(50)]
            parsed2, trunc2 = parse_rg_json_stream(many, ws_path, 10)
            assert len(parsed2) == 10 and trunc2, (len(parsed2), trunc2)

            # glob（pathlib 语义：*.py 只匹配顶层）
            files = await list_matching_files(ws_path, "*.py", respect_gitignore=False)
            assert files == ["b.py"], files
            # pathlib 的 **/ 匹配零或多个目录段 → 递归含顶层
            nested = await list_matching_files(ws_path, "**/*.py", respect_gitignore=False)
            assert nested == ["b.py", "sub/a.py"], nested
            print("glob:", files, nested)

            assert resolve_backend("python") == "python"
            assert find_ripgrep() is None or isinstance(find_ripgrep(), str)
            print("backend:", resolve_backend("auto"), "rg:", find_ripgrep())

    asyncio.run(_self_check())
    print("openx/tools/fs_search.py OK ✓")
