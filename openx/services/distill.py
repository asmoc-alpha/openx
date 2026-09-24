"""E6 经验沉淀闭环 · **离线提炼 + 人审写入 + 召回回账**。

设计（`docs/design/openx-self-evolution-design.md` §4.4）：单会话内的"学会了"要
跨会话存活，路径是——

```
会话账本（事件流）
  → 会话收尾：提炼经验（成功的路径、失败的教训、项目约定）
  → 写入记忆（memory / coding_memory）
  → 未来会话：context/v1 召回并入系统提示
  → 召回质量回流轨迹（被召回的记忆是否真被用）
```

本模块承接前两环与第四环：

1. **提炼**（:func:`distill_sessions`）：单遍扫会话账本，从工具轨迹里挖三类**候
   选经验**（保守启发式，宁可漏报不可误报）——
   `workflow`（反复成功的 shell 命令）、`debug_pattern`（某工具先错后成）、
   `project_fact`（反复编辑的热点文件）；
2. **写入**（:func:`save_candidates`）：人审后把候选落进 ``coding_memory``
   （``source="distill"``——与 agent 自主记忆同池可查、可按来源批量回滚）；
3. **回账**（:func:`recall_report`）：汇总会话账本里的 ``memory_recall`` 事件
   （agent 每次构建系统提示时记录被召回的记忆 id）——"哪条记忆被召回了几次"
   是召回质量的数据源。

纪律（同 E3/E5）：提炼**只读离线**、写入**由人触发**（``/distill save`` 的调用
即授权）、候选是**建议不是事实**（§4.3 人闭环）。第三环"context/v1 召回"沿用
既有 ``coding_memory.build_context_prompt`` 路径，本模块只在旁记录召回事实。
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
from typing import Any

from ..orchestration.sessions import SessionStore

#: 报告 schema 版本。
SCHEMA = "openx-distill-report/v1"

#: 最多分析最近多少会话。
MAX_SESSIONS = 50

#: 命令 / 热文件成候选的重复阈值（低于此值不报，避免噪音）。
MIN_OCCURRENCES = 2

#: 勘误（错误→成功）配对里，错误摘要的截断长度。
_ERROR_SNIPPET = 100

#: 有写副作用的工具（热点文件信号）。
_WRITE_TOOLS = ("write_file", "edit_file", "multi_edit")

#: 候选类型 → 记忆分类 + 严重度序（同类内按 occurrences 降序）。
_KIND_CATEGORY = {
    "workflow": "workflow",
    "debug_pattern": "debug_pattern",
    "project_fact": "project_fact",
}
_KIND_ORDER = {"debug_pattern": 0, "workflow": 1, "project_fact": 2}


# ── 工具轨迹提取 ─────────────────────────────────────────────────


def _parse_args(arguments: Any) -> dict[str, Any]:
    """assistant ``tool_calls[].function.arguments`` → dict（容忍 str/dict/坏值）。"""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str) and arguments.strip():
        try:
            parsed = json.loads(arguments)
        except (json.JSONDecodeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _is_error_result(content: Any) -> bool:
    """工具结果是否报错（行首 ``Error:``，与 E3 同口径）。"""
    if not isinstance(content, str):
        return False
    return any(line.startswith("Error:") for line in content.splitlines())


def _error_snippet(content: Any) -> str:
    if not isinstance(content, str):
        return ""
    for line in content.splitlines():
        if line.startswith("Error:"):
            return " ".join(line.split())[: _ERROR_SNIPPET]
    return ""


class _ToolCall:
    """一次工具调用及其结果（结果未配对时 ``is_error=None``）。"""

    __slots__ = ("args", "error", "is_error", "name")

    def __init__(self, name: str, args: dict[str, Any]) -> None:
        self.name = name
        self.args = args
        self.is_error: bool | None = None
        self.error = ""


def _tool_calls(events: list[dict[str, Any]]) -> list[_ToolCall]:
    """按调用序产出工具调用（结果按 ``tool_call_id`` 回填）。"""
    by_id: dict[str, _ToolCall] = {}
    ordered: list[_ToolCall] = []
    for event in events:
        if event.get("type") != "message":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function")
                if not isinstance(fn, dict) or not fn.get("name"):
                    continue
                rec = _ToolCall(str(fn["name"]), _parse_args(fn.get("arguments")))
                ordered.append(rec)
                cid = call.get("id")
                if cid:
                    by_id[str(cid)] = rec
        elif role == "tool":
            rec = by_id.get(str(message.get("tool_call_id") or ""))
            if rec is not None:
                rec.is_error = _is_error_result(message.get("content"))
                if rec.is_error:
                    rec.error = _error_snippet(message.get("content"))
    return ordered


def _norm_command(cmd: str) -> str:
    """命令归一化：折叠空白（同一命令不因空格差异重复计数）。"""
    return " ".join(cmd.split())


def _command_words(cmd: str) -> list[str]:
    """命令里可作关键词的词（去掉纯 flag）。"""
    return [w for w in cmd.split() if w and not w.startswith("-")][:6]


# ── 提炼 ─────────────────────────────────────────────────────────


def _mine_session(
    session_id: str,
    events: list[dict[str, Any]],
    workflows: dict[str, dict[str, Any]],
    debug: dict[str, dict[str, Any]],
    hot: dict[str, dict[str, Any]],
) -> None:
    """单会话挖掘：回填三类候选表（跨会话累加）。"""
    calls = _tool_calls(events)

    # ① workflow：成功的 shell 命令，按归一化命令计数
    for rec in calls:
        if rec.name != "shell" or rec.is_error is not False:
            continue
        cmd = _norm_command(str(rec.args.get("command") or ""))
        if not cmd:
            continue
        entry = workflows.setdefault(cmd, {
            "kind": "workflow", "cmd": cmd, "count": 0, "sessions": set(),
        })
        entry["count"] += 1
        entry["sessions"].add(session_id)

    # ② debug_pattern：同名工具先错后成
    first_error: dict[str, _ToolCall] = {}
    fixed: set[str] = set()
    for rec in calls:
        if rec.name in fixed:
            continue
        if rec.is_error and rec.name not in first_error:
            first_error[rec.name] = rec
        elif rec.is_error is False and rec.name in first_error:
            fixed.add(rec.name)
            src = first_error[rec.name]
            entry = debug.setdefault(rec.name, {
                "kind": "debug_pattern", "tool": rec.name, "error": src.error,
                "fix_args": _norm_command(json.dumps(rec.args, ensure_ascii=False)),
                "count": 0, "sessions": set(),
            })
            entry["count"] += 1
            entry["sessions"].add(session_id)

    # ③ project_fact：反复编辑的热点文件
    for rec in calls:
        if rec.name not in _WRITE_TOOLS:
            continue
        path = str(rec.args.get("path") or "").strip()
        if not path:
            continue
        entry = hot.setdefault(path, {
            "kind": "project_fact", "path": path, "count": 0, "sessions": set(),
        })
        entry["count"] += 1
        entry["sessions"].add(session_id)


def _workflow_candidate(entry: dict[str, Any]) -> dict[str, Any]:
    cmd = entry["cmd"]
    return {
        "kind": "workflow",
        "category": _KIND_CATEGORY["workflow"],
        "content": f"Frequently used command in this project: {cmd}",
        "keywords": _command_words(cmd),
        "related_paths": [],
        "occurrences": entry["count"],
        "sessions": sorted(entry["sessions"]),
        "evidence": {"command": cmd, "runs": entry["count"]},
    }


def _debug_candidate(entry: dict[str, Any]) -> dict[str, Any]:
    tool = entry["tool"]
    err = entry["error"] or "(unknown error)"
    return {
        "kind": "debug_pattern",
        "category": _KIND_CATEGORY["debug_pattern"],
        "content": f"`{tool}` failed with: {err} — retry/adjust the arguments and it succeeds.",
        "keywords": [tool, "error", "retry"],
        "related_paths": [],
        "occurrences": entry["count"],
        "sessions": sorted(entry["sessions"]),
        "evidence": {"tool": tool, "error": err, "fix_args": entry["fix_args"]},
    }


def _project_fact_candidate(entry: dict[str, Any]) -> dict[str, Any]:
    path = entry["path"]
    return {
        "kind": "project_fact",
        "category": _KIND_CATEGORY["project_fact"],
        "content": f"Frequently edited file: {path}",
        "keywords": [path.split("/")[-1]],
        "related_paths": [path],
        "occurrences": entry["count"],
        "sessions": sorted(entry["sessions"]),
        "evidence": {"path": path, "edits": entry["count"]},
    }


def distill_sessions(
    sessions: list[tuple[str, list[dict[str, Any]]]],
    workspace: str = "",
) -> dict[str, Any]:
    """一批 ``(session_id, events)`` → 结构化经验提炼报告（可 JSON 序列化）。"""
    workflows: dict[str, dict[str, Any]] = {}
    debug: dict[str, dict[str, Any]] = {}
    hot: dict[str, dict[str, Any]] = {}
    for session_id, events in sessions:
        _mine_session(session_id, events, workflows, debug, hot)

    candidates: list[dict[str, Any]] = []
    candidates.extend(
        _debug_candidate(e) for e in debug.values()
    )
    candidates.extend(
        _workflow_candidate(e)
        for e in workflows.values() if e["count"] >= MIN_OCCURRENCES
    )
    candidates.extend(
        _project_fact_candidate(e)
        for e in hot.values() if e["count"] >= MIN_OCCURRENCES
    )

    # 去重（同内容只留一条）+ 排序（类序，再 occurrences 降序，再内容）
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for cand in candidates:
        key = hashlib.sha256(cand["content"].encode()).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        unique.append(cand)
    unique.sort(key=lambda c: (
        _KIND_ORDER.get(c["kind"], 9), -int(c["occurrences"]), c["content"],
    ))

    return {
        "schema": SCHEMA,
        "workspace": workspace,
        "sessions": len({sid for sid, _ in sessions}),
        "candidates": unique,
    }


def distill_workspace(workspace: str, limit: int = MAX_SESSIONS) -> dict[str, Any]:
    """某工作区最近 ``limit`` 个会话 → 经验提炼报告（同 E3/E5 读取路径）。"""
    metas = SessionStore.list_for_workspace(workspace)[: max(0, limit)]
    sessions: list[tuple[str, list[dict[str, Any]]]] = []
    for meta in metas:
        if meta.path is None:
            continue
        sessions.append((meta.session_id, SessionStore.iter_events(meta.path)))
    return distill_sessions(sessions, workspace=workspace)


# ── 写入（人审后）：候选 → coding_memory ─────────────────────────


def save_candidates(
    store: Any,
    candidates: list[dict[str, Any]],
    *,
    scope: str = "project",
    source: str = "distill",
) -> list[Any]:
    """把候选经验写进 ``coding_memory``（``source="distill"``）。

    去重交由 ``CodingMemoryStore.remember`` 自己完成（同内容 → 更新不重复）。
    返回落库的 ``CodingMemory`` 列表。写入是**人触发**的（``/distill save``），
    故此函数不做额外审批——调用即授权。
    """
    saved: list[Any] = []
    for cand in candidates:
        content = str(cand.get("content") or "").strip()
        if not content:
            continue
        mem = store.remember(
            content,
            category=str(cand.get("category") or "project_fact"),
            keywords=list(cand.get("keywords") or []),
            related_paths=list(cand.get("related_paths") or []),
            scope=scope,
            source=source,
        )
        saved.append(mem)
    return saved


# ── 回账：召回质量 ───────────────────────────────────────────────


def recall_report(
    sessions: list[tuple[str, list[dict[str, Any]]]],
    workspace: str = "",
) -> dict[str, Any]:
    """汇总 ``memory_recall`` 事件：哪条记忆被召回了几次（召回质量数据源）。

    每次 agent 构建系统提示时记一条 ``memory_recall``（携带被召回的记忆 id）；
    本函数跨会话叠加，产出召回频次榜——被频繁召回却（将来）不见使用 = 该记忆
    低效的候选信号（§4.2 记忆质量）。
    """
    counts: dict[str, int] = {}
    builds = 0
    for _session_id, events in sessions:
        for event in events:
            if event.get("type") != "memory_recall":
                continue
            builds += 1
            for mem_id in event.get("ids") or []:
                counts[str(mem_id)] = counts.get(str(mem_id), 0) + 1
    ranked = sorted(
        ({"id": mid, "recalls": n} for mid, n in counts.items()),
        key=lambda r: (-r["recalls"], r["id"]),
    )
    return {
        "schema": "openx-recall-report/v1",
        "workspace": workspace,
        "sessions": len({sid for sid, _ in sessions}),
        "prompt_builds": builds,
        "distinct": len(counts),
        "ranked": ranked,
    }


def recall_report_workspace(workspace: str, limit: int = MAX_SESSIONS) -> dict[str, Any]:
    """某工作区最近 ``limit`` 个会话 → 召回报告。"""
    metas = SessionStore.list_for_workspace(workspace)[: max(0, limit)]
    sessions: list[tuple[str, list[dict[str, Any]]]] = []
    for meta in metas:
        if meta.path is None:
            continue
        sessions.append((meta.session_id, SessionStore.iter_events(meta.path)))
    return recall_report(sessions, workspace=workspace)


# ── 渲染 ─────────────────────────────────────────────────────────


def render_text(report: dict[str, Any]) -> str:
    """候选经验报告 → 人读文本（``/distill`` 的展示）。"""
    cands = report.get("candidates") or []
    head = f"Distill report · {report.get('sessions', 0)} session(s)"
    if not cands:
        return f"{head}\nNo candidate experiences found."
    lines = [head, ""]
    for cand in cands:
        lines.append(
            f"• [{cand['kind']}] ×{cand['occurrences']} {cand['content']}"
        )
        if cand.get("keywords") or cand.get("related_paths"):
            hint = ", ".join(list(cand.get("keywords") or [])[:4]
                             + list(cand.get("related_paths") or [])[:2])
            lines.append(f"    tags: {hint}")
    lines.append("")
    lines.append("Run /distill save to persist these into coding memory.")
    return "\n".join(lines)


def render_recall(report: dict[str, Any]) -> str:
    """召回报告 → 人读文本（``/distill recall`` 的展示）。"""
    head = (
        f"Recall report · {report.get('sessions', 0)} session(s) · "
        f"{report.get('prompt_builds', 0)} prompt build(s) · "
        f"{report.get('distinct', 0)} distinct memor(y/ies) recalled"
    )
    ranked = report.get("ranked") or []
    if not ranked:
        return f"{head}\nNo memories recalled yet."
    lines = [head, ""]
    for row in ranked:
        lines.append(f"  • {row['id']}: ×{row['recalls']}")
    return "\n".join(lines)


if __name__ == "__main__":
    def _msg(role: str, **kw: Any) -> dict[str, Any]:
        return {"type": "message", "ts": 0.0, "message": {"role": role, **kw}}

    def _call(cid: str, name: str, **args: Any) -> dict[str, Any]:
        return {"id": cid, "function": {
            "name": name, "arguments": json.dumps(args)}}

    s1 = [
        _msg("assistant", content="", tool_calls=[
            _call("a", "shell", command="pytest -q"),
            _call("b", "shell", command="pytest -q"),
        ]),
        _msg("tool", tool_call_id="a", content="ok"),
        _msg("tool", tool_call_id="b", content="2 passed"),
        _msg("assistant", content="", tool_calls=[
            _call("c", "shell", command="ruff check ."),
        ]),  # 只跑一次 → 不成候选
        _msg("tool", tool_call_id="c", content="All checks passed"),
        _msg("assistant", content="", tool_calls=[
            _call("d", "edit_file", path="openx/services/distill.py"),
            _call("e", "edit_file", path="openx/services/distill.py"),
        ]),
        _msg("tool", tool_call_id="d", content="patched"),
        _msg("tool", tool_call_id="e", content="patched"),
    ]
    s2 = [
        _msg("assistant", content="", tool_calls=[
            _call("f", "grep", pattern="foo"),
        ]),
        _msg("tool", tool_call_id="f", content="Error: pattern invalid"),
        _msg("assistant", content="", tool_calls=[
            _call("g", "grep", pattern="bar"),
        ]),
        _msg("tool", tool_call_id="g", content="bar: 3 matches"),
    ]

    report = distill_sessions([("s1", s1), ("s2", s2)], workspace="/tmp/ws")
    assert report["schema"] == SCHEMA and report["sessions"] == 2
    kinds = {c["kind"] for c in report["candidates"]}
    assert {"workflow", "debug_pattern", "project_fact"} <= kinds

    wf = next(c for c in report["candidates"] if c["kind"] == "workflow")
    assert "pytest -q" in wf["content"] and wf["occurrences"] == 2
    assert "pytest" in wf["keywords"]
    # 只跑一次的命令不成候选
    assert not any("ruff" in c["content"] for c in report["candidates"])

    dbg = next(c for c in report["candidates"] if c["kind"] == "debug_pattern")
    assert dbg["evidence"]["tool"] == "grep" and "Error" in dbg["content"]

    pf = next(c for c in report["candidates"] if c["kind"] == "project_fact")
    assert pf["related_paths"] == ["openx/services/distill.py"] and pf["occurrences"] == 2

    # 类序：debug_pattern 在前
    assert report["candidates"][0]["kind"] == "debug_pattern"
    assert "Distill report" in render_text(report)

    # 写入（临时 store，绝不碰真实 home）
    import tempfile
    from pathlib import Path as _P

    from ..coding_memory import CodingMemoryStore

    with tempfile.TemporaryDirectory() as td:
        store = CodingMemoryStore(
            workspace="/tmp/ws",
            global_dir=_P(td) / "global",
            projects_root=_P(td) / "projects",
        )
        saved = save_candidates(store, report["candidates"])
        assert len(saved) == len(report["candidates"])
        assert all(m.source == "distill" for m in saved)
        # 幂等：再存一次不新增
        save_candidates(store, report["candidates"])
        assert len(store.list_all()) == len(report["candidates"])

    # 回账：召回报告
    recall_events = [
        {"type": "memory_recall", "ids": ["m1", "m2"]},
        {"type": "memory_recall", "ids": ["m1"]},
    ]
    rr = recall_report([("s1", recall_events)])
    assert rr["schema"] == "openx-recall-report/v1"
    assert rr["prompt_builds"] == 2 and rr["distinct"] == 2
    assert rr["ranked"][0] == {"id": "m1", "recalls": 2}
    assert "m1" in render_recall(rr)
    assert "No memories recalled yet" in render_recall(recall_report([]))

    # 空会话：无候选
    empty = distill_sessions([])
    assert empty["candidates"] == []
    assert "No candidate experiences found" in render_text(empty)

    json.loads(json.dumps(report))
    print("openx/services/distill.py OK ✓")
