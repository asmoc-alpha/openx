"""P-A 模型驱动装配元工具面测试：list/load/unload/help + 会话级动态装载。

覆盖：
- list_plugins 轻量目录（summary/cost/phase/scope/注册项名）；
- load_plugin：disabled 插件会话激活、failed 重试预清理残留、新写文件
  fresh discover、not found / already loaded 拒绝；
- unload_plugin：session 插件清注册 + 记账、boot 插件拒绝；
- registry.unregister 幂等；
- 元工具 execute（假 agent 记录 _rebuild_tools 调用）+ ASK 权限。

环境：kernel_env fixture（临时 workspace + SETTINGS_PATH + 新内核）。
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from openx.kernel.protocol import Event
from openx.kernel import get_kernel
from openx.permissions import PermissionLevel
from openx.tools.plugin_tools import (
    ListPluginsTool,
    LoadPluginTool,
    PluginHelpTool,
    UnloadPluginTool,
)
from openx.tools.write_plugin_tools import (
    PromotePluginTool,
    TestPluginTool,
    WritePluginTool,
)

# 防 pytest 把导入的工具类当测试类收集（名字以 Test 开头）
TestPluginTool.__test__ = False  # type: ignore[attr-defined]

# P-F：模型自产插件的 code / test（生成用）
GEN_CODE = '''\
from openx.tools.base import Tool, ToolResult

class GreetTool(Tool):
    name = "greet"
    description = "打招呼"
    async def execute(self, **kw):
        return ToolResult(output="hi " + str(kw.get("who", "you")))

def factory(host):
    return [GreetTool()]

def apply(ctx):
    ctx.register_tool_factory("greet", factory)
'''
GEN_TEST = '''\
assert factory(None)[0].name == "greet"
'''


class Sink:
    """收集事件的账本 sink（test_ledger 同款）。"""

    def __init__(self):
        self.events: list[Event] = []

    def __call__(self, event: Event) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]

PLUGIN_OK = textwrap.dedent('''\
    """P-A 测试插件：可正常装载。"""
    __openx_meta__ = {"summary": "画调用关系图", "cost": {"schemaTokens": 400}}

    from openx.tools.base import Tool, ToolResult

    class VizTool(Tool):
        name = "viz"
        description = "画调用关系图"
        async def execute(self, **kw):
            return ToolResult(output="viz ok")

    def factory(host):
        return [VizTool()]

    def apply(ctx):
        ctx.register_tool_factory("viz", factory)
''')


def _write_plugin(ws: Path, name: str, body: str) -> Path:
    d = ws / ".openx" / "plugins"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.py"
    p.write_text(body, encoding="utf-8")
    return p


def _catalog(ws) -> dict:
    return {c["id"]: c for c in get_kernel().list_plugins()}


# ── list_plugins ─────────────────────────────────────────────────


def test_list_plugins_includes_metadata(kernel_env):
    ws, _ = kernel_env
    _write_plugin(ws, "vizplugin", PLUGIN_OK)
    get_kernel().ensure_loaded(str(ws))

    cats = _catalog(ws)
    assert "vizplugin" in cats
    entry = cats["vizplugin"]
    assert entry["summary"] == "画调用关系图"
    assert entry["cost"]["schemaTokens"] == 400
    assert entry["phase"] == "active"
    assert entry["scope"] == "boot"
    # 工厂注册名（register_tool_factory 的注册项名）
    assert entry["tools"] == ["<factory:viz>"]
    # 内置插件也在目录且带 summary
    assert "内置能力工具集" in cats["builtin-tools"]["summary"]


# ── load_plugin ──────────────────────────────────────────────────


def test_load_plugin_activates_disabled(kernel_env):
    ws, settings = kernel_env
    _write_plugin(ws, "vizplugin", PLUGIN_OK)
    settings.write_text(json.dumps({"plugins": {"disabled": ["vizplugin"]}}))
    k = get_kernel()
    k.ensure_loaded(str(ws))
    assert _catalog(ws)["vizplugin"]["phase"] == "disabled"

    ok, msg = k.load_plugin("vizplugin")
    assert ok, msg
    assert "session" in msg
    entry = _catalog(ws)["vizplugin"]
    assert entry["phase"] == "active" and entry["scope"] == "session"
    # tools 注册表里出现了 viz
    assert k.registry("tools").get("viz") is not None
    # 已 ACTIVE 再 load → 拒绝
    ok2, msg2 = k.load_plugin("vizplugin")
    assert not ok2 and "already loaded" in msg2


def test_load_plugin_retries_failed_with_preclean(kernel_env):
    """failed 插件：先清残留注册（前次 apply 部分入库）再重试。"""
    ws, _ = kernel_env
    broken = PLUGIN_OK.replace(
        '    ctx.register_tool_factory("viz", factory)\n',
        '    ctx.register_tool_factory("viz", factory)\n'
        '    raise RuntimeError("boom")\n',
    )
    _write_plugin(ws, "vizplugin", broken)
    k = get_kernel()
    k.ensure_loaded(str(ws))
    assert _catalog(ws)["vizplugin"]["phase"] == "failed"
    # 前次 apply 已注册 viz（残留）
    assert k.registry("tools").get("viz") is not None

    # 修复文件后重试：预清理残留 → 成功，viz 只出现一次、无冲突警告
    _write_plugin(ws, "vizplugin", PLUGIN_OK)
    ok, msg = k.load_plugin("vizplugin")
    assert ok, msg
    assert _catalog(ws)["vizplugin"]["phase"] == "active"
    entry = k.registry("tools").get("viz")
    assert entry is not None and entry.plugin == "vizplugin"
    assert not entry.warnings, entry.warnings


def test_load_plugin_finds_new_file_after_boot(kernel_env):
    """boot 之后新写的插件文件：load_plugin fresh discover 能找到（self-extension 种子）。"""
    ws, _ = kernel_env
    k = get_kernel()
    k.ensure_loaded(str(ws))
    assert "vizplugin" not in _catalog(ws)

    _write_plugin(ws, "vizplugin", PLUGIN_OK)
    ok, msg = k.load_plugin("vizplugin")
    assert ok, msg
    assert _catalog(ws)["vizplugin"]["phase"] == "active"
    assert k.registry("tools").get("viz") is not None


def test_load_plugin_not_found(kernel_env):
    ws, _ = kernel_env
    get_kernel().ensure_loaded(str(ws))
    ok, msg = get_kernel().load_plugin("no-such-plugin")
    assert not ok and "not found" in msg


# ── unload_plugin ────────────────────────────────────────────────


def test_unload_plugin_session_plugin(kernel_env):
    ws, settings = kernel_env
    _write_plugin(ws, "vizplugin", PLUGIN_OK)
    settings.write_text(json.dumps({"plugins": {"disabled": ["vizplugin"]}}))
    k = get_kernel()
    k.ensure_loaded(str(ws))
    ok, _ = k.load_plugin("vizplugin")
    assert ok

    ok, msg = k.unload_plugin("vizplugin")
    assert ok, msg
    assert "vizplugin" not in _catalog(ws)
    assert k.registry("tools").get("viz") is None
    # 再卸载 → not active
    ok2, msg2 = k.unload_plugin("vizplugin")
    assert not ok2 and "not active" in msg2


def test_unload_plugin_rejects_boot_plugin(kernel_env):
    ws, _ = kernel_env
    _write_plugin(ws, "vizplugin", PLUGIN_OK)
    k = get_kernel()
    k.ensure_loaded(str(ws))
    ok, msg = k.unload_plugin("vizplugin")
    assert not ok and "boot-scoped" in msg
    assert _catalog(ws)["vizplugin"]["phase"] == "active"  # 未被卸载


# ── plugin_help ─────────────────────────────────────────────────


def test_plugin_help_fields(kernel_env):
    ws, _ = kernel_env
    _write_plugin(ws, "vizplugin", PLUGIN_OK)
    get_kernel().ensure_loaded(str(ws))

    info = get_kernel().plugin_help("vizplugin")
    assert info is not None
    assert info["id"] == "vizplugin"
    assert info["summary"] == "画调用关系图"
    assert info["tools"] == ["<factory:viz>"]
    assert info["phase"] == "active"
    assert get_kernel().plugin_help("no-such") is None


# ── registry.unregister ─────────────────────────────────────────


def test_registry_unregister(kernel_env):
    ws, _ = kernel_env
    _write_plugin(ws, "vizplugin", PLUGIN_OK)
    get_kernel().ensure_loaded(str(ws))

    reg = get_kernel().registry("tools")
    assert reg.unregister("viz") is True
    assert reg.get("viz") is None
    assert reg.unregister("viz") is False  # 幂等：不存在返回 False


def test_unload_records_ledger_events(kernel_env):
    """记账纪律：卸载先记 unregistered + plugin_unloaded（记账先于动作）。"""
    ws, settings = kernel_env
    _write_plugin(ws, "vizplugin", PLUGIN_OK)
    settings.write_text(json.dumps({"plugins": {"disabled": ["vizplugin"]}}))
    k = get_kernel()
    sink = Sink()
    k.attach_ledger(sink, session="s1")
    k.ensure_loaded(str(ws))
    sink.events.clear()  # 忽略 boot 事件
    ok, _ = k.load_plugin("vizplugin")
    assert ok
    sink.events.clear()  # 忽略 load 事件

    ok, _ = k.unload_plugin("vizplugin")
    assert ok
    types = sink.types()
    assert "unregistered" in types
    assert "plugin_unloaded" in types
    # unregistered 事件 provenance = plugin:vizplugin，含 kind/name
    unreg = [e for e in sink.events if e.type == "unregistered"]
    assert unreg and unreg[0].origin == "plugin:vizplugin"
    assert unreg[0].payload["kind"] == "tools" and unreg[0].payload["name"] == "viz"


# ── 元工具 execute ──────────────────────────────────────────────


class _RebuildAgent:
    """假 agent：记录 _rebuild_tools 调用次数。"""

    def __init__(self):
        self.rebuilds = 0

    def _rebuild_tools(self):
        self.rebuilds += 1


async def test_meta_tools_execute(kernel_env):
    ws, settings = kernel_env
    _write_plugin(ws, "vizplugin", PLUGIN_OK)
    settings.write_text(json.dumps({"plugins": {"disabled": ["vizplugin"]}}))
    k = get_kernel()
    k.ensure_loaded(str(ws))
    agent = _RebuildAgent()

    # list_plugins：只读放行、文本含插件名（disabled 未加载 → 无 summary）
    r = await ListPluginsTool(k).execute()
    assert "vizplugin" in r.output
    assert ListPluginsTool(k).permission.level == PermissionLevel.ALLOW

    # plugin_help：disabled 插件详情（未加载 → 无 summary，phase=disabled）
    r = await PluginHelpTool(k).execute("vizplugin")
    assert "phase: disabled" in r.output

    # load_plugin：ASK + 成功后触发重建
    assert LoadPluginTool(k, agent).permission.level == PermissionLevel.ASK
    r = await LoadPluginTool(k, agent).execute("vizplugin")
    assert agent.rebuilds == 1 and "loaded" in r.output
    assert "vizplugin" in _catalog(ws)
    # 激活后 summary 出现
    r = await ListPluginsTool(k).execute()
    assert "画调用关系图" in r.output

    # unload_plugin：ASK + 成功后触发重建
    assert UnloadPluginTool(k, agent).permission.level == PermissionLevel.ASK
    r = await UnloadPluginTool(k, agent).execute("vizplugin")
    assert agent.rebuilds == 2 and "unloaded" in r.output
    assert "vizplugin" not in _catalog(ws)

    # 未找到路径：不触发重建、返回错误文本
    r = await LoadPluginTool(k, agent).execute("no-such")
    assert "not found" in r.output and agent.rebuilds == 2


# ── P-F：模型自产插件 ───────────────────────────────────────────


async def test_write_plugin_creates_and_loads(kernel_env):
    ws, _ = kernel_env
    k = get_kernel()
    k.ensure_loaded(str(ws))

    class _Agent:
        def __init__(self):
            self.rebuilds = 0
        def _rebuild_tools(self):
            self.rebuilds += 1

    agent = _Agent()
    r = await WritePluginTool(k, agent).execute(
        "greet", "打招呼插件", GEN_CODE, GEN_TEST, timeout=10, permissions=["fs:read"])
    assert r.success, r.error or r.output
    assert "auto-greet" in r.output and agent.rebuilds == 1
    # 落盘 + 加载 + 工具可用
    assert (ws / ".openx" / "plugins" / "auto-greet.py").is_file()
    info = k.plugin_help("auto-greet")
    assert info["phase"] == "active" and info["manifest"]["trust"] == "auto"
    assert info["manifest"]["timeout"] == 10
    assert k.registry("tools").get("greet") is not None


async def test_write_plugin_rejects_bad_code_and_selftest(kernel_env):
    ws, _ = kernel_env
    k = get_kernel()
    k.ensure_loaded(str(ws))
    w = WritePluginTool(k, None)

    # 缺 apply → 拒（不落盘）
    r = await w.execute("bad", "坏", "x = 1", GEN_TEST)
    assert not r.success and "apply" in r.error
    assert not (ws / ".openx" / "plugins" / "auto-bad.py").exists()
    # self_test 失败 → 拒
    r = await w.execute("badtest", "坏测试", GEN_CODE, "assert False")
    assert not r.success and "self_test" in r.error


async def test_test_plugin_passes_loaded(kernel_env):
    ws, _ = kernel_env
    k = get_kernel()
    k.ensure_loaded(str(ws))
    r = await WritePluginTool(k, None).execute("greet", "打招呼", GEN_CODE, GEN_TEST)
    assert r.success
    r = await TestPluginTool(k).execute("auto-greet")
    assert "PASS" in r.output, r.output
    # 未加载插件 → 拒
    # 未落盘插件的可测试文件路径 → 拒
    r = await TestPluginTool(k).execute("builtin-tools")
    assert "no testable file" in r.error
    # 未加载插件 → 拒
    r = await TestPluginTool(k).execute("no-such-plugin")
    assert "not loaded" in r.error


async def test_promote_plugin_records_decision(kernel_env):
    ws, _ = kernel_env
    k = get_kernel()
    sink = Sink()
    k.attach_ledger(sink, session="s1")
    k.ensure_loaded(str(ws))
    r = await WritePluginTool(k, None).execute("greet", "打招呼", GEN_CODE, GEN_TEST)
    assert r.success

    r = await PromotePluginTool(k).execute("auto-greet")
    assert r.success
    assert k.plugin_help("auto-greet")["manifest"]["trust"] == "user"
    assert any(e.type == "plugin_promoted" for e in sink.events)

    # 非 auto-* 不能晋升
    r = await PromotePluginTool(k).execute("builtin-tools")
    assert "only auto-" in r.output
