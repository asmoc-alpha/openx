"""K6 晋升门 / 动态插入测试：PromotionGate 裁决 + 内核接线（§2.4）。

覆盖三层：

- **PromotionGate 本体**：只读放行 / 变更档 fail-closed / 审批批拒 / 通道
  故障 = 拒 / 形状不全 = 拒 / 每条裁决留痕；
- **内核接线**：``set_admitter`` · ``admit`` · ``insert_contribution``（热插
  档即准入面、幂等重插、provenance）· ``remove_contribution`` · 组合重组后
  重放 · ``unload_plugin`` 注销；
- **MCP pilot 的机制前提**：一 plugin_id 一条贡献（多工具共用一个工厂），
  多 server 共存。

运行：``python -m pytest tests/kernel/test_admission.py -q``
"""

from __future__ import annotations

from types import SimpleNamespace

from openx.kernel import get_kernel
from openx.kernel.audit.admission import Contribution, PromotionGate
from openx.kernel.audit.guard import Verdict


def _tool_factory(name: str = "x"):
    """一个产出单个假工具的最小工厂（``factory(host) -> list``）。"""
    tool = SimpleNamespace(name=name)
    return lambda host: [tool]


def _hooks(**kw):
    return SimpleNamespace(**kw)


# ── 1. PromotionGate 本体 ────────────────────────────────────────


class TestPromotionGate:
    """分级裁决 + 记账；emit 与 admitter 皆可注入（UI 不进内核）。"""

    async def test_observe_allowed_without_admitter(self):
        events: list[dict] = []
        gate = PromotionGate(emit=lambda t, p, **kw: events.append(p))
        v = await gate.admit(
            Contribution(kind="tools", name="mcp__f__e", plugin="mcp:f", observe=True)
        )
        assert v is Verdict.ALLOW
        assert events[-1]["verdict"] == "ALLOW"
        assert events[-1]["reason"] == "read-only contribution (pure addition)"
        assert not gate.has_admitter

    async def test_act_denied_without_admitter_fail_closed(self):
        gate = PromotionGate()
        v = await gate.admit(
            Contribution(kind="tools", name="mcp__f__e", plugin="mcp:f")
        )
        assert v is Verdict.DENY  # 默认从严：判不清/无人可问先拒

    async def test_admitter_approves_and_rejects(self):
        seen: list[Contribution] = []

        async def yes(c):
            seen.append(c)
            return True

        gate = PromotionGate(admitter=yes)
        assert gate.has_admitter
        assert await gate.admit(Contribution(kind="tools", name="t", plugin="p"))
        assert seen[0].plugin == "p"

        async def no(c):
            return False

        gate.set_admitter(no)
        assert await gate.admit(Contribution(kind="tools", name="t", plugin="p")) is Verdict.DENY

    async def test_admitter_exception_denies(self):
        """通道故障 = 拒绝（fail-closed），绝不外抛、绝不默认同意。"""
        async def boom(c):
            raise RuntimeError("console gone")

        events: list[dict] = []
        gate = PromotionGate(emit=lambda t, p, **kw: events.append(p), admitter=boom)
        v = await gate.admit(Contribution(kind="tools", name="t", plugin="p"))
        assert v is Verdict.DENY
        assert "approver failed" in events[-1]["reason"]

    async def test_malformed_denied(self):
        gate = PromotionGate()
        assert await gate.admit(Contribution(kind="", name="t", plugin="p")) is Verdict.DENY
        assert await gate.admit(Contribution(kind="tools", name="", plugin="p")) is Verdict.DENY
        assert await gate.admit(Contribution(kind="tools", name="t", plugin="")) is Verdict.DENY

    async def test_every_decision_recorded(self):
        events: list[dict] = []
        gate = PromotionGate(emit=lambda t, p, **kw: events.append(p))
        for _ in range(3):
            await gate.admit(Contribution(kind="tools", name="t", plugin="p", observe=True))
        assert len(events) == 3
        assert all(e["type"] == "admission_decision" for e in events)
        assert all(e["verdict"] == "ALLOW" for e in events)


# ── 2. 内核接线：admit 门外即注册面 ─────────────────────────────


class TestKernelAdmission:
    async def test_set_admitter_and_has_admitter(self, kernel_env):
        kernel = get_kernel()
        assert not kernel.has_admitter
        kernel.set_admitter(lambda c: True)
        assert kernel.has_admitter
        kernel.set_admitter(None)
        assert not kernel.has_admitter

    async def test_admit_records_event_on_ledger(self, kernel_env):
        kernel = get_kernel()
        seen: list = []
        kernel.attach_ledger(lambda ev: seen.append(ev))
        verdict = await kernel.admit(
            Contribution(kind="tools", name="p", plugin="p", observe=True)
        )
        assert verdict is Verdict.ALLOW
        event = next(e for e in seen if e.type == "admission_decision")
        assert event.origin == "kernel"
        assert event.payload["name"] == "p" and event.payload["verdict"] == "ALLOW"

    async def test_insert_registers_tool_factory(self, kernel_env):
        kernel = get_kernel()
        problems = kernel.insert_contribution(
            plugin_id="mcp:fake", registry_kind="tools", name="mcp:fake",
            value=_tool_factory(), source="mcp", scope="session",
            summary="fake server",
        )
        assert problems == []
        entry = kernel.registry("tools").get("mcp:fake")
        assert entry is not None and entry.plugin == "mcp:fake"  # provenance
        assert "mcp:fake" in kernel.session_inserts()
        info = next(p for p in kernel.inventory() if p.id == "mcp:fake")
        assert info.scope == "session" and info.summary == "fake server"
        assert not info.builtin

    async def test_insert_supports_other_hotplug_kinds(self, kernel_env):
        """目录加一行不改内核：命令类注册项同样经 insert_contribution 热插。"""
        kernel = get_kernel()

        async def _handler(**kw):  # pragma: no cover - 形状校验用
            return ""

        contrib = SimpleNamespace(handler=_handler, aliases=[])
        problems = kernel.insert_contribution(
            plugin_id="cmd-pack", registry_kind="commands", name="hello",
            value=contrib, source="test",
        )
        assert problems == []
        assert kernel.registry("commands").get("hello").plugin == "cmd-pack"

    async def test_insert_refuses_non_session_hotplug_kind(self, kernel_env):
        """providers 是 boundary 档（仅 boot）——会话内插入被拒（标准六）。"""
        kernel = get_kernel()
        problems = kernel.insert_contribution(
            plugin_id="prov-pack", registry_kind="providers", name="vend",
            value=lambda settings: None,
        )
        assert problems and "hotplug=boundary" in problems[0]
        assert kernel.registry("providers").get("vend") is None
        assert "prov-pack" not in kernel.session_inserts()

    async def test_insert_refuses_unknown_kind(self, kernel_env):
        kernel = get_kernel()
        problems = kernel.insert_contribution(
            plugin_id="x", registry_kind="nope", name="x", value=lambda host: [],
        )
        assert problems == ["nope: unknown registration kind"]

    async def test_insert_rejects_bad_value(self, kernel_env):
        """校验不过 → 不落注册表，记 rejected 事件（同 boot 校验面）。"""
        kernel = get_kernel()
        seen: list = []
        kernel.attach_ledger(lambda ev: seen.append(ev))
        problems = kernel.insert_contribution(
            plugin_id="bad", registry_kind="tools", name="bad", value="not-callable",
        )
        assert problems and "not callable" in problems[0]
        assert kernel.registry("tools").get("bad") is None
        assert "bad" not in kernel.session_inserts()
        assert any(e.type == "rejected" for e in seen)

    async def test_reinsert_is_idempotent(self, kernel_env):
        """同 id 重插 = 净替换：注册表该名只有一条（重连撞"先见者赢"才是 bug）。"""
        kernel = get_kernel()
        for _ in range(3):
            kernel.insert_contribution(
                plugin_id="mcp:fake", registry_kind="tools", name="mcp:fake",
                value=_tool_factory(), source="mcp",
            )
        entries = [e for e in kernel.registry("tools").entries() if e.name == "mcp:fake"]
        assert len(entries) == 1
        assert kernel.session_inserts() == ("mcp:fake",)

    async def test_multiple_servers_coexist(self, kernel_env):
        """一个 server 一条贡献 → 多 server 各占一个 id，互不清。"""
        kernel = get_kernel()
        for srv in ("alpha", "beta"):
            kernel.insert_contribution(
                plugin_id=f"mcp:{srv}", registry_kind="tools", name=f"mcp:{srv}",
                value=_tool_factory(), source="mcp",
            )
        assert set(kernel.session_inserts()) == {"mcp:alpha", "mcp:beta"}
        assert kernel.registry("tools").get("mcp:alpha") is not None
        assert kernel.registry("tools").get("mcp:beta") is not None

    async def test_remove_contribution_triggers_unload_and_purges(self, kernel_env):
        kernel = get_kernel()
        seen_events: list = []
        kernel.attach_ledger(lambda ev: seen_events.append(ev))
        calls: list[str] = []
        kernel.insert_contribution(
            plugin_id="p1", registry_kind="lifecycle", name="p1",
            value=_hooks(on_unload=lambda: calls.append("unload")), source="test",
        )
        assert kernel.registry("lifecycle").get("p1") is not None

        ok, msg = kernel.remove_contribution("p1")
        assert ok and "removed" in msg
        assert calls == ["unload"]                      # 状态落盘契约先触发
        assert kernel.registry("lifecycle").get("p1") is None
        assert "p1" not in kernel.session_inserts()
        assert any(e.type == "plugin_unloaded" for e in seen_events)

    async def test_reinsert_purges_prior_kind(self, kernel_env):
        """一个 plugin_id 一条贡献：后插按 provenance 清前插（故 MCP 一 server
        一条贡献、一个工厂产出其全部工具——同 id 插两条会互相清）。"""
        kernel = get_kernel()
        kernel.insert_contribution(
            plugin_id="p1", registry_kind="lifecycle", name="p1",
            value=_hooks(on_unload=lambda: None), source="test",
        )
        kernel.insert_contribution(
            plugin_id="p1", registry_kind="tools", name="p1",
            value=_tool_factory(), source="test",
        )
        assert kernel.registry("lifecycle").get("p1") is None   # 前插被清
        assert kernel.registry("tools").get("p1") is not None

    async def test_remove_unknown_is_noop(self, kernel_env):
        kernel = get_kernel()
        ok, msg = kernel.remove_contribution("nope")
        assert ok is False and "not a session contribution" in msg

    async def test_replay_after_composition_reload(self, kernel_env):
        """组合重组重建注册表后，会话内插入被重放——session ≠ 中途消失。"""
        ws, _settings = kernel_env
        kernel = get_kernel()
        kernel.ensure_loaded(str(ws))
        kernel.insert_contribution(
            plugin_id="mcp:fake", registry_kind="tools", name="mcp:fake",
            value=_tool_factory(), source="mcp", scope="session",
        )
        assert kernel.registry("tools").get("mcp:fake") is not None

        # 改项目 overlay → 组合输入签名变化 → ensure_loaded 触发 _reload（清表 + 重放）
        (ws / ".openx" / "openx.json").write_text('{"enable": []}', encoding="utf-8")
        kernel.ensure_loaded(str(ws))
        assert kernel.registry("tools").get("mcp:fake") is not None  # 重放回来了
        assert "mcp:fake" in kernel.session_inserts()

    async def test_unload_plugin_drops_session_insert(self, kernel_env):
        """卸载 = 回滚必须彻底：会话插入的贡献被卸载后从重放表注销。"""
        kernel = get_kernel()
        kernel.insert_contribution(
            plugin_id="mcp:fake", registry_kind="tools", name="mcp:fake",
            value=_tool_factory(), source="mcp",
        )
        assert "mcp:fake" in kernel.session_inserts()
        ok, _ = kernel.unload_plugin("mcp:fake")
        assert ok
        assert "mcp:fake" not in kernel.session_inserts()
