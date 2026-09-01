"""执行闸 Guard：半格折叠、七站固定序、hooks 映射、决策记账。

运行：``python -m pytest tests/kernel/test_guard.py -q``
"""

from __future__ import annotations

import pytest

from openx.core.hooks import HookRunner
from openx.kernel.audit.guard import GateCall, Guard, Verdict
from openx.permissions import Permission, PermissionLevel, PermissionRules


class FakeTool:
    """形状最小集：Guard 只碰 permission / auto_allowed / is_high_risk。"""

    def __init__(self, level=PermissionLevel.ALLOW, reason="", auto=False, risk=False):
        self._perm = Permission(level=level, reason=reason)
        self._auto = auto
        self._risk = risk

    @property
    def permission(self):
        return self._perm

    def auto_allowed(self, args):
        return self._auto

    def is_high_risk(self, args):
        return self._risk


def make_guard(
    *,
    rules=None,
    mode="auto",
    auto_approve=False,
    hooks=None,
    prompt_answer=(True, False),
):
    """Guard + 事件收集器 + 弹窗记录器。prompter 缺省直接批准。"""
    events: list[dict] = []
    prompts: list[dict] = []
    rules_obj = rules if rules is not None else PermissionRules()

    async def prompter(call, perm):
        prompts.append({"tool": call.tool_name, "summary": call.args_summary})
        return prompt_answer

    guard = Guard(
        emit=lambda type_, payload, **kw: events.append(payload),
        rules=lambda: rules_obj,
        hooks=hooks,
        prompter=prompter,
        mode=lambda: mode,
        auto_approve=lambda: auto_approve,
    )
    return guard, events, prompts, rules_obj


def call(tool, name="fake", args=None, summary=""):
    return GateCall(name, tool, args or {}, summary)


# ── 半格 ────────────────────────────────────────────────────────


class TestLattice:
    def test_ordering(self):
        assert Verdict.DENY < Verdict.ASK < Verdict.ALLOW_ONCE
        assert Verdict.ALLOW_ONCE < Verdict.ALLOW_SESSION < Verdict.ALLOW

    def test_min_is_tighten(self):
        assert min(Verdict.ALLOW, Verdict.ASK) is Verdict.ASK
        assert min(Verdict.DENY, Verdict.ASK) is Verdict.DENY


# ── ① 硬拒绝 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestHardDeny:
    async def test_plan_mode_blocks_ask_tool(self):
        g, events, _, _ = make_guard(mode="plan")
        adj = await g.gate(call(FakeTool(PermissionLevel.ASK)))
        assert not adj.approved and adj.verdict is Verdict.DENY
        assert "exit_plan_mode" in adj.reason

    async def test_plan_mode_exempts_exit_plan_mode(self):
        g, _, _, _ = make_guard(mode="plan")
        adj = await g.gate(call(FakeTool(PermissionLevel.ALLOW), name="exit_plan_mode"))
        assert adj.approved

    async def test_choose_mode_only_in_manual(self):
        g, _, _, _ = make_guard(mode="auto")
        adj = await g.gate(call(FakeTool(), name="choose_mode"))
        assert not adj.approved and "manual mode" in adj.reason
        g2, _, _, _ = make_guard(mode="manual")
        adj2 = await g2.gate(call(FakeTool(), name="choose_mode"))
        assert adj2.approved

    async def test_stored_deny_blocks(self):
        rules = PermissionRules(deny=["fake(*)"])
        g, _, _, _ = make_guard(rules=rules)
        adj = await g.gate(call(FakeTool(), args={"file_path": "x.py"}, summary="x.py"))
        assert not adj.approved and "stored deny rule" in adj.reason


# ── ②③⑤⑥ 折叠序 ────────────────────────────────────────────────


@pytest.mark.asyncio
class TestFolding:
    async def test_allow_tool_passes_silently(self):
        g, _, prompts, _ = make_guard()
        adj = await g.gate(call(FakeTool()))
        assert adj.approved and adj.verdict is Verdict.ALLOW and not prompts

    async def test_auto_allowed_whitelist_skips_prompt(self):
        g, _, prompts, _ = make_guard()
        tool = FakeTool(PermissionLevel.ASK, auto=True)
        adj = await g.gate(call(tool))
        assert adj.approved and adj.verdict is Verdict.ALLOW and not prompts

    async def test_stored_allow_widens_to_session(self):
        rules = PermissionRules(allow=["fake(x.py)"])
        g, _, prompts, _ = make_guard(rules=rules)
        tool = FakeTool(PermissionLevel.ASK)
        adj = await g.gate(call(tool, args={"file_path": "x.py"}, summary="x.py"))
        assert adj.approved and adj.verdict is Verdict.ALLOW_SESSION and not prompts

    async def test_high_risk_not_skippable_by_stored_allow(self):
        """③抬严后⑥不得放宽：已存规则永不跳过危险弹窗。"""
        rules = PermissionRules(allow=["fake(x.py)"])
        g, _, prompts, _ = make_guard(rules=rules, auto_approve=True)
        tool = FakeTool(PermissionLevel.ASK, risk=True)
        adj = await g.gate(call(tool, args={"file_path": "x.py"}, summary="x.py"))
        assert adj.approved and len(prompts) == 1  # 弹了

    async def test_manual_forces_prompt_despite_rules_and_auto_approve(self):
        """⑤抬严：manual 下 ASK 工具逐项弹窗，存储规则/auto_approve 不跳过。"""
        rules = PermissionRules(allow=["fake(x.py)"])
        g, _, prompts, _ = make_guard(rules=rules, mode="manual", auto_approve=True)
        tool = FakeTool(PermissionLevel.ASK)
        adj = await g.gate(call(tool, args={"file_path": "x.py"}, summary="x.py"))
        assert adj.approved and len(prompts) == 1

    async def test_tool_deny_overridable_by_stored_allow(self):
        """现状语义：用户显式落盘的 allow 规则可越过工具级 DENY。"""
        rules = PermissionRules(allow=["fake"])
        g, _, _, _ = make_guard(rules=rules)
        tool = FakeTool(PermissionLevel.DENY, reason="danger")
        adj = await g.gate(call(tool))
        assert adj.approved and adj.verdict is Verdict.ALLOW_SESSION
        # 无规则时工具级 DENY 终局
        g2, _, _, _ = make_guard()
        adj2 = await g2.gate(call(tool))
        assert not adj2.approved and "danger" in adj2.reason


# ── ⑦ 用户裁决 ───────────────────────────────────────────────────


@pytest.mark.asyncio
class TestUserVerdict:
    async def test_auto_approve_answers_allow(self):
        g, _, prompts, _ = make_guard(auto_approve=True)
        adj = await g.gate(call(FakeTool(PermissionLevel.ASK)))
        assert adj.approved and not prompts

    async def test_user_reject_is_deny(self):
        g, _, _, _ = make_guard(prompt_answer=(False, False))
        adj = await g.gate(call(FakeTool(PermissionLevel.ASK)))
        assert not adj.approved and adj.reason == "Permission denied by user"

    async def test_remember_persists_rule(self, tmp_path, monkeypatch):
        import openx.permissions as perm_mod

        settings = tmp_path / "settings.json"
        rules = PermissionRules()
        monkeypatch.setattr(rules, "save", lambda path=None: None)  # 不落盘
        g, _, _, _ = make_guard(rules=rules, prompt_answer=(True, True))
        adj = await g.gate(
            call(FakeTool(PermissionLevel.ASK), args={"file_path": "x.py"}, summary="x.py")
        )
        assert adj.approved and adj.verdict is Verdict.ALLOW_SESSION
        assert rules.check("fake", "x.py") is PermissionLevel.ALLOW

    async def test_manual_never_persists_rule(self):
        rules = PermissionRules()
        g, _, _, _ = make_guard(rules=rules, mode="manual", prompt_answer=(True, True))
        adj = await g.gate(
            call(FakeTool(PermissionLevel.ASK), args={"file_path": "x.py"}, summary="x.py")
        )
        assert adj.approved and adj.verdict is Verdict.ALLOW_ONCE
        assert rules.check("fake", "x.py") is None


# ── ④ hooks 映射（§2.2 映射表）────────────────────────────────────


@pytest.mark.asyncio
class TestHookMapping:
    def _runner(self, command):
        return HookRunner({"PreToolUse": [
            {"hooks": [{"type": "command", "command": command}]},
        ]})

    async def test_exit2_is_deny(self):
        g, _, _, _ = make_guard(hooks=self._runner("echo veto >&2; exit 2"))
        adj = await g.gate(call(FakeTool()))
        assert not adj.approved and "veto" in adj.reason

    async def test_stdout_block_json_is_deny(self):
        cmd = "cat > /dev/null; echo '{\"decision\": \"block\", \"reason\": \"policy\"}'"
        g, _, _, _ = make_guard(hooks=self._runner(cmd))
        adj = await g.gate(call(FakeTool()))
        assert not adj.approved and "policy" in adj.reason

    async def test_exit0_abstains(self):
        g, _, _, _ = make_guard(hooks=self._runner("exit 0"))
        adj = await g.gate(call(FakeTool()))
        assert adj.approved

    async def test_failure_abstains_with_warning(self):
        """故障非零 -> 无意见 + warning（行为≡现状：不阻断）。"""
        g, events, _, _ = make_guard(hooks=self._runner("exit 3"))
        adj = await g.gate(call(FakeTool()))
        assert adj.approved and adj.hook_warnings
        # warning 进决策 payload
        assert events[-1]["hook_warnings"] == adj.hook_warnings

    async def test_hook_can_veto_stored_allow(self):
        """④排在⑥前：策略驳回已被缓存批准的调用。"""
        rules = PermissionRules(allow=["fake"])
        g, _, _, _ = make_guard(rules=rules, hooks=self._runner("exit 2"))
        adj = await g.gate(call(FakeTool(PermissionLevel.ASK)))
        assert not adj.approved


# ── 决策记账 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestDecisionLedger:
    async def test_every_gate_emits_decision_with_trace(self):
        g, events, _, _ = make_guard()
        await g.gate(call(FakeTool(), args={"a": 1}, summary="s"))
        assert len(events) == 1
        ev = events[0]
        assert ev["type"] == "permission_decision"
        assert ev["tool"] == "fake" and ev["args_summary"] == "s"
        assert ev["approved"] is True and ev["verdict"] == "ALLOW"
        stations = [s["station"] for s in ev["stations"]]
        assert stations == [
            "hard_deny", "self_declared", "high_risk",
            "policy_hooks", "force_prompt", "stored", "user",
        ]

    async def test_deny_also_emits(self):
        """记账先于动作：拒绝同样留痕（宁可记了没执行）。"""
        g, events, _, _ = make_guard(mode="plan")
        await g.gate(call(FakeTool(PermissionLevel.ASK)))
        assert events[-1]["approved"] is False
        assert events[-1]["verdict"] == "DENY"
