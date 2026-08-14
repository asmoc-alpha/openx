"""Minimal MCP client over a StdioTransport (JSON-RPC 2.0).

实现 MCP 客户端握手与工具三件套：

- ``initialize()``：协商协议版本、发送 ``notifications/initialized``；
- ``list_tools()``：分页（``nextCursor``）拉取工具定义列表；
- ``call_tool()``：调用远程工具，把 ``content[]`` 里的文本项拼成字符串；
  服务端返回 ``isError: true`` 时抛 :class:`JSONRPCError`。

仅 stdlib——零新依赖。
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

from .. import __version__
from .transport import JSONRPCError, StdioTransport

# 分页安全阀：恶意/故障服务端无限 nextCursor 时绝不无限循环
_MAX_PAGES = 100


class MCPClient:
    """单个 MCP 服务端的高层客户端（握手 / 列工具 / 调工具）。"""

    PROTOCOL_VERSION = "2024-11-05"

    def __init__(self, transport: StdioTransport) -> None:
        self.transport = transport

    async def initialize(self) -> dict:
        """握手：协商协议版本并确认初始化完成，返回服务端信息。"""
        result = await self.transport.request("initialize", {
            "protocolVersion": self.PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "openx", "version": __version__},
        })
        # 握手确认是通知（无 id、无响应）
        self.transport.notify("notifications/initialized")
        return result if isinstance(result, dict) else {}

    async def list_tools(self) -> list[dict]:
        """拉取全部工具定义（跟随 ``nextCursor`` 分页合并）。

        每个定义为 ``{name, description?, inputSchema?}``。
        """
        tools: list[dict] = []
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            params: dict = {"cursor": cursor} if cursor else {}
            result = await self.transport.request("tools/list", params)
            result = result if isinstance(result, dict) else {}
            page = result.get("tools")
            if isinstance(page, list):
                tools.extend(t for t in page if isinstance(t, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: dict) -> str:
        """调用远程工具，返回拼接后的文本内容。

        ``content[]`` 里的 ``{"type": "text", "text": ...}`` 项按行拼接；
        ``isError: true`` → 抛 :class:`JSONRPCError`（文本即错误信息）。
        """
        result = await self.transport.request(
            "tools/call", {"name": name, "arguments": arguments}
        )
        result = result if isinstance(result, dict) else {}
        texts: list[str] = []
        content = result.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(str(item.get("text") or ""))
        text = "\n".join(texts)
        if result.get("isError"):
            raise JSONRPCError(
                -32603, text or f"MCP tool '{name}' returned an error"
            )
        return text

    async def close(self) -> None:
        """关闭底层传输（幂等）。"""
        await self.transport.close()


if __name__ == "__main__":
    import asyncio

    class _FakeTransport:
        """鸭子替身：按 method 路由结果，记录请求与通知。"""

        def __init__(self, handlers):
            self.handlers = handlers
            self.requests: list[tuple[str, dict | None]] = []
            self.notifications: list[tuple[str, dict | None]] = []

        async def request(self, method, params=None, timeout=30.0):
            self.requests.append((method, params))
            return self.handlers[method](params or {})

        def notify(self, method, params=None):
            self.notifications.append((method, params))

        async def close(self):
            pass

    def _list_handler(params):
        # 两页：第一页带 nextCursor
        if not params.get("cursor"):
            return {"tools": [{"name": "a"}], "nextCursor": "p2"}
        assert params["cursor"] == "p2"
        return {"tools": [{"name": "b"}]}

    ft = _FakeTransport({
        "initialize": lambda p: {
            "protocolVersion": MCPClient.PROTOCOL_VERSION,
            "capabilities": {},
            "serverInfo": {"name": "fake", "version": "0.1"},
        },
        "tools/list": _list_handler,
        "tools/call": lambda p: {"content": [{"type": "text", "text": "ok"}]},
    })
    client = MCPClient(ft)

    info = asyncio.run(client.initialize())
    assert info["serverInfo"]["name"] == "fake"
    assert ft.requests[0][0] == "initialize"
    assert ft.requests[0][1]["clientInfo"]["name"] == "openx"
    assert ft.notifications == [("notifications/initialized", None)]

    tools = asyncio.run(client.list_tools())
    assert [t["name"] for t in tools] == ["a", "b"]  # 两页合并

    assert asyncio.run(client.call_tool("a", {})) == "ok"

    # isError → JSONRPCError，文本即错误信息
    ft_err = _FakeTransport({
        "tools/call": lambda p: {
            "content": [{"type": "text", "text": "kaboom"}], "isError": True,
        },
    })
    try:
        asyncio.run(MCPClient(ft_err).call_tool("boom", {}))
        raise AssertionError("expected JSONRPCError")
    except JSONRPCError as e:
        assert "kaboom" in e.message

    print("openx/mcp/client.py OK ✓")
