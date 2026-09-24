"""P6 组合输入：model_profile × 用户/项目 overlay → 应载清单。

覆盖：
- :func:`composition.resolve` 纯函数：空 overlay ⇒ 全集（「不加载 ≡ 现状」）、
  auto-* 出厂默认不进 boot、enable/disable 优先级（用户赢项目、overlay 赢
  settings）、档案 retire/require（联动回挂）；
- overlay/profile 文件 IO（临时目录，绝不碰真实 ~/.openx）；
- 内核接线：overlay enable/disable 生效、overlay 变更触发重组、
  ``composition_resolved`` payload 增字段、``/composition`` 只读面板。

运行：``python -m pytest tests/kernel/test_composition.py -q``
"""

from __future__ import annotations

import json

from openx.kernel import get_kernel
from openx.kernel.assembly import composition

from ._helpers import HELLO_SRC, write_plugin

# ── resolve：纯函数 ──────────────────────────────────────────────


def test_empty_overlay_is_full_set():
    """标准四：overlay 为空且档案未声明要求 ⇒ 全集（非 auto-*）。"""
    c = composition.resolve(["alpha", "beta"])
    assert c.load == ["alpha", "beta"] and c.disabled == {} and c.retired == {}


def test_auto_excluded_by_factory_default():
    c = composition.resolve(["alpha", "auto-x"])
    assert c.load == ["alpha"]
    assert c.disabled == {"auto-x": composition.REASON_AUTO}


def test_overlay_enable_brings_back_auto():
    c = composition.resolve(["auto-x"], user_overlay=composition.Overlay(enable={"auto-x"}))
    assert c.load == ["auto-x"]


def test_user_overlay_wins_over_project():
    c = composition.resolve(
        ["alpha"],
        user_overlay=composition.Overlay(enable={"alpha"}),
        project_overlay=composition.Overlay(disable={"alpha"}),
    )
    assert c.load == ["alpha"]
    # 反向：项目 enable、用户 disable → 用户赢（禁用）
    c = composition.resolve(
        ["alpha"],
        user_overlay=composition.Overlay(disable={"alpha"}),
        project_overlay=composition.Overlay(enable={"alpha"}),
    )
    assert c.disabled == {"alpha": composition.REASON_USER_DISABLE}


def test_settings_disabled_sugar_and_enable_override():
    c = composition.resolve(["alpha"], settings_disabled=["alpha"])
    assert c.disabled == {"alpha": composition.REASON_SETTINGS}
    c = composition.resolve(
        ["alpha"], settings_disabled=["alpha"], user_overlay=composition.Overlay(enable={"alpha"})
    )
    assert c.load == ["alpha"]


def test_profile_retire_and_require():
    prof = composition.ModelProfile(name="m1", retire={"scaf"})
    c = composition.resolve(["scaf", "other"], profile=prof)
    assert c.retired == {"scaf": composition.REASON_PROFILE_RETIRED}
    assert c.load == ["other"]
    # require 撤销 profile 派生退场（联动自动回挂）
    prof2 = composition.ModelProfile(name="m2", retire={"scaf"}, require={"scaf"})
    c = composition.resolve(["scaf"], profile=prof2)
    assert c.retired == {} and c.load == ["scaf"]


def test_ledger_retire_not_overridden_by_require():
    c = composition.resolve(
        ["scaf"], retired_by_ledger=["scaf"], profile=composition.ModelProfile(require={"scaf"})
    )
    assert c.retired == {"scaf": composition.REASON_LEDGER_RETIRED} and c.load == []


# ── overlay/profile 文件 IO ──────────────────────────────────────


def test_overlay_roundtrip(tmp_path):
    p = tmp_path / "openx.json"
    assert composition.load_overlay(p).is_empty()
    composition.enable_in_overlay(p, "auto-x")
    assert composition.load_overlay(p).enable == {"auto-x"}
    composition.disable_in_overlay(p, "auto-x")
    ov = composition.load_overlay(p)
    assert ov.disable == {"auto-x"} and ov.enable == set()


def test_overlay_preserves_other_keys(tmp_path):
    p = tmp_path / "openx.json"
    p.write_text(json.dumps({"other": 1, "plugins": {"disable": ["x"]}}))
    composition.enable_in_overlay(p, "y")
    data = json.loads(p.read_text())
    assert data["other"] == 1 and set(data["plugins"]["enable"]) == {"y"}


def test_overlay_bad_file_is_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    assert composition.load_overlay(p).is_empty()


# ── 内核接线 ─────────────────────────────────────────────────────


class Sink:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)

    def of(self, kind):
        return [e for e in self.events if e.type == kind]

    def types(self):
        return [e.type for e in self.events]


def test_overlay_disable_via_file(kernel_env):
    ws, _ = kernel_env
    write_plugin(ws, "hello", HELLO_SRC)
    (ws / ".openx" / "openx.json").write_text(
        json.dumps({"plugins": {"disable": ["hello"]}})
    )
    k = get_kernel()
    k.ensure_loaded(str(ws))
    info = next(i for i in k.inventory() if i.id == "hello")
    assert info.phase == "disabled"
    assert info.skip_reason == composition.REASON_PROJECT_DISABLE


def test_auto_plugin_not_boot_loaded(kernel_env):
    ws, _ = kernel_env
    write_plugin(ws, "auto-greet", HELLO_SRC)
    k = get_kernel()
    k.ensure_loaded(str(ws))
    info = next(i for i in k.inventory() if i.id == "auto-greet")
    assert info.phase == "disabled" and info.skip_reason == composition.REASON_AUTO


def test_user_overlay_enable_boot_loads_auto(kernel_env):
    ws, settings = kernel_env
    write_plugin(ws, "auto-greet", HELLO_SRC)
    (settings.parent / "openx.json").write_text(
        json.dumps({"plugins": {"enable": ["auto-greet"]}})
    )
    k = get_kernel()
    k.ensure_loaded(str(ws))
    info = next(i for i in k.inventory() if i.id == "auto-greet")
    assert info.phase == "active" and info.scope == "persistent"


def test_reload_on_overlay_change(kernel_env):
    ws, _ = kernel_env
    write_plugin(ws, "hello", HELLO_SRC)
    k = get_kernel()
    k.ensure_loaded(str(ws))
    assert next(i for i in k.inventory() if i.id == "hello").phase == "active"
    # 写项目 overlay disable → 键（含 overlay 签名）变 → 重组
    (ws / ".openx" / "openx.json").write_text(
        json.dumps({"plugins": {"disable": ["hello"]}})
    )
    k.ensure_loaded(str(ws))
    assert next(i for i in k.inventory() if i.id == "hello").phase == "disabled"


def test_profile_retire_skips_scaffold(kernel_env):
    ws, settings = kernel_env
    write_plugin(ws, "hello", HELLO_SRC)
    profiles = settings.parent / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    (profiles / "m1.json").write_text(json.dumps({"name": "m1", "retire": ["hello"]}))
    settings.write_text(json.dumps({"plugins": {"profile": "m1"}}))
    k = get_kernel()
    k.ensure_loaded(str(ws))
    info = next(i for i in k.inventory() if i.id == "hello")
    assert info.phase == "retired" and info.skip_reason == composition.REASON_PROFILE_RETIRED


def test_composition_resolved_payload(kernel_env):
    ws, _ = kernel_env
    write_plugin(ws, "hello", HELLO_SRC)
    write_plugin(ws, "auto-x", HELLO_SRC)
    k = get_kernel()
    sink = Sink()
    k.attach_ledger(sink, session="s1")
    k.ensure_loaded(str(ws))
    comp = sink.of("composition_resolved")
    assert len(comp) == 1
    payload = comp[0].payload
    assert "hello" in payload["plugins"]
    # auto-* 未晋升 → 不算进 disabled 键？它确实被跳过，故在 disabled 里
    assert "auto-x" in payload["disabled"]
    assert payload["skipped"]["auto-x"] == composition.REASON_AUTO
    assert payload["profile"] == "" and "user" in payload["overlay"]


def test_no_reload_when_key_unchanged(kernel_env):
    ws, _ = kernel_env
    write_plugin(ws, "hello", HELLO_SRC)
    k = get_kernel()
    sink = Sink()
    k.attach_ledger(sink, session="s1")
    k.ensure_loaded(str(ws))
    k.ensure_loaded(str(ws))  # 键未变：幂等跳过，不重记
    assert len(sink.of("composition_resolved")) == 1


# ── /composition 命令（只读面板）─────────────────────────────────


class _Cap:
    def __init__(self):
        self.raw_lines = []
        self.infos = []
        self.raw = self

    def print(self, *args, **kwargs):
        self.raw_lines.append(" ".join(str(a) for a in args))

    def print_info(self, message):
        self.infos.append(message)

    def print_success(self, message):
        pass


class _Agent:
    def __init__(self, ws):
        self.workspace = ws


async def test_composition_command(kernel_env):
    from openx.app.cli import commands

    ws, _ = kernel_env
    write_plugin(ws, "hello", HELLO_SRC)
    c = _Cap()
    assert await commands.handle_slash_command("composition", _Agent(ws), c, []) is True
    text = "\n".join(c.raw_lines)
    assert "hello" in text and "Composition" in text

    c2 = _Cap()
    await commands.handle_slash_command("composition", _Agent(ws), c2, ["json"])
    data = json.loads("\n".join(c2.raw_lines))
    assert data["loaded"] is True and "hello" in data["load"]
