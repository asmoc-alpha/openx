"""Structured error types for OpenX.

Replaces bare strings with typed exceptions so callers can
programmatically distinguish error categories.
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


class OpenXError(Exception):
    """Base error for all OpenX-specific exceptions."""


class ConfigError(OpenXError):
    """Configuration-related error (missing key, invalid value, etc.)."""


class LLMError(OpenXError):
    """LLM API error."""

    def __init__(self, message: str, status_code: int = 0, provider_message: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.provider_message = provider_message


# ── tool errors ─────────────────────────────────────────────────


class ToolError(OpenXError):
    """Base for tool execution errors."""


class ToolNotFoundError(ToolError):
    """Tool name not found in registry."""


class ToolPermissionError(ToolError):
    """Permission denied for tool execution."""


class ToolExecutionError(ToolError):
    """Tool failed during execution."""

    def __init__(self, message: str, tool_name: str = "", exit_code: int = 0):
        super().__init__(message)
        self.tool_name = tool_name
        self.exit_code = exit_code


class ValidationError(ToolError):
    """Tool input validation failed."""

    def __init__(self, message: str, field_errors: dict[str, str] | None = None):
        super().__init__(message)
        self.field_errors = field_errors or {}


if __name__ == "__main__":
    err = LLMError("rate limited", status_code=429, provider_message="slow down")
    print(f"LLMError: {err} | status={err.status_code} | provider={err.provider_message!r}")

    tool_err = ToolExecutionError("command failed", tool_name="shell", exit_code=1)
    print(f"ToolExecutionError: {tool_err} | tool={tool_err.tool_name} | exit={tool_err.exit_code}")

    val_err = ValidationError("bad input", field_errors={"path": "required"})
    print(f"ValidationError: {val_err} | fields={val_err.field_errors}")

    # 验证继承关系与可捕获性
    try:
        raise val_err
    except OpenXError as e:
        print(f"caught via base class: {type(e).__name__}")

    assert issubclass(ToolNotFoundError, ToolError)
    print("openx/utils/errors.py OK ✓")
