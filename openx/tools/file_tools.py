"""File operation tools for OpenX.

Provides: read_file, write_file, edit_file (find-and-replace), glob, list_directory.
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

import os
from pathlib import Path
from typing import Any, Optional

from .base import Tool, ToolResult, WorkspaceTool, truncate_output, unified_diff_text
from ..permissions import Permission, PermissionLevel


class ReadFileTool(WorkspaceTool):
    """Read a file from the filesystem with line-range support."""

    name = "read_file"
    description = (
        "Read a file from the local filesystem. "
        "Use start_line and end_line (1-based, inclusive) to read a range. "
        "Always prefer this over shell 'cat' — it respects workspace boundaries."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the file to read (absolute or relative to workspace).",
            },
            "start_line": {
                "type": "integer",
                "description": "First line to read (1-based, inclusive). Optional.",
            },
            "end_line": {
                "type": "integer",
                "description": "Last line to read (1-based, inclusive). Optional.",
            },
        },
        "required": ["file_path"],
    }

    @property
    def permission(self) -> Permission:
        return Permission.allow()

    async def execute(
        self,
        file_path: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
    ) -> ToolResult:
        path = self._resolve_path(file_path)
        if not path.exists():
            return ToolResult(error=f"File not found: {path}")
        if not path.is_file():
            return ToolResult(error=f"Not a file: {path}")

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return ToolResult(error=f"Failed to read file: {e}")

        lines = content.splitlines()

        # Apply line range
        if start_line is not None or end_line is not None:
            start = max(1, (start_line or 1)) - 1
            end = min(len(lines), (end_line or len(lines)))
            selected = lines[start:end]

            # Format with line numbers
            formatted = "\n".join(
                f"{i + start + 1:>6}\t{line}"
                for i, line in enumerate(selected)
            )
            output, truncated, notice = truncate_output(formatted)
            return ToolResult(output=output, truncated=truncated, truncated_notice=notice)

        # Full file
        formatted = "\n".join(
            f"{i + 1:>6}\t{line}" for i, line in enumerate(lines)
        )
        output, truncated, notice = truncate_output(formatted)
        return ToolResult(output=output, truncated=truncated, truncated_notice=notice)


class WriteFileTool(WorkspaceTool):
    """Create or overwrite a file."""

    name = "write_file"
    description = (
        "Create a new file or overwrite an existing file with the given content. "
        "Use edit_file for partial modifications instead of overwriting. "
        "Relative paths are resolved from the workspace root."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the file (absolute or relative to workspace).",
            },
            "content": {
                "type": "string",
                "description": "Full content to write to the file.",
            },
        },
        "required": ["file_path", "content"],
    }

    def __init__(self, workspace: str, allow_outside_workspace: bool = False):
        super().__init__(workspace)
        self.allow_outside = allow_outside_workspace

    @property
    def permission(self) -> Permission:
        return Permission(level=PermissionLevel.ASK, reason="Writing files")

    # 预览大小封顶：超大文件不做 diff 预览（弹窗渲染与内存保护）→ None
    _PREVIEW_MAX_CHARS = 200_000

    def preview_diff(self, args: dict) -> Optional[tuple[str, str, str]]:
        """变更预览：新文件 = 空→全文 diff；覆写 = 旧内容→新内容。"""
        try:
            file_path = args.get("file_path") or ""
            content = args.get("content")
            if not file_path or content is None:
                return None
            content = str(content)
            if len(content) > self._PREVIEW_MAX_CHARS:
                return None
            path = self._resolve_path(str(file_path))
            # 与 execute 同口径的工作区边界（预览不放大权限面）
            if not self.allow_outside and not path.is_relative_to(self.workspace):
                return None
            old = ""
            if path.exists():
                old = path.read_text(encoding="utf-8")
                if len(old) > self._PREVIEW_MAX_CHARS:
                    return None
            if old == content:
                return None  # 无变化不预览
            return (str(path), old, content)
        except Exception:
            return None

    async def execute(self, file_path: str, content: str) -> ToolResult:
        path = self._resolve_path(file_path)

        # Permission check for outside workspace.
        # is_relative_to 而非字符串 startswith —— 后者会被同前缀的
        # 兄弟目录绕过（如 workspace=/x/ws 时 /x/ws-evil 会通过检查）。
        if not self.allow_outside and not path.is_relative_to(self.workspace):
            return ToolResult(
                error=f"Cannot write outside workspace: {path}\n"
                f"Workspace: {self.workspace}"
            )

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            size = path.stat().st_size
            lines = content.count("\n") + 1
            return ToolResult(
                output=f"Wrote {lines} lines ({size} bytes) to {path}"
            )
        except Exception as e:
            return ToolResult(error=f"Failed to write file: {e}")


class EditFileTool(WorkspaceTool):
    """Find-and-replace text in a file (like sed but safer).

    语义参考 claude-code 的 FileEditTool：

    - 默认 ``replace_all=False``：要求 ``old_text`` 在文件中 **唯一**，
      否则报错——迫使模型提供更多上下文精确定位，避免误改多处。
    - ``replace_all=True``：替换全部匹配（适合批量重命名）。
    """

    name = "edit_file"
    description = (
        "Find and replace text in an existing file. By default old_text must be "
        "UNIQUE in the file (provide more surrounding context if not); set "
        "replace_all=true to replace every occurrence. This is the preferred way "
        "to modify files — use it instead of reading+writing the whole file."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the file to edit.",
            },
            "old_text": {
                "type": "string",
                "description": "Exact text to find. Must match exactly including whitespace.",
            },
            "new_text": {
                "type": "string",
                "description": "Replacement text.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace all occurrences. Default: false (requires unique match).",
            },
        },
        "required": ["file_path", "old_text", "new_text"],
    }

    def __init__(self, workspace: str, allow_outside_workspace: bool = False):
        super().__init__(workspace)
        self.allow_outside = allow_outside_workspace

    @property
    def permission(self) -> Permission:
        return Permission(level=PermissionLevel.ASK, reason="Editing files")

    async def execute(
        self,
        file_path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
    ) -> ToolResult:
        path = self._resolve_path(file_path)

        # 工作区边界保护（is_relative_to 防止 startswith 同前缀绕过）
        if not self.allow_outside and not path.is_relative_to(self.workspace):
            return ToolResult(error=f"Cannot edit outside workspace: {path}")

        if not path.exists():
            return ToolResult(error=f"File not found: {path}")

        try:
            content = path.read_text(encoding="utf-8")
        except Exception as e:
            return ToolResult(error=f"Failed to read file: {e}")

        count = content.count(old_text)
        if count == 0:
            return ToolResult(
                error=f"Text not found in {path}. The old_text must match exactly."
            )

        # 唯一匹配保护：非 replace_all 时，多处匹配会报错
        if not replace_all and count > 1:
            return ToolResult(
                error=(
                    f"old_text is not unique in {path} ({count} matches). "
                    "Provide more surrounding context to make it unique, "
                    "or set replace_all=true to replace all occurrences."
                )
            )

        # replace_all=False 且唯一 → 只替换第一处；
        # replace_all=True → 替换全部
        if replace_all:
            new_content = content.replace(old_text, new_text)
            replaced = count
        else:
            new_content = content.replace(old_text, new_text, 1)
            replaced = 1

        try:
            path.write_text(new_content, encoding="utf-8")
        except Exception as e:
            return ToolResult(error=f"Failed to write file: {e}")

        # 结果附带紧凑 unified diff：transcript 可见变更内容，模型也拿到
        # 自我校验依据。封顶 20 行——完整 diff 的权威展示面是审批弹窗。
        output = f"Replaced {replaced} occurrence(s) in {path}"
        diff = unified_diff_text(str(path), content, new_content, max_lines=20)
        if diff:
            output += f"\n{diff}"
        return ToolResult(output=output)

    # 预览大小封顶（同 WriteFileTool）：超大文件 → None 回退 JSON 参数
    _PREVIEW_MAX_CHARS = 200_000

    def preview_diff(self, args: dict) -> Optional[tuple[str, str, str]]:
        """变更预览：**严格镜像 execute 的替换语义**。

        execute 会失败的情形（文件不存在 / 零匹配 / 非 replace_all 下多
        匹配）一律返回 None——弹窗回退 JSON 参数，绝不预览一个必失败的
        变更。只读探测，任何异常落回 None。
        """
        try:
            file_path = args.get("file_path") or ""
            old_text = args.get("old_text") or ""
            new_text = args.get("new_text") or ""
            replace_all = bool(args.get("replace_all"))
            if not file_path or not old_text or old_text == new_text:
                return None
            path = self._resolve_path(str(file_path))
            if not self.allow_outside and not path.is_relative_to(self.workspace):
                return None
            if not path.exists():
                return None
            content = path.read_text(encoding="utf-8")
            if len(content) > self._PREVIEW_MAX_CHARS:
                return None
            count = content.count(old_text)
            if count == 0:
                return None
            if replace_all:
                new_content = content.replace(old_text, new_text)
            else:
                if count > 1:
                    return None  # execute 将报"不唯一"错——不预览必失败变更
                new_content = content.replace(old_text, new_text, 1)
            return (str(path), content, new_content)
        except Exception:
            return None


class GlobTool(WorkspaceTool):
    """Find files matching a glob pattern."""

    name = "glob"
    description = (
        "Find files matching a glob pattern (e.g., '**/*.py', '*.json'). "
        "Returns relative file paths."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern to match (supports ** for recursive).",
            },
        },
        "required": ["pattern"],
    }

    @property
    def permission(self) -> Permission:
        return Permission.allow()

    async def execute(self, pattern: str) -> ToolResult:
        try:
            matches = sorted(self.workspace.glob(pattern))
            # Ignore common non-project directories
            ignore_dirs = {"node_modules", ".git", "__pycache__", ".venv", "venv", ".tox"}
            filtered = [
                str(m.relative_to(self.workspace))
                for m in matches
                if not any(ig in m.parts for ig in ignore_dirs)
            ]

            if not filtered:
                return ToolResult(output=f"No files matched pattern: {pattern}")

            output = "\n".join(filtered)
            result, truncated, notice = truncate_output(output, max_lines=500)
            return ToolResult(output=result, truncated=truncated, truncated_notice=notice)
        except Exception as e:
            return ToolResult(error=f"Glob failed: {e}")


class ListDirectoryTool(WorkspaceTool):
    """List contents of a directory."""

    name = "list_directory"
    description = (
        "List files and directories in a given path. "
        "Shows file sizes and types."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory path (absolute or relative). Defaults to workspace root.",
            },
        },
        "required": [],
    }

    @property
    def permission(self) -> Permission:
        return Permission.allow()

    async def execute(self, path: str = ".") -> ToolResult:
        p = Path(path)
        if not p.is_absolute():
            p = self.workspace / p
        p = p.resolve()

        if not p.exists():
            return ToolResult(error=f"Directory not found: {p}")
        if not p.is_dir():
            return ToolResult(error=f"Not a directory: {p}")

        try:
            items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            lines = []
            for item in items:
                if item.is_dir():
                    lines.append(f"{item.name}/")
                else:
                    size = self._format_size(item.stat().st_size)
                    lines.append(f"{item.name} ({size})")

            output = "\n".join(lines)
            result, truncated, notice = truncate_output(output, max_lines=500)
            return ToolResult(output=result, truncated=truncated, truncated_notice=notice)
        except PermissionError:
            return ToolResult(error=f"Permission denied: {p}")
        except Exception as e:
            return ToolResult(error=f"Failed to list directory: {e}")


if __name__ == "__main__":
    # 独立调试：临时 workspace 内 写 → 读 → glob → 列目录
    import asyncio
    import tempfile

    async def _self_check():
        with tempfile.TemporaryDirectory() as ws:
            r = await WriteFileTool(ws).execute("hello.txt", "hello openx\n")
            assert r.success, r.error
            r = await ReadFileTool(ws).execute("hello.txt")
            assert r.success and "hello openx" in r.output, r.output
            r = await GlobTool(ws).execute("**/*.txt")
            assert "hello.txt" in r.output, r.output
            r = await ListDirectoryTool(ws).execute(".")
            assert "hello.txt" in r.output, r.output
            print(r.output)

    asyncio.run(_self_check())
    print("openx/tools/file_tools.py OK ✓")
