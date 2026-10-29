"""S5b 插件管理元工具 + MCP bundle。

覆盖：
- ``plugin_manager``：install（目录插件 → 沙箱 → 用户目录 → 装载）、
  set_enabled（禁用表写回 + 重组，builtin 拒绝）、list；
- ``plugin_inspect``：list / query（归属+schema+权限）/ config；
- ``load_plugin_bundles``：只收 ACTIVE 目录插件的 mcp.json，settings 优先；
- ``merge_servers``：同名以用户配置为准。

运行：``python -m pytest tests/tools/test_plugin_manager_tool.py -q``
"""

from __future__ import annotations

import asyncio
import json

import pytest

import openx.config as config_mod
import openx.kernel.global_ledger as global_ledger_mod
from openx.config import OpenXConfig
from openx.kernel import get_kernel, reset_kernel
from openx.mcp.manager import MCPManager, load_plugin_bundles
from openx.tools.plugin_manager_tool import PluginInspectTool, PluginManagerTool


@pytest.fixture
def mgr_env(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(config_mod, "SETTINGS_PATH", settings)
    monkeypatch.setattr(
        global_ledger_mod, "GLOBAL_LEDGER_PATH", tmp_path / "ledger.jsonl"
    )
    monkeypatch.setenv("OPENX_PLUGIN_ADMISSION", "0")
    reset_kernel()
    ws = tmp_path / "ws"
    (ws / ".openx" / "plugins").mkdir(parents=True)
    OpenXConfig.add_trusted_dir(str(ws))
    yield tmp_path, ws
    reset_kernel()


class _FakeAgent:
    def __init__(self, ws):
        self.workspace = ws
        self.rebuilds = 0

    def _rebuild_tools(self):
        self.rebuilds += 1


def _dir_plugin(base, name, tool="dtool"):
    root = base / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.py").write_text(
        f"__openx_meta__ = {{'summary': '{name}', 'trust': 'user'}}\n"
        "from openx.tools.base import Tool, ToolResult\n"
        f"class T(Tool):\n    name = '{tool}'\n    description = 'd'\n"
        "    parameters = {'type': 'object', 'properties': {}}\n"
        "    async def execute(self, **kw):\n        return ToolResult(output='x')\n"
        "def apply(ctx):\n    ctx.register_tool(T())\n"
        "def self_test():\n    assert True\n",
        encoding="utf-8",
    )
    return root


def _tool_names(k):
    return {e.name for e in k.registry("tools").entries()}


async def test_manager_installs_dir_plugin(mgr_env):
    tmp_path, ws = mgr_env
    k = get_kernel()
    k.ensure_loaded(str(ws))
    src = _dir_plugin(ws / ".openx" / "plugins", "bundled", "bundledtool")
    agent = _FakeAgent(ws)
    tool = PluginManagerTool(k, agent)

    r = await tool.execute("install", target=str(src))
    assert r.success, r.error
    assert "bundled" in r.output
    # 落用户目录 + 会话装载 + 工具重建
    assert (config_mod.SETTINGS_PATH.parent / "plugins" / "bundled" / "plugin.py").is_file()
    assert "bundledtool" in _tool_names(k)
    assert agent.rebuilds == 1

    # 重复安装拒
    r2 = await tool.execute("install", target=str(src))
    assert "already exists" in r2.error


async def test_manager_set_enabled_and_list(mgr_env):
    tmp_path, ws = mgr_env
    src = _dir_plugin(ws / ".openx" / "plugins", "toggleme", "ttool")
    k = get_kernel()
    k.ensure_loaded(str(ws))
    assert "ttool" in _tool_names(k)
    tool = PluginManagerTool(k, _FakeAgent(ws))

    # 禁用：禁用表落盘 + 重组摘除
    r = await tool.execute("set_enabled", plugin_id="toggleme", enabled=False)
    assert r.success and "disabled" in r.output
    assert "toggleme" in OpenXConfig.load_plugin_settings().get("disabled", [])
    k.ensure_loaded(str(ws))
    assert "ttool" not in _tool_names(k)

    # 启用：恢复
    r = await tool.execute("set_enabled", plugin_id="toggleme", enabled=True)
    assert r.success and "enabled" in r.output
    k.ensure_loaded(str(ws))
    assert "ttool" in _tool_names(k)

    # builtin 拒绝
    r = await tool.execute("set_enabled", plugin_id="builtin-tools", enabled=False)
    assert not r.success and "builtin" in r.error

    # list：已装 + 可发现
    r = await tool.execute("list")
    assert "installed:" in r.output and "toggleme" in r.output


async def test_inspector_list_query_config(mgr_env):
    tmp_path, ws = mgr_env
    root = _dir_plugin(ws / ".openx" / "plugins", "inspectme", "itool")
    (root / "plugin.py").write_text(
        "__openx_meta__ = " + json.dumps({
            "summary": "inspect me", "trust": "user",
            "config": {"type": "object",
                       "properties": {"level": {"type": "string"}}},
        }) + "\n"
        "from openx.tools.base import Tool, ToolResult\n"
        "from openx.permissions import Permission, PermissionLevel\n"
        "class IT(Tool):\n    name = 'itool'\n    description = 'd'\n"
        "    parameters = {'type': 'object', 'properties': {}}\n"
        "    @property\n    def permission(self):\n"
        "        return Permission(level=PermissionLevel.ASK)\n"
        "    async def execute(self, **kw):\n        return ToolResult(output='x')\n"
        "def apply(ctx):\n    ctx.register_tool(IT())\n"
        "def self_test():\n    assert True\n",
        encoding="utf-8",
    )
    k = get_kernel()
    k.ensure_loaded(str(ws))
    insp = PluginInspectTool(k)

    # list：注册项按归属列出
    r = await insp.execute("list", kind="tools")
    assert "itool  ← inspectme" in r.output

    # query：归属 + manifest 摘要
    r = await insp.execute("query", kind="tools", name="itool")
    payload = json.loads(r.output)
    assert payload["plugin"] == "inspectme"
    assert payload["permissions"] == [] and payload["config_schema"]

    # config：schema + 当前值
    r = await insp.execute("config", plugin_id="inspectme")
    payload = json.loads(r.output)
    assert payload["config_schema"]["properties"]["level"] == {"type": "string"}
    assert payload["current"] == {}

    # 坏 kind / 坏名
    assert not (await insp.execute("list", kind="nope")).success
    assert not (await insp.execute("query", kind="tools", name="ghost")).success


# ── MCP bundle ───────────────────────────────────────────────────


def test_load_plugin_bundles_active_only_and_merge_priority(mgr_env, monkeypatch):
    tmp_path, ws = mgr_env
    _dir_plugin(ws / ".openx" / "plugins", "mcplug", "mtool")
    root = ws / ".openx" / "plugins" / "mcplug"
    (root / "mcp.json").write_text(json.dumps({
        "mcpServers": {"fs": {"command": "cat"},
                       "user-owned": {"command": "from-plugin"}}
    }), encoding="utf-8")

    k = get_kernel()
    k.ensure_loaded(str(ws))
    active = {i.id for i in k.inventory() if i.phase == "active"}
    assert "mcplug" in active
    bundles = load_plugin_bundles(str(ws), active)
    assert set(bundles) == {"fs", "user-owned"}
    assert bundles["fs"]["plugin"] == "mcplug"

    # settings 已声明的同名项：用户配置为准
    mgr = MCPManager({"user-owned": {"command": "from-settings"}})
    assert mgr.merge_servers(bundles) == 1  # 只新增 fs
    assert mgr._servers_config["user-owned"]["command"] == "from-settings"
    assert mgr._servers_config["fs"]["command"] == "cat"

    # 非 ACTIVE 插件不贡献（信任门/沙箱语义由装载层把关）
    bundles2 = load_plugin_bundles(str(ws), set())
    assert bundles2 == {}
