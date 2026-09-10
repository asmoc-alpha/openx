"""容灾（recovery）：快照模型、旁挂存储、恢复裁决——不涉及 agent。

覆盖：schema 往返与摘要可复算 / 原子写不留 tmp / 超限拒绝落盘而非裁剪 /
写盘失败永不抛 / 损坏与版本不符一律弃用 / 裁决五种结论（OK / ABSENT /
ALREADY_COMPLETE / TORN / MISMATCH）/ 在途轮补合成结果且不重跑 /
半写回的并行批次只补缺失的那个 / 旁挂丢失时"账本为准"。

运行：``python -m pytest tests/kernel/test_recovery.py -q``
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from openx.kernel.recovery import (
    CHECKPOINT_KIND,
    CHECKPOINT_VERSION,
    INTERRUPTED_TOOL_RESULT,
    MAX_SNAPSHOT_BYTES,
    PHASE_COMMITTED,
    PHASE_INFLIGHT,
    REASON_IO,
    REASON_OK,
    REASON_OVERSIZE,
    REASON_SKIPPED,
    REASON_TOOL_ROUND,
    REASON_TURN_END,
    CheckpointRecord,
    CheckpointStore,
    ResumeVerdict,
    TurnSnapshot,
    ledger_envelope_count,
    ledger_last_checkpoint,
    inflight_projection,
    record_from_json,
    record_to_json,
    resolve_resume,
    sidecar_path,
    snapshot_digest,
)


# ── 构造助手 ────────────────────────────────────────────────────


def _snapshot(**over) -> TurnSnapshot:
    """一个"完成了一轮 echo 工具调用"的基准快照。"""
    base = dict(
        user_message="do it",
        engine="stream_run",
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
        counters={"input": 10, "output": 5, "cached": 0, "plugin": 0},
    )
    base.update(over)
    return TurnSnapshot(**base)


def _record(**over) -> CheckpointRecord:
    snap = over.pop("snapshot", None) or _snapshot()
    record = CheckpointRecord(
        version=CHECKPOINT_VERSION,
        kind=CHECKPOINT_KIND,
        session_id="s1",
        workspace="/ws",
        ledger_seq=2,
        ledger_digest="d2",
        phase=PHASE_COMMITTED,
        reason=REASON_TOOL_ROUND,
        snapshot_digest=snapshot_digest(snap),
        snapshot=snap,
    )
    for key, value in over.items():
        setattr(record, key, value)
    return record


def _write_ledger(path: Path, seqs: list[int], extra: list[dict] | None = None) -> None:
    """写一个带 meta 行 + 若干信封行的会话文件。"""
    lines = [json.dumps({"type": "meta", "session_id": "s1"})]
    for seq in seqs:
        lines.append(json.dumps({
            "seq": seq, "digest": f"d{seq}", "type": "probe",
            "payload": {"type": "probe"},
        }))
    for line in extra or []:
        lines.append(json.dumps(line))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def ckpt_env(tmp_path):
    """(jsonl_path, store)：会话文件已含 2 条信封行，旁挂文件空缺。"""
    jsonl = tmp_path / "s1.jsonl"
    _write_ledger(jsonl, [1, 2])
    store = CheckpointStore(jsonl)
    return jsonl, store


def _base(history=None) -> dict:
    return {
        "session_id": "s1",
        "workspace": "/ws",
        "history": history if history is not None else [{"role": "user", "content": "x"}],
    }


# ── 模型 ────────────────────────────────────────────────────────


class TestModel:
    """快照 schema：往返保真、摘要可复算、脏数据整体弃用。"""

    def test_roundtrip_is_lossless(self):
        snap = _snapshot()
        record = _record(snapshot=snap)
        back = record_from_json(record_to_json(record))
        assert back is not None
        assert back.snapshot.tool_rounds == 1
        assert back.snapshot.new_turn == snap.new_turn
        assert back.snapshot.completed_tool_call_ids == ["t1"]
        assert back.ledger_seq == 2 and back.ledger_digest == "d2"

    def test_digest_is_stable_and_content_sensitive(self):
        snap = _snapshot()
        assert snapshot_digest(snap) == snapshot_digest(_snapshot())
        assert snapshot_digest(snap) != snapshot_digest(_snapshot(tool_rounds=2))

    def test_structured_result_roundtrips_and_digest_holds(self):
        """结构化结果（含未捕获哨兵）落盘再读回，摘要必须仍能复算。

        回归：写盘路径与摘要路径若各自变换一次 ``structured_result``，
        "存进去的内容"和"摘要覆盖的内容"就会分叉--每次恢复都被误判撕裂。
        """
        from openx.kernel.recovery.model import _NO_STRUCTURED

        for snap in (
            TurnSnapshot(has_structured_result=False, structured_result=_NO_STRUCTURED),
            TurnSnapshot(has_structured_result=True, structured_result={"a": [1, 2]}),
            TurnSnapshot(has_structured_result=True, structured_result=None),
        ):
            record = _record(snapshot=snap)
            back = record_from_json(record_to_json(record))
            assert back is not None
            assert snapshot_digest(back.snapshot) == record.snapshot_digest

    @pytest.mark.parametrize("raw", [
        "not json",
        "[]",                                              # 不是对象
        '{"kind": "other.kind", "version": 1, "snapshot": {}}',   # 别类 JSON
        '{"kind": "openx.turn_checkpoint"}',               # 缺 version
        json.dumps({"kind": CHECKPOINT_KIND, "version": 999, "snapshot": {}}),
        json.dumps({"kind": CHECKPOINT_KIND, "version": 1, "snapshot": None}),
        json.dumps({"kind": CHECKPOINT_KIND, "version": 1,
                    "snapshot": {"new_turn": "oops"}}),    # 容器字段类型错
        json.dumps({"kind": CHECKPOINT_KIND, "version": 1,
                    "snapshot": {"counters": []}}),
    ])
    def test_malformed_is_rejected_not_raised(self, raw):
        assert record_from_json(raw) is None


# ── 存储 ────────────────────────────────────────────────────────


class TestStore:
    """旁挂文件：原子写、无残留 tmp、超限拒绝、失败不抛。"""

    def test_path_is_sibling_of_session_file(self, tmp_path):
        assert sidecar_path(tmp_path / "abc.jsonl").name == "abc.ckpt.json"

    def test_atomic_write_leaves_no_tmp_and_overwrites(self, ckpt_env):
        jsonl, store = ckpt_env
        assert store.write(_record()) == REASON_OK
        assert store.read().ledger_seq == 2
        # 覆盖式最新态（last-writer-wins），不是追加
        assert store.write(_record(ledger_seq=5)) == REASON_OK
        assert store.read().ledger_seq == 5
        # 原子写不留垃圾（崩溃留下的 .tmp 会被后续写清理）
        assert list(jsonl.parent.glob("*.tmp.*")) == []

    def test_read_missing_is_none(self, ckpt_env):
        _, store = ckpt_env
        assert store.read() is None
        assert store.delete() is False

    def test_truncated_sidecar_rejected(self, ckpt_env):
        _, store = ckpt_env
        store.path.write_text('{"kind": "openx.turn_checkpoint", "ver',
                              encoding="utf-8")
        assert store.read() is None      # 不抛

    def test_oversize_is_refused_not_truncated(self, ckpt_env):
        _, store = ckpt_env
        huge = _record(snapshot=_snapshot(
            new_turn=[{"role": "tool", "content": "x" * (MAX_SNAPSHOT_BYTES + 1)}],
        ))
        assert store.write(huge) == REASON_OVERSIZE
        # 关键：正文没有被裁剪（宁可退化为"锚在上一轮"也不同源出错）
        assert len(huge.snapshot.new_turn[0]["content"]) > MAX_SNAPSHOT_BYTES

    def test_write_failure_returns_io_not_raises(self, ckpt_env):
        _, store = ckpt_env
        assert store.write(_record()) == REASON_OK
        os.chmod(store.path.parent, 0o500)
        try:
            assert store.write(_record(ledger_seq=9)) == REASON_IO
        finally:
            os.chmod(store.path.parent, 0o700)

    def test_delete_removes_and_second_delete_is_false(self, ckpt_env):
        _, store = ckpt_env
        store.write(_record())
        assert store.delete() is True
        assert store.delete() is False
        assert not store.path.exists()

    def test_ledger_reverse_scan_takes_last(self, ckpt_env):
        jsonl, _ = ckpt_env
        _write_ledger(jsonl, [1], extra=[
            {"seq": 2, "digest": "d2", "type": "checkpoint",
             "payload": {"type": "checkpoint", "phase": PHASE_COMMITTED,
                         "tool_rounds": 1, "reason": REASON_TOOL_ROUND}},
            {"seq": 3, "digest": "d3", "type": "checkpoint",
             "payload": {"type": "checkpoint", "phase": PHASE_COMMITTED,
                         "tool_rounds": 2, "reason": REASON_TOOL_ROUND}},
            "corrupt line, skipped",
        ])
        last = ledger_last_checkpoint(jsonl)
        assert last["seq"] == 3 and last["tool_rounds"] == 2
        assert ledger_envelope_count(jsonl) == 3
        assert ledger_last_checkpoint(jsonl.parent / "nope.jsonl") is None
        assert ledger_envelope_count(jsonl.parent / "nope.jsonl") == 0


# ── 裁决 ────────────────────────────────────────────────────────


class TestResumeVerdicts:
    """恢复裁决：每条不可用路径都退化为"普通恢复"，绝不挡住启动。"""

    def test_absent_when_no_checkpoint(self, ckpt_env):
        jsonl, _ = ckpt_env
        plan = resolve_resume(jsonl, **_base())
        assert plan.verdict is ResumeVerdict.ABSENT
        assert not plan.usable

    def test_ok_resumes_completed_round(self, ckpt_env):
        jsonl, store = ckpt_env
        store.write(_record())
        plan = resolve_resume(jsonl, **_base())
        assert plan.verdict is ResumeVerdict.OK, plan.detail
        assert plan.tool_rounds == 1
        assert len(plan.new_turn) == 3
        assert plan.repaired_calls == []

    def test_session_and_workspace_mismatch(self, ckpt_env):
        jsonl, store = ckpt_env
        store.write(_record(session_id="other"))
        assert (resolve_resume(jsonl, **_base()).verdict
                is ResumeVerdict.MISMATCH)
        store.write(_record(workspace="/elsewhere"))
        assert (resolve_resume(jsonl, **_base()).verdict
                is ResumeVerdict.MISMATCH)

    def test_digest_mismatch_is_torn(self, ckpt_env):
        jsonl, store = ckpt_env
        store.write(_record(snapshot_digest="deadbeef"))
        plan = resolve_resume(jsonl, **_base())
        assert plan.verdict is ResumeVerdict.TORN
        assert "digest" in plan.detail

    def test_reference_beyond_ledger_is_torn(self, ckpt_env):
        jsonl, store = ckpt_env
        store.write(_record(ledger_seq=99))     # 账本只有 2 条
        plan = resolve_resume(jsonl, **_base())
        assert plan.verdict is ResumeVerdict.TORN
        assert "ledger" in plan.detail

    def test_stale_checkpoint_is_already_complete(self, ckpt_env):
        """崩在 _persist_turn 与删旁挂文件之间：回合其实已经落盘。"""
        jsonl, store = ckpt_env
        store.write(_record())
        history = [
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "done"},     # history 变长 = 已落盘
        ]
        plan = resolve_resume(jsonl, **_base(history=history))
        assert plan.verdict is ResumeVerdict.ALREADY_COMPLETE
        assert not plan.usable

    def test_system_prompt_drift_is_torn(self, ckpt_env):
        jsonl, store = ckpt_env
        store.write(_record(snapshot=_snapshot(system_prompt_digest="aaa")))
        plan = resolve_resume(jsonl, **{**_base(), "system_prompt_digest": "bbb"})
        assert plan.verdict is ResumeVerdict.TORN
        assert "system prompt" in plan.detail
        # 摘要一致则放行
        plan = resolve_resume(jsonl, **{**_base(), "system_prompt_digest": "aaa"})
        assert plan.verdict is ResumeVerdict.OK

    def test_sidecar_lost_but_ledger_recorded_is_torn(self, tmp_path):
        """旁挂文件丢了、账本记过：快照体不在账本里，无法重建 -> 撕裂。"""
        jsonl = tmp_path / "s1.jsonl"
        _write_ledger(jsonl, [], extra=[
            {"seq": 1, "digest": "d1", "type": "checkpoint",
             "payload": {"type": "checkpoint", "phase": PHASE_COMMITTED,
                         "reason": REASON_TOOL_ROUND}},
        ])
        plan = resolve_resume(jsonl, **_base())
        assert plan.verdict is ResumeVerdict.TORN
        assert "sidecar" in plan.detail

    @pytest.mark.parametrize("reason", [REASON_TURN_END, REASON_SKIPPED])
    def test_terminal_ledger_reason_is_absent_not_torn(self, tmp_path, reason):
        """回合正常收口 / 快照被主动跳过：没有进展可恢复，别制造假警报。"""
        jsonl = tmp_path / "s1.jsonl"
        _write_ledger(jsonl, [], extra=[
            {"seq": 1, "digest": "d1", "type": "checkpoint",
             "payload": {"type": "checkpoint", "phase": PHASE_COMMITTED,
                         "reason": reason}},
        ])
        plan = resolve_resume(jsonl, **_base())
        assert plan.verdict is ResumeVerdict.ABSENT
        assert "terminal" in plan.detail


class TestInFlightRound:
    """在途轮：结果未知 -> 补合成结果，绝不重跑。"""

    def test_interrupted_call_gets_synthetic_result(self, ckpt_env):
        jsonl, store = ckpt_env
        snap = _snapshot(
            tool_rounds=1,
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
        store.write(_record(snapshot=snap, phase=PHASE_INFLIGHT))
        plan = resolve_resume(jsonl, **_base())
        assert plan.verdict is ResumeVerdict.OK, plan.detail
        assert plan.repaired_calls == ["t2"]
        # 在途轮算作已消耗（模型确实发过一次调用），下一轮不该重发
        assert plan.tool_rounds == 2
        last = plan.new_turn[-1]
        assert last["role"] == "tool" and last["tool_call_id"] == "t2"
        assert last["content"] == INTERRUPTED_TOOL_RESULT

    def test_half_written_parallel_batch_repairs_only_missing(self, ckpt_env):
        """并行批次只写回一半结果：缺哪个补哪个，已有的原样保留。"""
        jsonl, store = ckpt_env
        snap = _snapshot(
            tool_rounds=0, completed_tool_call_ids=["a"],
            new_turn=[
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "a", "type": "function",
                     "function": {"name": "x", "arguments": "{}"}},
                    {"id": "b", "type": "function",
                     "function": {"name": "y", "arguments": "{}"}},
                ]},
                {"role": "tool", "tool_call_id": "a", "content": "done"},
            ],
        )
        store.write(_record(snapshot=snap))
        plan = resolve_resume(jsonl, **_base())
        assert plan.verdict is ResumeVerdict.OK
        assert plan.repaired_calls == ["b"]
        kept = [m for m in plan.new_turn
                if m.get("role") == "tool" and m.get("tool_call_id") == "a"]
        assert kept and kept[0]["content"] == "done"

    def test_orphan_tool_result_is_torn(self, ckpt_env):
        """凭空出现的 tool 结果无法修复（会造出非法的消息序列）-> 撕裂。"""
        jsonl, store = ckpt_env
        snap = _snapshot(
            tool_rounds=0,
            new_turn=[{"role": "tool", "tool_call_id": "ghost", "content": "x"}],
        )
        store.write(_record(snapshot=snap))
        plan = resolve_resume(jsonl, **_base())
        assert plan.verdict is ResumeVerdict.TORN
        assert "orphan" in plan.detail

    def test_inflight_projection_drops_full_arguments(self):
        """入参只留长度：write_file 的 content 可能含整个文件，不能进快照。"""
        proj = inflight_projection([{
            "id": "t", "function": {"name": "write_file", "arguments": "x" * 5000},
        }])
        assert proj == [{"id": "t", "name": "write_file", "arguments_len": 5000}]
        assert inflight_projection(None) == []


class TestVerdictNeverRaises:
    """裁决本身出错也必须退化为"不可用"，绝不冒泡到启动路径。"""

    def test_garbage_path_yields_non_ok(self, tmp_path):
        plan = resolve_resume(tmp_path / "missing.jsonl", **_base())
        assert not plan.usable
