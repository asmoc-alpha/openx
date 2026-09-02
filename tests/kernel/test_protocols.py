"""P-D 协议分类测试：协议目录路由 + context/lifecycle 注册面与消费面。

覆盖：
- protocols.py 路由（已知 type -> 协议；未知/缺失 -> tool/v1 默认；mount 派生）；
- ctx.register_context / register_lifecycle（注册、校验拒载记警告、清单暴露）；
- collect_context_fragments（注册序征集、预算、单插件崩溃隔离、子代理不继承）；
- trigger_lifecycle（按序回调、单钩子异常隔离 + plugin_error 记账）；
- unload_plugin 的 on_unload 状态落盘契约（先回调再清注册）；
- 协议一致性 warning（声明 type 与实际注册面不符）；
- write_plugin 按 type 生成 context/lifecycle 插件端到端（含注册面契约拒绝）。

环境：kernel_env fixture（临时 workspace + SETTINGS_PATH + 新内核）。
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from openx.kernel.protocol import Event
from openx.kernel import get_kernel
from openx.kernel.assembly.protocols import (
    DEFAULT_PROTOCOL,
    PROTOCOLS,
    derive_mount,
    find,
    route,
)
from openx.services.assembly import collect_context_fragments
from openx.tools.write_plugin_tools import TestPluginTool, WritePluginTool

# 防 pytest 把工具类当测试类收集
TestPluginTool.__test__ = False  # type: ignore[attr-defined]


class Sink:
    """收集事件的账本 sink（test_ledger 同款）。"""

    def __init__(self):
        self.events: list[Event] = []

    def __call__(self, event: Event) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]


# ── 协议目录：路由与派生 ─────────────────────────────────────────


def test_protocol_catalog_and_route():
    kinds = {p.ptype: p.registry_kind for p in PROTOCOLS}
    assert kinds == {
        "capability.tool": "tools",
        "context.memory": "contexts",
        "lifecycle": "lifecycle",
        "ui.panel": "ui_slots",
    }
    # 已知 type 路由到对应协议，mount 由表派生
    assert route("capability.tool").registry_kind == "tools"
    assert route("context.memory").protocol == "context/v1"
    assert route("context.memory").mount == "loop.pre-inference"
    assert derive_mount("lifecycle") == "lifecycle.session"
    assert route("ui.panel").mount == "ui.deck"
    # 未知 / 缺失 -> tool/v1 默认路由（向后兼容）
    assert route("strategy.planning") is DEFAULT_PROTOCOL
    assert route(None) is DEFAULT_PROTOCOL
    assert route("") is DEFAULT_PROTOCOL
    # 按协议版本号反查
    assert find("context/v1").ptype == "context.memory"
    assert find("nope/v9") is None


# ── 插件样本 ─────────────────────────────────────────────────────

CONTEXT_PLUGIN = textwrap.dedent('''\
    """上下文插件：往系统提示贡献片段。"""
    __openx_meta__ = {"type": "context.memory", "mount": "loop.pre-inference",
                      "trust": "user", "summary": "注入项目术语表",
                      "cost": {"schemaTokens": 200}}

    def contribute():
        return ["## Project Glossary", "foo = bar"]

    def apply(ctx):
        ctx.register_context("glossary", contribute, priority=50)
''')


def _lifecycle_plugin(marker: Path) -> str:
    """生命周期插件：钩子把标记写进文件（观察副作用，不依赖模块句柄）。"""
    return textwrap.dedent(f'''\
        """生命周期插件：会话钩子。"""
        __openx_meta__ = {{"type": "lifecycle", "mount": "lifecycle.session",
                          "trust": "user", "summary": "会话钩子",
                          "cost": {{"schemaTokens": 100}}}}
        _MARKER = {str(marker)!r}

        def on_start():
            with open(_MARKER, "a", encoding="utf-8") as f:
                f.write("start;")

        def on_unload():
            with open(_MARKER, "a", encoding="utf-8") as f:
                f.write("unload;")

        def apply(ctx):
            ctx.register_lifecycle("hooks", on_session_start=on_start,
                                   on_unload=on_unload)
    ''')


def _write_plugin_file(ws: Path, name: str, body: str) -> None:
    d = ws / ".openx" / "plugins"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.py").write_text(body, encoding="utf-8")


def _load_session_plugin(ws, settings, name, body):
    """boot 禁用 + 会话内 load_plugin -> 拿到 session 作用域插件。"""
    _write_plugin_file(ws, name, body)
    settings.write_text(json.dumps({"plugins": {"disabled": [name]}}))
    k = get_kernel()
    k.ensure_loaded(str(ws))
    ok, msg = k.load_plugin(name)
    assert ok, msg
    return k


# ── context/v1：注册与征集 ───────────────────────────────────────


def test_context_plugin_registers_and_collects(kernel_env):
    ws, _ = kernel_env
    _write_plugin_file(ws, "glossary", CONTEXT_PLUGIN)
    k = get_kernel()
    k.ensure_loaded(str(ws))

    entry = k.registry("contexts").get("glossary")
    assert entry is not None and entry.plugin == "glossary"
    # 清单暴露：目录带 type，详情带 contexts 注册名
    cat = {c["id"]: c for c in k.list_plugins()}["glossary"]
    assert cat["type"] == "context.memory"
    assert cat["contexts"] == ["glossary"]
    help_ = k.plugin_help("glossary")
    assert help_["contexts"] == ["glossary"]
    # 征集：展平为片段列表
    fragments = collect_context_fragments(k)
    assert "## Project Glossary" in fragments and "foo = bar" in fragments
    # 卸载：注册清掉、片段消失
    # （boot 插件不能 session unload，这里直接验证注册表 purge 语义）
    k._purge_plugin_entries("glossary")
    assert collect_context_fragments(k) == []


def test_collect_context_fault_isolation_and_budget(kernel_env):
    """单插件 contribute 崩溃 -> 跳过 + warning；预算超限 -> 截断征集。"""
    ws, _ = kernel_env
    bad = textwrap.dedent('''\
        __openx_meta__ = {"type": "context.memory", "summary": "坏的"}
        def contribute():
            raise RuntimeError("no context for you")
        def apply(ctx):
            ctx.register_context("badctx", contribute)
    ''')
    _write_plugin_file(ws, "badctx", bad)
    _write_plugin_file(ws, "glossary", CONTEXT_PLUGIN)
    k = get_kernel()
    k.ensure_loaded(str(ws))

    fragments = collect_context_fragments(k)
    assert "## Project Glossary" in fragments  # 坏插件不炸征集
    entry = k.registry("contexts").get("badctx")
    assert entry is not None and any("contribute failed" in w for w in entry.warnings)

    # 预算：5 字符装不下第一个片段 -> 空 + 警告
    fragments = collect_context_fragments(k, budget=5)
    assert fragments == []
    assert any(
        "budget exhausted" in w for w in k.registry("contexts").get("glossary").warnings
    )


def test_collect_excludes_plugin_contexts_for_subagents(kernel_env):
    """include_plugins=False：子代理只征集内置贡献（能力继承=父集的子集）。"""
    ws, _ = kernel_env
    _write_plugin_file(ws, "glossary", CONTEXT_PLUGIN)
    k = get_kernel()
    k.ensure_loaded(str(ws))

    assert collect_context_fragments(k, include_plugins=True)
    # 内置插件当前不贡献上下文 -> 子代理视角为空
    assert collect_context_fragments(k, include_plugins=False) == []


def test_context_registration_validates_shape(kernel_env):
    """contribute 不可调用 -> 拒载记警告，插件仍 active（不炸加载）。"""
    ws, _ = kernel_env
    bad = textwrap.dedent('''\
        __openx_meta__ = {"summary": "形状坏的上下文插件"}
        def apply(ctx):
            ctx.register_context("badshape", "not-callable")
    ''')
    _write_plugin_file(ws, "badshape", bad)
    k = get_kernel()
    k.ensure_loaded(str(ws))

    assert k.registry("contexts").get("badshape") is None
    info = k.plugin_help("badshape")
    assert info["phase"] == "active"
    assert any("rejected context" in w for w in info["warnings"])


# ── lifecycle/v1：触发与故障隔离 ─────────────────────────────────


def test_lifecycle_trigger_and_unload_contract(kernel_env):
    """unload 先回调 on_unload（落盘契约）再清注册；session_start 按序触发。"""
    ws, settings = kernel_env
    marker = ws / "marker.txt"
    k = _load_session_plugin(ws, settings, "hooks", _lifecycle_plugin(marker))

    k.trigger_lifecycle("session_start")
    assert marker.read_text(encoding="utf-8") == "start;"

    ok, msg = k.unload_plugin("hooks")
    assert ok, msg
    assert marker.read_text(encoding="utf-8") == "start;unload;"  # 落盘契约先于清注册
    assert k.registry("lifecycle").get("hooks") is None


def test_trigger_lifecycle_fault_isolation(kernel_env):
    """坏钩子：吞异常 + plugin_error 记账，后续钩子照常触发。"""
    ws, settings = kernel_env
    marker = ws / "marker.txt"
    bad = textwrap.dedent(f'''\
        __openx_meta__ = {{"type": "lifecycle", "summary": "坏钩子"}}
        def bad_start():
            raise RuntimeError("hook boom")
        def apply(ctx):
            ctx.register_lifecycle("badhooks", on_session_start=bad_start)
    ''')
    _write_plugin_file(ws, "badhooks", bad)
    settings.write_text(json.dumps({"plugins": {"disabled": ["badhooks"]}}))
    k = get_kernel()
    sink = Sink()
    k.attach_ledger(sink, session="s1")
    k.ensure_loaded(str(ws))
    ok, _ = k.load_plugin("badhooks")
    assert ok

    k.trigger_lifecycle("session_start")  # 不抛
    errors = [e for e in sink.events if e.type == "plugin_error"]
    assert errors and errors[0].origin == "plugin:badhooks"
    assert errors[0].payload["where"] == "lifecycle.on_session_start"
    entry = k.registry("lifecycle").get("badhooks")
    assert any("on_session_start failed" in w for w in entry.warnings)


def test_lifecycle_registration_requires_a_hook(kernel_env):
    """一个钩子都不给 -> 拒载记警告（注册无意义）。"""
    ws, _ = kernel_env
    bad = textwrap.dedent('''\
        __openx_meta__ = {"summary": "空钩子"}
        def apply(ctx):
            ctx.register_lifecycle("empty", on_session_start=None)
    ''')
    _write_plugin_file(ws, "emptyhooks", bad)
    k = get_kernel()
    k.ensure_loaded(str(ws))

    assert k.registry("lifecycle").get("empty") is None
    info = k.plugin_help("emptyhooks")
    assert info["phase"] == "active"
    assert any("rejected lifecycle" in w for w in info["warnings"])


# ── 协议一致性 ───────────────────────────────────────────────────


def test_protocol_consistency_warning(kernel_env):
    """声明 type 与实际注册面不符 -> manifest_warnings 记警告（不拒载）。"""
    ws, _ = kernel_env
    mismatch = textwrap.dedent('''\
        __openx_meta__ = {"type": "context.memory", "summary": "挂羊头"}
        from openx.tools.base import Tool, ToolResult

        class T(Tool):
            name = "t"
            description = "工具"
            async def execute(self, **kw):
                return ToolResult(output="ok")

        def factory(host):
            return [T()]

        def apply(ctx):
            ctx.register_tool_factory("t", factory)
    ''')
    _write_plugin_file(ws, "mismatch", mismatch)
    k = get_kernel()
    k.ensure_loaded(str(ws))

    info = k.plugin_help("mismatch")
    assert info["phase"] == "active"  # 不拒载
    assert any(
        "declared type" in w and "no contexts" in w
        for w in info["manifest_warnings"]
    )
    # 一致的插件（显式 type + 对应注册面）无此警告
    _write_plugin_file(ws, "glossary", CONTEXT_PLUGIN)
    k._reload(str(ws), (str(ws), str(ws), ()))  # 强制重组（目录内容不在加载键里）
    assert not any(
        "declared type" in w for w in k.plugin_help("glossary")["manifest_warnings"]
    )


# ── write_plugin：按 type 生成 context / lifecycle 插件 ──────────

CONTEXT_CODE = '''\
def contribute():
    return "## Auto Context: remember the foobar convention"

def apply(ctx):
    ctx.register_context("auto_ctx", contribute, priority=200)
'''
CONTEXT_TEST = '''\
assert contribute().startswith("## Auto Context")
'''


async def test_write_plugin_generates_context_plugin(kernel_env):
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
        "ctx", "自动上下文插件", CONTEXT_CODE, CONTEXT_TEST,
        type="context.memory", permissions=["fs:read"])
    assert r.success, r.error or r.output
    assert agent.rebuilds == 1
    # manifest：type/mount 由协议表派生；timeout 不进 manifest
    info = k.plugin_help("auto-ctx")
    assert info["manifest"]["type"] == "context.memory"
    assert info["manifest"]["mount"] == "loop.pre-inference"
    assert "timeout" not in info["manifest"]
    assert info["contexts"] == ["auto_ctx"]
    # 片段可征集
    assert any("foobar convention" in f for f in collect_context_fragments(k))
    # test_plugin 重跑自测
    r = await TestPluginTool(k).execute("auto-ctx")
    assert "PASS" in r.output, r.output


LIFECYCLE_CODE_TMPL = '''\
_MARKER = {marker!r}

def on_start():
    with open(_MARKER, "a", encoding="utf-8") as f:
        f.write("gen-start;")

def on_unload():
    with open(_MARKER, "a", encoding="utf-8") as f:
        f.write("gen-unload;")

def apply(ctx):
    ctx.register_lifecycle("gen_hooks", on_session_start=on_start,
                           on_unload=on_unload)
'''
LIFECYCLE_TEST = '''\
assert callable(on_start) and callable(on_unload)
'''


async def test_write_plugin_generates_lifecycle_plugin(kernel_env):
    ws, _ = kernel_env
    marker = ws / "gen-marker.txt"
    code = LIFECYCLE_CODE_TMPL.format(marker=str(marker))
    k = get_kernel()
    k.ensure_loaded(str(ws))

    r = await WritePluginTool(k, None).execute(
        "hooks", "自动生命周期插件", code, LIFECYCLE_TEST, type="lifecycle")
    assert r.success, r.error or r.output
    info = k.plugin_help("auto-hooks")
    assert info["manifest"]["type"] == "lifecycle"
    assert info["manifest"]["mount"] == "lifecycle.session"
    assert info["lifecycle"] == ["gen_hooks"]

    k.trigger_lifecycle("session_start")
    assert marker.read_text(encoding="utf-8") == "gen-start;"
    # unload：on_unload 落盘契约 + 注册清理
    ok, _ = k.unload_plugin("auto-hooks")
    assert ok
    assert marker.read_text(encoding="utf-8") == "gen-start;gen-unload;"
    assert k.registry("lifecycle").get("gen_hooks") is None


async def test_write_plugin_rejects_type_mismatch(kernel_env):
    """type 与注册面不匹配 / 未知 type -> 拒绝（不落盘）。"""
    ws, _ = kernel_env
    k = get_kernel()
    k.ensure_loaded(str(ws))
    w = WritePluginTool(k, None)

    # 声明 context 却注册工具
    tool_code = '''\
from openx.tools.base import Tool, ToolResult
class T(Tool):
    name = "t"
    description = "工具"
    async def execute(self, **kw):
        return ToolResult(output="ok")
def factory(host):
    return [T()]
def apply(ctx):
    ctx.register_tool_factory("t", factory)
'''
    r = await w.execute("mism", "错配", tool_code, "assert True",
                        type="context.memory")
    assert not r.success and "register_context" in r.error
    assert not (ws / ".openx" / "plugins" / "auto-mism.py").exists()

    # 声明 lifecycle 却没有 register_lifecycle
    r = await w.execute("nolc", "无钩子", "def apply(ctx):\n    pass\n",
                        "assert True", type="lifecycle")
    assert not r.success and "register_lifecycle" in r.error

    # 未知 type：拒绝（write 侧不走默认路由）
    r = await w.execute("plan", "规划器", tool_code, "assert True",
                        type="strategy.planning")
    assert not r.success and "未知插件类型" in r.error

    # 工具类缺 factory：按协议要求必备函数
    nofactory = '''\
def apply(ctx):
    ctx.register_tool("x", None)
'''
    r = await w.execute("nofac", "无工厂", nofactory, "assert True")
    assert not r.success and "factory" in r.error


# ── 端到端：context 插件片段进系统提示 ───────────────────────────


async def test_context_plugin_reaches_system_prompt(kernel_env):
    """write_plugin 生成 context 插件 -> 片段进系统提示；unload 后消失。

    走真 agent（OpenXAgent）：write_plugin 成功回调 agent._rebuild_tools，
    P-D 起该方法同时重建系统提示--上下文类插件装配后下一轮即生效。
    """
    from openx.agent import OpenXAgent
    from openx.config import OpenXConfig

    ws, _ = kernel_env
    config = OpenXConfig()
    config.workspace = str(ws)
    config.api_key = "sk-test"
    config.api_base = "https://example.com/v1"
    config.model = "test-model"
    agent = OpenXAgent(config)
    k = get_kernel()

    r = await WritePluginTool(k, agent).execute(
        "ctx", "自动上下文插件", CONTEXT_CODE, CONTEXT_TEST,
        type="context.memory")
    assert r.success, r.error or r.output
    assert "foobar convention" in agent._system_prompt

    ok, _ = k.unload_plugin("auto-ctx")
    assert ok
    agent._rebuild_tools()
    assert "foobar convention" not in agent._system_prompt
