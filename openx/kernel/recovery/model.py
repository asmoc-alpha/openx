"""容灾 · 检查点模型——可续跑的回合快照（纯数据，无 IO、不认识 agent）。

**为什么需要它**：会话持久化（``orchestration/sessions.py``）只发生在**回合
边界**（``agent._persist_turn``），且明确吞掉异常、不在关键路径上。进程崩溃、
断电、Ctrl-C 打断一个正在跑工具的长回合，这一轮的全部进展直接丢失--已完成
20 个工具调用的回合，第 21 个崩溃就归零。

回合级 checkpoint 要解决的就是这段窗口：每个工具轮之后把**可续跑的状态**
落盘，重启后从最近的提交点继续，**已完成的工具调用不重放**。

**为什么快照必须自带消息体**：``tool_use`` / ``tool_result`` 从不进账本
（它们只下行给端展示），所以账本尾部**无法**推断哪些工具调用已完成。
快照因此必须原样携带本轮消息（``new_turn``），这也是"不重放"的根据--
消息日志本身就是幂等单元（每个 ``tool_call`` 都已有一条配对的 ``tool`` 结果
消息），恢复后重入循环时模型不可能再发起已应答的调用。

**保真纪律**：快照必须与 provider 当时看到的**逐字节一致**。``content``
可能是富 parts 列表，不得折叠、不得截断--一旦规范化，恢复后重建的 prompt
就变了，模型行为随之漂移。体积超限时**拒绝落盘**而不是裁剪（见 store.py）。
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

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# schema 版本：不识别即弃（checkpoint 是优化，宁可丢也不能拖垮启动）
CHECKPOINT_VERSION = 1

# 文件标识：与 ``kind`` 同名，防误读他类 JSON
CHECKPOINT_KIND = "openx.turn_checkpoint"

# phase：持久化状态。inflight = 工具执行中（结果未知）；committed = 某轮已收口。
PHASE_INFLIGHT = "inflight"
PHASE_COMMITTED = "committed"

# reason：谁触发的这次落盘（审计用，不参与恢复裁决）
REASON_TOOL_ROUND = "tool_round"
REASON_GATE_TRIPPED = "gate_tripped"
REASON_ESC = "esc"
REASON_SIGNAL = "signal"
REASON_TURN_END = "turn_end"
REASON_SKIPPED = "skipped"

# 结构化输出哨兵：``agent._structured_result`` 的"未设置"态不能与 JSON null
# 混同，故快照单带一个布尔位，值本身只在已设置时参与序列化。
_NO_STRUCTURED = object()

# 容器字段的期望形状：反序列化时按此校验。显式列出而非读 dataclass 元数据--
# ``field(default_factory=list)`` 的 ``.default`` 是 MISSING，反射拿不到类型。
_LIST_FIELDS = ("new_turn", "completed_tool_call_ids", "inflight", "todos")
_DICT_FIELDS = ("counters",)


@dataclass
class TurnSnapshot:
    """一个进行中回合的可续跑状态（不含 system prompt 与历史--那些能重建）。

    字段按"能否从别处重建"取舍：``system_prompt`` 不存（从配置重建，只存
    摘要校验），``history`` 不存（在会话文件里），只存**本回合独有**的部分。
    """

    user_message: Any = None
    """本轮用户消息（``new_turn[0]`` 的 content）。富 parts 原样保留。"""

    engine: str = "stream_run"
    """哪个循环写的：``stream_run``（REPL/serve 主路径）或 ``run``（非流式）。"""

    modal: bool = False
    """``turn_llm`` 是否走了 ``client_for("modal")``。

    整轮固定同一客户端（tool-call 序列对 provider 格式敏感），恢复时必须
    按原选择重建，否则多模回合会中途换 provider。"""

    system_prompt_digest: str = ""
    """系统提示摘要。插件在轮间可能装卸，提示前缀变了就不该续跑旧快照
    （发出去的 prefix 不同 = 另一局对话）。"""

    tool_rounds: int = 0
    """已完成的工具轮数（``state.tool_rounds``）。恢复后循环从此续起。"""

    history_len: int = 0
    """回合开始时 ``len(history.messages)``。

    这是**陈旧判定**的精确依据：``history.add(new_turn)`` 只在回合结束时
    整体追加，故回合进行中 ``len(history) == history_len``，回合结束后
    ``len(history) > history_len``。据此可判定"这个 checkpoint 对应回合
    其实已经正常落盘了"（崩溃在 persist 与删除旁挂文件之间）。"""

    new_turn: list[dict[str, Any]] = field(default_factory=list)
    """本回合已产生、尚未并入 history 的消息（assistant tool_calls + tool 结果）。"""

    completed_tool_call_ids: list[str] = field(default_factory=list)
    """已完成轮次的 tool_call id（**仅用于一致性校验**，不用于跳过）。

    循环里没有"跳过表"逻辑--消息日志就是幂等单元。这份 id 表的作用是让
    恢复裁决能断言"每个 id 都有配对的消息"，不成立即判撕裂。"""

    inflight: list[dict[str, Any]] = field(default_factory=list)
    """在途调用的投影 ``[{id, name, arguments}]``，仅 ``phase=inflight`` 时非空。

    这些调用的副作用**结果未知**（可能已写文件、已发请求）。恢复时绝不重跑，
    而是合成一条 ``[status: interrupted]`` 的 tool 结果让模型看到--
    保住"不重放"的承诺，也不因猜测而重复产生副作用。"""

    todos: list[dict[str, Any]] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
    """四个 token 累计：input / output / cached / plugin。"""

    last_tool_rounds: int = 0

    has_structured_result: bool = False
    structured_result: Any = None


@dataclass
class CheckpointRecord:
    """旁挂文件的完整内容：簿记字段 + 快照体。

    ``ledger_seq`` / ``ledger_digest`` 是一对**双向绑定**：快照指向账本里
    那条 ``checkpoint`` 事件，事件里的 ``snapshot_digest`` 又指回快照。
    于是"旁挂文件被改过"和"账本被改过"都能被发现--可审计不是口号，
    是这里可复算的摘要。
    """

    version: int = CHECKPOINT_VERSION
    kind: str = CHECKPOINT_KIND
    session_id: str = ""
    workspace: str = ""
    ledger_seq: int = 0
    ledger_digest: str = ""
    cause: Optional[int] = None
    phase: str = PHASE_COMMITTED
    reason: str = REASON_TOOL_ROUND
    created_at: float = 0.0
    snapshot_digest: str = ""
    snapshot: Optional[TurnSnapshot] = None


def snapshot_payload(snapshot: TurnSnapshot) -> dict[str, Any]:
    """快照的规范 dict 形态：写盘与算摘要**共用同一份变换**。

    两处必须一致，否则"存进去的内容"和"摘要覆盖的内容"会悄悄分叉：
    ``_NO_STRUCTURED`` 哨兵经 ``default=str`` 会变成一串对象表示，读回来
    复算的摘要就对不上，每次恢复都会被误判为撕裂。故哨兵在此统一折成
    ``None``，两个消费方都只能从这里取。
    """
    payload = asdict(snapshot)
    if snapshot.structured_result is _NO_STRUCTURED:
        payload["structured_result"] = None
    return payload


def snapshot_digest(snapshot: TurnSnapshot) -> str:
    """快照内容摘要：h(canonical(snapshot))。

    与账本信封的 digest 同法（键排序、紧凑分隔、``default=str``），
    使得"同一份快照"在任何进程/任何时间都算出同一个值。
    """
    return hashlib.sha256(
        _canonical(snapshot_payload(snapshot)).encode("utf-8")
    ).hexdigest()


def _canonical(obj: Any) -> str:
    """规范序列化：排序键 + 紧凑分隔 + 非 JSON 类型降级为 str。"""
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    )


def record_to_json(record: CheckpointRecord) -> str:
    """序列化整条记录（写入旁挂文件用）。

    快照体走 :func:`snapshot_payload`，与摘要计算同源--否则落盘内容与摘要
    覆盖的内容会分叉，读回来的摘要永远对不上。
    """
    payload = asdict(record)
    if record.snapshot is not None:
        payload["snapshot"] = snapshot_payload(record.snapshot)
    return _canonical(payload)


def record_from_json(raw: str) -> Optional[CheckpointRecord]:
    """反序列化；任何畸形/不识别一律返回 None（调用方记日志后退化）。

    容忍是本模块的纪律：checkpoint 是**优化**，坏文件绝不能挡住启动--
    与 ``SessionStore.load`` 跳过损坏行同构。
    """
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("kind") != CHECKPOINT_KIND:
        return None
    version = obj.get("version")
    if not isinstance(version, int) or version != CHECKPOINT_VERSION:
        return None
    snap_obj = obj.get("snapshot")
    if not isinstance(snap_obj, dict):
        return None
    snap = TurnSnapshot()
    for key in TurnSnapshot.__dataclass_fields__:
        if key not in snap_obj:
            continue
        value = snap_obj[key]
        if key == "structured_result":
            if snap.has_structured_result:
                snap.structured_result = value
            continue
        # 形状校验：容器字段拿到错类型即判脏，整体弃（不半信半疑地拼）
        if key in _LIST_FIELDS and not isinstance(value, list):
            return None
        if key in _DICT_FIELDS and not isinstance(value, dict):
            return None
        setattr(snap, key, value)
    record = CheckpointRecord(
        version=version,
        kind=CHECKPOINT_KIND,
        session_id=str(obj.get("session_id") or ""),
        workspace=str(obj.get("workspace") or ""),
        ledger_seq=_as_int(obj.get("ledger_seq")),
        ledger_digest=str(obj.get("ledger_digest") or ""),
        cause=_as_int(obj.get("cause"), default=None),
        phase=str(obj.get("phase") or PHASE_COMMITTED),
        reason=str(obj.get("reason") or REASON_TOOL_ROUND),
        created_at=_as_float(obj.get("created_at")),
        snapshot_digest=str(obj.get("snapshot_digest") or ""),
        snapshot=snap,
    )
    return record


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


if __name__ == "__main__":
    # 自检：往返保真 + 摘要可复算 + 畸形/版本不符一律 None
    snap = TurnSnapshot(
        user_message="hi",
        tool_rounds=2,
        new_turn=[{"role": "tool", "tool_call_id": "t1", "content": "ok"}],
        completed_tool_call_ids=["t1"],
        counters={"input": 10, "output": 5},
    )
    digest = snapshot_digest(snap)
    record = CheckpointRecord(
        session_id="s1", workspace="/ws", ledger_seq=7,
        snapshot_digest=digest, snapshot=snap,
    )
    raw = record_to_json(record)
    back = record_from_json(raw)
    assert back is not None
    assert back.session_id == "s1" and back.ledger_seq == 7
    assert back.snapshot.tool_rounds == 2
    assert back.snapshot.completed_tool_call_ids == ["t1"]
    # 摘要稳定：同一份快照任何时候都算出同一个值
    assert snapshot_digest(back.snapshot) == digest
    # 内容变了摘要就变
    changed = TurnSnapshot(**{**asdict(snap), "tool_rounds": 3})
    assert snapshot_digest(changed) != digest
    # 畸形 / 版本不符 / 缺快照 → None（绝不抛）
    assert record_from_json("not json") is None
    assert record_from_json('{"kind": "openx.turn_checkpoint"}') is None
    assert record_from_json(json.dumps({
        "kind": CHECKPOINT_KIND, "version": 999, "snapshot": {},
    })) is None
    assert record_from_json(json.dumps({
        "kind": CHECKPOINT_KIND, "version": 1, "snapshot": None,
    })) is None
    # 容器字段类型错 → 整体弃
    assert record_from_json(json.dumps({
        "kind": CHECKPOINT_KIND, "version": 1,
        "snapshot": {"new_turn": "oops"},
    })) is None
    # 结构化输出哨兵：未设置时不落值
    s2 = TurnSnapshot(has_structured_result=False, structured_result=_NO_STRUCTURED)
    assert record_from_json(record_to_json(
        CheckpointRecord(snapshot=s2)
    )).snapshot.structured_result is None
    print("openx/kernel/recovery/model.py OK ✓")
