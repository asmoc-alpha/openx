"""会话协议 P1 —— 线格式 schema 的单一真源（标准三载体）。

下行事件与既有 stream-json 输出**逐字段一致**（存量消费者零改动），
``init`` 增 ``protocol_version``；新增 ``permission_request`` 下行。
上行 P1 只收 ``permission_response``；未知类型容忍（前向兼容，回报
``UplinkUnknown``），畸形行返回 ``None`` 由调用方记日志。

serve 扩展（P4，additive、版本不变）：下行新增 ``history`` 会话快照与
``result`` 终局事件；``permission_request`` 增 ``can_remember`` 可选字段
（Web 弹窗显示"记住"选项）；上行新增 ``message`` / ``interrupt`` 意图。
headless 的 ``_NdjsonPermissionBridge`` 按 isinstance 忽略新类型，向后兼容。

事件信封（``Event``）：协议 = 账本的外化--内核 ``emit()`` 记账的信封
条目去掉簿记字段就是下行事件；回放 = 把存储的事件再发一遍，复盘回放
与实时观看共用同一 schema。

设计纪律：核心 schema 是内核不变量——插件/客户端只能加命名空间扩展
字段，不能改核心字段；版本演进规则收敛在 :func:`negotiate` 一处。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional, Union

# P1：严格相等。未来minor 演进时在此放宽（如 client <= server 且同 major）。
PROTOCOL_VERSION = 1

# 上行 message 的 attachments（上传 id）上限：图/文件数太多会撑爆单轮
# 上下文，也防脏值撑开解析缓存。单个 id 超 64 视为脏值丢弃。
_MAX_ATTACHMENTS = 12


# ── 事件信封（K2a）：账本条目 = 协议下行事件的超集 ────────────────


@dataclass
class Event:
    """事件信封：内核记账（emit）的唯一条目形态。

    信封 = 协议事件（``payload``，自含 type，下行原样发出）+ 簿记字段
    （seq/ts/session/digest）+ 因果与归因（cause/origin）。协议 = 账本的
    外化：下行就是信封的 payload 投影，两者共用同一 schema，不另造格式。
    """

    seq: int                      # 会话内单调递增（attach_ledger 时续起）
    ts: float
    session: str
    type: str                     # 事件族：text_delta / tool_use / registered / ...
    payload: dict[str, Any]       # 完整协议事件（自含 type）
    cause: Optional[int] = None   # 因果前驱 seq（tool_result.cause = tool_use.seq）
    origin: str = "kernel"        # 归因：user | model | plugin:<id> | kernel
    digest: str = ""              # h(prev_digest || canonical(本条))；只填不校验

    def to_line(self) -> dict[str, Any]:
        """账本行：信封全字段（会话 JSONL 的一行）。"""
        return {
            "seq": self.seq,
            "ts": self.ts,
            "session": self.session,
            "type": self.type,
            "payload": self.payload,
            "cause": self.cause,
            "origin": self.origin,
            "digest": self.digest,
        }


def project(event: Event) -> dict[str, Any]:
    """下行投影：信封 -> 协议事件（payload 原样，存量消费者零改动）。"""
    return dict(event.payload)


def canonical_event(event: Event) -> str:
    """规范序列化（键排序、紧凑分隔、不含 digest）--digest 计算与审计
    比对共用。摘要不覆盖自身：digest 字段不参与本条摘要。"""
    line = event.to_line()
    line.pop("digest", None)
    return json.dumps(
        line, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    )


def digest_of(prev_digest: str, event: Event) -> str:
    """轻量哈希链：h(prev || canonical(event))。

    强度取舍：目标是事后审计*可发现*，不是密码学对抗--单链摘要即可，
    不引签名、不引外部信任锚。P1 只填不校验；校验工具随 K5 全局账本。
    """
    blob = (prev_digest + canonical_event(event)).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# ── 下行（server → client）──────────────────────────────────────

def init_event(session_id: str, model: str, tools: list[str]) -> dict[str, Any]:
    """开场事件：存量字段 + protocol_version。"""
    return {
        "type": "system",
        "subtype": "init",
        "protocol_version": PROTOCOL_VERSION,
        "session_id": session_id,
        "model": model,
        "tools": tools,
    }


def text_delta(text: str) -> dict[str, Any]:
    return {"type": "text_delta", "text": text}


def thinking_delta(text: str) -> dict[str, Any]:
    return {"type": "thinking_delta", "text": text}


def tool_use(name: str, args_summary: str = "", target: str = "") -> dict[str, Any]:
    """一次工具调用开始（可选携带**派生**的展示字段）。

    - ``args_summary``：一行人类可读摘要（shell 前三词 / 路径 / 搜索模式）；
    - ``target``：目标型工具的主要路径（读、写、glob、列目录等），供 Web
      右栏「上下文」面板收录；非目标型工具为空串。

    两字段由 serve 从工具入参**派生**后下发，**不原样回传入参**——
    ``write_file`` 的 ``content`` 可能含整个文件，会把每条下行事件撑大；
    产物路径另走 ``artifact`` 单发事件。可选、默认空串——既有 NDJSON
    消费者零改动（headless stream-json 不传即不带信息）。
    """
    return {
        "type": "tool_use",
        "name": name,
        "args_summary": args_summary,
        "target": target,
    }


def artifact(path: str, tool: str) -> dict[str, Any]:
    """serve 下行：一次写工具产出的文件（产物面板的增量源）。

    OpenX 内核没有 artifact 概念，产物是**读侧派生**：serve 从写类工具的
    入参抽 path 后单发此事件。不塞进 ``tool_use``——``write_file`` 的入参
    可能含整个文件内容，会把每条下行事件撑大；产物面板只需要路径。
    """
    return {"type": "artifact", "path": path, "tool": tool}


def tool_result(name: str, is_error: bool, output: str) -> dict[str, Any]:
    return {"type": "tool_result", "name": name, "is_error": is_error, "output": output}


def permission_request(
    request_id: str,
    tool: str,
    reason: str,
    details: str = "",
    can_remember: bool = True,
) -> dict[str, Any]:
    """权限请求下行：上行以同 request_id 的 permission_response 应答。

    ``can_remember`` 是 serve 扩展字段（Web 弹窗据此显示"记住"选项）：
    手动模式逐项授权时 False。可选、默认 True——既有 NDJSON 消费者零改动。
    """
    return {
        "type": "permission_request",
        "request_id": request_id,
        "tool": tool,
        "reason": reason,
        "details": details,
        "can_remember": can_remember,
    }


# ── serve 扩展（P4）：会话快照与终局事件（协议 = 账本外化的服务端应用）─

def user_message(text: str, content: Any = None) -> dict[str, Any]:
    """serve 下行：一条用户消息（live 广播 / attach 快照共用）。

    ``content``：带图片/文件时的多模态 parts 列表（serve 扩展，可选）——
    前端据此渲染缩略图与文件 chip。不带则事件与纯文本版完全一致
    （无 ``content`` 键），存量消费者零改动。
    """
    ev = {"type": "user_message", "text": text}
    if content is not None:
        ev["content"] = content
    return ev


def message_ack(msg_id: str) -> dict[str, Any]:
    """serve 下行：上行 ``message`` 的**回执**（收到即回，非回合开始）。

    至少一次投递的闭环：客户端带 ``msg_id`` 发消息、持有到回执为止；
    断连重连后重发，服务端按 ``msg_id`` 去重（回合不跑两遍）并**再次
    回执**让客户端清掉待决条目。回执只在 uplink 解析处发出、不经回合
    队列，对客户端是即时的——据此可实现半开连接探测（发了消息迟迟无
    回执 = 连接已死）。其它客户端收到未知 ``msg_id`` 的回执是 no-op
    （reducer 未知事件容忍），重放无副作用。
    """
    return {"type": "message_ack", "msg_id": msg_id}


def serve_history(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """attach 会话快照下行：新客户端连上时补发既有对话。

    ``messages`` 为渲染段列表 ``[{role, content, ...}]``（agent.history 或
    会话文件回放的行）；端是哑渲染器，只按序渲染，不持会话状态语义。
    """
    return {"type": "history", "messages": messages}


def serve_todos(todos: list[dict[str, Any]]) -> dict[str, Any]:
    """serve 下行：执行计划（todo_write 维护的任务清单）全量快照。

    计划是**状态**而非增量——``todo_write`` 每次全量覆盖，故下行也发全量，
    端收到即整体替换（与 CLI 读 ``agent.todos`` 同源）。快照随
    ``todo_write`` 的 tool_result 触发，并在 attach 时补发（迟到客户端
    拿到当前计划）。
    """
    return {"type": "todos", "todos": todos}


def serve_fleet(agents: list[dict[str, Any]]) -> dict[str, Any]:
    """serve 下行：子 agent（task 工具委派）运行态快照。

    ``agents = [{id, label, subagent_type, status, tools_count, elapsed}]``
    ——源自 ``FleetMonitor.snapshot()`` 的投影（不带行缓冲：展示只需
    状态与活跃度，转录行会把事件面撑大）。回合内由 ticker 变化才广播，
    attach 时补发当前快照。
    """
    return {"type": "fleet", "agents": agents}


def serve_panels(panels: list[dict[str, Any]]) -> dict[str, Any]:
    """serve 下行：插件 UI 面板快照（ui/v1，web 常驻面板）。

    ``panels = [{"name": str, "lines": [str, ...]}]``——行已剥 rich 标签
    （与 text_delta 同款），端是哑渲染器只按行渲染纯文本；空列表 = 面板
    全部消失（端清空面板区）。ticker 变化才广播（动画帧即天然变化源）。
    """
    return {"type": "panels", "panels": panels}


def serve_ask_user(
    request_id: str,
    question: str,
    options: list[dict[str, Any]],
    multi_select: bool = False,
) -> dict[str, Any]:
    """serve 下行：交互式提问（ask_user，P4.1 交互化）。

    ``options = [{"label": str, "description": str}]``（label 是回传值）；
    ``multi_select`` 时端允许多选。上行以同 ``request_id`` 的
    ``ask_user_response`` 应答；超时服务端落保守默认（端展示倒计时提示
    由实现自理，协议不携带）。
    """
    return {
        "type": "ask_user",
        "request_id": request_id,
        "question": question,
        "options": [
            {
                "label": o.get("label", ""),
                "description": o.get("description", ""),
            }
            if isinstance(o, dict) else {"label": str(o), "description": ""}
            for o in options
        ],
        "multi_select": bool(multi_select),
    }


def serve_plan_request(request_id: str, plan: str = "") -> dict[str, Any]:
    """serve 下行：计划审批请求（P4.1 交互化）。

    上行以同 ``request_id`` 的 ``plan_response`` 应答；超时按拒绝。
    """
    return {
        "type": "plan_request",
        "request_id": request_id,
        "plan": plan,
    }


def result_event(
    result: str | None,
    is_error: bool,
    duration_ms: int,
    num_turns: int,
    session_id: str,
    usage: dict[str, Any],
    error: str = "",
) -> dict[str, Any]:
    """单轮终局事件：镜像 single_shot 的 result 形状（同 schema）。

    供 serve 广播与回放共用；``error`` 非空时对应 ``subtype="error"``。
    """
    return {
        "type": "result",
        "subtype": "error" if is_error else "success",
        "is_error": is_error,
        "duration_ms": duration_ms,
        "num_turns": num_turns,
        "result": result,
        "session_id": session_id,
        "usage": usage,
        **({"error": error} if error else {}),
    }


# ── 容灾（recovery）：回合级 checkpoint 与控制事件（内核第六件）─────
#
# 事件族归属（openx-kernel-design §3.2）：全部落**控制族**（会话账本）。
# 与转录族的分工：转录说"发生了什么"，控制说"为什么停下/为什么能继续"。


def checkpoint_event(
    *,
    phase: str,
    reason: str,
    tool_rounds: int,
    gate: str = "",
    completed_tool_calls: list[str] | None = None,
    snapshot_digest: str = "",
    snapshot_bytes: int = 0,
    repaired_calls: list[str] | None = None,
) -> dict[str, Any]:
    """一次 checkpoint 提交的**事实**（快照体在旁挂文件里，不在这里）。

    账本只存元信息是刻意的：``iter_events`` 会把 payload 原样喂给 Web 回放，
    把几百 KB 的工具输出塞进事件流会让每次回放都被撑爆。``snapshot_digest``
    与旁挂文件里的同名字段互为绑定--两侧数据谁被改过都能被发现。

    ``phase``：``committed``（某轮已收口）/ ``inflight``（工具执行中）。
    ``reason``：谁触发的（tool_round / gate_tripped / esc / signal / turn_end）。
    """
    return {
        "type": "checkpoint",
        "phase": phase,
        "reason": reason,
        "tool_rounds": int(tool_rounds),
        "gate": gate,
        "completed_tool_calls": list(completed_tool_calls or []),
        "snapshot_digest": snapshot_digest,
        "snapshot_bytes": int(snapshot_bytes),
        "repaired_calls": list(repaired_calls or []),
    }


def interrupt_event(
    kind: str,
    checkpoint_seq: int = 0,
    tool_rounds: int = 0,
) -> dict[str, Any]:
    """一次中断（SIGINT / SIGTERM / Esc / 客户端）及其落盘结果。

    ``kind``：``sigint`` / ``sigterm`` / ``esc`` / ``client``。
    ``checkpoint_seq``：中断时落下的 checkpoint 事件 seq；**0 表示没落成**
    （写盘失败或二次信号跳过 flush）--"记了没执行"可接受，"执行了没记"不行，
    故这里如实记 0 而不是省略字段。
    """
    return {
        "type": "interrupt",
        "kind": kind,
        "checkpoint_seq": int(checkpoint_seq),
        "tool_rounds": int(tool_rounds),
    }


def resource_gate_tripped(
    gate: str,
    limit: int,
    rounds: int,
    checkpoint_seq: int = 0,
) -> dict[str, Any]:
    """资源闸触顶（内核 §2.3）：触顶即记账、即停止，且**可续跑**。

    与中断的区别：中断是外部事件，触顶是内生边界。共同点是都要先落
    checkpoint 再停--这样触顶从"回合丢失"变成"可从该点继续"。
    """
    return {
        "type": "resource_gate_tripped",
        "gate": gate,
        "limit": int(limit),
        "rounds": int(rounds),
        "checkpoint_seq": int(checkpoint_seq),
    }


def turn_started(
    session_id: str,
    history_len: int,
    resumed: bool = False,
) -> dict[str, Any]:
    """一个回合开始（``resumed`` 标记它是从 checkpoint 接续的）。

    这是"某个回合曾经开着"的无状态证据：即使没有任何 checkpoint 落盘
    （比如崩在第一个模型请求期间），账本尾部也能看出上一回合没跑完。
    """
    return {
        "type": "turn_started",
        "session_id": session_id,
        "history_len": int(history_len),
        "resumed": bool(resumed),
    }


def resume_event(
    verdict: str,
    checkpoint_seq: int = 0,
    tool_rounds: int = 0,
    repaired: int = 0,
    detail: str = "",
) -> dict[str, Any]:
    """一次恢复裁决的结论（含被拒的那些--弃用也要留痕）。

    ``verdict`` 取值同 ``recovery.ResumeVerdict``；非 ``ok`` 的记录是审计
    的关键：它解释"为什么这一轮没被恢复"。
    """
    return {
        "type": "resume",
        "verdict": verdict,
        "checkpoint_seq": int(checkpoint_seq),
        "tool_rounds": int(tool_rounds),
        "repaired": int(repaired),
        "detail": detail,
    }


def checkpoint_discarded(reason: str, checkpoint_seq: int = 0) -> dict[str, Any]:
    """checkpoint 被丢弃（陈旧 / 撕裂 / 身份不符）：弃用本身也是决策。"""
    return {
        "type": "checkpoint_discarded",
        "reason": reason,
        "checkpoint_seq": int(checkpoint_seq),
    }


# ── 上行（client → server）──────────────────────────────────────

@dataclass
class PermissionResponse:
    """对 permission_request 的裁决：allowed=False 等同弹窗里拒绝。

    ``remember``：serve 扩展字段——"允许并记住"（落盘存储规则）；headless
    的 ``_NdjsonPermissionBridge`` 只读 ``allowed``，增量兼容。
    """

    request_id: str
    allowed: bool
    remember: bool = False


@dataclass
class AskUserResponse:
    """对 ask_user（交互提问）的回答：answers 是所选 label（或自由文本）。

    单选传一个元素；多选传多个。bridge 侧按提问时的 request_id 匹配，
    未匹配/已决一律忽略（同 permission_response 纪律）。
    """

    request_id: str
    answers: list[str]


@dataclass
class PlanResponse:
    """对 plan_request（计划审批）的裁决：approved=False 等同拒绝。"""

    request_id: str
    approved: bool


@dataclass
class UserMessage:
    """用户发送的聊天消息（serve 上行意图：message）。

    ``msg_id``：客户端生成的消息标识（serve 扩展字段，可选）——服务端
    按它去重（重连重发不跑两遍）并即时回 ``message_ack``。空串 = 存量
    客户端未带，不参与去重/回执，行为与扩展前完全一致。

    ``attachments``：随消息上传的图片/文件 id 列表（serve 扩展字段，可选；
    由 /api/upload 预先上传、此处引用）——服务端据注册表解析成多模态
    content。空列表 = 纯文本消息，走原路径零改动。
    """

    text: str
    msg_id: str = ""
    attachments: list[str] = field(default_factory=list)


@dataclass
class Interrupt:
    """打断当前回合（serve 上行意图：interrupt；Esc 的 Web 等价物）。"""


@dataclass
class UplinkUnknown:
    """未知上行类型：容忍不炸（前向兼容），调用方可记日志。"""

    type: str


UplinkMessage = Union[
    PermissionResponse,
    AskUserResponse,
    PlanResponse,
    UserMessage,
    Interrupt,
    UplinkUnknown,
]


def parse_uplink(line: str) -> Optional[UplinkMessage]:
    """解析一行上行 NDJSON；畸形 → None（调用方记日志，不断流）。"""
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    kind = obj.get("type")
    if kind == "permission_response":
        request_id = obj.get("request_id")
        if not isinstance(request_id, str):
            return None
        return PermissionResponse(
            request_id,
            bool(obj.get("allowed", False)),
            bool(obj.get("remember", False)),
        )
    if kind == "message":
        text = obj.get("text")
        if not isinstance(text, str):
            return None
        # msg_id 清洗：非串 / 超长（防脏值撑爆去重缓存键）→ 视为未带
        msg_id = obj.get("msg_id", "")
        if not isinstance(msg_id, str) or len(msg_id) > 128:
            msg_id = ""
        # attachments 清洗：仅非空短 str、去重、封顶（防脏值撑大单轮上下文）
        attachments: list[str] = []
        raw_att = obj.get("attachments")
        if isinstance(raw_att, list):
            for att in raw_att:
                if len(attachments) >= _MAX_ATTACHMENTS:
                    break
                if (
                    isinstance(att, str) and att
                    and len(att) <= 64 and att not in attachments
                ):
                    attachments.append(att)
        return UserMessage(text, msg_id, attachments)
    if kind == "ask_user_response":
        request_id = obj.get("request_id")
        answers = obj.get("answers")
        if not isinstance(request_id, str):
            return None
        if isinstance(answers, str):
            answers = [answers]
        if not isinstance(answers, list) or not all(
            isinstance(a, str) and a for a in answers
        ):
            return None
        return AskUserResponse(request_id, answers)
    if kind == "plan_response":
        request_id = obj.get("request_id")
        if not isinstance(request_id, str):
            return None
        return PlanResponse(request_id, bool(obj.get("approved", False)))
    if kind == "interrupt":
        return Interrupt()
    if isinstance(kind, str):
        return UplinkUnknown(kind)
    return None


def negotiate(client_version: int) -> bool:
    """能力协商 P1：严格相等；演进规则改这一处。"""
    return client_version == PROTOCOL_VERSION


if __name__ == "__main__":
    # 上行解析：message / interrupt / permission_response / 未知 / 畸形
    _m = parse_uplink('{"type": "message", "text": "hello"}')
    assert isinstance(_m, UserMessage) and _m.text == "hello"
    # msg_id：合法 / 缺失 / 非串 / 超长（至少一次投递的去重键）
    _m2 = parse_uplink('{"type": "message", "text": "hi", "msg_id": "m1"}')
    assert isinstance(_m2, UserMessage) and _m2.msg_id == "m1"
    assert parse_uplink('{"type": "message", "text": "hi"}').msg_id == ""
    assert parse_uplink('{"type": "message", "text": "hi", "msg_id": 5}').msg_id == ""
    _long = json.dumps({"type": "message", "text": "hi", "msg_id": "x" * 200})
    assert parse_uplink(_long).msg_id == ""
    # attachments：合法列表 / 脏值清洗（非串、超长、重复、超上限）
    _ma = parse_uplink('{"type": "message", "text": "hi", "attachments": ["u1", "u2"]}')
    assert _ma.attachments == ["u1", "u2"]
    _ma2 = parse_uplink(json.dumps({
        "type": "message", "text": "hi",
        "attachments": ["u1", 5, "", "x" * 90, "u1", "u2"],
    }))
    assert _ma2.attachments == ["u1", "u2"], _ma2.attachments
    _many = json.dumps({"type": "message", "text": "hi",
                        "attachments": [f"u{i}" for i in range(30)]})
    assert len(parse_uplink(_many).attachments) == _MAX_ATTACHMENTS
    assert parse_uplink('{"type": "message", "text": "hi", "attachments": "u1"}').attachments == []
    assert isinstance(parse_uplink('{"type": "interrupt"}'), Interrupt)
    _p = parse_uplink('{"type": "permission_response", "request_id": "r1", "allowed": true, "remember": true}')
    assert isinstance(_p, PermissionResponse) and _p.allowed and _p.remember
    assert isinstance(parse_uplink('{"type": "nope"}'), UplinkUnknown)
    assert parse_uplink("not json") is None
    assert parse_uplink('{"type": "message"}') is None  # 缺 text
    # 缺 allowed 字段仍容忍：request_id 合法 → 视为拒绝（fail-closed 默认）
    _pr = parse_uplink('{"type": "permission_response", "request_id": "x"}')
    assert isinstance(_pr, PermissionResponse) and _pr.allowed is False
    assert parse_uplink("") is None

    # serve 下行扩展：用户消息 / 历史快照 / 终局事件 / permission_request 带 can_remember
    _um = user_message("hi")
    assert _um == {"type": "user_message", "text": "hi"}   # 无 content 键（存量一致）
    _umc = user_message("hi", content=[{"type": "text", "text": "hi"}])
    assert _umc == {"type": "user_message", "text": "hi",
                    "content": [{"type": "text", "text": "hi"}]}
    _ack = message_ack("m1")
    assert _ack == {"type": "message_ack", "msg_id": "m1"}
    _h = serve_history([{"role": "user", "content": "hi"}])
    assert _h["type"] == "history" and _h["messages"][0]["content"] == "hi"
    _r = result_event("done", False, 10, 2, "s1", {"input_tokens": 1, "output_tokens": 2})
    assert _r["type"] == "result" and _r["subtype"] == "success" and _r["num_turns"] == 2
    _e = result_event(None, True, 10, 0, "s1", {}, error="boom")
    assert _e["subtype"] == "error" and _e["error"] == "boom"
    _pr = permission_request("r2", "shell", "run", can_remember=False)
    assert _pr["can_remember"] is False and _pr["type"] == "permission_request"
    # 执行计划 / 子 agent 快照（serve 右栏任务面板）
    _td = serve_todos([{"content": "x", "activeForm": "x", "status": "pending"}])
    assert _td["type"] == "todos" and _td["todos"][0]["status"] == "pending"
    _fl = serve_fleet([{"id": 1, "label": "find X", "subagent_type": "explore",
                        "status": "running", "tools_count": 2, "elapsed": 3}])
    assert _fl["type"] == "fleet" and _fl["agents"][0]["tools_count"] == 2

    # 容灾控制事件（第七组：checkpoint / interrupt / 资源闸 / 回合起止 / 恢复）
    _ck = checkpoint_event(
        phase="committed", reason="tool_round", tool_rounds=2,
        completed_tool_calls=["t1", "t2"], snapshot_digest="abc", snapshot_bytes=99,
    )
    assert _ck["type"] == "checkpoint" and _ck["phase"] == "committed"
    assert _ck["completed_tool_calls"] == ["t1", "t2"] and _ck["snapshot_bytes"] == 99
    # 默认值：可选序列字段恒为 list（消费者可无条件迭代）
    _ck_min = checkpoint_event(phase="inflight", reason="tool_round", tool_rounds=0)
    assert _ck_min["completed_tool_calls"] == [] and _ck_min["repaired_calls"] == []
    assert _ck_min["gate"] == "" and _ck_min["snapshot_digest"] == ""

    _it = interrupt_event("sigint", checkpoint_seq=7, tool_rounds=3)
    assert _it["type"] == "interrupt" and _it["kind"] == "sigint"
    assert _it["checkpoint_seq"] == 7
    # 没落成 checkpoint 时如实记 0，而不是省略字段
    assert interrupt_event("sigterm")["checkpoint_seq"] == 0

    _gt = resource_gate_tripped("max_tool_rounds", 30, 30, checkpoint_seq=8)
    assert _gt["type"] == "resource_gate_tripped"
    assert _gt["gate"] == "max_tool_rounds" and _gt["limit"] == 30 and _gt["rounds"] == 30

    _ts = turn_started("s1", 4)
    assert _ts["type"] == "turn_started" and _ts["history_len"] == 4
    assert _ts["resumed"] is False and turn_started("s1", 0, True)["resumed"] is True

    _rs = resume_event("ok", 9, 3, repaired=1, detail="resumed at tool round 3")
    assert _rs["type"] == "resume" and _rs["verdict"] == "ok" and _rs["repaired"] == 1
    assert resume_event("torn", detail="x")["checkpoint_seq"] == 0

    _cd = checkpoint_discarded("stale")
    assert _cd["type"] == "checkpoint_discarded" and _cd["reason"] == "stale"
    # 全部新事件可 JSON 序列化（要落账本）
    for _ev in (_ck, _it, _gt, _ts, _rs, _cd):
        json.loads(json.dumps(_ev))

    print("openx/kernel/protocol.py OK ✓")
