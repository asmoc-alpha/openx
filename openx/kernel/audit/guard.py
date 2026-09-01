"""执行闸 Guard -- 内核详设 v2.1 §2：裁决管线 + 半格折叠 + 决策记账。

七站固定序（序即不变量）：

    ① 硬拒绝      plan 模式写操作 / choose_mode 防线 / 存储 deny 规则
    ② 自声明      permission level -> 默认 Verdict（auto_allowed 白名单
                  折入本站的 per-call 默认值）
    ③ 高危强制    is_high_risk -> 抬到至少 ASK（后续各站不得降宽）
    ④ 策略贡献    PreToolUse hooks，按 §2.2 映射表折叠：
                  exit 2 / decision:block -> DENY；exit 0 -> 无意见；
                  故障非零/超时 -> 无意见 + warning 入决策 payload
    ⑤ force_prompt  manual 模式 ASK 级 -> 抬到 ASK
    ⑥ 存储裁决    已存 allow -> 可放宽至 ALLOW_SESSION，**不得越过 ③⑤
                  的抬严**（已存规则永不放宽危险，序保证）
    ⑦ 用户裁决    弹窗/远程批准（prompter 注入，UI 不进内核）；fail-
                  closed：prompter 异常向外传播，调用方落成拒绝

半格折叠：除⑥外各站只紧不松（``min()`` 抬严）；⑥是唯一可放宽站。
每次裁决（含放行）产一条 ``permission_decision`` 事件经注入的内核
emit 上账本--记账先于动作：gate() 先于工具执行返回裁决，事件先落账。

依赖注入纪律：Guard 不 import ui/console、不读 settings；每会话状态
（mode/auto_approve/rules）以 callable 晚绑定注入（executor 的这些
字段运行期可变，闭包保证读当下值）。K3 落地形态：Guard 由 executor
持有；kernel.gate() 单例形态待会话上下文对象化（K1c ExecutionScope）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Awaitable, Callable, Optional

from ...core.hooks import build_pretooluse_payload
from ...permissions import PermissionLevel


class Verdict(IntEnum):
    """裁决半格：值越小越严，``min()`` = 抬严折叠（只紧不松）。"""

    DENY = 0
    ASK = 1
    ALLOW_ONCE = 2
    ALLOW_SESSION = 3
    ALLOW = 4


@dataclass
class GateCall:
    """一次待裁决的工具调用（参数已解析、形状已校验）。"""

    tool_name: str
    tool: Any
    args: dict
    args_summary: str = ""


@dataclass
class Adjudication:
    """裁决结果：approved=False 时 ``error`` 是落成工具结果的原文。"""

    approved: bool
    verdict: Verdict
    reason: str = ""
    hook_warnings: list = field(default_factory=list)


# prompter 签名：async (call, permission) -> (approved, remember)
Prompter = Callable[[GateCall, Any], Awaitable[tuple]]


class Guard:
    """裁决管线：每一工具调用必经（执行闸的 K3 形态）。"""

    def __init__(
        self,
        *,
        emit: Callable[..., Any],
        rules: Callable[[], Any],
        hooks: Optional[Any] = None,
        prompter: Optional[Prompter] = None,
        mode: Callable[[], str] = lambda: "auto",
        auto_approve: Callable[[], bool] = lambda: False,
    ) -> None:
        self._emit = emit
        self._rules = rules
        self._hooks = hooks
        self._prompter = prompter
        self._mode = mode
        self._auto_approve = auto_approve

    async def gate(self, call: GateCall) -> Adjudication:
        """七站裁决；任何路径只返回一次，且每次裁决都记账。"""
        tool = call.tool
        name = call.tool_name
        mode = self._mode()
        trace: list[dict] = []

        # ① 硬拒绝：plan 模式写操作 / choose_mode 防线 / 存储 deny 规则。
        # exit_plan_mode 是审批出口本身，按名字豁免（同现状）。
        perm = tool.permission
        if (
            mode == "plan"
            and perm.level in (PermissionLevel.ASK, PermissionLevel.DENY)
            and name != "exit_plan_mode"
        ):
            return self._final(
                call, Verdict.DENY, trace, mode, station="hard_deny",
                reason="Plan mode is active — write tools are disabled. "
                       "Present your plan and call exit_plan_mode.",
            )
        if name == "choose_mode" and mode != "manual":
            return self._final(
                call, Verdict.DENY, trace, mode, station="hard_deny",
                reason=f"choose_mode is only available in manual mode "
                       f"(current mode: {mode}). Proceed in the current mode.",
            )
        stored = self._rules().check(name, call.args_summary)
        if stored == PermissionLevel.DENY:
            return self._final(
                call, Verdict.DENY, trace, mode, station="hard_deny",
                reason=f"Tool '{name}' is blocked by a stored deny rule",
            )
        trace.append({"station": "hard_deny", "verdict": None, "detail": "pass"})

        # ② 自声明：permission level -> 默认 Verdict；auto_allowed 白名单
        # 是 per-call 的自声明修正（ASK -> ALLOW），折入本站。
        # 工具级 DENY 在本站**挂起**而非终局：现状语义里用户显式落盘的
        # 存储 allow 规则（⑥）可越过它（现行 executor 的顺序如此；
        # 行为≡现状，半格纯化留给评审决断）。
        if perm.level == PermissionLevel.DENY:
            v = Verdict.DENY
        elif perm.level == PermissionLevel.ASK:
            v = Verdict.ASK
            if tool.auto_allowed(call.args):
                v = Verdict.ALLOW
        else:
            v = Verdict.ALLOW
        trace.append({"station": "self_declared", "verdict": v.name, "detail": ""})

        # ③ 高危强制：is_high_risk -> 至少 ASK，不可被后续任何站降宽。
        high_risk = tool.is_high_risk(call.args)
        if high_risk:
            v = min(v, Verdict.ASK)
        trace.append({
            "station": "high_risk",
            "verdict": v.name if high_risk else None,
            "detail": "is_high_risk" if high_risk else "",
        })

        # ④ 策略贡献：PreToolUse hooks 只紧不松（§2.2 映射表：阻断 ->
        # DENY；故障 -> 无意见 + warning）。排在存储 allow 之前--策略可以
        # 驳回已被缓存批准的调用。
        hook_warnings: list = []
        if self._hooks is not None and self._hooks.has_hooks("PreToolUse", name):
            outcome = await self._hooks.run(
                "PreToolUse",
                build_pretooluse_payload(
                    name,
                    call.args,
                    workspace=self._hooks.workspace,
                    session_id=self._hooks.session_id,
                ),
            )
            hook_warnings = list(outcome.warnings)
            if outcome.blocked:
                return self._final(
                    call, Verdict.DENY, trace, mode, station="policy_hooks",
                    reason=f"Blocked by PreToolUse hook: {outcome.reason}",
                    hook_warnings=hook_warnings,
                )
        trace.append({"station": "policy_hooks", "verdict": None, "detail": "pass"})

        # ⑤ force_prompt：manual 模式下 ASK 级 -> 抬到 ASK（绕过白名单/
        # auto_approve，逐项授权）。判定基于工具自声明级别（非 auto_allowed
        # 修正后的值）--与现状 force_prompt 计算逐字对齐。
        forced = high_risk or (mode == "manual" and perm.level == PermissionLevel.ASK)
        if mode == "manual" and perm.level == PermissionLevel.ASK:
            v = min(v, Verdict.ASK)
        trace.append({
            "station": "force_prompt",
            "verdict": v.name if forced else None,
            "detail": "forced" if forced else "",
        })

        # ⑥ 存储裁决：已存 allow 可放宽至 ALLOW_SESSION--唯一可放宽站；
        # 不得越过 ③⑤ 的抬严（forced 时跳过）。可越过②挂起的工具级
        # DENY（现状语义，见②注释）。
        if stored == PermissionLevel.ALLOW and not forced:
            v = Verdict.ALLOW_SESSION
        trace.append({
            "station": "stored",
            "verdict": v.name if stored == PermissionLevel.ALLOW else None,
            "detail": "stored allow" if stored == PermissionLevel.ALLOW else "",
        })
        if v == Verdict.DENY:
            return self._final(
                call, Verdict.DENY, trace, mode, station="self_declared",
                reason=f"Tool '{name}' is blocked: {perm.reason}",
                hook_warnings=hook_warnings,
            )

        # ⑦ 用户裁决：ALLOW/ALLOW_SESSION 直放；auto_approve 是自动应答器
        # （forced 时失效）；否则 prompter 弹窗。fail-closed：prompter 异常
        # 向外传播，由调用方落成错误结果（绝不执行）。
        if v >= Verdict.ALLOW_ONCE:
            return self._final(call, v, trace, mode, station="user",
                               approved=True, hook_warnings=hook_warnings)
        if self._auto_approve() and not forced:
            return self._final(call, Verdict.ALLOW, trace, mode, station="user",
                               approved=True, hook_warnings=hook_warnings)
        assert self._prompter is not None, "ASK 裁决缺 prompter"
        approved, remember = await self._prompter(call, perm)
        if not approved:
            return self._final(
                call, Verdict.DENY, trace, mode, station="user",
                reason="Permission denied by user", hook_warnings=hook_warnings,
            )
        # manual 模式绝不落盘规则（双保险；prompter 侧 can_remember 已挡）
        if remember and call.args_summary and mode != "manual":
            self._rules().add_allow(f"{name}({call.args_summary})")
            v = Verdict.ALLOW_SESSION
        else:
            v = Verdict.ALLOW_ONCE
        return self._final(call, v, trace, mode, station="user", approved=True,
                           hook_warnings=hook_warnings)

    # ── internals ───────────────────────────────────────────────

    def _final(
        self,
        call: GateCall,
        verdict: Verdict,
        trace: list,
        mode: str,
        *,
        station: str,
        approved: bool = False,
        reason: str = "",
        hook_warnings: Optional[list] = None,
    ) -> Adjudication:
        """收口：补最后一站 trace，记账，返回裁决。"""
        trace.append({
            "station": station,
            "verdict": verdict.name,
            "detail": reason,
        })
        # 记账先于动作：裁决先落账，工具后执行（宁可记了没执行）。
        self._emit(
            "permission_decision",
            {
                "type": "permission_decision",
                "tool": call.tool_name,
                "args_summary": call.args_summary,
                "verdict": verdict.name,
                "approved": approved,
                "reason": reason,
                "mode": mode,
                "stations": trace,
                "hook_warnings": hook_warnings or [],
            },
            origin="kernel",
        )
        return Adjudication(
            approved=approved,
            verdict=verdict,
            reason=reason,
            hook_warnings=hook_warnings or [],
        )
