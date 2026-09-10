"""容灾（recovery）——回合级 checkpoint 与中断恢复的**机制**面。

内核给机制，消费方给策略（与 ``Guard`` 同款分工）：本包只负责"快照长什么样、
怎么原子落盘、磁盘上那份还能不能用"，**不认识 agent、不进事件循环**。
"何时快照、快照什么"住在 ``openx/services/checkpoint.py``--它才知道
``state.messages`` / ``new_turn`` / ``tool_rounds``。

```
model.py    快照与记录的纯数据模型（schema 版本、摘要可复算）
store.py    旁挂文件：原子写（tmp→fsync→replace→fsync dir）、倒序扫账本
resume.py   恢复裁决：OK / ABSENT / ALREADY_COMPLETE / TORN / MISMATCH
```

**落点**：``~/.openx/sessions/<workspace_hash>/<session_id>.ckpt.json``，
与会话账本 ``<session_id>.jsonl`` 同目录。旁挂文件存**快照体**（覆盖式最新态），
账本存**事实**（``checkpoint`` 事件的 seq + 快照摘要），两者用摘要互指--
于是"旁挂被改过"与"账本被改过"都能被发现。

**为什么需要它**：会话持久化只在回合边界发生（``agent._persist_turn``），
且明确不在关键路径上。进程崩溃、断电、Ctrl-C 打断一个正在跑工具的长回合，
这一轮进展全部丢失。本模块把可续跑的状态按**工具轮**推进落盘，重启后从最近
的提交点继续，**已完成的工具调用不重放**。

**不重放的根据**：快照原样携带本轮消息（``new_turn``），恢复后重入循环时
每个 ``tool_call`` 都已有配对的 ``tool`` 结果消息--消息日志本身就是幂等单元，
循环里没有"跳过表"。工具执行**中途**崩溃时（``phase=inflight``），那些调用的
结果未知，``resume.py`` 给它们补一条 ``[status: interrupted]`` 的合成结果，
**绝不重跑**：不重放是硬承诺，不因猜测而重复产生副作用。
"""

from .model import (
    CHECKPOINT_KIND,
    CHECKPOINT_VERSION,
    PHASE_COMMITTED,
    PHASE_INFLIGHT,
    REASON_ESC,
    REASON_GATE_TRIPPED,
    REASON_SIGNAL,
    REASON_SKIPPED,
    REASON_TOOL_ROUND,
    REASON_TURN_END,
    CheckpointRecord,
    TurnSnapshot,
    record_from_json,
    record_to_json,
    snapshot_digest,
)
from .resume import (
    INTERRUPTED_TOOL_RESULT,
    ResumePlan,
    ResumeVerdict,
    inflight_projection,
    resolve_resume,
)
from .store import (
    MAX_SNAPSHOT_BYTES,
    REASON_IO,
    REASON_OK,
    REASON_OVERSIZE,
    SIDECAR_SUFFIX,
    CheckpointStore,
    ledger_envelope_count,
    ledger_last_checkpoint,
    sidecar_path,
)

__all__ = [
    "CHECKPOINT_KIND",
    "CHECKPOINT_VERSION",
    "MAX_SNAPSHOT_BYTES",
    "PHASE_COMMITTED",
    "PHASE_INFLIGHT",
    "REASON_ESC",
    "REASON_GATE_TRIPPED",
    "REASON_IO",
    "REASON_OK",
    "REASON_OVERSIZE",
    "REASON_SIGNAL",
    "REASON_SKIPPED",
    "REASON_TOOL_ROUND",
    "REASON_TURN_END",
    "SIDECAR_SUFFIX",
    "INTERRUPTED_TOOL_RESULT",
    "CheckpointRecord",
    "CheckpointStore",
    "ResumePlan",
    "ResumeVerdict",
    "TurnSnapshot",
    "inflight_projection",
    "ledger_envelope_count",
    "ledger_last_checkpoint",
    "record_from_json",
    "record_to_json",
    "resolve_resume",
    "sidecar_path",
    "snapshot_digest",
]
