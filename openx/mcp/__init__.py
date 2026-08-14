"""MCP (Model Context Protocol) support — minimal stdio JSON-RPC client.

最小 MCP 支持（Phase 9）：NDJSON stdio 传输 + 客户端握手 + 远程工具
包装 + 连接管理器。仅 stdlib，零新依赖。配置见 :mod:`openx.mcp.manager`。
"""

from .client import MCPClient
from .manager import MCPManager
from .tools import MCPTool
from .transport import JSONRPCError, StdioTransport

__all__ = [
    "JSONRPCError",
    "StdioTransport",
    "MCPClient",
    "MCPTool",
    "MCPManager",
]
