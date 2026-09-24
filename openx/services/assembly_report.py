"""E5 装配策略学习 · **离线装配报告**——装配了什么 vs 实际用了什么。

设计（`docs/design/openx-self-evolution-design.md` §4.2-§4.3）：调优线要学的
四样里有"装配策略"——"这类任务预装这些插件"的配对建议。本模块是它的**离线
证据面**：把**当前组合**（``kernel.list_plugins()`` 的只读投影）与**近期会话
用量**（会话账本里的工具调用）对齐，算出——

- **配对命中率**：某插件装进来后，它的工具是否真被调用过；
- **未使用者**：装了却零调用的插件（可考虑不装 / 批量回滚，§2.3 存活率盘点）；
- **使用频率序**：按调用次数排序的插件（§4.2「auto-* 目录按使用频率排序」的
  数据源）+ 最常用工具。

纪律（同 E3）：只**读**会话账本 + 只**读**内核清单；产出**建议不产动作**——
§4.3「先离线」：建议永远只是候选，人终审改 overlay（overlay 原语待 P6），
**绝不**自动改组合。报告只对**带工具的插件**判"未使用"——上下文/生命周期/UI
类插件不贡献工具，离线无调用可测，故标 ``tracked=false`` 而非误报"没用"。
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

from typing import Any

from ..orchestration.sessions import SessionStore

#: 报告 schema 版本。
SCHEMA = "openx-assembly-report/v1"

#: 最多分析最近多少会话（同 gap_report）。
MAX_SESSIONS = 50

#: 报告里展示的最常用工具条数。
TOP_TOOLS = 10


def _tool_names(message: dict[str, Any]) -> list[str]:
    """一条 assistant 消息里的工具名（保序、可重复）。"""
    names: list[str] = []
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function")
        name = fn.get("name") if isinstance(fn, dict) else None
        if name:
            names.append(str(name))
    return names


def _usage_by_tool(sessions: list[tuple[str, list[dict[str, Any]]]]) -> dict[str, int]:
    """跨会话统计每个工具的调用次数（assistant ``tool_calls`` 计数）。"""
    counts: dict[str, int] = {}
    for _session_id, events in sessions:
        for event in events:
            if event.get("type") != "message":
                continue
            message = event.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            for name in _tool_names(message):
                counts[name] = counts.get(name, 0) + 1
    return counts


def build_report(
    plugins: list[dict[str, Any]],
    sessions: list[tuple[str, list[dict[str, Any]]]],
    workspace: str = "",
) -> dict[str, Any]:
    """``(内核插件投影, 近期会话)`` → 装配报告（可 JSON 序列化）。

    ``plugins`` 取自 ``kernel.list_plugins()``（每行含 ``id`` / ``tools`` /
    ``builtin`` / ``scope`` / ``phase``）。只对 ``phase == "active"`` 的插件
    统计；``tracked`` 标记它是否贡献工具（无工具则离线不可测使用）。
    """
    calls = _usage_by_tool(sessions)
    session_ids = {sid for sid, _ in sessions}

    rows: list[dict[str, Any]] = []
    for plugin in plugins:
        if plugin.get("phase") != "active":
            continue
        tools = [str(t) for t in (plugin.get("tools") or [])]
        tracked = bool(tools)
        plugin_calls = sum(calls.get(t, 0) for t in tools)
        rows.append({
            "id": str(plugin.get("id", "")),
            "builtin": bool(plugin.get("builtin")),
            "scope": str(plugin.get("scope", "boot")),
            "type": str(plugin.get("type", "")),
            "tools": tools,
            "tracked": tracked,
            "calls": plugin_calls,
            "hit": bool(tracked and plugin_calls > 0),
        })

    unused = [
        r["id"] for r in rows
        if r["tracked"] and not r["builtin"] and r["calls"] == 0
    ]
    ranked = sorted(
        ({"id": r["id"], "calls": r["calls"]} for r in rows if r["tracked"]),
        key=lambda r: (-r["calls"], r["id"]),
    )
    tools_ranked = sorted(
        ({"name": name, "calls": n} for name, n in calls.items()),
        key=lambda t: (-t["calls"], t["name"]),
    )[:TOP_TOOLS]

    return {
        "schema": SCHEMA,
        "workspace": workspace,
        "sessions": len(session_ids),
        "tool_calls": sum(calls.values()),
        "plugins": rows,
        "unused": sorted(unused),
        "ranked": ranked,
        "tools": tools_ranked,
    }


def build_workspace_report(
    workspace: str,
    plugins: list[dict[str, Any]],
    limit: int = MAX_SESSIONS,
) -> dict[str, Any]:
    """某工作区最近 ``limit`` 个会话 + 当前组合 → 装配报告（同 E3 读取路径）。"""
    metas = SessionStore.list_for_workspace(workspace)[: max(0, limit)]
    sessions: list[tuple[str, list[dict[str, Any]]]] = []
    for meta in metas:
        if meta.path is None:
            continue
        sessions.append((meta.session_id, SessionStore.iter_events(meta.path)))
    return build_report(plugins, sessions, workspace=workspace)


def render_text(report: dict[str, Any]) -> str:
    """报告 dict → 人读文本（``/assembly`` 的展示）。"""
    head = (
        f"Assembly report · {report.get('sessions', 0)} session(s) · "
        f"{report.get('tool_calls', 0)} tool call(s) · "
        f"{len(report.get('plugins', []))} active plugin(s)"
    )
    lines = [head, ""]
    ranked = report.get("ranked") or []
    if ranked:
        lines.append("Usage (by tool calls):")
        for row in ranked:
            lines.append(f"  • {row['id']}: {row['calls']}")
    unused = report.get("unused") or []
    lines.append("")
    if unused:
        lines.append(f"Assembled but never used ({len(unused)}):")
        lines.extend(f"  • {pid}" for pid in unused)
    else:
        lines.append("Assembled but never used: none")
    tools = report.get("tools") or []
    if tools:
        top = ", ".join(f"{t['name']}×{t['calls']}" for t in tools)
        lines.append("")
        lines.append(f"Top tools: {top}")
    return "\n".join(lines)


def suggestions(report: dict[str, Any]) -> list[str]:
    """报告 → **建议**列表（人读；不是动作——§4.3 建议永远只是候选）。

    建议面向"改 overlay / 不该装什么"，文本形态（overlay 原语待 P6）。
    """
    out: list[str] = []
    unused = sorted(report.get("unused") or [])
    for pid in unused:
        if pid.startswith("auto-"):
            out.append(f"roll back auto plugin '{pid}': assembled but never invoked")
        else:
            out.append(f"consider not loading '{pid}': assembled but never invoked")
    if not unused:
        out.append("assembly matches usage — no unused plugins")
    return out


if __name__ == "__main__":
    import json as _json

    def _msg(role: str, **kw: Any) -> dict[str, Any]:
        return {"type": "message", "ts": 0.0, "message": {"role": role, **kw}}

    _plugins = [
        {"id": "builtin-tools", "phase": "active", "builtin": True,
         "tools": ["read_file", "grep", "shell"]},
        {"id": "dataviz", "phase": "active", "builtin": False, "scope": "session",
         "tools": ["plot"]},
        {"id": "auto-helper", "phase": "active", "builtin": False, "scope": "session",
         "tools": ["helper"]},
        {"id": "histcompact", "phase": "active", "builtin": False,
         "tools": [], "type": "context.memory"},   # 无工具 → 不可测，不误报
        {"id": "old-tool", "phase": "disabled", "builtin": False, "tools": ["x"]},
    ]
    _s1 = [
        _msg("assistant", content="", tool_calls=[
            {"id": "a", "function": {"name": "read_file", "arguments": "{}"}},
            {"id": "b", "function": {"name": "grep", "arguments": "{}"}},
            {"id": "c", "function": {"name": "plot", "arguments": "{}"}},
        ]),
    ]
    _s2 = [
        _msg("assistant", content="", tool_calls=[
            {"id": "d", "function": {"name": "shell", "arguments": "{}"}},
            {"id": "e", "function": {"name": "grep", "arguments": "{}"}},
        ]),
    ]

    report = build_report(_plugins, [("s1", _s1), ("s2", _s2)], workspace="/tmp/ws")
    assert report["schema"] == SCHEMA
    assert report["sessions"] == 2 and report["tool_calls"] == 5
    by_id = {r["id"]: r for r in report["plugins"]}
    assert "old-tool" not in by_id                      # 非 active 不入统计
    assert by_id["builtin-tools"]["calls"] == 4 and by_id["builtin-tools"]["hit"]
    assert by_id["dataviz"]["calls"] == 1 and by_id["dataviz"]["hit"]
    assert by_id["auto-helper"]["calls"] == 0 and not by_id["auto-helper"]["hit"]
    assert by_id["histcompact"]["tracked"] is False      # 无工具 → 不计未使用
    assert report["unused"] == ["auto-helper"]           # builtin/无工具不误报
    assert report["ranked"][0]["id"] == "builtin-tools" and report["ranked"][0]["calls"] == 4
    assert report["tools"][0]["name"] == "grep" and report["tools"][0]["calls"] == 2

    text = render_text(report)
    assert "dataviz: 1" in text and "auto-helper" in text and "grep×2" in text

    sug = suggestions(report)
    assert any("auto-helper" in s and "roll back" in s for s in sug)

    # 无未使用：建议为"一致"（补一次 helper 调用，令 auto-helper 命中）
    _s3 = [_msg("assistant", content="", tool_calls=[
        {"id": "f", "function": {"name": "helper", "arguments": "{}"}},
    ])]
    clean = build_report(_plugins, [("s1", _s1 + _s2 + _s3)])
    assert not clean["unused"]
    assert suggestions(clean) == ["assembly matches usage — no unused plugins"]

    # 空会话：全零命中，未使用 = 全部带工具的非内置插件
    empty = build_report(_plugins, [])
    assert empty["tool_calls"] == 0 and "auto-helper" in empty["unused"]

    _json.loads(_json.dumps(report))
    print("openx/services/assembly_report.py OK ✓")
