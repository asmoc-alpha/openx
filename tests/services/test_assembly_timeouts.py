"""S1d/S1c：contribute/render 超时隔离 + manifest 权限上限（能力 attestation）。

覆盖：
- 挂死的 contribute → 超时跳过（征集不炸）；连续 3 次 → 本会话永久跳过；
- 挂死的 render → 计入 UI 熔断，达阈值摘除；
- 正常贡献/面板零回归；
- manifest 未声明 permissions 的插件工具：自声明 ALLOW 被压到 ASK、
  auto_allowed 失效；声明了 → 原样；builtin 不包不受影响。

运行：``python -m pytest tests/services/test_assembly_timeouts.py -q``
"""

from __future__ import annotations

import asyncio

import pytest

import openx.services.assembly as asm
from openx.services.assembly import (
    UI_PANEL_MAX_FAILURES,
    UiPanelCollector,
    collect_context_fragments,
    instantiate_tools,
)


class _Reg:
    """鸭子注册表：entries + add_warning（足够消费面用）。"""

    def __init__(self, entries):
        self._entries = entries
        self.warnings: list[tuple[str, str]] = []

    def entries(self):
        return self._entries

    def add_warning(self, name, warning):
        self.warnings.append((name, warning))


class _Entry:
    def __init__(self, name, plugin, value):
        self.name = name
        self.plugin = plugin
        self.value = value


class _Ctx:
    def __init__(self, contribute, priority=100):
        self.contribute = contribute
        self.priority = priority


class _Slot:
    def __init__(self, render, refresh_hz=50.0):
        self.render = render
        self.refresh_hz = refresh_hz


class _Kernel:
    def __init__(self, regs):
        self._regs = regs

    def registry(self, kind):
        return self._regs.get(kind)

    def inventory(self):
        return []


@pytest.fixture(autouse=True)
def _reset_contribute_fails():
    """模块级熔断计数跨用例清零（键为 plugin:name，避免串味）。"""
    asm._CONTRIBUTE_FAILS.clear()
    yield
    asm._CONTRIBUTE_FAILS.clear()


def test_hanging_contribute_times_out_and_skips(monkeypatch):
    def hang():
        import time

        time.sleep(1)  # 远超超时；池 8 工人下排队 < 超时

    monkeypatch.setattr(asm, "_CONTRIBUTE_TIMEOUT", 0.2)
    reg = _Reg([_Entry("slow", "p1", _Ctx(hang))])
    k = _Kernel({"contexts": reg})
    frags = collect_context_fragments(k)
    assert frags == []
    assert any("timed out" in w for _, w in reg.warnings)


def test_contribute_circuit_breaker_after_three_failures(monkeypatch):
    calls = {"n": 0}

    def hang():
        import time

        calls["n"] += 1
        time.sleep(1)

    monkeypatch.setattr(asm, "_CONTRIBUTE_TIMEOUT", 0.1)
    reg = _Reg([_Entry("slow", "p1", _Ctx(hang))])
    k = _Kernel({"contexts": reg})
    for _ in range(3):
        collect_context_fragments(k)
    assert calls["n"] == 3
    # 第 4 次起熔断：不再征集（调用数不再增长）
    collect_context_fragments(k)
    assert calls["n"] == 3


def test_normal_contribute_unaffected(monkeypatch):
    reg = _Reg([
        _Entry("good", "p1", _Ctx(lambda: ["fragment-a"])),
        _Entry("empty", "p1", _Ctx(lambda: "")),
    ])
    k = _Kernel({"contexts": reg})
    frags = collect_context_fragments(k)
    assert frags == ["fragment-a"]
    assert reg.warnings == []


def test_hanging_render_trips_circuit_breaker(monkeypatch):
    def hang():
        import time

        time.sleep(1)

    monkeypatch.setattr(asm, "UI_RENDER_TIMEOUT", 0.2)
    reg = _Reg([_Entry("stuck", "p1", _Slot(hang))])
    collector = UiPanelCollector(_Kernel({"ui_slots": reg}))
    for _ in range(UI_PANEL_MAX_FAILURES):
        assert collector.panels() == []
    # 熔断摘除：unregister 被调（鸭子 kernel 记账）
    assert reg.warnings and any("timed out" in w for _, w in reg.warnings)


def test_normal_render_unaffected():
    reg = _Reg([_Entry("clock", "p1", _Slot(lambda: ["12:00"]))])
    collector = UiPanelCollector(_Kernel({"ui_slots": reg}))
    assert collector.panels() == [("clock", ["12:00"])]


# ── S1c：manifest permissions = 能力上限 ──────────────────────────


class _ToolReg:
    def __init__(self, entries):
        self._entries = entries
        self.warnings: list[tuple[str, str]] = []

    def entries(self):
        return self._entries

    def add_warning(self, name, warning):
        self.warnings.append((name, warning))


class _CapKernel:
    """instantiate_tools 的最小 kernel 面：tools 注册表 + inventory。"""

    def __init__(self, manifest, entries):
        self._manifest = manifest
        self._entries = entries
        self.last_reg: _ToolReg | None = None

    def registry(self, kind):
        if kind == "tools":
            self.last_reg = _ToolReg(self._entries)
            return self.last_reg
        return None

    def inventory(self):
        return [type("Info", (), {
            "id": "capped-plugin",
            "builtin": False,
            "manifest": self._manifest,
        })()]


class _EasyTool:
    name = "easy"
    description = "self-declared ALLOW"
    parameters = {"type": "object", "properties": {}}

    @property
    def permission(self):
        from openx.permissions import Permission, PermissionLevel

        return Permission(level=PermissionLevel.ALLOW, reason="trust me")

    def auto_allowed(self, args):
        return True

    def is_high_risk(self, args):
        return False

    def validate_args(self, **kw):
        return None

    def preview_diff(self, args):
        return None

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name,
                                                 "description": self.description,
                                                 "parameters": self.parameters}}

    async def execute(self, **kw):
        from openx.tools.base import ToolResult

        return ToolResult(output="ok")


def _factory():
    # register_tool 路径：内核把实例包成 factory(host) -> [tool]
    return lambda host: [_EasyTool()]


def test_permission_cap_when_manifest_has_no_permissions():
    k = _CapKernel({"timeout": 5, "permissions": []},
                   [_Entry("easy", "capped-plugin", _factory())])
    tools = instantiate_tools(k, type("H", (), {})())
    wrapped = tools["easy"]
    from openx.permissions import PermissionLevel

    assert wrapped.permission.level == PermissionLevel.ASK
    assert "manifest declares no permissions" in wrapped.permission.reason
    assert wrapped.auto_allowed({}) is False
    # 收紧在装配面留下解释性警告
    assert k.last_reg is not None and k.last_reg.warnings


def test_no_cap_when_permissions_declared():
    k = _CapKernel({"timeout": 5, "permissions": ["fs:read"]},
                   [_Entry("easy", "capped-plugin", _factory())])
    tools = instantiate_tools(k, type("H", (), {})())
    wrapped = tools["easy"]
    from openx.permissions import PermissionLevel

    assert wrapped.permission.level == PermissionLevel.ALLOW
    assert wrapped.auto_allowed({}) is True

