"""容灾提交器——把 agent 的**回合内**状态搬进内核的 checkpoint 机制。

内核给机制（``openx/kernel/recovery/``：快照长什么样、怎么原子落盘、还能不能
用），本模块给策略：**何时快照、快照什么**。只有 agent 才知道 ``state.messages``
/ ``new_turn`` / ``tool_rounds`` 这些本地变量，所以"取景"这一半必须住在这里
（与 ``services/assembly.py`` 承接工具实例化仲裁同理--消费方策略不住内核）。

**提交时机是"不重放"的全部保证**：

```
回合开始  begin_turn(state, new_turn)  记 history_len + 持有本回合活引用
工具轮 ①  mark_inflight()              进 asyncio.gather **之前**
工具轮 ②  commit()                     结果消息**全部**追加完之后
回合结束  finish_turn()                emit turn_end + 删旁挂文件
中断      flush_current(kind)          取消/信号退出前的兜底落盘
```

**持有活引用是刻意的**：``state`` / ``new_turn`` 在整个回合里是同一批可变
对象，握着引用就等于永远看得到最新内容。于是"落盘"可以从**任何**方便的
位置发起--取消处理分支、REPL 的 ``except KeyboardInterrupt``、serve 的
``interrupt()``--而不必把 ``state`` 层层传出去，也不必依赖 asyncio 把
``KeyboardInterrupt`` 如何穿透 Task 的实现细节（那条路径的语义很微妙：
``app/cli/interactive.py`` 的 ``_StreamInterrupted`` 就是为它而生的）。

②必须在结果 append 循环**之后**：只有那时 ``new_turn`` 里每个 ``tool_call``
才有配对的结果消息，消息序列才是 provider 合法的、才构成幂等单元。崩在 ②
之前 = 旁挂文件仍是①的在途态 → 续跑补合成结果，已提交的轮次纹丝不动。

①是刻意保守的：崩在工具执行中途时那些调用的**结果未知**（可能已写文件、
已发请求），续跑时由 ``recovery.resume`` 补一条 ``[status: interrupted]``
的合成结果，**绝不重跑**。不重放是硬承诺，不因猜测而重复产生副作用。

**落盘纪律**：与 ``agent._persist_turn`` 同一条--任何异常全部吞掉，
绝不停在回合的关键路径上。持久化是优化，坏了只是少一次恢复机会。

**与账本的关系（先记账、后落盘）**：``emit`` 先写一行 ``checkpoint`` 事件
（账本=事实），随后才写旁挂文件（快照体=可覆盖的索引）。崩在两者之间留下
"账本新、旁挂旧"--续跑恰好回退一轮，安全；反过来则会让索引指向一个不存在的
seq，白丢一轮。故次序不可颠倒。
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Optional

from ..kernel.protocol import checkpoint_event
from ..kernel.recovery import (
    PHASE_COMMITTED,
    PHASE_INFLIGHT,
    REASON_ESC,
    REASON_OK,
    REASON_SIGNAL,
    REASON_SKIPPED,
    REASON_TOOL_ROUND,
    REASON_TURN_END,
    CheckpointRecord,
    CheckpointStore,
    ResumePlan,
    TurnSnapshot,
    inflight_projection,
    resolve_resume,
    snapshot_digest,
)
from ..orchestration.sessions import sanitize_message

_log = logging.getLogger("openx")

# 中断种类 → 落盘 reason（审计口径：为什么停下）
_INTERRUPT_REASONS = {
    "sigint": REASON_SIGNAL,
    "sigterm": REASON_SIGNAL,
    "esc": REASON_ESC,
    "client": REASON_ESC,
}


class CheckpointManager:
    """单会话的 checkpoint 提交器。无会话存储时整体退化为 no-op。

    子代理恒无 ``session_store``（委派任务的中间过程不进会话文件），
    故子代理的容灾天然关闭--与 ``attach_ledger`` 的守卫同一条纪律。
    """

    def __init__(self, agent: Any, session_store: Any = None) -> None:
        self._agent = agent
        self._session = (
            session_store if session_store is not None
            else getattr(agent, "session_store", None)
        )
        path = getattr(self._session, "path", None)
        self._store: Optional[CheckpointStore] = (
            CheckpointStore(path) if path is not None else None
        )
        # 回合起点：history_len 是**陈旧判定**的精确依据（回合结束时
        # history.add 整体追加，长度才变）。
        self._history_len = 0
        self._open = False
        self._engine = "stream_run"
        self._modal = False
        # 本回合的活引用：同一批可变对象，握着就等于看得到最新内容，
        # 于是落盘可以从任何位置发起（见模块文档）。
        self._state: Any = None
        self._new_turn: list[dict[str, Any]] = []
        self._interrupted = False
        # 最近一次提交的账本位置（插件的 on_checkpoint 钩子据此回查）
        self.last_seq = 0
        self.last_reason = ""
        self._last_digest = ""

    # ── 能力探测 ────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        """是否有可落盘的去处（无会话存储 = 子代理/嵌入式用法）。"""
        return self._store is not None

    @property
    def open(self) -> bool:
        """当前是否有开着的回合（finish/discard 之后为 False）。"""
        return self._open

    # ── 回合生命周期 ────────────────────────────────────────

    def begin_turn(
        self,
        state: Any,
        new_turn: list[dict[str, Any]],
        *,
        history_len: int,
        engine: str,
        modal: bool,
        resumed: bool = False,
    ) -> None:
        """回合开始：持有活引用、记起点。同时把上一回合的旁挂文件作废。

        作废是有意的语义：用户新起一个回合 = 明确放弃被打断的旧回合
        （与"Esc 丢弃当前回合"的既有行为一致），不留下会被误恢复的陈旧锚点。
        ``resumed=True`` 时保留--那正是我们刚恢复过来的那份。
        """
        if not self.enabled:
            return
        self._state = state
        self._new_turn = new_turn
        self._history_len = int(history_len)
        self._open = True
        self._engine = engine
        self._modal = bool(modal)
        self._interrupted = False
        if not resumed:
            self._store.delete()

    def mark_inflight(self) -> int:
        """① 工具执行前的在途标记。**先于** ``asyncio.gather`` 调用。

        此刻 ``new_turn`` 已含本轮带 ``tool_calls`` 的 assistant 消息、尚无
        任何结果。落这份前缀的价值在于：续跑时能重建出正确的消息序列，
        并对缺结果的调用补合成结果--**绝不重跑**。

        写盘失败无所谓：那只会退化成"锚在上一轮"，仍然安全。
        """
        if not self._open:
            return 0
        return self._commit_raw(phase=PHASE_INFLIGHT, reason=REASON_TOOL_ROUND)

    def commit(self, *, reason: str = REASON_TOOL_ROUND, gate: str = "") -> int:
        """② 提交一个已收口的工具轮（结果消息全部追加完之后）。

        返回 checkpoint 事件的账本 seq（0 = 未提交/失败）。
        """
        if not self._open:
            return 0
        return self._commit_raw(
            phase=PHASE_COMMITTED, reason=reason, gate=gate, inflight=[],
        )

    def flush_current(self, kind: str = "signal") -> int:
        """中断兜底落盘：用**活引用**把当前进展写下来。

        可在任意取消/退出前的位置调用（``except CancelledError``、
        ``except KeyboardInterrupt``、serve 的 ``interrupt()``）。落在
        ``phase=inflight``：此刻该轮工具**可能只写回了一半结果**，
        续跑补齐缺失结果、绝不重跑。

        **不要在信号处理器里调用**--信号处理器运行在任意两条字节码之间，
        碰 agent 状态或做文件 IO 都不安全。信号处理器只置位，由取消路径
        或 REPL 的退出分支来调本方法。
        """
        if not self._open or self._state is None:
            return 0
        if self._interrupted:
            # 一条退出路径可能被多个分支兜底调用（取消分支 + REPL 的
            # KeyboardInterrupt 分支 + serve 的 interrupt）——重入时
            # 返回上次结果，别把同一次中断记成好几次。
            return self.last_seq
        self._interrupted = True
        seq = self._commit_raw(
            phase=PHASE_INFLIGHT,
            reason=_INTERRUPT_REASONS.get(kind, REASON_SIGNAL),
        )
        # 中断本身也留痕（kind + 落盘结果）。checkpoint_seq=0 表示没落成--
        # 如实记，不省略字段。
        try:
            from ..kernel import get_kernel

            from ..kernel.protocol import interrupt_event

            get_kernel().emit(
                "interrupt",
                interrupt_event(kind, checkpoint_seq=seq, tool_rounds=self._rounds()),
                origin="user",
            )
            self._flush_session()
        except Exception:
            _log.debug("interrupt event failed", exc_info=True)
        return seq

    def finish_turn(self) -> None:
        """回合正常收口：记一条终局事实 + 删旁挂文件。

        删文件让"没有陈旧 checkpoint"成为**默认状态**--下次启动无需任何
        判断就知道没什么可恢复。先记账再删：崩在两者之间时，账本末条
        checkpoint 的 ``reason=turn_end`` 会让裁决判 ABSENT（而非撕裂）。
        """
        if not self._open:
            return
        self._open = False
        self._state = None
        self._new_turn = []
        if not self.enabled:
            return
        try:
            self._emit(
                reason=REASON_TURN_END,
                phase=PHASE_COMMITTED,
                tool_rounds=self._rounds(),
            )
        except Exception:
            _log.debug("checkpoint turn_end event failed", exc_info=True)
        self._store.delete()

    def discard(self) -> None:
        """丢弃当前回合的旁挂文件（不记账；用于显式放弃）。"""
        self._open = False
        self._state = None
        self._new_turn = []
        if self._store is not None:
            self._store.delete()

    # ── 取景 ───────────────────────────────────────────────

    def snapshot(
        self,
        state: Any,
        new_turn: list[dict[str, Any]],
        *,
        inflight: Optional[list[dict[str, Any]]] = None,
    ) -> TurnSnapshot:
        """把 agent 的回合内状态取景成快照（纯函数，便于单测）。"""
        agent = self._agent
        has_structured = _has_structured_result(agent)
        return TurnSnapshot(
            user_message=(new_turn[0].get("content") if new_turn else None),
            engine=self._engine,
            modal=self._modal,
            system_prompt_digest=_text_digest(getattr(agent, "_system_prompt", "")),
            tool_rounds=int(getattr(state, "tool_rounds", 0) or 0),
            history_len=self._history_len,
            # 逐条清洗：base64 图片与上传附件绝不落盘（与 sessions 同一纪律）
            new_turn=[sanitize_message(m) for m in new_turn],
            completed_tool_call_ids=_completed_ids(new_turn),
            inflight=list(inflight or []),
            todos=[
                dict(t) for t in getattr(agent, "todos", []) or []
                if isinstance(t, dict)
            ],
            counters={
                "input": int(getattr(agent, "total_input_tokens", 0) or 0),
                "output": int(getattr(agent, "total_output_tokens", 0) or 0),
                "cached": int(getattr(agent, "total_cached_tokens", 0) or 0),
                "plugin": int(getattr(agent, "total_plugin_tokens", 0) or 0),
            },
            last_tool_rounds=int(getattr(agent, "last_tool_rounds", 0) or 0),
            has_structured_result=has_structured,
            structured_result=(
                getattr(agent, "structured_result", None) if has_structured else None
            ),
        )

    # ── 恢复 ───────────────────────────────────────────────

    def recover(self) -> ResumePlan:
        """裁决磁盘上的 checkpoint 能否续跑（含账本裁决与钩子触发）。

        恢复成功后触发 ``on_resume`` 生命周期钩子，并把快照里的 token 累计
        **上调**到磁盘元数据之上（两者取大：都单调递增，磁盘值来自更早的
        边界，取大不会倒退）。
        """
        path = getattr(self._session, "path", None)
        if path is None:
            return ResumePlan()
        plan = resolve_resume(
            path,
            session_id=str(getattr(self._agent, "session_id", "") or ""),
            workspace=str(getattr(self._agent, "workspace", "") or ""),
            history=list(getattr(self._agent.history, "messages", []) or []),
            system_prompt_digest=_text_digest(
                getattr(self._agent, "_system_prompt", "")
            ),
        )
        if plan.usable:
            self._apply(plan)
            self._trigger("resume", _resume_payload(plan))
        return plan

    def _apply(self, plan: ResumePlan) -> None:
        """把恢复方案灌回 agent（与 ``load_session`` 同款--恢复跨轮状态）。"""
        agent = self._agent
        try:
            for attr, key in (
                ("total_input_tokens", "input"),
                ("total_output_tokens", "output"),
                ("total_cached_tokens", "cached"),
                ("total_plugin_tokens", "plugin"),
            ):
                from_snapshot = int(plan.counters.get(key, 0) or 0)
                if from_snapshot > int(getattr(agent, attr, 0) or 0):
                    setattr(agent, attr, from_snapshot)
            if plan.todos:
                agent.todos[:] = plan.todos
            agent.last_tool_rounds = plan.last_tool_rounds
        except Exception:
            _log.debug("resume state rebuild failed", exc_info=True)

    # ── 内部 ───────────────────────────────────────────────

    def _rounds(self) -> int:
        return int(getattr(self._agent, "last_tool_rounds", 0) or 0)

    def _commit_raw(
        self,
        *,
        phase: str,
        reason: str,
        gate: str = "",
        inflight: Optional[list[dict[str, Any]]] = None,
    ) -> int:
        """取景 → 记账 → 落盘。**永不抛**（持久化是优化，不在关键路径上）。

        ``inflight=None`` 时从 ``new_turn`` 反推缺结果的调用--中断路径上
        该轮可能只写回了一半结果，反推比"记录调用发起时的快照"更准确。
        """
        if not self.enabled:
            return 0
        state = self._state
        new_turn = self._new_turn
        try:
            calls = (
                inflight_projection(_unanswered_calls(new_turn))
                if inflight is None else list(inflight)
            )
            snapshot = self.snapshot(state, new_turn, inflight=calls)
            digest = snapshot_digest(snapshot)
            # 先记账、后落盘：账本是事实，旁挂是可覆盖的索引。
            seq = self._emit(
                reason=reason,
                phase=phase,
                tool_rounds=snapshot.tool_rounds,
                gate=gate,
                snapshot_digest=digest,
                completed=snapshot.completed_tool_call_ids,
            )
            if not seq:
                return 0
            # session 惰性落盘：不主动 flush 的话刚 emit 的 seq 还在内存缓冲里，
            # 旁挂文件引用的 seq 会在盘上找不到（恢复裁决判撕裂）。
            self._flush_session()
            record = CheckpointRecord(
                session_id=str(getattr(self._agent, "session_id", "") or ""),
                workspace=str(getattr(self._agent, "workspace", "") or ""),
                ledger_seq=seq,
                ledger_digest=self._last_digest,
                phase=phase,
                reason=reason,
                created_at=time.time(),
                snapshot_digest=digest,
                snapshot=snapshot,
            )
            if self._store.write(record) != REASON_OK:
                # 落盘失败（超限/IO）：补一条终局事实，让下次裁决判 ABSENT
                # 而不是撕裂--"我们试过但没存下"才是准确的说法。
                self._emit(
                    reason=REASON_SKIPPED,
                    phase=phase,
                    tool_rounds=snapshot.tool_rounds,
                    gate=gate,
                )
                self._flush_session()
            self._trigger("checkpoint", {
                "event": "checkpoint",
                "reason": reason,
                "phase": phase,
                "ledger_seq": seq,
                "tool_rounds": snapshot.tool_rounds,
                "session_id": getattr(self._agent, "session_id", ""),
                "workspace": getattr(self._agent, "workspace", ""),
            })
            return seq
        except Exception:
            _log.debug("checkpoint commit failed", exc_info=True)
            return 0

    def _emit(
        self,
        *,
        reason: str,
        phase: str,
        tool_rounds: int,
        gate: str = "",
        snapshot_digest: str = "",
        completed: Any = None,
    ) -> int:
        """写一条 ``checkpoint`` 账本事件，返回 seq（未挂账本时仍非 0）。

        ``cause`` 指向前一次 checkpoint 的 seq：checkpoint 链本身就是
        "这个回合一步步推进"的因果证据，不必额外造事件。
        """
        from ..kernel import get_kernel

        cause = self.last_seq or None
        payload = checkpoint_event(
            phase=phase,
            reason=reason,
            tool_rounds=tool_rounds,
            gate=gate,
            completed_tool_calls=list(completed or []),
            snapshot_digest=snapshot_digest,
        )
        event = get_kernel().emit("checkpoint", payload, cause=cause)
        self.last_seq = int(getattr(event, "seq", 0) or 0)
        self.last_reason = reason
        self._last_digest = str(getattr(event, "digest", "") or "")
        return self.last_seq

    def _flush_session(self) -> None:
        """把账本事件强行拖到盘上（session 惰性落盘）。"""
        flush = getattr(self._session, "flush", None)
        if callable(flush):
            flush()

    def _trigger(self, event: str, payload: dict[str, Any]) -> None:
        """触发生命周期钩子（插件观察点；异常由内核隔离，绝不外溢）。"""
        try:
            from ..kernel import get_kernel

            get_kernel().trigger_lifecycle(event, payload=payload)
        except Exception:
            _log.debug("lifecycle %s trigger failed", event, exc_info=True)


def _has_structured_result(agent: Any) -> bool:
    """agent 是否已捕获结构化结果。

    ``has_structured_result`` 是**方法**（不是属性），而 ``structured_result``
    在未捕获时访问会抛 ``RuntimeError``--先问再取是硬次序，不能图省事用
    ``getattr(agent, "structured_result", None)``（那会在未捕获时炸穿取景）。
    """
    probe = getattr(agent, "has_structured_result", None)
    if callable(probe):
        try:
            return bool(probe())
        except Exception:
            return False
    return False


def _unanswered_calls(new_turn: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从消息序列里挑出**没有配对结果**的 tool_calls（在途，或半写回）。"""
    answered: set[str] = set()
    for message in new_turn:
        if isinstance(message, dict) and message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str):
                answered.add(call_id)
    pending: list[dict[str, Any]] = []
    for message in new_turn:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            if call.get("id") in answered:
                continue
            pending.append(call)
    return pending


def _completed_ids(new_turn: list[dict[str, Any]]) -> list[str]:
    """已配对的 tool_call id（一致性校验用，**不用于跳过**）。"""
    declared: list[str] = []
    for message in new_turn:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict) and isinstance(call.get("id"), str):
                declared.append(call["id"])
    answered = {
        m.get("tool_call_id") for m in new_turn
        if isinstance(m, dict) and m.get("role") == "tool"
    }
    return [call_id for call_id in declared if call_id in answered]


def _text_digest(text: str) -> str:
    """系统提示摘要：轮间插件装卸会让 prefix 变化，变了就不该续跑旧快照。"""
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _resume_payload(plan: ResumePlan) -> dict[str, Any]:
    """``on_resume`` 钩子的 payload（契约见 kernel/assembly/context.py）。"""
    return {
        "event": "resume",
        "phase": plan.record.phase if plan.record else "",
        "ledger_seq": plan.record.ledger_seq if plan.record else 0,
        "tool_rounds": plan.tool_rounds,
        "repaired_calls": list(plan.repaired_calls),
        "todos": list(plan.todos),
        "counters": dict(plan.counters),
        "detail": plan.detail,
    }
