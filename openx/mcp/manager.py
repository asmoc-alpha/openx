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

    # ── 插件捆绑的 server（S5b MCP bundle）──────────────────────

    def merge_servers(self, servers: dict) -> int:
        """合并插件捆绑的 server 配置（connect_all 之前调用）。

        只**新增**：settings.json 已声明的同名项以用户配置为准（用户显式
        配置压过插件捆绑）。返回实际新增数。校验同 ``__init__``
        （``command`` 非空串；坏条目丢弃）。
        """
        added = 0
        for name, cfg in (servers or {}).items():
            if name in self._servers_config:
                continue
            if (
                isinstance(cfg, dict)
                and isinstance(cfg.get("command"), str)
                and cfg["command"].strip()
            ):
                self._servers_config[str(name)] = cfg
                added += 1
        return added

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

    def connected_servers(self) -> list[str]:
        """已成功握手连接（登记了工具）的 server 名（配置序）。

        消费方据此把 MCP 工具作为会话内贡献插入内核、并在卸载时按 server
        撤销（K6 pilot）；与 ``status()`` 的展示面分工：这是给代码用的取用面。
        """
        return list(self._connected)

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


def load_plugin_bundles(workspace: str, active_ids: set[str]) -> dict[str, dict]:
    """收集**已装载目录插件**捆绑的 MCP server（S5b MCP bundle）。

    约定：目录插件根下的 ``mcp.json``，形状与 settings 的 ``mcpServers``
    一致（``{name: {command, args?, env?}}``，仅 stdio）。只读 ACTIVE
    插件的（未装载/失败插件不贡献——信任门/沙箱语义由装载层把关）。
    同名冲突由 ``MCPManager.merge_servers`` 裁决（settings 优先）。
    """
    from ..kernel.assembly import loader

    merged: dict[str, dict] = {}
    for spec in loader.discover(workspace):
        if spec.root is None or spec.id not in active_ids:
            continue
        servers = _read_json(spec.root / "mcp.json").get("mcpServers")
        if not isinstance(servers, dict):
            continue
        for name, cfg in servers.items():
            if isinstance(cfg, dict) and name not in merged:
                entry = dict(cfg)
                entry["plugin"] = spec.id  # 归因：inventory/日志可见
                merged[str(name)] = entry
    return merged


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
        assert mgr.connected_servers() == []   # 未连接 → 空（K6 取用面）
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
        assert bad_mgr.connected_servers() == []   # 拉不起进程 → 不算已连接

    print("openx/mcp/manager.py OK ✓")
