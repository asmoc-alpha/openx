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
from dataclasses import dataclass
from typing import Any, Optional, Union

# P1：严格相等。未来minor 演进时在此放宽（如 client <= server 且同 major）。
PROTOCOL_VERSION = 1


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


def tool_use(name: str) -> dict[str, Any]:
    return {"type": "tool_use", "name": name}


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

def user_message(text: str) -> dict[str, Any]:
    """serve 下行：一条用户消息（live 广播 / attach 快照共用）。"""
    return {"type": "user_message", "text": text}


def serve_history(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """attach 会话快照下行：新客户端连上时补发既有对话。

    ``messages`` 为渲染段列表 ``[{role, content, ...}]``（agent.history 或
    会话文件回放的行）；端是哑渲染器，只按序渲染，不持会话状态语义。
    """
    return {"type": "history", "messages": messages}


def serve_panels(panels: list[dict[str, Any]]) -> dict[str, Any]:
    """serve 下行：插件 UI 面板快照（ui/v1，web 常驻面板）。

    ``panels = [{"name": str, "lines": [str, ...]}]``——行已剥 rich 标签
    （与 text_delta 同款），端是哑渲染器只按行渲染纯文本；空列表 = 面板
    全部消失（端清空面板区）。ticker 变化才广播（动画帧即天然变化源）。
    """
    return {"type": "panels", "panels": panels}


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
class UserMessage:
    """用户发送的聊天消息（serve 上行意图：message）。"""

    text: str


@dataclass
class Interrupt:
    """打断当前回合（serve 上行意图：interrupt；Esc 的 Web 等价物）。"""


@dataclass
class UplinkUnknown:
    """未知上行类型：容忍不炸（前向兼容），调用方可记日志。"""

    type: str


UplinkMessage = Union[PermissionResponse, UserMessage, Interrupt, UplinkUnknown]


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
        return UserMessage(text)
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
    assert _um["type"] == "user_message" and _um["text"] == "hi"
    _h = serve_history([{"role": "user", "content": "hi"}])
    assert _h["type"] == "history" and _h["messages"][0]["content"] == "hi"
    _r = result_event("done", False, 10, 2, "s1", {"input_tokens": 1, "output_tokens": 2})
    assert _r["type"] == "result" and _r["subtype"] == "success" and _r["num_turns"] == 2
    _e = result_event(None, True, 10, 0, "s1", {}, error="boom")
    assert _e["subtype"] == "error" and _e["error"] == "boom"
    _pr = permission_request("r2", "shell", "run", can_remember=False)
    assert _pr["can_remember"] is False and _pr["type"] == "permission_request"

    print("openx/core/protocol.py OK ✓")
