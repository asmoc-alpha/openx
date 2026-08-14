"""Search tools for OpenX — grep-based code search."""

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

import os
import re
from pathlib import Path
from typing import Optional

from .base import Tool, ToolResult, WorkspaceTool, truncate_output
from ..permissions import Permission


class GrepTool(WorkspaceTool):
    """Search file contents by pattern (like grep)."""

    name = "grep"
    description = (
        "Search file contents for a pattern. "
        "Returns matching lines with file path and line number. "
        "Use this to find where a function, class, variable, or string is used. "
        "Supports regex when is_regex=True. "
        "Automatically skips binary files, node_modules, .git, and __pycache__."
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

    def __init__(self, workspace: str):
        super().__init__(workspace)
        self._skip_dirs = {
            ".git", "node_modules", "__pycache__", ".venv", "venv",
            ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
            "dist", "build", ".eggs", "*.egg-info",
        }

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
        include: Optional[str] = None,
    ) -> ToolResult:
        search_path = Path(path)
        if not search_path.is_absolute():
            search_path = self.workspace / path
        search_path = search_path.resolve()

        if not search_path.exists():
            return ToolResult(error=f"Path not found: {search_path}")

        flags = 0 if case_sensitive else re.IGNORECASE

        try:
            if is_regex:
                compiled = re.compile(pattern, flags)
            else:
                compiled = re.compile(re.escape(pattern), flags)
        except re.error as e:
            return ToolResult(error=f"Invalid regex pattern: {e}")

        context_lines = max(0, min(5, context_lines))

        results: list[str] = []
        match_count = 0
        max_matches = 500

        files = self._collect_files(search_path, include)
        for file_path in files:
            if match_count >= max_matches:
                break
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            lines = content.splitlines()
            for i, line in enumerate(lines):
                if match_count >= max_matches:
                    break
                if compiled.search(line):
                    match_count += 1
                    rel_path = file_path.relative_to(self.workspace)

                    if context_lines > 0:
                        ctx_start = max(0, i - context_lines)
                        ctx_end = min(len(lines), i + context_lines + 1)
                        for j in range(ctx_start, ctx_end):
                            marker = ">" if j == i else " "
                            results.append(
                                f"{rel_path}:{j + 1}:{marker} {lines[j]}"
                        )
                        results.append("---")
                    else:
                        results.append(f"{rel_path}:{i + 1}: {line}")

        if not results:
            return ToolResult(output=f"No matches found for: {pattern}")

        output = "\n".join(results)
        truncated, was_truncated, notice = truncate_output(output, max_lines=1000)
        if was_truncated:
            notice = f"\n[Showing first ~1000 lines. {match_count} total matches.]"
        else:
            notice = f"\n[{match_count} match(es) total]"

        return ToolResult(
            output=truncated + notice,
            truncated=was_truncated,
            truncated_notice="",
        )

    def _collect_files(self, search_path: Path, include: Optional[str]) -> list[Path]:
        """收集待搜索的文本文件。

        用 ``os.walk`` 而非 ``Path.walk``——后者仅在 Python 3.12+ 可用，
        而项目声明支持 3.10+。通过原地修改 ``dirs[:]`` 剪枝，避免递归进入
        ``_skip_dirs`` 中的目录，显著减少 IO。
        """
        if search_path.is_file():
            return [search_path]

        files: list[Path] = []
        # os.walk 的 dirs 列表原地修改即可实现剪枝（walk 不会再访问被删除的目录）
        for root, dirs, filenames in os.walk(search_path):
            # 跳过无关目录：node_modules、虚拟环境、各种缓存等
            dirs[:] = [d for d in dirs if d not in self._skip_dirs]

            for fname in filenames:
                fpath = Path(root) / fname
                # 应用 include glob 过滤（如 '*.py'）
                if include:
                    if not fpath.match(include):
                        continue

                # 跳过二进制文件：搜索二进制既慢又无意义
                if fpath.suffix in {".pyc", ".pyo", ".so", ".dll", ".exe", ".bin",
                                     ".jpg", ".png", ".gif", ".ico", ".svg",
                                     ".mp3", ".mp4", ".avi", ".mov",
                                     ".zip", ".tar", ".gz", ".7z",
                                     ".pdf", ".doc", ".docx", ".xlsx",
                                     ".ttf", ".otf", ".woff", ".woff2"}:
                    continue

                files.append(fpath)

        return sorted(files)


if __name__ == "__main__":
    # 独立调试：临时目录写含关键字的文件，GrepTool 搜索并断言命中
    import asyncio
    import tempfile

    async def _self_check():
        with tempfile.TemporaryDirectory() as ws:
            (Path(ws) / "sample.py").write_text(
                "def openx_grep_target():\n    return 42\n"
            )
            r = await GrepTool(ws).execute(pattern="openx_grep_target")
            assert r.success and "openx_grep_target" in r.output, r.output
            print(r.output)

    asyncio.run(_self_check())
    print("openx/tools/search_tools.py OK ✓")
