"""Tool execution orchestrator.

Extracted from the agent's inline tool-execution path: handles the
parse-args → validate → check-permission → call → return-result pipeline.

Permission rules are checked against stored allow/deny lists before
falling back to interactive prompts.

Two-phase API（串行准备 / 并行执行）
====================================
- :meth:`prepare` 把旧 ``execute()`` 里**运行工具之前**的全部关卡（JSON 解析、
  dict 检查、``validate_args``、未知工具、存储 deny/allow 规则、``auto_allowed``、
  ASK 权限询问）做完，产出一个 :class:`PreparedCall`——任何失败都落成
  ``pre_result``，**绝不抛异常**；
- :meth:`execute_prepared` 只负责真正跑 ``tool.execute()``。

agent 循环串行 prepare（权限弹窗走 raw-mode stdin，绝不能互相重叠），再用
``asyncio.gather`` 并行 execute_prepared——gather 保持参数顺序，结果按原
tool_call 顺序回喂，OpenAI 消息序列依然合法。
"""

from __future__ import annotations

# ── 独立调试支持：允许直接运行本文件（python openx/.../xxx.py）──────
if __name__ == "__main__" and not __package__:
    import sys as _sys
    from pathlib import Path as _Path
    _file = _Path(__file__).resolve()
    _root = _file.parent
    while _root != _root.parent and not (_root / "pyproject.toml").exists():
        _root = _root.parent
    _sys.path.insert(0, str(_root))
    __package__ = ".".join(_file.relative_to(_root).parts[:-1])

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..kernel.audit.hooks import (
    HookRunner,
    build_posttooluse_payload,
)
from ..kernel.audit.guard import Adjudication, GateCall, Guard
from ..permissions import PermissionRules
from ..tools.base import Tool, ToolResult
from ..ui.console import Console


def _kernel_emit(*args, **kwargs):
    """晚绑定内核单例 emit：测试 reset_kernel 后仍指向当下内核。"""
    from ..kernel import get_kernel

    return get_kernel().emit(*args, **kwargs)


@dataclass
class PreparedCall:
    """一次工具调用的"准备完成"快照。

    ``prepare()`` 串行跑完全部关卡后产出；``execute_prepared()`` 凭它执行。

    - ``pre_result is not None`` → 准备阶段已被拦截（参数非法 / 未知工具 /
      deny 规则 / 用户拒绝），跳过执行、直接把它当结果；
    - ``approved`` 记录本次调用是否被放行（供旧版 ``execute()`` 兼容返回值），
      运行时工具异常仍算已放行（与旧语义一致）。
    """

    tc_id: str
    tool_name: str
    tool: Optional[Tool]
    args: Optional[dict] = None
    pre_result: Optional[ToolResult] = None   # set → skip execute, use as result
    approved: bool = True


class ToolExecutor:
    """Orchestrate a single tool invocation with permission checking."""

    def __init__(
        self,
        console: Console,
        auto_approve: bool = False,
        plan_mode: bool = False,
        hook_runner: Optional[HookRunner] = None,
        rules: Optional[PermissionRules] = None,
        prompt_lock: Optional[asyncio.Lock] = None,
        mode: Optional[str] = None,
    ) -> None:
        self.console = console
        self.auto_approve = auto_approve
        # Phase 5 hooks：PreToolUse 可阻断工具调用（在 prepare 里），
        # PostToolUse 仅通知（在 execute_prepared 里）。默认 None → 零行为变化。
        self.hooks = hook_runner
        # 权限模式（manual/auto/plan）——与 agent.mode 镜像，prepare 闸门据此
        # 分流：plan 硬拦截写入类；manual 对写入类强制逐项弹窗（绕过规则/
        # 白名单/auto_approve）；危险命令（is_high_risk）任何模式永远弹窗。
        # plan_mode 构造参数保留兼容：True 等价 mode="plan"。
        self.mode: str = mode or ("plan" if plan_mode else "auto")
        # Phase 8：rules 缺省 → 从 settings.json 全新加载（顶层 agent 语义）；
        # 传入对象 → 直接共享（子 agent 复用父 executor 的同一 PermissionRules，
        # "don't ask again" 的决定在父子之间双向传播）。
        self._rules = rules if rules is not None else PermissionRules.load()
        # 防御性串行锁：权限弹窗走 raw-mode stdin（dialogs._raw_select），
        # 绝不能有两个弹窗并发。prepare 当前由 agent 循环串行调用，此锁是
        # 双保险。Python 3.10+ 的 asyncio.Lock 不在构造时绑定事件循环，
        # 在无 running loop 时创建（如 __init__ / 自检）是安全的。
        # prompt_lock 可外部注入（Phase 10）：工作流引擎让一次运行内的
        # **所有**并发子代理共享同一把锁——它们各自的 agent 循环虽并行，
        # 权限弹窗仍全局串行。None → 自建（既有行为不变）。
        self._prompt_lock = prompt_lock or asyncio.Lock()
        # Bug 10 钩子：交互式弹窗前后触发，供流式显示暂停/恢复 InputCapture，
        # 避免弹窗的 raw 模式与捕获线程的 cbreak 争抢 termios。
        # 可为 None；回调异常会被吞掉，绝不影响主流程。
        self.on_prompt_start: Optional[Callable[[], None]] = None
        self.on_prompt_end: Optional[Callable[[], None]] = None
        # K3 执行闸：裁决管线析出入 kernel/guard.py。mode/auto_approve/
        # rules 以闭包晚绑定（运行期可变：set_mode、plan 批准开
        # auto_approve、测试重挂 _rules）；prompter 留在本层——UI 不进内核。
        self._guard = Guard(
            emit=_kernel_emit,
            rules=lambda: self._rules,
            hooks=hook_runner,
            prompter=self._prompt_user,
            mode=lambda: self.mode,
            auto_approve=lambda: self.auto_approve,
        )

    # ── public API ──────────────────────────────────────────────

    @property
    def rules(self) -> PermissionRules:
        """The current stored permission rules."""
        return self._rules

    @property
    def plan_mode(self) -> bool:
        """兼容属性：True 当且仅当 mode == "plan"（旧 API 读写桥接）。"""
        return self.mode == "plan"

    @plan_mode.setter
    def plan_mode(self, on: bool) -> None:
        self.mode = "plan" if on else "auto"

    async def prepare(
        self,
        tool_name: str,
        tool: Tool | None,
        raw_args: str,
        tc_id: str = "",
    ) -> PreparedCall:
        """Serial gating phase: everything the old ``execute()`` did BEFORE
        running the tool.

        关卡顺序：未知工具 → JSON 解析 → dict 检查 → ``validate_args`` →
        **Guard 七站裁决**（kernel/guard.py：plan/choose_mode 硬拒绝 →
        自声明 → 高危强制 → PreToolUse hooks 只紧不松 → force_prompt →
        存储裁决 → 用户裁决，弹窗在 ``_prompt_lock`` 下串行、前后触发
        ``on_prompt_start``/``on_prompt_end``；每次裁决记
        ``permission_decision`` 事件）。

        任何失败都落成 ``pc.pre_result``（``approved=False``）——**绝不抛
        异常**，因此可以安全地对一批调用依次 prepare 再并行执行。
        """
        pc = PreparedCall(tc_id=tc_id, tool_name=tool_name, tool=tool)
        try:
            # Unknown tool: handled before arg parsing (same priority as the
            # old agent loop, keeps the exact "Unknown tool:" message).
            if tool is None:
                pc.pre_result = ToolResult(error=f"Unknown tool: {tool_name}")
                pc.approved = False
                return pc

            # Parse arguments
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError as e:
                pc.pre_result = ToolResult(error=f"Invalid arguments: {e}")
                pc.approved = False
                return pc

            # JSON scalars/arrays (e.g. "5" → int) are not valid tool arguments
            if not isinstance(args, dict):
                pc.pre_result = ToolResult(
                    error=f"Invalid arguments: expected a JSON object, "
                    f"got {type(args).__name__}"
                )
                pc.approved = False
                return pc
            pc.args = args

            # Tool-level validation: missing required kwargs raise TypeError;
            # validators may also return an error string.
            try:
                validation_error = tool.validate_args(**args)
            except TypeError as err:
                pc.pre_result = ToolResult(error=f"Invalid arguments: {err}")
                pc.approved = False
                return pc
            if validation_error:
                pc.pre_result = ToolResult(
                    error=f"Invalid arguments: {validation_error}"
                )
                pc.approved = False
                return pc

            # Build a short summary for rule matching
            args_summary = _summarize_args(args, tool_name)

            # 裁决（K3 执行闸）：七站管线在 kernel/guard.py——plan/
            # choose_mode 硬拒绝、存储规则、PreToolUse hooks（只紧不松）、
            # force_prompt、自声明、用户裁决全部在 gate() 内完成；每次
            # 裁决记 permission_decision 事件上账本（记账先于动作）。
            adj: Adjudication = await self._guard.gate(
                GateCall(tool_name, tool, args, args_summary)
            )
            self._report_hook_warnings(adj.hook_warnings)
            if not adj.approved:
                pc.pre_result = ToolResult(error=adj.reason)
                pc.approved = False
                return pc

            return pc
        except Exception as e:
            # 准备阶段的任何意外都不得外泄：落成错误结果，让对话轮继续。
            pc.pre_result = ToolResult(
                error=f"Tool preparation failed: {type(e).__name__}: {e}"
            )
            pc.approved = False
            return pc

    async def execute_prepared(self, pc: PreparedCall) -> ToolResult:
        """Execute phase: run a prepared call (safe to gather concurrently).

        ``pre_result`` 已设 → 直接返回（准备阶段已拦截）；否则在异常兜底
        下真正执行工具。多个 ``execute_prepared`` 可经 ``asyncio.gather``
        并行——它们彼此不触碰 stdin。
        """
        if pc.pre_result is not None:
            return pc.pre_result
        result = await self._call(pc.tool, pc.args)

        # PostToolUse 钩子：仅通知——v1 忽略 blocked 标志，只打印警告。
        # 钩子系统的任何故障都绝不能让已完成的工具调用变成失败。
        if self.hooks and self.hooks.has_hooks("PostToolUse", pc.tool_name):
            try:
                outcome = await self.hooks.run(
                    "PostToolUse",
                    build_posttooluse_payload(
                        pc.tool_name,
                        pc.args or {},
                        result.to_message(),
                        workspace=self.hooks.workspace,
                        session_id=self.hooks.session_id,
                    ),
                )
                self._report_hook_warnings(outcome.warnings)
            except Exception:
                pass
        return result

    async def execute(
        self,
        tool_name: str,
        tool: Tool | None,
        raw_args: str,
        tc_id: str = "",
    ) -> tuple[ToolResult, bool]:
        """Back-compat wrapper: prepare → execute_prepared.

        Returns ``(result, was_approved)`` exactly like the original
        single-phase implementation, so existing callers/tests keep working.
        """
        pc = await self.prepare(tool_name, tool, raw_args, tc_id)
        result = await self.execute_prepared(pc)
        return result, pc.approved

    # ── internals ───────────────────────────────────────────────

    async def _prompt_user(self, call: GateCall, perm) -> tuple:
        """⑦站 prompter：变更预览 + 锁串行 + 弹窗钩子 + console 询问。

        UI 全部留在本层（内核不 import console）。fail-closed：异常向外
        传播，prepare 的兜底把它落成错误结果——绝不执行。
        """
        # 变更预览：write/edit 类工具实现 preview_diff → 弹窗渲染彩色
        # unified diff（manual 模式的审批依据）。预览成功时 JSON 参数已
        # 冗余（diff 含全部变更信息）→ 清空 details；预览 None（其他
        # 工具 / 探测失败）→ 回退 JSON 参数展示。
        args = call.args
        diff = None
        preview = getattr(call.tool, "preview_diff", None)
        if preview is not None:
            try:
                diff = preview(args)
            except Exception:
                diff = None  # 预览绝不允许拖垮弹窗
        details = (
            "" if diff
            else json.dumps(args, ensure_ascii=False, indent=2)[:500]
        )
        # raw-mode stdin 上的弹窗绝不能并发：锁串行化。
        async with self._prompt_lock:
            # 流式期（console 注册了活动流式服务）：权限选择委托 Live
            # 内嵌面板（框下，不占满屏）——此时**不得**触发 pause 钩子
            # （捕获线程正是面板热键来源，暂停即面板失灵）。非流式路径
            # 走传统全屏弹窗，钩子照旧暂停流式把终端交还弹窗（Bug 10）。
            svc = getattr(self.console, "_streaming_service", None)
            bridged = svc is not None and svc.is_live_active()
            if not bridged:
                self._fire_callback(self.on_prompt_start)
            try:
                return await self.console.ask_permission(
                    call.tool_name, perm.reason, details,
                    args_summary=call.args_summary,
                    # manual 模式逐项授权：隐藏"不再询问"选项
                    can_remember=(self.mode != "manual"),
                    diff=diff,
                )
            finally:
                if not bridged:
                    self._fire_callback(self.on_prompt_end)

    @staticmethod
    def _fire_callback(cb: Callable[[], None] | None) -> None:
        """触发弹窗钩子：None 安全、异常吞掉——钩子绝不能打断权限流程。"""
        if cb is None:
            return
        try:
            cb()
        except Exception:
            pass

    def _report_hook_warnings(self, warnings: list[str]) -> None:
        """打印钩子非阻塞警告：console 无 print_warning（如测试替身）则忽略。"""
        warn = getattr(self.console, "print_warning", None)
        if not callable(warn):
            return
        for w in warnings:
            try:
                warn(w)
            except Exception:
                pass

    async def _call(self, tool: Tool, args: dict) -> ToolResult:
        """Run ``tool.execute(**args)`` with an exception guard.

        单个工具的未捕获异常不应终结整个对话轮：在这里兜底并转成
        错误结果回喂给模型，让它有机会自我纠正。
        """
        try:
            return await tool.execute(**args)
        except Exception as e:
            return ToolResult(error=f"Tool raised {type(e).__name__}: {e}")


# ── helpers ──────────────────────────────────────────────────────


def _summarize_args(args: dict, tool_name: str) -> str:
    """Build a compact summary string for rule matching.

    Examples:
        ``shell(npm test)``, ``edit_file(*.py)``,
        ``write_file(/path/to/file)``.
    """
    if tool_name == "shell":
        cmd = args.get("command", "")
        # Use only the first token group for matching
        words = cmd.strip().split()
        return " ".join(words[:3]) if words else ""
    # For file tools, include the file path/pattern
    for key in ("file_path", "pattern", "path"):
        if key in args:
            return str(args[key])
    return ""


if __name__ == "__main__":
    import asyncio
    from ..config import OpenXConfig

    class _EchoTool(Tool):
        name = "selftest_echo"
        async def execute(self, **kw):
            return ToolResult(output=f"echo:{kw.get('text', '')}")

    executor = ToolExecutor(Console(config=OpenXConfig()), auto_approve=True)
    executor._rules = PermissionRules()  # 忽略用户 settings.json，保证自检确定性
    result, approved = asyncio.run(executor.execute("selftest_echo", _EchoTool(), '{"text": "hi"}'))
    assert approved and result.success and result.output == "echo:hi"
    bad, _ = asyncio.run(executor.execute("selftest_echo", _EchoTool(), "{invalid"))
    assert bad.error.startswith("Invalid arguments")

    # 两阶段 API：prepare 绝不抛异常，未知工具落成 pre_result；
    # execute_prepared 凭 PreparedCall 执行。两个 asyncio.run（两个事件循环）
    # 共用同一 executor 也无碍——自检路径从不获取 _prompt_lock。
    async def _two_phase():
        pc = await executor.prepare("selftest_echo", _EchoTool(), '{"text": "2p"}', "id-1")
        assert pc.pre_result is None and pc.args == {"text": "2p"} and pc.approved
        r = await executor.execute_prepared(pc)
        assert r.success and r.output == "echo:2p"
        unknown = await executor.prepare("nope", None, "{}", "id-2")
        assert unknown.pre_result is not None and "Unknown tool" in unknown.pre_result.error
        assert (await executor.execute_prepared(unknown)).error == unknown.pre_result.error
    asyncio.run(_two_phase())

    # 弹窗钩子：start/end 成对触发；回调自身抛异常也被吞掉，不影响主流程。
    from ..permissions import Permission

    class _AskingTool(_EchoTool):
        @property
        def permission(self):
            return Permission(level=PermissionLevel.ASK, reason="selftest")

    events: list = []
    def _boom():
        events.append("end")
        raise RuntimeError("callback boom")  # 必须被 _fire_callback 吞掉

    hooked = ToolExecutor(Console(config=OpenXConfig()), auto_approve=False)
    hooked._rules = PermissionRules()
    hooked.on_prompt_start = lambda: events.append("start")
    hooked.on_prompt_end = _boom
    async def _ask_ok(*a, **kw):
        return (True, False)
    hooked.console.ask_permission = _ask_ok
    r, ok = asyncio.run(hooked.execute("selftest_echo", _AskingTool(), '{"text": "h"}'))
    assert ok and r.success and events == ["start", "end"]

    # Plan-mode 第二道防线：ASK 级工具被硬拦截（pre_result 错误提及
    # exit_plan_mode），exit_plan_mode 自身按名字豁免。
    gated = ToolExecutor(Console(config=OpenXConfig()), auto_approve=True, plan_mode=True)
    gated._rules = PermissionRules()

    async def _plan_gate():
        pc = await gated.prepare("selftest_echo", _AskingTool(), '{"text": "x"}', "id-3")
        assert pc.pre_result is not None and "exit_plan_mode" in pc.pre_result.error
        assert not pc.approved
        ok_pc = await gated.prepare("exit_plan_mode", _EchoTool(), '{"text": "y"}', "id-4")
        assert ok_pc.pre_result is None  # 审批出口不被闸门拦截
    asyncio.run(_plan_gate())

    # ── manual 模式：ASK 工具强制逐项弹窗 ──────────────────────────
    # auto_approve=True 与存储 allow 规则都不得跳过弹窗；can_remember=False
    # 传给弹窗；即便弹窗返回 (True, True) 也绝不落盘规则。
    class _RecordingConsole:
        def __init__(self):
            self.calls: list[dict] = []
        async def ask_permission(self, tool_name, reason, details="",
                                 args_summary="", can_remember=True, diff=None):
            self.calls.append({"tool": tool_name, "args_summary": args_summary,
                               "can_remember": can_remember})
            return (True, True)  # 故意返回"记住"——manual 必须丢弃

    class _HighRiskTool(_AskingTool):
        def auto_allowed(self, args):
            return True  # 白名单也声明免询问——force_prompt 必须压倒它
        def is_high_risk(self, args):
            return True

    manual_console = _RecordingConsole()
    manual = ToolExecutor(manual_console, auto_approve=True, mode="manual")
    manual._rules = PermissionRules()
    # 存储 allow 规则精确匹配本次调用（summary 取 file_path）——manual 必须绕过它
    manual._rules.add_allow("selftest_echo(x.py)")

    async def _manual_gate():
        pc = await manual.prepare(
            "selftest_echo", _AskingTool(), '{"file_path": "x.py"}', "id-5")
        assert pc.pre_result is None and pc.approved  # 批准后放行
        assert manual_console.calls[-1]["can_remember"] is False
        # 落盘双保险：弹窗返回 remember=True，manual 也绝不新增规则
        assert manual._rules.check("selftest_echo", "y.py") is None
    asyncio.run(_manual_gate())
    assert len(manual_console.calls) == 1, "manual 下 auto_approve+存储规则仍须弹窗"

    # ── 高风险（is_high_risk）：任何模式永远弹窗 ────────────────────
    hr_console = _RecordingConsole()
    hr = ToolExecutor(hr_console, auto_approve=True, mode="auto")
    hr._rules = PermissionRules()
    hr._rules.add_allow("selftest_echo(x.py)")

    async def _high_risk_gate():
        pc = await hr.prepare(
            "selftest_echo", _HighRiskTool(), '{"file_path": "x.py"}', "id-6")
        assert pc.pre_result is None and pc.approved  # 批准后执行
        # 第一次批准时已落盘 allow 规则（auto 模式允许）；第二次仍必须弹窗
        assert hr._rules.check("selftest_echo", "x.py") == PermissionLevel.ALLOW
        pc2 = await hr.prepare(
            "selftest_echo", _HighRiskTool(), '{"file_path": "x.py"}', "id-7")
        assert pc2.pre_result is None
    asyncio.run(_high_risk_gate())
    assert len(hr_console.calls) == 2, "高风险调用不得被规则/auto_approve/白名单跳过"

    # ── choose_mode 防线：非 manual 模式调用被拒 ────────────────────
    async def _choose_mode_backstop():
        pc = await hr.prepare("choose_mode", _EchoTool(), '{}', "id-8")
        assert pc.pre_result is not None and "manual mode" in pc.pre_result.error
        pc2 = await manual.prepare("choose_mode", _EchoTool(), '{}', "id-9")
        assert pc2.pre_result is None  # manual 下放行
    asyncio.run(_choose_mode_backstop())

    # Phase 5 hooks：exit 2 的钩子在 prepare 阶段阻断（pre_result 落错），
    # exit 0 的钩子放行不影响执行。内联 shell 一行即可，无需临时脚本。
    from ..kernel.audit.hooks import HookRunner

    hook_blocked = ToolExecutor(
        Console(config=OpenXConfig()), auto_approve=True,
        hook_runner=HookRunner({"PreToolUse": [
            {"hooks": [{"type": "command", "command": "echo veto >&2; exit 2"}]},
        ]}),
    )
    hook_blocked._rules = PermissionRules()
    hb, hb_ok = asyncio.run(
        hook_blocked.execute("selftest_echo", _EchoTool(), '{"text": "z"}')
    )
    assert not hb_ok and "Blocked by PreToolUse hook" in hb.error and "veto" in hb.error

    hook_pass = ToolExecutor(
        Console(config=OpenXConfig()), auto_approve=True,
        hook_runner=HookRunner({"PreToolUse": [
            {"hooks": [{"type": "command", "command": "exit 0"}]},
        ]}),
    )
    hook_pass._rules = PermissionRules()
    hp, hp_ok = asyncio.run(
        hook_pass.execute("selftest_echo", _EchoTool(), '{"text": "go"}')
    )
    assert hp_ok and hp.output == "echo:go"

    # Phase 10：prompt_lock 可由构造参数注入（工作流并发子代理共享一把锁）；
    # 缺省 → 自建，既有行为不变。
    _shared = asyncio.Lock()
    assert ToolExecutor(
        Console(config=OpenXConfig()), prompt_lock=_shared
    )._prompt_lock is _shared
    assert ToolExecutor(Console(config=OpenXConfig()))._prompt_lock is not _shared

    print("openx/services/tool_executor.py OK ✓")

