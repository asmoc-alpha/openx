"""ui/v1 协议测试：UI 面板注册面 + 征集器（故障隔离/熔断）+ deck 接线。

覆盖：
- ctx.register_ui_slot（注册、形状校验拒载、清单/详情暴露）；
- UiPanelCollector：str/list 渲染、行数限额、refresh_hz 节流、单面板
  崩溃跳过、连续失败熔断自动 unregister（unregistered 记账）；
- StreamingService._plugin_deck_renderable：面板进 deck、行预算折叠、
  坏 markup 面板缺席（deck 行不变量不破）；
- write_plugin 生成 ui.panel 插件端到端（manifest 派生 + 面板可征集）；
- self_test 死循环超时拒绝（daemon 线程兜底）。

环境：kernel_env fixture（临时 workspace + SETTINGS_PATH + 新内核）。
"""

from __future__ import annotations

import json
import textwrap
from types import SimpleNamespace

from openx.kernel import get_kernel
from openx.services.assembly import UiPanelCollector
from openx.services.streaming import StreamingService
from openx.tools.write_plugin_tools import WritePluginTool

WritePluginTool.__test__ = False  # type: ignore[attr-defined]


def _write_plugin_file(ws, name: str, body: str) -> None:
    d = ws / ".openx" / "plugins"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.py").write_text(body, encoding="utf-8")


# ── 注册面 ───────────────────────────────────────────────────────


PET_PLUGIN = textwrap.dedent('''\
    """桌面宠物插件：状态层显示一只会眨眼的小宠物。"""
    __openx_meta__ = {"type": "ui.panel", "mount": "ui.deck",
                      "trust": "user", "summary": "输入框下方的桌面宠物",
                      "cost": {"schemaTokens": 120}}

    _FRAMES = ["(=^··^=)", "(=^-^=)", "(=··ω··=)"]
    _state = {"i": 0}

    def render():
        _state["i"] = (_state["i"] + 1) % len(_FRAMES)
        return ["[dim]" + _FRAMES[_state["i"]] + "  pet is happy[/dim]"]

    def apply(ctx):
        ctx.register_ui_slot("pet", render, refresh_hz=2.0)

    def self_test():
        lines = render()
        assert len(lines) == 1 and "pet is happy" in lines[0]
''')


def test_ui_plugin_registers(kernel_env):
    ws, _ = kernel_env
    _write_plugin_file(ws, "pet", PET_PLUGIN)
    k = get_kernel()
    k.ensure_loaded(str(ws))

    entry = k.registry("ui_slots").get("pet")
    assert entry is not None and entry.plugin == "pet"
    assert entry.value.refresh_hz == 2.0
    # 清单暴露：目录带 type，详情带 ui_slots 注册名
    cat = {c["id"]: c for c in k.list_plugins()}["pet"]
    assert cat["type"] == "ui.panel"
    assert cat["ui_slots"] == ["pet"]
    help_ = k.plugin_help("pet")
    assert help_["ui_slots"] == ["pet"]
    assert "declared type" not in " ".join(help_["manifest_warnings"])
    # 面板可征集
    collector = UiPanelCollector(k)
    panels = collector.panels()
    assert panels == [("pet", ["[dim](=^-^=)  pet is happy[/dim]"])]


def test_ui_registration_validates_shape(kernel_env):
    """render 不可调用 / refresh_hz 非正数 -> 拒载记警告，插件仍 active。"""
    ws, _ = kernel_env
    bad = textwrap.dedent('''\
        __openx_meta__ = {"summary": "形状坏的面板"}
        def apply(ctx):
            ctx.register_ui_slot("bad", "not-callable")
            ctx.register_ui_slot("badhz", lambda: "x", refresh_hz=0)
    ''')
    _write_plugin_file(ws, "badui", bad)
    k = get_kernel()
    k.ensure_loaded(str(ws))

    assert k.registry("ui_slots").get("bad") is None
    assert k.registry("ui_slots").get("badhz") is None
    info = k.plugin_help("badui")
    assert info["phase"] == "active"
    assert sum("rejected ui slot" in w for w in info["warnings"]) == 2


# ── 征集器：隔离 / 限额 / 节流 / 熔断 ───────────────────────────


def _load_ui_plugin(ws, settings, name, body):
    _write_plugin_file(ws, name, body)
    settings.write_text(json.dumps({"plugins": {"disabled": [name]}}))
    k = get_kernel()
    k.ensure_loaded(str(ws))
    ok, msg = k.load_plugin(name)
    assert ok, msg
    return k


def test_collector_crash_isolation_and_breaker(kernel_env):
    """坏面板崩溃跳过（好面板照常）；连续 3 次崩溃熔断自动摘除。"""
    ws, settings = kernel_env
    bad = textwrap.dedent('''\
        __openx_meta__ = {"type": "ui.panel", "summary": "坏面板"}
        def render():
            raise RuntimeError("render boom")
        def apply(ctx):
            ctx.register_ui_slot("badpanel", render)
        def self_test():
            assert callable(render)
    ''')
    k = _load_ui_plugin(ws, settings, "badpanel", bad)
    _write_plugin_file(ws, "pet", PET_PLUGIN)
    k.load_plugin("pet")

    sink = []
    k.attach_ledger(lambda e: sink.append(e), session="s1")
    collector = UiPanelCollector(k)
    # 3 帧内：坏面板缺席、好面板照常；第 3 次崩溃触发熔断摘除
    for i in range(3):
        panels = dict(collector.panels())
        assert "pet" in panels
        assert "badpanel" not in panels
    # 熔断已摘除 + 记账（unregistered 事件 + 注册表警告）
    assert k.registry("ui_slots").get("badpanel") is None
    assert any(e.type == "unregistered" and e.payload["kind"] == "ui_slots"
               for e in sink)
    entry = k.registry("ui_slots").get("pet")
    assert entry is not None  # 好面板不受牵连
    # 摘除后征集只剩好面板
    assert [name for name, _ in collector.panels()] == ["pet"]


def test_collector_line_cap(kernel_env):
    """单面板行数超限截断（资源限额）。"""
    ws, _ = kernel_env
    many = textwrap.dedent('''\
        __openx_meta__ = {"type": "ui.panel", "summary": "多行面板"}
        def render():
            return [f"line {i}" for i in range(20)]
        def apply(ctx):
            ctx.register_ui_slot("many", render)
        def self_test():
            assert len(render()) == 20
    ''')
    _write_plugin_file(ws, "many", many)
    k = get_kernel()
    k.ensure_loaded(str(ws))

    panels = dict(UiPanelCollector(k).panels())
    assert len(panels["many"]) == 8  # UI_PANEL_MAX_LINES


def test_collector_throttle(kernel_env):
    """refresh_hz 节流：未到刷新周期沿用缓存（不再调 render）。"""
    ws, _ = kernel_env
    counter = {"n": 0}

    class _SlowSlot:
        refresh_hz = 0.001  # 周期 1000s：一次渲染后长期沿用缓存

        def render(self):
            counter["n"] += 1
            return f"frame {counter['n']}"

    k = get_kernel()
    k.ensure_loaded(str(ws))
    k.registry("ui_slots").register("slow", _SlowSlot(), "builtin-tools")

    collector = UiPanelCollector(k)
    first = dict(collector.panels())["slow"]
    for _ in range(5):
        again = dict(collector.panels())["slow"]
    assert counter["n"] == 1  # 只渲染了一次
    assert again == first


# ── deck 接线 ────────────────────────────────────────────────────


def _svc(panels_collector):
    """最小 StreamingService 桩（_plugin_deck_renderable 所需面）。"""
    console = SimpleNamespace(
        _console=SimpleNamespace(height=24),
        _input_queue=[],
        _frame_on_screen=False,
        _frame_renderable=lambda i, o: None,
    )
    return StreamingService(console, panels=panels_collector)


def test_plugin_deck_renderable(kernel_env):
    """面板行进 deck（Group + 行数）；无面板/无收集器 = 零变化。"""
    ws, _ = kernel_env
    _write_plugin_file(ws, "pet", PET_PLUGIN)
    k = get_kernel()
    k.ensure_loaded(str(ws))
    collector = UiPanelCollector(k)

    group, h = _svc(collector)._plugin_deck_renderable()
    assert group is not None and h == 1
    assert "pet is happy" in group.renderables[0].plain

    # 无收集器 / 无面板 → (None, 0)
    assert _svc(None)._plugin_deck_renderable() == (None, 0)
    empty_kernel = get_kernel()
    empty_kernel._purge_plugin_entries("pet")
    assert _svc(UiPanelCollector(k))._plugin_deck_renderable() == (None, 0)


def test_plugin_deck_budget_folds_overflow(kernel_env):
    """超视口预算的行折叠成 "+N more"（deck 永不撑爆视口）。"""
    ws, _ = kernel_env
    many = textwrap.dedent('''\
        __openx_meta__ = {"type": "ui.panel", "summary": "巨型面板"}
        def render():
            return [f"line {i}" for i in range(8)]
        def apply(ctx):
            ctx.register_ui_slot("many", render)
        def self_test():
            assert render()
    ''')
    _write_plugin_file(ws, "many", many)
    k = get_kernel()
    k.ensure_loaded(str(ws))

    svc = _svc(UiPanelCollector(k))
    svc._rich = SimpleNamespace(height=16)  # 预算 = 16-7-5 = 4 行
    group, h = svc._plugin_deck_renderable()
    assert h == 5  # 4 行 + "+N more"
    assert group.renderables[-1].plain.strip() == "+4 more"


def test_plugin_deck_bad_markup_skips_panel(kernel_env):
    """坏 markup（游离闭合标签 → MarkupError）→ 该面板本帧缺席，不炸 deck。"""
    ws, _ = kernel_env
    badmk = textwrap.dedent('''\
        __openx_meta__ = {"type": "ui.panel", "summary": "坏markup"}
        def render():
            return ["[/bold]stray close"]
        def apply(ctx):
            ctx.register_ui_slot("badmk", render)
        def self_test():
            assert render()
    ''')
    _write_plugin_file(ws, "pet", PET_PLUGIN)
    _write_plugin_file(ws, "badmk", badmk)
    k = get_kernel()
    k.ensure_loaded(str(ws))

    group, h = _svc(UiPanelCollector(k))._plugin_deck_renderable()
    assert h == 1 and "pet is happy" in group.renderables[0].plain


# ── write_plugin：生成 ui.panel 插件端到端 ──────────────────────

PET_CODE = '''\
_FRAMES = ["(=^··^=)", "(=^-^=)"]
_i = {"n": 0}

def render():
    _i["n"] = (_i["n"] + 1) % 2
    return "[dim]" + _FRAMES[_i["n"]] + "  auto pet[/dim]"

def apply(ctx):
    ctx.register_ui_slot("auto_pet", render, refresh_hz=1000)
'''
PET_TEST = '''\
assert "auto pet" in render()
'''


async def test_write_plugin_generates_ui_plugin(kernel_env):
    ws, _ = kernel_env
    k = get_kernel()
    k.ensure_loaded(str(ws))

    r = await WritePluginTool(k, None).execute(
        "pet", "自动桌面宠物", PET_CODE, PET_TEST, type="ui.panel")
    assert r.success, r.error or r.output
    info = k.plugin_help("auto-pet")
    assert info["manifest"]["type"] == "ui.panel"
    assert info["manifest"]["mount"] == "ui.deck"
    assert info["ui_slots"] == ["auto_pet"]
    # 动画：两次征集（跨过刷新周期）产出不同帧
    import time

    collector = UiPanelCollector(k)
    frames = [dict(collector.panels())["auto_pet"]]
    time.sleep(0.01)  # 跨过 refresh_hz 周期（1ms）
    frames.append(dict(collector.panels())["auto_pet"])
    assert frames[0] != frames[1]
    # unload → 面板消失
    ok, _ = k.unload_plugin("auto-pet")
    assert ok
    assert UiPanelCollector(k).panels() == []


async def test_write_plugin_rejects_ui_type_mismatch(kernel_env):
    """声明 ui.panel 却注册工具 → 拒（不落盘）。"""
    ws, _ = kernel_env
    k = get_kernel()
    k.ensure_loaded(str(ws))
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
    r = await WritePluginTool(k, None).execute(
        "uimix", "错配", tool_code, "assert True", type="ui.panel")
    assert not r.success and "register_ui_slot" in r.error
    assert not (ws / ".openx" / "plugins" / "auto-uimix.py").exists()


async def test_write_plugin_rejects_hanging_self_test(kernel_env, monkeypatch):
    """self_test 挂死 → 超时拒绝（daemon 线程兜底，主进程不卡死）。

    用 sleep 模拟挂死（释放 GIL，不烧 CPU 拖慢后续测试）；纯 spin 死循环
    走同一条 join 超时路径。
    """
    import openx.tools.write_plugin_tools as wpt

    monkeypatch.setattr(wpt, "SELF_TEST_TIMEOUT", 0.2)
    ws, _ = kernel_env
    k = get_kernel()
    k.ensure_loaded(str(ws))

    hang = PET_CODE  # 代码本身没问题；self_test 挂死
    r = await WritePluginTool(k, None).execute(
        "hang", "挂死测试", hang,
        "import time\ntime.sleep(5)", type="ui.panel")
    assert not r.success and "超时" in r.error
    assert not (ws / ".openx" / "plugins" / "auto-hang.py").exists()
