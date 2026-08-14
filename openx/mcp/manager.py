"""MCPManager — 管理一组 MCP server 连接的生命周期。

配置 schema（镜像 Claude Code），写在 ``~/.openx/settings.json``（全局）
和/或项目 ``<workspace>/.openx/settings.json``（项目级**扩展**全局——
同名 server 以项目条目为准）的 ``mcpServers`` 键下::

    {
      "mcpServers": {
        "filesystem": {"command": "npx",
                       "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                       "env": {"FOO": "bar"}}
      }
    }

核心承诺：**MCP 永远不能拖垮主流程**。``connect_all()`` 逐 server
吞掉一切异常（打印警告后继续），``shutdown()`` 幂等且从不抛出。
env 值只传给子进程，绝不出现在日志或警告文本里。
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
from pathlib import Path

from ..config import SETTINGS_PATH
from .client import MCPClient
from .tools import MCPTool
from .transport import StdioTransport

# initialize 握手总超时（秒）：卡死的 server 绝不能拖住 agent 启动
INITIALIZE_TIMEOUT = 10.0


def _read_json(path: Path) -> dict:
    """读取 JSON 文件；缺失/损坏静默跳过（返回 {}），与 hooks 加载器一致。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, FileNotFoundError):
        return {}
    return data if isinstance(data, dict) else {}


class MCPManager:
    """按配置启动、登记、关闭 MCP server 连接及其远程工具。"""

    def __init__(self, servers_config: dict | None = None) -> None:
        # 只接受 {"command": 非空字符串} 的合法条目；坏条目直接丢弃
        self._servers_config: dict[str, dict] = {}
        for name, cfg in (servers_config or {}).items():
            if (
                isinstance(cfg, dict)
                and isinstance(cfg.get("command"), str)
                and cfg["command"].strip()
            ):
                self._servers_config[str(name)] = cfg
        self._tools: dict[str, MCPTool] = {}       # 工具全名 → MCPTool
        self._clients: dict[str, MCPClient] = {}   # server 名 → 已连接客户端
        self._connected: dict[str, int] = {}       # server 名 → 工具数
        self._shut_down = False

    # ── loading ─────────────────────────────────────────────────

    @classmethod
    def load(cls, workspace: str) -> "MCPManager":
        """从全局 settings.json + 项目 ``<workspace>/.openx/settings.json`` 加载。

        项目级**扩展**全局：同名 server 以项目条目为准。文件缺失/损坏、
        ``mcpServers`` 不是 dict——一律静默跳过（同步、不建连接）。
        """
        sources: list[Path] = [SETTINGS_PATH]
        if workspace:
            sources.append(Path(workspace) / ".openx" / "settings.json")

        merged: dict[str, dict] = {}
        for src in sources:
            servers = _read_json(src).get("mcpServers")
            if not isinstance(servers, dict):
                continue
            for name, cfg in servers.items():
                if isinstance(cfg, dict):
                    merged[str(name)] = cfg  # 项目条目后写入 → 覆盖全局同名项
        return cls(merged)

    # ── connections ─────────────────────────────────────────────

    async def connect_all(self, console=None) -> int:
        """逐个连接配置的 server 并登记其工具，返回登记的工具总数。

        单个 server 的任何失败（进程拉不起来、握手超时、list_tools 坏
        响应……）都降级为一条警告并继续——**绝不抛出**。失败的 server
        best-effort 关闭，不留孤儿进程。
        """
        for name, cfg in self._servers_config.items():
            transport = StdioTransport(
                command=cfg["command"],
                args=cfg.get("args") or [],
                env=cfg.get("env") or {},  # env 值只进子进程，绝不入日志
                name=name,
            )
            client = MCPClient(transport)
            try:
                await transport.start()
                await asyncio.wait_for(client.initialize(), timeout=INITIALIZE_TIMEOUT)
                tool_defs = await client.list_tools()
            except Exception as e:
                message = (
                    f"MCP server '{name}' unavailable: {e} — continuing without it"
                )
                warned = False
                warn = getattr(console, "print_warning", None)
                if callable(warn):
                    try:
                        warn(message)
                        warned = True
                    except Exception:
                        pass
                if not warned:
                    print(message)
                try:
                    await transport.close()
                except Exception:
                    pass
                continue

            count = 0
            for tool_def in tool_defs:
                tool = MCPTool(name, client, tool_def)
                self._tools[tool.name] = tool
                count += 1
            self._clients[name] = client
            self._connected[name] = count
        return len(self._tools)

    # ── queries ─────────────────────────────────────────────────

    @property
    def tools(self) -> dict[str, MCPTool]:
        """已登记的远程工具（工具全名 → MCPTool）。"""
        return self._tools

    def status(self) -> list[str]:
        """人类可读状态行：``"name: connected (N tools)"`` / ``"name: not connected"``。"""
        lines: list[str] = []
        for name in self._servers_config:
            if name in self._connected:
                n = self._connected[name]
                lines.append(f"{name}: connected ({n} tool{'s' if n != 1 else ''})")
            else:
                lines.append(f"{name}: not connected")
        return lines

    # ── teardown ────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """关闭所有已连接 client（逐个兜底异常）；幂等、绝不抛出。"""
        if self._shut_down:
            return
        self._shut_down = True
        for client in self._clients.values():
            try:
                await client.close()
            except Exception:
                pass
        self._clients.clear()


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as _td:
        td = Path(_td)

        # load：全局 + 项目合并，同名以项目为准，坏条目在构造时过滤
        g = td / "global-settings.json"
        g.write_text(json.dumps({"mcpServers": {
            "one": {"command": "echo", "args": ["hi"]},
            "two": {"command": "true"},
            "bad": {"no_command": 1},       # 无 command → 构造时丢弃
        }}))
        (td / ".openx").mkdir()
        (td / ".openx" / "settings.json").write_text(json.dumps({"mcpServers": {
            "two": {"command": "false"},    # 项目覆盖全局同名项
        }}))
        _saved = SETTINGS_PATH
        SETTINGS_PATH = g
        try:
            mgr = MCPManager.load(str(td))
        finally:
            SETTINGS_PATH = _saved
        assert set(mgr._servers_config) == {"one", "two"}
        assert mgr._servers_config["two"]["command"] == "false"
        assert mgr.tools == {}
        assert mgr.status() == ["one: not connected", "two: not connected"]
        asyncio.run(mgr.shutdown())   # 无连接 → no-op
        asyncio.run(mgr.shutdown())   # 幂等

        # 损坏的 settings → 静默跳过
        corrupt = td / "corrupt.json"
        corrupt.write_text("{not json")
        SETTINGS_PATH = corrupt
        try:
            assert MCPManager.load(str(td))._servers_config == {"two": {"command": "false"}}
        finally:
            SETTINGS_PATH = _saved

        # connect_all 优雅降级：拉不起进程 → 返回 0、不抛、status 显示未连接
        bad_mgr = MCPManager({"nope": {"command": "/nonexistent/binary-xyz"}})
        assert asyncio.run(bad_mgr.connect_all()) == 0
        assert bad_mgr.tools == {}
        assert bad_mgr.status() == ["nope: not connected"]

    print("openx/mcp/manager.py OK ✓")
