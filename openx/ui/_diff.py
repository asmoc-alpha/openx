"""Language-aware before/after diff rendering for the streaming transcript.

文本操作（create / update / delete）的「变更前后对比」渲染归属地
==============================================================
主转录里的文本操作块需要三件事，且**全在展示层**完成（绝不改工具 schema
或结果文本，模型可见接口零变化）：

1. **操作分类**（``classify_text_op``）：由变更本身推导 create / update /
   delete 三类，供头行打标；
2. **变更前后对比**（``render_diff_lines``）：unified diff 逐行渲染成 Rich
   ``Text``，``+``/``-`` 标记保留并按语义着色（绿/红）；
3. **多语言代码高亮**：每行**内容**按文件扩展名对应的语言 lexer 着色
   （``detect_lexer`` + pygments → ANSI → ``Text.from_ansi``）——同一份 diff
   里 Python / JS / JSON / Rust… 各按各的语言上色。

高亮只用 pygments 公有 API（``highlight`` + ``Terminal256Formatter`` +
``Text.from_ansi``），不触碰 rich 私有面；任何环节失败一律 fail-open
（退回无高亮的纯色标记），坏行绝不能让会话崩。
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

import json
from pathlib import Path

from rich.text import Text

# ── 扩展名 → pygments lexer 别名（多语言高亮表）────────────────────
# 覆盖常见语言；未命中 → None（无语言高亮，仅 +/- 语义色）。
EXT_LEXERS: dict[str, str] = {
    # 脚本 / 通用
    ".py": "python", ".pyi": "python", ".pyw": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "jsx", ".ts": "typescript", ".tsx": "tsx",
    ".rb": "ruby", ".php": "php", ".pl": "perl", ".pm": "perl",
    ".lua": "lua", ".r": "r", ".jl": "julia",
    # 系统 / 编译型
    ".go": "go", ".rs": "rust", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".cs": "csharp", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".swift": "swift", ".scala": "scala", ".dart": "dart", ".m": "objective-c",
    # 标记 / 数据 / 配置
    ".json": "json", ".jsonc": "json", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".ini": "ini", ".cfg": "ini", ".conf": "ini",
    ".xml": "xml", ".html": "html", ".htm": "html", ".svg": "xml",
    ".md": "markdown", ".markdown": "markdown", ".rst": "rst",
    ".css": "css", ".scss": "scss", ".sass": "sass", ".less": "less",
    # shell / 构建 / 其他
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".fish": "fish",
    ".ps1": "powershell", ".psm1": "powershell",
    ".sql": "sql", ".tex": "tex", ".vim": "vim", ".dockerfile": "docker",
    ".gradle": "groovy", ".groovy": "groovy", ".el": "common-lisp",
    ".ex": "elixir", ".exs": "elixir", ".erl": "erlang", ".hs": "haskell",
    ".clj": "clojure", ".fs": "fsharp", ".vb": "vbnet",
}

# 无扩展名 / 特殊文件名 → lexer（小写匹配）
_FILENAME_LEXERS: dict[str, str] = {
    "dockerfile": "docker",
    "makefile": "make",
    "gnumakefile": "make",
    "cmakelists.txt": "cmake",
    ".gitignore": "gitignore",
    ".dockerignore": "gitignore",
    ".env": "bash",
    "rakefile": "ruby",
    "gemfile": "ruby",
    "justfile": "make",
}

# 操作标记的语义配色（green 增 / red 删；update 用黄色区分）
_OP_STYLES: dict[str, str] = {
    "create": "green",
    "update": "yellow",
    "delete": "red",
}


def detect_lexer(path: str) -> str | None:
    """文件路径 → pygments lexer 别名（多语言高亮）；未知 → None。"""
    try:
        p = Path(str(path))
    except Exception:
        return None
    name = p.name.lower()
    if name in _FILENAME_LEXERS:
        return _FILENAME_LEXERS[name]
    return EXT_LEXERS.get(p.suffix.lower())


def op_style(op: str) -> str:
    """操作名 → 语义色（未知回落 dim）。"""
    return _OP_STYLES.get(op, "dim")


def classify_text_op(
    name: str, arguments: str, old: str, new: str
) -> str | None:
    """文本操作 → ``create`` / ``update`` / ``delete``（无变更 → None）。

    纯展示层推导（无 delete 工具）：分类只依据工具名 + 参数 + 变更前后内容：

    - ``write_file``：old 空 → create；old 非空且 new 空 → delete；否则 update；
    - ``edit_file``：``new_text`` 为空串（删掉匹配文本）→ delete，否则 update；
    - 其他/兜底：按内容推导（仅增 → create、仅减 → delete、其余 → update）。

    任一环节异常 → None（调用方回退既有渲染）。
    """
    try:
        args = json.loads(arguments) if arguments else {}
    except (ValueError, TypeError):
        args = {}
    if not isinstance(args, dict):
        args = {}

    if name == "edit_file":
        # 仅在参数明确给出且 new_text 为空串时判为 delete（删掉匹配文本）；
        # 参数缺失/坏 JSON（无法判定）保守回落 update——绝不无根据地声称删除。
        if "new_text" in args:
            new_text = args.get("new_text", "")
            if isinstance(new_text, str) and new_text.strip() == "":
                return "delete"
        return "update"

    if name == "write_file":
        if not old and new:
            return "create"
        if old and not new:
            return "delete"
        if not old and not new:
            return None
        return "update"

    # 兜底：仅按内容推导（未知工具名但带变更预览）
    if not old and new:
        return "create"
    if old and not new:
        return "delete"
    if not old and not new:
        return None
    return "update"


def diff_stat(old: str, new: str) -> tuple[int, int]:
    """变更行数统计 ``(added, removed)``（``---``/``+++`` 头不计）。"""
    import difflib

    added = removed = 0
    for ln in difflib.unified_diff(
        old.splitlines(), new.splitlines(), lineterm="", n=0
    ):
        if ln.startswith(("+++", "---")):
            continue
        if ln.startswith("+"):
            added += 1
        elif ln.startswith("-"):
            removed += 1
    return added, removed


def _highlight_ansi(code: str, lexer_name: str, theme: str) -> str:
    """单行源码 → ANSI 串（语言高亮）；任何失败 → 原样返回（fail-open）。"""
    try:
        from pygments import highlight
        from pygments.formatters import Terminal256Formatter
        from pygments.lexers import get_lexer_by_name

        lexer = get_lexer_by_name(lexer_name)
        out = highlight(
            code, lexer, Terminal256Formatter(style=theme, nowrap=True)
        )
        return out.rstrip("\n")
    except Exception:
        return code


def _content_text(content: str, lexer_name: str | None, theme: str) -> Text:
    """行内容 → Rich ``Text``（有 lexer 则语言高亮，否则纯文本）。"""
    if not content:
        return Text()
    ansi = _highlight_ansi(content, lexer_name, theme) if lexer_name else content
    try:
        return Text.from_ansi(ansi)
    except Exception:
        return Text(content)


def render_diff_lines(
    old: str,
    new: str,
    path: str,
    theme: str = "monokai",
    max_lines: int = 12,
    context: int = 3,
    more_hint: str = "",
) -> list[Text]:
    """变更前后对比 → Rich ``Text`` 行列表（无缩进，缩进由调用方加）。

    - ``@@`` 块头 → 青色；``+`` 行 → 绿标 + 语言高亮；``-`` 行 → 红标 +
      语言高亮；上下文行 → dim 标 + 语言高亮；
    - ``---``/``+++`` 头行略去（路径已在操作块头行展示）；
    - ``max_lines > 0`` 时按行截断，尾附 ``… +N more lines{more_hint}``
      （``more_hint`` 供调用方并入如 " (ctrl+t to expand)" 的热键提示）；
      无差异返回空列表。
    """
    import difflib

    lexer_name = detect_lexer(path)
    raw = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile="", tofile="", lineterm="", n=context,
    ))
    # 略去 ---/+++ 头（去掉后缀空格对齐的路径噪声）
    body = [
        ln for ln in raw
        if not (ln.startswith(("--- ", "+++ ")) or ln in ("---", "+++"))
    ]
    truncated = 0
    if max_lines and len(body) > max_lines:
        truncated = len(body) - max_lines
        body = body[:max_lines]

    lines: list[Text] = []
    for ln in body:
        if ln.startswith("@@"):
            lines.append(Text(ln, style="cyan"))
            continue
        marker = ln[:1]
        content = ln[1:]
        t = Text()
        if marker == "+":
            t.append("+", style="green")
            t.append(" ")
            t.append_text(_content_text(content, lexer_name, theme))
        elif marker == "-":
            t.append("-", style="red")
            t.append(" ")
            t.append_text(_content_text(content, lexer_name, theme))
        else:  # 上下文行（difflib 以单个空格开头）
            t.append(" ", style="dim")
            t.append(" ")
            t.append_text(_content_text(content, lexer_name, theme))
        lines.append(t)

    if truncated:
        lines.append(
            Text(f"… +{truncated} more lines{more_hint}", style="dim")
        )
    return lines


if __name__ == "__main__":
    # 独立调试：lexer 探测 / 分类 / 统计 / diff 渲染（纯内存，无副作用）
    assert detect_lexer("src/app.py") == "python"
    assert detect_lexer("a/b.tsx") == "tsx"
    assert detect_lexer("conf.json") == "json"
    assert detect_lexer("lib.rs") == "rust"
    assert detect_lexer("Dockerfile") == "docker"
    assert detect_lexer("weird.unknownext") is None

    # classify：create / update / delete 三类
    assert classify_text_op("write_file", "{}", "", "hello\n") == "create"
    assert classify_text_op("write_file", "{}", "old\n", "new\n") == "update"
    assert classify_text_op("write_file", "{}", "old\n", "") == "delete"
    assert classify_text_op(
        "edit_file", '{"old_text": "x", "new_text": ""}', "x\ny\n", "y\n"
    ) == "delete"
    assert classify_text_op(
        "edit_file", '{"old_text": "x", "new_text": "z"}', "x\n", "z\n"
    ) == "update"
    assert classify_text_op("write_file", "{}", "", "") is None

    a, r = diff_stat("a\nb\nc\n", "a\nX\nc\n")
    assert (a, r) == (1, 1), (a, r)

    # 渲染：包含 +/- 标记行；python 关键字带非默认样式
    rows = render_diff_lines(
        "def f():\n    return 1\n", "def f():\n    return 2\n", "m.py"
    )
    plain = "\n".join(t.plain for t in rows)
    assert "-" in plain and "+" in plain
    assert any(
        any("bold" in str(s.style) or "color" in str(s.style) or "#" in str(s.style)
            for s in t.spans)
        for t in rows if t.plain.strip().startswith("+")
    ), "期望语言高亮产生非默认样式 span"

    # 截断提示
    big_old = "\n".join(f"o{i}" for i in range(50))
    big_new = "\n".join(f"n{i}" for i in range(50))
    capped = render_diff_lines(big_old, big_new, "big.py", max_lines=5)
    assert capped[-1].plain.startswith("… +")

    from rich.console import Console
    Console().print("\n".join(t.plain for t in rows))
    print("openx/ui/_diff.py OK ✓")
