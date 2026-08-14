"""MCPTool — 把一个远程 MCP 工具包装成 OpenX 本地 Tool。

命名约定 ``mcp__<server>__<tool>``（与 Claude Code 一致），权限恒为
``ASK``——远程工具的能力不受本进程约束，每次调用都要用户确认。
``execute()`` 把一切异常（JSON-RPC 错误、isError、传输故障）降级成
``ToolResult(error=...)``，绝不向 agent 循环抛异常。
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

from typing import Any

from ..permissions import Permission, PermissionLevel
from ..tools.base import Tool, ToolResult
from .client import MCPClient
from .transport import JSONRPCError

_EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


class MCPTool(Tool):
    """远程 MCP 工具的本地代理（名字/参数 schema 来自服务端 tools/list）。"""

    def __init__(self, server_name: str, client: MCPClient, tool_def: dict) -> None:
        self.server_name = server_name
        self._client = client
        self._remote_name = str(tool_def.get("name") or "")
        self.name = f"mcp__{server_name}__{self._remote_name}"
        self.description = f"[MCP: {server_name}] {tool_def.get('description') or ''}"
        schema = tool_def.get("inputSchema")
        self.parameters = dict(schema) if isinstance(schema, dict) else dict(_EMPTY_SCHEMA)

    @property
    def permission(self) -> Permission:
        """MCP 调用一律 ASK——用户确认后才执行。"""
        return Permission.ask(
            f"MCP tool '{self._remote_name}' via server '{self.server_name}'"
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        """调用远程工具；任何失败都降级为带 error 的 ToolResult。"""
        try:
            text = await self._client.call_tool(self._remote_name, kwargs)
        except JSONRPCError as e:
            return ToolResult(error=e.message or str(e))
        except Exception as e:
            return ToolResult(error=f"MCP call failed: {e}")
        return ToolResult(output=text or "(no output)")


if __name__ == "__main__":
    import asyncio

    class _FakeClient:
        """鸭子替身：正常返回文本，或按配置抛 JSONRPCError。"""

        def __init__(self, text: str = "ok", exc: Exception | None = None):
            self.text = text
            self.exc = exc
            self.calls: list[tuple[str, dict]] = []

        async def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            if self.exc is not None:
                raise self.exc
            return self.text

    _tool_def = {
        "name": "echo",
        "description": "Echo back a message",
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    }
    fc = _FakeClient(text="echo: hi")
    tool = MCPTool("fake", fc, _tool_def)  # type: ignore[arg-type]

    assert tool.name == "mcp__fake__echo"
    assert tool.description == "[MCP: fake] Echo back a message"
    assert tool.parameters == _tool_def["inputSchema"]
    assert tool.permission.level is PermissionLevel.ASK
    assert tool.to_openai_schema()["function"]["name"] == "mcp__fake__echo"

    result = asyncio.run(tool.execute(message="hi"))
    assert result.success and result.output == "echo: hi"
    assert fc.calls == [("echo", {"message": "hi"})]

    # JSON-RPC 错误 → ToolResult(error=...)，绝不抛
    err_tool = MCPTool("fake", _FakeClient(exc=JSONRPCError(-32603, "kaboom")), _tool_def)  # type: ignore[arg-type]
    bad = asyncio.run(err_tool.execute(message="x"))
    assert not bad.success and "kaboom" in bad.error

    # 无 inputSchema → 空对象 schema；空文本 → "(no output)"
    bare = MCPTool("s", _FakeClient(text=""), {"name": "noop"})  # type: ignore[arg-type]
    assert bare.parameters == {"type": "object", "properties": {}}
    assert asyncio.run(bare.execute()).output == "(no output)"

    print("openx/mcp/tools.py OK ✓")
