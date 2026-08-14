"""Base tool definitions for OpenX."""

from __future__ import annotations

# ── 独立调试支持：允许 `python openx/tools/base.py` 直接运行 ─────────
if __name__ == "__main__" and not __package__:
    import sys as _sys
    from pathlib import Path as _Path
    _file = _Path(__file__).resolve()
    _root = _file.parent
    while _root != _root.parent and not (_root / "pyproject.toml").exists():
        _root = _root.parent
    _sys.path.insert(0, str(_root))
    __package__ = ".".join(_file.relative_to(_root).parts[:-1])

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..permissions import Permission, PermissionLevel
from ..utils.text import truncate_output, unified_diff_text  # canonical location, re-exported here


@dataclass
class ToolResult:
    """Result from executing a tool."""

    output: str = ""
    error: str = ""
    truncated: bool = False
    truncated_notice: str = ""

    @property
    def success(self) -> bool:
        return not self.error

    def to_message(self) -> str:
        """Format tool result for the LLM."""
        parts: list[str] = []
        if self.output:
            parts.append(self.output)
        if self.error:
            parts.append(f"Error: {self.error}")
        if self.truncated and self.truncated_notice:
            parts.append(self.truncated_notice)
        return "\n".join(parts) if parts else "(no output)"


class Tool(ABC):
    """Base class for all OpenX tools."""

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}

    @property
    def permission(self) -> Permission:
        """The permission required for this tool."""
        return Permission(level=PermissionLevel.ALLOW)

    def to_openai_schema(self) -> dict[str, Any]:
        """Convert to OpenAI function definition."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        """Execute the tool with given arguments."""
        ...

    def validate_args(self, **kwargs: Any) -> Optional[str]:
        """Validate arguments. Returns error message or None."""
        return None  # default: no validation

    def auto_allowed(self, args: dict) -> bool:
        """Whether this specific invocation may skip the interactive ASK prompt.

        预批准（pre-approval）语义：返回 ``True`` 表示本次调用命中工具自身的
        白名单（如 ``shell`` 的 ``allowed_commands``），执行器可跳过用户确认
        直接执行。返回 ``False``（默认）**不阻止**执行——仅表示仍需走正常的
        ASK 权限流程。即：白名单用来"免询问"，而非拦截未列出的命令。
        """
        return False

    def is_high_risk(self, args: dict) -> bool:
        """Whether THIS invocation is high-risk and must ALWAYS be prompted.

        高风险（always-ask）语义：返回 ``True`` 时执行器绕过存储 allow 规则、
        工具白名单与 ``auto_approve``/-y，强制交互式确认——且本次确认不得
        豁免后续同类调用（危险检查排在存储规则提前返回之前）。默认
        ``False``；``ShellTool`` 按 ``config.dangerous_commands`` 覆写。
        """
        return False

    def preview_diff(self, args: dict) -> Optional[tuple[str, str, str]]:
        """权限弹窗的变更预览：返回 ``(path, old_content, new_content)``。

        执行器在 ASK 弹窗前调用，把"将要发生的变更"渲染成彩色 diff 供
        用户审批（manual 模式的信任基础）。默认 ``None`` → 弹窗回退到
        JSON 参数展示。实现要求：

        - **镜像 execute 语义**：预览的 new_content 必须与真实执行结果
          一致；无法确定（如 edit 匹配不唯一将报错）时返回 None；
        - **绝不抛异常/副作用**：只读探测，任何失败都落回 None；
        - 大文件自行封顶（弹窗渲染与内存保护）。
        """
        return None


class WorkspaceTool(Tool, ABC):
    """Base class for tools that operate within a workspace directory.

    Provides ``workspace`` init, ``_resolve_path()``, and ``_format_size()``
    so individual tool classes don't need to duplicate these.
    """

    def __init__(self, workspace: str) -> None:
        self.workspace = Path(workspace).resolve()

    def _resolve_path(self, file_path: str) -> Path:
        """Resolve *file_path* against the workspace.

        Absolute paths are kept as-is; relative paths are joined with the
        workspace directory.  The result is always resolved (no ``..`` or
        symlink components).
        """
        p = Path(file_path)
        if not p.is_absolute():
            p = self.workspace / p
        return p.resolve()

    @staticmethod
    def _format_size(size: int) -> str:
        """Return a human-readable size string (e.g. ``\"1.5 MB\"``)."""
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
            size /= 1024
        return f"{size:.1f} TB"


# ── truncate_output is defined in utils/text.py and re-exported above ──


if __name__ == "__main__":
    # 独立调试：验证 ToolResult 格式化与 WorkspaceTool 路径解析
    r = ToolResult(output="hello")
    assert r.success and r.to_message() == "hello"
    r_err = ToolResult(error="boom")
    assert not r_err.success and "boom" in r_err.to_message()
    assert truncate_output("x", max_chars=50_000)[1] is False

    # is_high_risk 默认 False（子类如 ShellTool 覆写）
    class _Probe(Tool):
        async def execute(self, **kwargs):
            return ToolResult(output="")

    assert _Probe().is_high_risk({"command": "rm -rf /"}) is False
    print(WorkspaceTool._format_size(1536))  # 1.5 KB
    print("tools/base.py OK ✓")
