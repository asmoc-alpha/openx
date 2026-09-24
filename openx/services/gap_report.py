"""E3 缺口感知 · **离线分析器**——账本 → 失败模式聚类 → "缺口报告"。

设计（`docs/design/openx-self-evolution-design.md` §2.1）：缺口的来源之一是
"失败模式复现"——同类任务反复绕路（轨迹中反复出现的笨路径）。本模块是**离线
消费方**：单遍扫最近若干会话的账本，聚出四类**可操作信号**——

| 类别 | 信号 | 依据事件 |
|---|---|---|
| ``tool_failure``      | 某工具反复报错 | ``message`` role=tool（内容含 ``Error:``）+ assistant ``tool_calls`` 回填工具名 |
| ``permission_friction`` | 同工具反复 ASK 且被批准 | ``permission_decision``（verdict=ASK & approved） |
| ``denied_calls``      | 同工具反复被拒 | ``permission_decision``（未批准） |
| ``detour``            | 同一 (工具, 参数) 反复调用（笨路径） | assistant ``tool_calls`` 去重计数 |

产出三层，逐层更**可机读/可回喂**：

1. :func:`analyze_workspace` → 结构化报告 dict（schema ``openx-gap-report/v1``）；
2. :func:`render_text` → 人读文本（``/gaps`` 命令的展示）；
3. :func:`as_context_fragment` → **上下文片段**（可回喂下次会话的系统提示——
   "报告可作为 context 片段回喂会话"）。

纪律（同 eval_export）：只**读**会话（机制住内核、消费住 services）；默认只碰
本模块的读取路径，**绝不**在项目里建 ``.openx``。报告是**证据不是审批**——
人（§4.3 人闭环）据此决定是否沉淀规则 / 生成插件 / 调整装配。
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

import json
from typing import Any

from ..orchestration.sessions import SessionStore

#: 报告 schema 版本（消费方据此判断字段形状）。
SCHEMA = "openx-gap-report/v1"

#: 最多分析最近多少会话（离线人工触发；上限防一次扫全量）。
MAX_SESSIONS = 50

#: 成"缺口"的阈值（低于阈值的偶发信号不入报告，避免噪音）。
MIN_TOOL_ERRORS = 2      # 某工具错误次数 ≥ 此值
MIN_FRICTION = 3         # 同工具 ASK-批准次数 ≥ 此值（≥3 次重复才值得沉淀规则）
MIN_DENIED = 2           # 同工具被拒次数 ≥ 此值
MIN_DETOUR = 3           # 同一 (工具, 参数) 单会话重复 ≥ 此值（绕路/循环）

#: 每类缺口的处置建议（人读；不是自动动作——建议永远只是候选）。
_RECOMMENDATIONS = {
    "tool_failure": "repeated tool errors — fix usage or add a dedicated tool",
    "permission_friction": "frequently approved — consider saving a permission rule",
    "denied_calls": "repeatedly blocked — the model keeps trying a denied operation",
    "detour": "same call repeated — possible loop; consider a helper tool",
}

#: 同类计数并列时的类序（越靠前越"硬"——错误 > 拒绝 > 摩擦 > 绕路）。
_KIND_ORDER = {
    "tool_failure": 0,
    "denied_calls": 1,
    "permission_friction": 2,
    "detour": 3,
}


def _is_error_result(content: Any) -> bool:
    """工具结果消息是否报错。

    ``ToolResult.to_message()`` 把 error 拼成 ``Error: <msg>``（单独一行或
    尾行）。这是启发式——输出正文若恰好含 ``Error:`` 行会误判，故只认**行首**
    ``Error:``（离线提示报告可接受，非裁决依据）。
    """
    if not isinstance(content, str):
        return False
    for line in content.splitlines():
        if line.startswith("Error:"):
            return True
    return False


def _tool_calls(message: dict[str, Any]) -> list[tuple[str, str, str]]:
    """一条 assistant 消息的 ``tool_calls`` → ``[(call_id, name, args)]``。"""
    out: list[tuple[str, str, str]] = []
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function")
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name") or "")
        if not name:
            continue
        args = fn.get("arguments")
        out.append((str(call.get("id") or ""), name, str(args if args is not None else "")))
    return out


def _truncate(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _session_detour_sigs(events: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """本会话内 (工具, 参数) 出现次数 ≥ ``MIN_DETOUR`` 的签名集合（绕路候选）。"""
    seen: dict[tuple[str, str], int] = {}
    for event in events:
        if event.get("type") != "message":
            continue
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for _, name, args in _tool_calls(message):
            sig = (name, args)
            seen[sig] = seen.get(sig, 0) + 1
    return {sig for sig, n in seen.items() if n >= MIN_DETOUR}


class _Cluster:
    """一类缺口的一个键（工具名 / (工具,参数)）的累计。"""

    def __init__(self, kind: str, key: str) -> None:
        self.kind = kind
        self.key = key
        self.count = 0
        self.sessions: set[str] = set()
        self.sample = ""

    def to_dict(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "kind": self.kind,
            "key": self.key,
            "count": self.count,
            "sessions": sorted(self.sessions),
            "recommendation": _RECOMMENDATIONS.get(self.kind, ""),
        }
        if self.sample:
            item["sample"] = self.sample
        return item


def _analyze_session(
    session_id: str,
    events: list[dict[str, Any]],
    errors: dict[str, _Cluster],
    friction: dict[str, _Cluster],
    denied: dict[str, _Cluster],
    calls: dict[str, int],
    detour: dict[tuple[str, str], _Cluster],
) -> None:
    """单会话扫描：回填共享的聚类表（工具名 -> 计数）。"""
    id_to_name: dict[str, str] = {}
    detour_sigs = _session_detour_sigs(events)

    for event in events:
        etype = event.get("type")
        if etype == "message":
            message = event.get("message")
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role == "assistant":
                for call_id, name, args in _tool_calls(message):
                    id_to_name[call_id] = name
                    calls[name] = calls.get(name, 0) + 1
                    # 绕路：同一 (工具, 参数) 在本会话内反复出现
                    sig = (name, args)
                    if sig in detour_sigs:
                        cluster = detour.get(sig)
                        if cluster is None:
                            key = f"{name} {_truncate(args, 40)}".strip()
                            cluster = _Cluster("detour", key)
                            cluster.sample = _truncate(args)
                            detour[sig] = cluster
                        cluster.count += 1
                        cluster.sessions.add(session_id)
            elif role == "tool":
                name = id_to_name.get(str(message.get("tool_call_id") or ""))
                if name and _is_error_result(message.get("content")):
                    cluster = errors.setdefault(name, _Cluster("tool_failure", name))
                    cluster.count += 1
                    cluster.sessions.add(session_id)
        elif etype == "permission_decision":
            tool = str(event.get("tool") or "")
            if not tool:
                continue
            verdict = str(event.get("verdict") or "")
            approved = bool(event.get("approved"))
            if verdict.upper() == "ASK" and approved:
                cluster = friction.setdefault(tool, _Cluster("permission_friction", tool))
                cluster.count += 1
                cluster.sessions.add(session_id)
            elif not approved:
                cluster = denied.setdefault(tool, _Cluster("denied_calls", tool))
                cluster.count += 1
                cluster.sessions.add(session_id)


def analyze_sessions(
    sessions: list[tuple[str, list[dict[str, Any]]]],
    workspace: str = "",
) -> dict[str, Any]:
    """一批 ``(session_id, events)`` → 结构化缺口报告（可 JSON 序列化）。

    ``events`` 取自 ``SessionStore.iter_events``（消息行 + 账本行投影）。计数
    跨会话累加；阈值过滤后按严重度（count 降序）排列。
    """
    errors: dict[str, _Cluster] = {}
    friction: dict[str, _Cluster] = {}
    denied: dict[str, _Cluster] = {}
    calls: dict[str, int] = {}
    detour: dict[tuple[str, str], _Cluster] = {}

    for session_id, events in sessions:
        _analyze_session(session_id, events, errors, friction, denied, calls, detour)

    gaps: list[dict[str, Any]] = []
    for name, cluster in errors.items():
        if cluster.count >= MIN_TOOL_ERRORS:
            item = cluster.to_dict()
            item["calls"] = calls.get(name, 0)   # 该工具被调用总数（语境）
            gaps.append(item)
    gaps.extend(c.to_dict() for c in friction.values() if c.count >= MIN_FRICTION)
    gaps.extend(c.to_dict() for c in denied.values() if c.count >= MIN_DENIED)
    gaps.extend(c.to_dict() for c in detour.values() if c.count >= MIN_DETOUR)

    # 严重度排序：先按 count 降序，并列再按类序（错误 > 拒绝 > 摩擦 > 绕路）
    gaps.sort(key=lambda g: (-int(g["count"]), _KIND_ORDER.get(g["kind"], 9), g["key"]))

    return {
        "schema": SCHEMA,
        "workspace": workspace,
        "sessions": len(sessions),
        "tool_calls": sum(calls.values()),
        "gaps": gaps,
    }


def analyze_workspace(workspace: str, limit: int = MAX_SESSIONS) -> dict[str, Any]:
    """某工作区最近 ``limit`` 个会话 → 缺口报告。

    与 eval_export 同源（``SessionStore.list_for_workspace`` + ``iter_events``）；
    无会话时返回空报告（``gaps=[]``），绝不抛。
    """
    metas = SessionStore.list_for_workspace(workspace)[: max(0, limit)]
    sessions: list[tuple[str, list[dict[str, Any]]]] = []
    for meta in metas:
        if meta.path is None:
            continue
        sessions.append((meta.session_id, SessionStore.iter_events(meta.path)))
    return analyze_sessions(sessions, workspace=workspace)


def render_text(report: dict[str, Any]) -> str:
    """报告 dict → 人读文本（``/gaps`` 的展示）。无缺口给明确结论。"""
    gaps = report.get("gaps") or []
    head = (
        f"Gap report · {report.get('sessions', 0)} session(s) · "
        f"{report.get('tool_calls', 0)} tool call(s)"
    )
    if not gaps:
        return f"{head}\nNo gaps detected."
    lines = [head, ""]
    for gap in gaps:
        sessions = ", ".join(gap.get("sessions", [])[:3])
        more = "" if len(gap.get("sessions", [])) <= 3 else "…"
        calls = f" ({gap['calls']} calls)" if "calls" in gap else ""
        lines.append(
            f"• [{gap['kind']}] {gap['key']} ×{gap['count']}{calls} "
            f"[{sessions}{more}]"
        )
        if gap.get("sample"):
            lines.append(f"    sample: {gap['sample']}")
        if gap.get("recommendation"):
            lines.append(f"    → {gap['recommendation']}")
    return "\n".join(lines)


def as_context_fragment(report: dict[str, Any], max_gaps: int = 6) -> str:
    """报告 → **上下文片段**（可并入系统提示，回喂下次会话）。

    无缺口返回空串（调用方据此跳过，不注入空片段）。顺序即严重度序，只取
    前 ``max_gaps`` 条；一行一条，短到可忽略 token 成本。
    """
    gaps = report.get("gaps") or []
    if not gaps:
        return ""
    lines = ["Observed capability gaps (auto-analysis of recent sessions):"]
    for gap in gaps[:max_gaps]:
        lines.append(f"- {gap['kind']}: {gap['key']} (×{gap['count']})")
    return "\n".join(lines)


if __name__ == "__main__":
    # 合成一批会话：错误工具 / 权限摩擦 / 拒绝 / 绕路
    def _msg(role: str, **kw: Any) -> dict[str, Any]:
        return {"type": "message", "ts": 0.0, "message": {"role": role, **kw}}

    def _perm(tool: str, verdict: str, approved: bool) -> dict[str, Any]:
        return {"type": "permission_decision", "tool": tool,
                "verdict": verdict, "approved": approved}

    s1 = [
        _msg("user", content="build it"),
        _msg("assistant", content="", tool_calls=[
            {"id": "c1", "function": {"name": "shell", "arguments": '{"cmd":"make"}'}},
            {"id": "c2", "function": {"name": "grep", "arguments": '{"pattern":"x"}'}},
        ]),
        _msg("tool", tool_call_id="c1", content="Error: make not found"),
        _msg("tool", tool_call_id="c2", content="match"),
        _msg("assistant", content="", tool_calls=[
            {"id": "c3", "function": {"name": "shell", "arguments": '{"cmd":"make"}'}},
            {"id": "c4", "function": {"name": "shell", "arguments": '{"cmd":"make"}'}},
            {"id": "c5", "function": {"name": "shell", "arguments": '{"cmd":"make"}'}},
        ]),
        _msg("tool", tool_call_id="c3", content="Error: make not found"),
        _msg("tool", tool_call_id="c4", content="Error: make not found"),
        _msg("tool", tool_call_id="c5", content="Error: make not found"),
        _perm("shell", "ASK", True), _perm("shell", "ASK", True),
        _perm("shell", "ASK", True), _perm("write_file", "DENY", False),
        _perm("write_file", "DENY", False),
    ]
    s2 = [
        _msg("assistant", content="", tool_calls=[
            {"id": "d1", "function": {"name": "shell", "arguments": '{"cmd":"ls"}'}},
        ]),
        _msg("tool", tool_call_id="d1", content="ok"),
    ]

    report = analyze_sessions([("s1", s1), ("s2", s2)], workspace="/tmp/ws")
    assert report["schema"] == SCHEMA
    assert report["sessions"] == 2 and report["tool_calls"] == 6
    kinds = {g["kind"] for g in report["gaps"]}
    assert {"tool_failure", "permission_friction", "denied_calls", "detour"} <= kinds

    shell = next(g for g in report["gaps"] if g["kind"] == "tool_failure")
    assert shell["key"] == "shell" and shell["count"] == 4 and shell["calls"] == 5
    assert shell["sessions"] == ["s1"]

    friction = next(g for g in report["gaps"] if g["kind"] == "permission_friction")
    assert friction["key"] == "shell" and friction["count"] == 3
    denied = next(g for g in report["gaps"] if g["kind"] == "denied_calls")
    assert denied["key"] == "write_file" and denied["count"] == 2
    detour = next(g for g in report["gaps"] if g["kind"] == "detour")
    assert detour["count"] == 4 and "shell" in detour["key"]

    # 严重度序：shell 工具错误（4）排最前
    assert report["gaps"][0]["kind"] == "tool_failure"

    # 人读文本 + 上下文片段
    text = render_text(report)
    assert "tool_failure" in text and "shell" in text and "No gaps detected" not in text
    frag = as_context_fragment(report)
    assert frag.startswith("Observed capability gaps")
    assert "tool_failure: shell (×4)" in frag

    # 无缺口：空报告 → 明确结论 + 空片段
    empty = analyze_sessions([])
    assert empty["gaps"] == []
    assert "No gaps detected" in render_text(empty)
    assert as_context_fragment(empty) == ""
    # 低于阈值的偶发信号不入报告
    low = analyze_sessions([("s", [s2[0], _msg("tool", tool_call_id="d1",
                                                content="Error: transient")])])
    assert low["gaps"] == []

    # 可 JSON 序列化（将来可落盘/回喂）
    json.loads(json.dumps(report))

    print("openx/services/gap_report.py OK ✓")
