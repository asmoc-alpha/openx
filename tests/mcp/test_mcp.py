"""Phase 9 MCP 支持回归测试。

覆盖：完整握手 + tools/list + tools/call 往返 / 工具名与 schema 转换 /
ASK 权限 / 进程拉不起来时的优雅降级（警告捕获）/ 握手超时（transport
层与 connect_all 层）/ nextCursor 分页合并 / isError 工具失败 /
agent.startup/shutdown 接线与幂等 / status 状态行 / settings.json
配置加载合并。

假 MCP server 是真实写入 tmp_path 的 Python 脚本（以 sys.executable
运行），在 stdin/stdout 上说 NDJSON JSON-RPC 2.0；settings.json 与
TASKS_DIR 均经 monkeypatch 隔离，绝不触碰真实 ~/.openx。所有连接在
fixture 的 teardown 里关闭，不留孤儿子进程。

运行：``python -m pytest tests/test_mcp.py -q``
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from openx.config import OpenXConfig
from openx.mcp import MCPManager, StdioTransport
from openx.mcp.client import MCPClient
from openx.permissions import PermissionLevel


# ── fake MCP servers（NDJSON over stdio）────────────────────────

# 正常 server（echo + boom 两个工具）；mode=paged 时 tools/list 分两页
FAKE_SERVER = '''\
import json
import sys

MODE = sys.argv[1] if len(sys.argv) > 1 else "normal"

ECHO_SCHEMA = {
    "type": "object",
    "properties": {"message": {"type": "string"}},
    "required": ["message"],
}
UPPER_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
}


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()


while True:
    line = sys.stdin.readline()
    if not line:  # stdin EOF → 父进程已关闭管道
        break
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "serverInfo": {"name": "fake", "version": "0.1"},
        }})
    elif method == "tools/list":
        if MODE == "paged" and params.get("cursor") != "p2":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "tools": [{"name": "echo", "description": "Echo back a message",
                           "inputSchema": ECHO_SCHEMA}],
                "nextCursor": "p2",
            }})
        elif MODE == "paged":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "tools": [{"name": "upper", "description": "Uppercase a string",
                           "inputSchema": UPPER_SCHEMA}],
            }})
        else:
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "tools": [
                    {"name": "echo", "description": "Echo back a message",
                     "inputSchema": ECHO_SCHEMA},
                    {"name": "boom", "description": "Always fails"},
                ],
            }})
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "boom":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "kaboom"}],
                "isError": True,
            }})
        else:
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text",
                             "text": "echo: " + str(args.get("message", ""))}],
            }})
    # notifications/initialized 与未知通知：无 id、不回复
'''

# 超时 server：只读 stdin、永不响应（initialize 必须超时而非挂死）
SILENT_SERVER = '''\
import sys
while True:
    line = sys.stdin.readline()
    if not line:
        break
'''


# ── fixtures / helpers ──────────────────────────────────────────


@pytest.fixture
async def managers():
    """登记测试里创建的 MCPManager，teardown 统一关闭——不留孤儿进程。"""
    created: list[MCPManager] = []
    yield created
    for mgr in created:
        try:
            await mgr.shutdown()
        except Exception:
            pass


def _write_server(tmp_path: Path, mode: str = "normal", name: str = "server.py") -> Path:
    """把假 MCP server 脚本写入 tmp_path，返回路径。

    mode="silent" 写永不响应的超时 server；"normal"/"paged" 写同一个
    FAKE_SERVER（"paged" 行为经 argv 参数切换，见 _fake_config）。
    """
    script = tmp_path / name
    script.write_text(FAKE_SERVER if mode in ("normal", "paged") else SILENT_SERVER)
    return script


def _fake_config(script: Path, mode: str = "normal") -> dict:
    """构造指向假 server 的 mcpServers 条目。"""
    args = [str(script)] + (["paged"] if mode == "paged" else [])
    return {"command": sys.executable, "args": args}


class RecordingConsole:
    """Duck-typed console：只记录 print_warning 输出。"""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def print_warning(self, message: str) -> None:
        self.warnings.append(message)


def _make_agent(tmp_path: Path):
    """构造 OpenXAgent（绕过真实 API 与 settings.json）。"""
    from openx.agent import OpenXAgent

    config = OpenXConfig()
    config.workspace = str(tmp_path)
    config.api_key = "sk-test"
    config.api_base = "https://example.com/v1"
    config.model = "test-model"
    return OpenXAgent(config)


# ── 1. 完整往返 ─────────────────────────────────────────────────


class TestRoundtrip:
    """connect_all → 工具登记 → execute 往返出文本。"""

    async def test_connect_and_call_tool(self, tmp_path, managers):
        script = _write_server(tmp_path)
        mgr = MCPManager({"fake": _fake_config(script)})
        managers.append(mgr)

        n = await mgr.connect_all()
        assert n == 2  # echo + boom
        assert "mcp__fake__echo" in mgr.tools
        assert "mcp__fake__boom" in mgr.tools

        result = await mgr.tools["mcp__fake__echo"].execute(message="hi")
        assert result.success
        assert result.output == "echo: hi"

    async def test_initialize_handshake_details(self, tmp_path):
        """MCPClient.initialize 返回 serverInfo 并发 initialized 通知。"""
        script = _write_server(tmp_path)
        transport = StdioTransport(sys.executable, [str(script)], name="fake")
        client = MCPClient(transport)
        await transport.start()
        try:
            info = await client.initialize()
            assert info["serverInfo"]["name"] == "fake"
            assert info["protocolVersion"] == MCPClient.PROTOCOL_VERSION
            tools = await client.list_tools()
            assert [t["name"] for t in tools] == ["echo", "boom"]
            assert await client.call_tool("echo", {"message": "yo"}) == "echo: yo"
        finally:
            await client.close()


# ── 2. 名字 / schema 转换 ───────────────────────────────────────


class TestToolConversion:
    """mcp__<server>__<tool> 命名、inputSchema 直用、描述前缀。"""

    async def test_name_schema_description(self, tmp_path, managers):
        script = _write_server(tmp_path)
        mgr = MCPManager({"fake": _fake_config(script)})
        managers.append(mgr)
        await mgr.connect_all()

        tool = mgr.tools["mcp__fake__echo"]
        assert tool.name == "mcp__fake__echo"
        assert tool.parameters == {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        }
        assert tool.description == "[MCP: fake] Echo back a message"
        schema = tool.to_openai_schema()
        assert schema["function"]["name"] == "mcp__fake__echo"
        assert schema["function"]["parameters"] == tool.parameters

    async def test_missing_inputschema_defaults_to_empty_object(self, tmp_path, managers):
        script = _write_server(tmp_path)
        mgr = MCPManager({"fake": _fake_config(script)})
        managers.append(mgr)
        await mgr.connect_all()
        # boom 的 tool 定义没有 inputSchema
        assert mgr.tools["mcp__fake__boom"].parameters == {
            "type": "object", "properties": {},
        }


# ── 3. 权限 ─────────────────────────────────────────────────────


class TestPermission:
    """MCP 工具恒为 ASK——远程能力必须经用户确认。"""

    async def test_ask_level_with_reason(self, tmp_path, managers):
        script = _write_server(tmp_path)
        mgr = MCPManager({"fake": _fake_config(script)})
        managers.append(mgr)
        await mgr.connect_all()
        perm = mgr.tools["mcp__fake__echo"].permission
        assert perm.level is PermissionLevel.ASK
        assert "echo" in perm.reason and "fake" in perm.reason


# ── 4. 优雅降级 ─────────────────────────────────────────────────


class TestGracefulDegradation:
    """坏 server 只出警告：connect_all 返回 0、不抛、不影响其他 server。"""

    async def test_missing_binary_warns_and_continues(self, tmp_path, managers):
        console = RecordingConsole()
        mgr = MCPManager({"bad": {"command": "/nonexistent/binary-xyz"}})
        managers.append(mgr)

        n = await mgr.connect_all(console=console)
        assert n == 0
        assert mgr.tools == {}
        assert any(
            "'bad'" in w and "unavailable" in w for w in console.warnings
        )

    async def test_bad_server_does_not_break_good_one(self, tmp_path, managers):
        script = _write_server(tmp_path)
        console = RecordingConsole()
        mgr = MCPManager({
            "bad": {"command": "/nonexistent/binary-xyz"},
            "fake": _fake_config(script),
        })
        managers.append(mgr)

        n = await mgr.connect_all(console=console)
        assert n == 2  # 好 server 照常登记
        assert "mcp__fake__echo" in mgr.tools
        assert len(console.warnings) == 1  # 只有坏 server 出警告


# ── 5. 超时 ─────────────────────────────────────────────────────


class TestTimeout:
    """永不响应的 server：transport 层超时抛错、connect_all 层降级。"""

    async def test_request_timeout_raises_and_close_idempotent(self, tmp_path):
        script = _write_server(tmp_path, mode="silent")
        transport = StdioTransport(sys.executable, [str(script)], name="silent")
        await transport.start()
        try:
            # 3.10 的 asyncio.TimeoutError 尚非内置 TimeoutError（3.11 才统一），
            # 元组写法全版本兼容（3.11+ 二者为同一类）。
            with pytest.raises((TimeoutError, asyncio.TimeoutError)):
                await transport.request("initialize", {}, timeout=0.5)
        finally:
            await transport.close()
            await transport.close()  # 幂等

    async def test_connect_all_degrades_on_silent_server(
        self, tmp_path, monkeypatch, managers
    ):
        monkeypatch.setattr("openx.mcp.manager.INITIALIZE_TIMEOUT", 0.5)
        script = _write_server(tmp_path, mode="silent")
        console = RecordingConsole()
        mgr = MCPManager({"silent": _fake_config(script)})
        managers.append(mgr)

        n = await mgr.connect_all(console=console)
        assert n == 0
        assert mgr.tools == {}
        assert mgr.status() == ["silent: not connected"]
        assert any("'silent'" in w for w in console.warnings)


# ── 6. 游标分页 ─────────────────────────────────────────────────


class TestCursorPagination:
    """tools/list 的 nextCursor 分页：两页工具全部合并登记。"""

    async def test_two_pages_merged(self, tmp_path, managers):
        script = _write_server(tmp_path, mode="paged")
        mgr = MCPManager({"fake": _fake_config(script, mode="paged")})
        managers.append(mgr)

        n = await mgr.connect_all()
        assert n == 2
        assert set(mgr.tools) == {"mcp__fake__echo", "mcp__fake__upper"}


# ── 7. isError ──────────────────────────────────────────────────


class TestIsError:
    """服务端 isError 结果 → ToolResult(error=...)，含错误文本。"""

    async def test_error_tool_returns_error_result(self, tmp_path, managers):
        script = _write_server(tmp_path)
        mgr = MCPManager({"fake": _fake_config(script)})
        managers.append(mgr)
        await mgr.connect_all()

        result = await mgr.tools["mcp__fake__boom"].execute()
        assert not result.success
        assert "kaboom" in result.error


# ── 8. agent 接线 ───────────────────────────────────────────────


class TestAgentWiring:
    """startup() 登记工具进 tools 与 schemas；shutdown() 幂等。"""

    async def test_startup_registers_tools_and_shutdown_idempotent(
        self, tmp_path, monkeypatch
    ):
        script = _write_server(tmp_path)
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({
            "mcpServers": {"fake": _fake_config(script)},
        }))
        monkeypatch.setattr("openx.mcp.manager.SETTINGS_PATH", settings)
        monkeypatch.setattr(
            "openx.kernel.audit.hooks.SETTINGS_PATH", tmp_path / "no-such-settings.json"
        )
        monkeypatch.setattr("openx.orchestration.tasks.TASKS_DIR", tmp_path / "tasks")

        agent = _make_agent(tmp_path)
        assert "mcp__fake__echo" not in agent.tools  # 连接前：只有本地工具
        try:
            await agent.startup()
            assert "mcp__fake__echo" in agent.tools
            names = [s["function"]["name"] for s in agent.tool_schemas]
            assert "mcp__fake__echo" in names

            await agent.startup()  # 幂等：不重连、不抛
            assert agent._started is True

            await agent.shutdown()
            await agent.shutdown()  # 幂等：不抛
            assert agent._started is False
        finally:
            await agent.shutdown()

    async def test_child_inherits_parent_mcp_tools(self, tmp_path, monkeypatch):
        """子代理复用父已连接的 MCP 工具（共享实例），且不自己建连。"""
        script = _write_server(tmp_path)
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({
            "mcpServers": {"fake": _fake_config(script)},
        }))
        monkeypatch.setattr("openx.mcp.manager.SETTINGS_PATH", settings)
        monkeypatch.setattr(
            "openx.kernel.audit.hooks.SETTINGS_PATH", tmp_path / "no-such-settings.json"
        )
        monkeypatch.setattr("openx.orchestration.tasks.TASKS_DIR", tmp_path / "tasks")

        from openx.agent import OpenXAgent

        parent = _make_agent(tmp_path)
        try:
            await parent.startup()
            config = OpenXConfig()
            config.workspace = str(tmp_path)
            config.api_key = "sk-test"
            config.api_base = "https://example.com/v1"
            config.model = "test-model"
            child = OpenXAgent(config, parent=parent)
            assert child.tools["mcp__fake__echo"] is parent.tools["mcp__fake__echo"]
            # 白名单也能自然排除 mcp 工具
            restricted = OpenXAgent(
                config, parent=parent, tool_allowlist=["read_file"],
            )
            assert "mcp__fake__echo" not in restricted.tools
        finally:
            await parent.shutdown()


# ── 9. status ───────────────────────────────────────────────────


class TestStatus:
    """status() 每 server 一行：server 名 + 工具数 / 未连接。"""

    async def test_status_lines(self, tmp_path, managers):
        script = _write_server(tmp_path)
        mgr = MCPManager({
            "fake": _fake_config(script),
            "bad": {"command": "/nonexistent/binary-xyz"},
        })
        managers.append(mgr)
        await mgr.connect_all()

        lines = mgr.status()
        assert any("fake" in line and "connected (2 tools)" in line for line in lines)
        assert any("bad" in line and "not connected" in line for line in lines)

    def test_status_empty_when_unconfigured(self):
        assert MCPManager().status() == []


# ── 配置加载合并 ────────────────────────────────────────────────


class TestConfigLoading:
    """load()：项目扩展全局（同名以项目为准）；坏文件/坏条目静默跳过。"""

    def test_project_extends_global_and_overrides_same_name(
        self, tmp_path, monkeypatch
    ):
        global_settings = tmp_path / "global-settings.json"
        global_settings.write_text(json.dumps({"mcpServers": {
            "alpha": {"command": "echo", "args": ["global"]},
            "beta": {"command": "true"},
        }}))
        monkeypatch.setattr("openx.mcp.manager.SETTINGS_PATH", global_settings)
        (tmp_path / ".openx").mkdir()
        (tmp_path / ".openx" / "settings.json").write_text(json.dumps({"mcpServers": {
            "beta": {"command": "false"},  # 覆盖全局同名项
            "gamma": {"command": "ls"},
        }}))

        mgr = MCPManager.load(str(tmp_path))
        assert set(mgr._servers_config) == {"alpha", "beta", "gamma"}
        assert mgr._servers_config["beta"]["command"] == "false"

    def test_corrupt_global_skipped_project_still_loaded(self, tmp_path, monkeypatch):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json at all")
        monkeypatch.setattr("openx.mcp.manager.SETTINGS_PATH", bad)
        (tmp_path / ".openx").mkdir()
        (tmp_path / ".openx" / "settings.json").write_text(json.dumps({
            "mcpServers": {"only": {"command": "true"}},
        }))
        mgr = MCPManager.load(str(tmp_path))
        assert set(mgr._servers_config) == {"only"}

    def test_entries_without_command_dropped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "openx.mcp.manager.SETTINGS_PATH", tmp_path / "no-such-settings.json"
        )
        mgr = MCPManager({
            "ok": {"command": "true"},
            "no_command": {"args": ["x"]},
            "blank_command": {"command": "   "},
            "not_a_dict": "oops",
        })
        assert set(mgr._servers_config) == {"ok"}
