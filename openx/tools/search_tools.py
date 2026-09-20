"""Search tools for OpenX — ripgrep-accelerated code search.

The heavy lifting lives in :mod:`openx.tools.fs_search`: an ``rg`` backend when
ripgrep is available, and an optimized pure-Python fallback (git-aware file
listing, expanded pruning, thread pool) otherwise. Both run off the event loop
so a search never freezes the TUI. This module only formats the shared result
shape into the stable ``path:line: text`` output the model expects.
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

import re
from pathlib import Path

from ..permissions import Permission
from . import fs_search
from .base import ToolResult, WorkspaceTool, truncate_output


class GrepTool(WorkspaceTool):
    """Search file contents by pattern (like grep)."""

    name = "grep"
    description = (
        "Search file contents for a pattern. "
        "Returns matching lines with file path and line number. "
        "Use this to find where a function, class, variable, or string is used. "
        "Supports regex when is_regex=True. "
        "Backed by ripgrep when available (parallel, respects .gitignore); "
        "otherwise falls back to an equally-scoped pure-Python search. "
        "Binary files, node_modules, .git, and __pycache__ are always skipped."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Search pattern. Plain text by default, regex if is_regex=True.",
            },
            "path": {
                "type": "string",
                "description": "File or directory to search in. Defaults to workspace root.",
            },
            "is_regex": {
                "type": "boolean",
                "description": "Treat pattern as regex. Default: false.",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "Case-sensitive search. Default: true.",
            },
            "context_lines": {
                "type": "integer",
                "description": "Number of context lines before/after each match (0-5). Default: 0.",
            },
            "include": {
                "type": "string",
                "description": "Glob pattern to filter files (e.g., '*.py'). Optional.",
            },
        },
        "required": ["pattern"],
    }

    _MAX_MATCHES = 500

    def __init__(
        self,
        workspace: str,
        respect_gitignore: bool = True,
        backend: str = "auto",
    ):
        super().__init__(workspace)
        self.respect_gitignore = respect_gitignore
        self.backend = backend

    @property
    def permission(self) -> Permission:
        return Permission.allow()

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        is_regex: bool = False,
        case_sensitive: bool = True,
        context_lines: int = 0,
        include: str | None = None,
    ) -> ToolResult:
        search_path = Path(path)
        if not search_path.is_absolute():
            search_path = self.workspace / path
        search_path = search_path.resolve()

        if not search_path.exists():
            return ToolResult(error=f"Path not found: {search_path}")

        # 早期正则校验：非法 pattern 立刻报错（rg 与纯 Python 口径一致）。
        if is_regex:
            try:
                re.compile(pattern)
            except re.error as e:
                return ToolResult(error=f"Invalid regex pattern: {e}")

        context_lines = max(0, min(5, context_lines))

        matches = await fs_search.grep_files(
            search_path,
            pattern,
            is_regex=is_regex,
            case_sensitive=case_sensitive,
            include=include,
            respect_gitignore=self.respect_gitignore,
            backend=self.backend,
            max_matches=self._MAX_MATCHES,
        )
        if not matches:
            return ToolResult(output=f"No matches found for: {pattern}")

        match_count = len(matches)
        ctx_cache: dict[Path, list[str]] = {}
        results: list[str] = []
        for m in matches:
            rel_path = self._rel(m.path)
            if context_lines > 0:
                lines = self._lines(m.path, ctx_cache)
                if lines is None:
                    results.append(f"{rel_path}:{m.lineno}: {m.line}")
                    continue
                idx = m.lineno - 1
                ctx_start = max(0, idx - context_lines)
                ctx_end = min(len(lines), idx + context_lines + 1)
                for j in range(ctx_start, ctx_end):
                    marker = ">" if j == idx else " "
                    results.append(f"{rel_path}:{j + 1}:{marker} {lines[j]}")
                results.append("---")
            else:
                results.append(f"{rel_path}:{m.lineno}: {m.line}")

        output = "\n".join(results)
        truncated, was_truncated, _notice = truncate_output(output, max_lines=1000)
        if was_truncated:
            notice = f"\n[Showing first ~1000 lines. {match_count} total matches.]"
        else:
            notice = f"\n[{match_count} match(es) total]"

        return ToolResult(
            output=truncated + notice,
            truncated=was_truncated,
            truncated_notice="",
        )

    def _rel(self, path: Path) -> str:
        """工作区相对路径；不在工作区内（绝对路径搜索）时回退为绝对路径。"""
        try:
            return str(path.relative_to(self.workspace))
        except ValueError:
            return str(path)

    @staticmethod
    def _lines(path: Path, cache: dict[Path, list[str]]) -> list[str] | None:
        """读取文件行（带缓存）；失败返回 None（context 渲染回退到单行）。"""
        if path in cache:
            return cache[path]
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001  读失败 → 上下文渲染回退到单行
            cache[path] = []  # 占位，避免重复尝试
            return None
        lines = content.splitlines()
        cache[path] = lines
        return lines


if __name__ == "__main__":
    # 独立调试：临时目录写含关键字的文件，GrepTool 搜索并断言命中
    import asyncio
    import tempfile

    async def _self_check():
        with tempfile.TemporaryDirectory() as ws:
            (Path(ws) / "sample.py").write_text(
                "def openx_grep_target():\n    return 42\n"
            )
            (Path(ws) / "node_modules").mkdir()
            (Path(ws) / "node_modules" / "junk.py").write_text("openx_grep_target\n")

            r = await GrepTool(ws).execute(pattern="openx_grep_target")
            assert r.success and "sample.py" in r.output, r.output
            assert "node_modules" not in r.output, r.output
            print(r.output)

            # 上下文行：命中行带 '>' 标记，块尾 '---'
            r2 = await GrepTool(ws).execute(
                pattern="openx_grep_target", context_lines=1
            )
            assert "> " in r2.output and "---" in r2.output, r2.output

            # 非法 regex → 报错
            bad = await GrepTool(ws).execute(pattern="(", is_regex=True)
            assert not bad.success and "Invalid regex" in bad.error, bad.error
            print("openx/tools/search_tools.py OK ✓")

    asyncio.run(_self_check())
