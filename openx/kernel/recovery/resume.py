"""容灾 · 恢复裁决——把"磁盘上那份 checkpoint 还能不能用"变成可判定的结论。

本模块只做**裁决与重建**，不认识 agent、不碰事件循环：输入是磁盘事实
（旁挂文件 + 会话账本 + 已加载的历史），输出是一份 ``ResumePlan``。这样
"什么情况下算撕裂"可以脱离 agent 单独测试。

**核心不变量**：恢复后的消息序列必须**与原回合逐字节同源**，且每个
``assistant.tool_calls`` 都有配对的 ``tool`` 结果。满足这两条时，模型不可能
重发已应答的调用--**消息日志本身就是幂等单元**，循环里没有"跳过表"。

**在途轮（phase=inflight）不是撕裂**：它是一份有效的、锚在上一轮边界的
快照，只是附带"有几个调用正在执行、结果未知"。恢复时给这些调用补一条
``[status: interrupted]`` 的合成结果，**绝不重跑**--保住"不重放"的承诺，
也不因猜测而重复产生副作用。
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

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from .model import (
    REASON_SKIPPED,
    REASON_TURN_END,
    CheckpointRecord,
    TurnSnapshot,
    snapshot_digest,
)
from .store import CheckpointStore, ledger_envelope_count, ledger_last_checkpoint

# 旁挂文件缺失时，账本里这两类 checkpoint 说明"没有可恢复的进展"，
# 而不是"有进展却丢了"：
#   turn_end -- 回合已正常收口，本该删旁挂文件（崩溃在删之前）
#   skipped  -- 上次提交因超限/IO 失败被主动跳过（快照本就没写成）
_TERMINAL_REASONS = frozenset({REASON_TURN_END, REASON_SKIPPED})

# 合成结果正文：模型读到它应当知道"这件事没成，且没有被重做"，
# 从而自行决定重试还是绕路。措辞刻意显式，避免模型误以为工具返回了空结果。
INTERRUPTED_TOOL_RESULT = (
    "[status: interrupted] This tool call was interrupted mid-execution "
    "(process crash or user interrupt). Its outcome is unknown and it was "
    "NOT replayed. Re-issue the call if you still need its result."
)


class ResumeVerdict(str, Enum):
    """恢复裁决。除 ``OK`` 外一律退化为"普通恢复会话"（回合丢失），绝不挡住启动。"""

    OK = "ok"
    """可用：照快照续跑。"""

    ABSENT = "absent"
    """压根没有 checkpoint（或属于别的会话/工作区）--正常起新回合。"""

    ALREADY_COMPLETE = "already_complete"
    """该回合其实已正常落盘（崩溃在持久化与删旁挂文件之间）--静默丢弃即可。"""

    TORN = "torn"
    """有 checkpoint 但不可用（摘要不符/引用越界/结构损坏）--弃用并告警。"""

    MISMATCH = "mismatch"
    """身份不符（会话 id / 工作区对不上）--弃用并告警。"""


@dataclass
class ResumePlan:
    """恢复方案：调用方据此直接重入循环，无需再判断。

    ``new_turn`` 已是**可用的最终消息列表**（含为在途调用补的合成结果），
    调用方只要 ``state.messages = [system] + history + plan.new_turn`` 即可。
    """

    verdict: ResumeVerdict = ResumeVerdict.ABSENT
    record: Optional[CheckpointRecord] = None
    detail: str = ""

    tool_rounds: int = 0
    new_turn: list[dict[str, Any]] = field(default_factory=list)
    user_message: Any = None
    modal: bool = False
    todos: list[dict[str, Any]] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
    last_tool_rounds: int = 0
    has_structured_result: bool = False
    structured_result: Any = None

    repaired_calls: list[str] = field(default_factory=list)
    """被补了合成结果的 tool_call id（在途或因崩溃丢结果的）。"""

    @property
    def usable(self) -> bool:
        return self.verdict is ResumeVerdict.OK


def resolve_resume(
    jsonl_path: Path,
    *,
    session_id: str,
    workspace: str,
    history: list[dict[str, Any]],
    system_prompt_digest: str = "",
) -> ResumePlan:
    """裁决磁盘上的 checkpoint 能否续跑，并重建出可用的恢复方案。

    校验按"先廉价后昂贵、先身份后结构"排序，**首个失败即定论**。
    任何异常路径都返回一个非 ``OK`` 的 plan 而不是抛出--恢复是优化，
    坏文件绝不能挡住启动（与 ``SessionStore.load`` 跳过损坏行同纪律）。
    """
    jsonl_path = Path(jsonl_path)
    try:
        return _resolve(
            jsonl_path,
            session_id=session_id,
            workspace=workspace,
            history=history,
            system_prompt_digest=system_prompt_digest,
        )
    except Exception as exc:  # 裁决本身出错 = 弃用，绝不冒泡
        return ResumePlan(
            verdict=ResumeVerdict.TORN,
            detail=f"checkpoint verdict failed: {type(exc).__name__}: {exc}",
        )


def _resolve(
    jsonl_path: Path,
    *,
    session_id: str,
    workspace: str,
    history: list[dict[str, Any]],
    system_prompt_digest: str,
) -> ResumePlan:
    store = CheckpointStore(jsonl_path)
    record = store.read()
    if record is None:
        last = ledger_last_checkpoint(jsonl_path)
        if last is None:
            return ResumePlan(verdict=ResumeVerdict.ABSENT, detail="no checkpoint")
        # 账本末条 checkpoint 说明这个回合已收口（或快照被主动跳过）--
        # 没有进展可恢复，安静退化为普通恢复，别用 TORN 制造假警报。
        if last.get("reason") in _TERMINAL_REASONS:
            return ResumePlan(
                verdict=ResumeVerdict.ABSENT,
                detail=f"last checkpoint was terminal ({last.get('reason')})",
            )
        # 账本记过、快照体却没了：快照体不在账本里（体积原因），无法重建。
        return ResumePlan(
            verdict=ResumeVerdict.TORN,
            detail="checkpoint recorded in ledger but sidecar is missing or unreadable",
        )

    # ① 身份：属于别的会话/工作区的快照一律弃用（防串台续跑）
    if record.session_id and record.session_id != session_id:
        return ResumePlan(
            verdict=ResumeVerdict.MISMATCH,
            detail=f"checkpoint is for session {record.session_id!r}, not {session_id!r}",
        )
    if record.workspace and record.workspace != workspace:
        return ResumePlan(
            verdict=ResumeVerdict.MISMATCH,
            detail="checkpoint is for a different workspace",
        )

    snapshot = record.snapshot
    if snapshot is None:
        return ResumePlan(verdict=ResumeVerdict.TORN, detail="checkpoint has no snapshot body")

    # ② 陈旧：回合已正常落盘 → 静默丢弃（正常路径，不是错误）
    if len(history) > snapshot.history_len:
        return ResumePlan(
            verdict=ResumeVerdict.ALREADY_COMPLETE,
            record=record,
            detail="turn was already persisted; checkpoint is stale",
        )

    # ③ 完整性：摘要复算（防篡改/半截写）
    if record.snapshot_digest and snapshot_digest(snapshot) != record.snapshot_digest:
        return ResumePlan(
            verdict=ResumeVerdict.TORN,
            record=record,
            detail="snapshot digest mismatch (tampered or truncated)",
        )

    # ④ 引用：checkpoint 指向的账本事件必须真的存在
    if record.ledger_seq > ledger_envelope_count(jsonl_path):
        return ResumePlan(
            verdict=ResumeVerdict.TORN,
            record=record,
            detail=(
                f"checkpoint references ledger seq {record.ledger_seq} "
                "beyond the session's last event"
            ),
        )

    # ⑤ 前缀同源：系统提示变了（插件装卸、指令文件改动）就不是同一局对话。
    # 宁可拒绝续跑也不要在不同 prefix 上接续--那会得到一份"语义漂移"的历史。
    if (
        snapshot.system_prompt_digest
        and system_prompt_digest
        and snapshot.system_prompt_digest != system_prompt_digest
    ):
        return ResumePlan(
            verdict=ResumeVerdict.TORN,
            record=record,
            detail="system prompt changed since the checkpoint; refusing to resume",
        )

    # ⑥ 消息序列修复：为没有配对结果的 tool_call 补合成结果。在途轮（工具
    # 正在执行时崩溃）与"并行批次里只写回了一半结果"都靠这一步收敛。
    new_turn = [dict(m) for m in snapshot.new_turn]
    repaired = _repair_missing_tool_results(new_turn)

    problem = _first_illegal_tool_pair(new_turn)
    if problem:
        return ResumePlan(
            verdict=ResumeVerdict.TORN, record=record, detail=problem,
        )

    # 在途轮算"已消耗一轮"：模型确实出了一次调用，恢复后应当继续下一轮，
    # 而不是在同一轮里再发一次请求。
    tool_rounds = snapshot.tool_rounds + (1 if snapshot.inflight else 0)

    return ResumePlan(
        verdict=ResumeVerdict.OK,
        record=record,
        tool_rounds=tool_rounds,
        new_turn=new_turn,
        user_message=snapshot.user_message,
        modal=bool(snapshot.modal),
        todos=list(snapshot.todos or []),
        counters=dict(snapshot.counters or {}),
        last_tool_rounds=snapshot.last_tool_rounds,
        has_structured_result=bool(snapshot.has_structured_result),
        structured_result=snapshot.structured_result,
        repaired_calls=repaired,
        detail=(
            f"resumed at tool round {tool_rounds}"
            + (f"; {len(repaired)} interrupted call(s) not replayed" if repaired else "")
        ),
    )


# ── 内部：消息序列完整性 ──────────────────────────────────────


def _tool_call_ids(message: dict[str, Any]) -> list[str]:
    """一条 assistant 消息里的 tool_call id 列表（畸形一律忽略）。"""
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return []
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return []
    ids: list[str] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        call_id = call.get("id")
        if isinstance(call_id, str) and call_id:
            ids.append(call_id)
    return ids


def _answered_ids(messages: list[dict[str, Any]]) -> set[str]:
    """已被 ``tool`` 结果消息应答的 tool_call id 集合。"""
    answered: set[str] = set()
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        call_id = message.get("tool_call_id")
        if isinstance(call_id, str) and call_id:
            answered.add(call_id)
    return answered


def _repair_missing_tool_results(messages: list[dict[str, Any]]) -> list[str]:
    """给缺结果的 tool_call 就地补合成结果，返回被补的 id（保持消息顺序）。

    **只补不删、只补不跑**：补的是"结果未知"的声明，不是猜测出来的结果。
    """
    answered = _answered_ids(messages)
    repaired: list[str] = []
    # 遍历副本：循环体向 messages 追加，直接迭代原列表会边遍历边看到新元素
    # （追加的是无 tool_calls 的 tool 消息，当前不会死循环，但那是巧合而非保证）
    for message in list(messages):
        for call_id in _tool_call_ids(message):
            if call_id in answered:
                continue
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": INTERRUPTED_TOOL_RESULT,
            })
            answered.add(call_id)
            repaired.append(call_id)
    return repaired


def _first_illegal_tool_pair(messages: list[dict[str, Any]]) -> str:
    """返回首个非法的 tool 配对描述；全部合法返回 ""。

    两条禁令：``tool`` 结果必须能追溯到某个 ``assistant.tool_calls``；
    每个 ``tool_call`` 必须有配对结果。这是发给 provider 的硬约束--
    序列不合法会被 API 直接拒，比"续跑丢一轮"更糟。
    """
    declared: set[str] = set()
    for message in messages:
        declared.update(_tool_call_ids(message))
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in declared:
                return f"orphan tool result for unknown tool_call id {call_id!r}"
    answered = _answered_ids(messages)
    missing = sorted(declared - answered)
    if missing:
        return f"tool_call(s) without a result: {', '.join(missing[:3])}"
    return ""


def inflight_projection(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把一轮的 ``response["tool_calls"]`` 投影成可落盘的轻量描述。

    **不落完整入参**：``write_file`` 的入参可能含整个文件内容，会让快照
    随轮次线性膨胀。这里只留 id/name/参数长度--足够审计"当时在跑什么"，
    而真正的复原依据是 ``new_turn`` 里的原始消息。
    """
    projection: list[dict[str, Any]] = []
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function")
        name = ""
        arguments = ""
        if isinstance(fn, dict):
            name = str(fn.get("name") or "")
            raw = fn.get("arguments")
            arguments = raw if isinstance(raw, str) else ""
        projection.append({
            "id": call.get("id", ""),
            "name": name,
            "arguments_len": len(arguments),
        })
    return projection


if __name__ == "__main__":
    import json
    import tempfile

    from .model import (
        CHECKPOINT_VERSION,
        PHASE_COMMITTED,
        CheckpointRecord,
        TurnSnapshot,
    )

    def _write_ledger(path: Path, seqs: list[int]) -> None:
        lines = [json.dumps({"type": "meta", "session_id": "s1"})]
        for seq in seqs:
            lines.append(json.dumps({
                "seq": seq, "digest": f"d{seq}", "type": "probe",
                "payload": {"type": "probe"},
            }))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _record(**over) -> CheckpointRecord:
        snap = TurnSnapshot(
            user_message="do it",
            history_len=1,
            tool_rounds=1,
            new_turn=[
                {"role": "user", "content": "do it"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "t1", "type": "function",
                     "function": {"name": "echo", "arguments": "{}"}},
                ]},
                {"role": "tool", "tool_call_id": "t1", "content": "ok"},
            ],
            completed_tool_call_ids=["t1"],
        )
        rec = CheckpointRecord(
            version=CHECKPOINT_VERSION,
            session_id="s1",
            workspace="/ws",
            ledger_seq=2,
            phase=PHASE_COMMITTED,
            snapshot_digest=snapshot_digest(snap),
            snapshot=snap,
        )
        for key, value in over.items():
            if key == "snapshot":
                rec.snapshot = value
                rec.snapshot_digest = snapshot_digest(value)
            else:
                setattr(rec, key, value)
        return rec

    def _store(tmp: Path, rec: CheckpointRecord | None) -> Path:
        jsonl = tmp / "s1.jsonl"
        _write_ledger(jsonl, [1, 2])
        store = CheckpointStore(jsonl)
        store.delete()
        if rec is not None:
            assert store.write(rec) == ""
        return jsonl

    base = {"session_id": "s1", "workspace": "/ws", "history": [{"role": "user", "content": "x"}]}

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # 无 checkpoint → ABSENT
        plan = resolve_resume(_store(tmp, None), **base)
        assert plan.verdict is ResumeVerdict.ABSENT and not plan.usable

        # 正常续跑 → OK，消息序列保持
        plan = resolve_resume(_store(tmp, _record()), **base)
        assert plan.verdict is ResumeVerdict.OK, plan.detail
        assert plan.tool_rounds == 1 and len(plan.new_turn) == 3
        assert plan.repaired_calls == []

        # 身份不符 → MISMATCH
        plan = resolve_resume(
            _store(tmp, _record(session_id="other")),
            **{**base, "session_id": "s1"},
        )
        assert plan.verdict is ResumeVerdict.MISMATCH
        plan = resolve_resume(
            _store(tmp, _record(workspace="/elsewhere")), **base
        )
        assert plan.verdict is ResumeVerdict.MISMATCH

        # 摘要不符（篡改/半截写）→ TORN
        rec = _record()
        rec.snapshot_digest = "deadbeef"
        plan = resolve_resume(_store(tmp, rec), **base)
        assert plan.verdict is ResumeVerdict.TORN and "digest" in plan.detail

        # 引用越界 → TORN
        plan = resolve_resume(_store(tmp, _record(ledger_seq=99)), **base)
        assert plan.verdict is ResumeVerdict.TORN and "ledger" in plan.detail

        # 陈旧：回合已落盘（history 变长）→ ALREADY_COMPLETE
        plan = resolve_resume(
            _store(tmp, _record()),
            **{**base, "history": [{"role": "user", "content": "x"},
                                   {"role": "assistant", "content": "done"}]},
        )
        assert plan.verdict is ResumeVerdict.ALREADY_COMPLETE and not plan.usable

        # 系统提示漂移 → TORN
        snap = _record().snapshot
        snap.system_prompt_digest = "aaa"
        plan = resolve_resume(
            _store(tmp, _record(snapshot=snap)),
            **{**base, "system_prompt_digest": "bbb"},
        )
        assert plan.verdict is ResumeVerdict.TORN and "system prompt" in plan.detail
        # 摘要一致则放行
        plan = resolve_resume(
            _store(tmp, _record(snapshot=snap)),
            **{**base, "system_prompt_digest": "aaa"},
        )
        assert plan.verdict is ResumeVerdict.OK

        # 在途轮：assistant 有 tool_calls 但无结果 → 补合成结果、不重跑
        snap = TurnSnapshot(
            user_message="do it", history_len=1, tool_rounds=1,
            inflight=[{"id": "t2", "name": "write_file", "arguments_len": 10}],
            new_turn=[
                {"role": "user", "content": "do it"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "t1", "type": "function",
                     "function": {"name": "echo", "arguments": "{}"}},
                ]},
                {"role": "tool", "tool_call_id": "t1", "content": "ok"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "t2", "type": "function",
                     "function": {"name": "write_file", "arguments": "{}"}},
                ]},
            ],
        )
        plan = resolve_resume(_store(tmp, _record(snapshot=snap)), **base)
        assert plan.verdict is ResumeVerdict.OK, plan.detail
        assert plan.repaired_calls == ["t2"]
        assert plan.tool_rounds == 2          # 在途轮算已消耗
        last = plan.new_turn[-1]
        assert last["role"] == "tool" and last["tool_call_id"] == "t2"
        assert "interrupted" in last["content"].lower()

        # 只写回一半的并行批次：缺哪个补哪个
        snap = TurnSnapshot(
            history_len=1, tool_rounds=0,
            new_turn=[
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "a", "type": "function", "function": {"name": "x", "arguments": "{}"}},
                    {"id": "b", "type": "function", "function": {"name": "y", "arguments": "{}"}},
                ]},
                {"role": "tool", "tool_call_id": "a", "content": "done"},
            ],
        )
        plan = resolve_resume(_store(tmp, _record(snapshot=snap)), **base)
        assert plan.verdict is ResumeVerdict.OK and plan.repaired_calls == ["b"]

        # 无法修复的非法配对 → TORN（孤儿 tool 结果）
        snap = TurnSnapshot(history_len=1, new_turn=[
            {"role": "tool", "tool_call_id": "ghost", "content": "x"},
        ])
        plan = resolve_resume(_store(tmp, _record(snapshot=snap)), **base)
        assert plan.verdict is ResumeVerdict.TORN and "orphan" in plan.detail

        # 旁挂文件丢失但账本记过 → TORN（体现"账本为准"）
        jsonl = tmp / "s1.jsonl"
        CheckpointStore(jsonl).delete()   # 先清掉上一用例留下的旁挂文件
        jsonl.write_text("\n".join([
            json.dumps({"type": "meta", "session_id": "s1"}),
            json.dumps({"seq": 1, "digest": "d1", "type": "checkpoint",
                        "payload": {"type": "checkpoint", "phase": "committed"}}),
        ]) + "\n", encoding="utf-8")
        plan = resolve_resume(jsonl, **base)
        assert plan.verdict is ResumeVerdict.TORN and "sidecar" in plan.detail

        # 入参投影：不落完整入参（防 write_file 撑爆快照）
        proj = inflight_projection([{"id": "t", "function": {"name": "write_file",
                                  "arguments": "x" * 5000}}])
        assert proj == [{"id": "t", "name": "write_file", "arguments_len": 5000}]
        assert inflight_projection(None) == []

    print("openx/kernel/recovery/resume.py OK ✓")
