"""会话协议 P1 —— 线格式 schema 的单一真源（标准三载体）。

下行事件与既有 stream-json 输出**逐字段一致**（存量消费者零改动），
``init`` 增 ``protocol_version``；新增 ``permission_request`` 下行。
上行 P1 只收 ``permission_response``；未知类型容忍（前向兼容，回报
``UplinkUnknown``），畸形行返回 ``None`` 由调用方记日志。

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
    request_id: str, tool: str, reason: str, details: str = ""
) -> dict[str, Any]:
    """权限请求下行：上行以同 request_id 的 permission_response 应答。"""
    return {
        "type": "permission_request",
        "request_id": request_id,
        "tool": tool,
        "reason": reason,
        "details": details,
    }


# ── 上行（client → server）──────────────────────────────────────

@dataclass
class PermissionResponse:
    """对 permission_request 的裁决：allowed=False 等同弹窗里拒绝。"""

    request_id: str
    allowed: bool


@dataclass
class UplinkUnknown:
    """未知上行类型：容忍不炸（前向兼容），调用方可记日志。"""

    type: str


UplinkMessage = Union[PermissionResponse, UplinkUnknown]


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
        return PermissionResponse(request_id, bool(obj.get("allowed", False)))
    if isinstance(kind, str):
        return UplinkUnknown(kind)
    return None


def negotiate(client_version: int) -> bool:
    """能力协商 P1：严格相等；演进规则改这一处。"""
    return client_version == PROTOCOL_VERSION
