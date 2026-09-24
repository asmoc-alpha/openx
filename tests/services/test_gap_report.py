"""E3 缺口感知 · ``gap_report``（账本 → 失败模式聚类 → 缺口报告）。

覆盖：
- 四类信号：工具错误（按 tool_call_id 回填工具名）、权限摩擦（ASK-批准）、
  反复被拒、绕路（同 (工具,参数) 重复）；
- 阈值过滤（偶发信号不入报告）；严重度排序；
- ``render_text`` 人读文本；``as_context_fragment`` 回喂片段（空报告 → 空串）；
- ``analyze_workspace`` 端到端读会话文件（SESSIONS_DIR 隔离到 tmp）。

运行：``python -m pytest tests/services/test_gap_report.py -q``
"""

from __future__ import annotations

import json

import pytest

from openx.orchestration.sessions import SessionStore
from openx.services.gap_report import (
    SCHEMA,
    analyze_sessions,
    analyze_workspace,
    as_context_fragment,
    render_text,
)


def _msg(role: str, **kw):
    return {"type": "message", "ts": 0.0, "message": {"role": role, **kw}}


def _perm(tool: str, verdict: str, approved: bool):
    return {"type": "permission_decision", "tool": tool,
            "verdict": verdict, "approved": approved}


def _rich_session() -> list[dict]:
    return [
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
        _perm("shell", "ASK", True),
        _perm("write_file", "DENY", False), _perm("write_file", "DENY", False),
    ]


# ── analyze_sessions：四类信号 ───────────────────────────────────


def test_analyze_clusters_all_four_signals():
    report = analyze_sessions([("s1", _rich_session())], workspace="/tmp/ws")
    assert report["schema"] == SCHEMA
    assert report["sessions"] == 1 and report["tool_calls"] == 5

    by_kind = {g["kind"]: g for g in report["gaps"]}
    assert set(by_kind) == {
        "tool_failure", "permission_friction", "denied_calls", "detour"
    }
    fail = by_kind["tool_failure"]
    assert fail["key"] == "shell" and fail["count"] == 4 and fail["calls"] == 4
    assert fail["sessions"] == ["s1"]
    assert by_kind["permission_friction"]["count"] == 3
    assert by_kind["denied_calls"]["key"] == "write_file"
    assert by_kind["detour"]["count"] == 4


def test_severity_order_count_then_kind():
    report = analyze_sessions([("s1", _rich_session())])
    # count 并列（shell 错误 4、绕路 4）时按类序：tool_failure 在前
    assert report["gaps"][0]["kind"] == "tool_failure"


def test_thresholds_filter_incidental_signals():
    # 单次错误 / 两次 ASK / 单次拒绝 / 两次重复 —— 均低于阈值，不入报告
    events = [
        _msg("assistant", content="", tool_calls=[
            {"id": "a", "function": {"name": "read_file", "arguments": "{}"}},
        ]),
        _msg("tool", tool_call_id="a", content="Error: nope"),
        _perm("shell", "ASK", True), _perm("shell", "ASK", True),
        _perm("edit", "DENY", False),
    ]
    assert analyze_sessions([("s", events)])["gaps"] == []


def test_error_detection_is_line_anchored():
    """行首 ``Error:`` 才算错误；正文中间的 'Error:' 不算（启发式收紧）。"""
    events = [
        _msg("assistant", content="", tool_calls=[
            {"id": "a", "function": {"name": "grep", "arguments": "{}"}},
            {"id": "b", "function": {"name": "grep", "arguments": "{}"}},
        ]),
        _msg("tool", tool_call_id="a", content="log: something Error: happened"),
        _msg("tool", tool_call_id="b", content="Error: real failure"),
    ]
    report = analyze_sessions([("s", events)])
    fail = next((g for g in report["gaps"] if g["kind"] == "tool_failure"), None)
    assert fail is None or fail["count"] == 1  # 仅 b 计入（<2 阈值 → 不入）


def test_multi_session_accumulates():
    short = [
        _msg("assistant", content="", tool_calls=[
            {"id": "x", "function": {"name": "shell", "arguments": '{"cmd":"ls"}'}},
        ]),
        _msg("tool", tool_call_id="x", content="Error: ls fail"),
    ]
    report = analyze_sessions([("s1", short), ("s2", short)])
    fail = next(g for g in report["gaps"] if g["kind"] == "tool_failure")
    assert fail["count"] == 2 and fail["sessions"] == ["s1", "s2"]


# ── 渲染与回喂 ───────────────────────────────────────────────────


def test_render_and_fragment():
    report = analyze_sessions([("s1", _rich_session())])
    text = render_text(report)
    assert "tool_failure" in text and "shell" in text
    assert "No gaps detected" not in text

    frag = as_context_fragment(report)
    assert frag.startswith("Observed capability gaps")
    assert "tool_failure: shell (×4)" in frag


def test_empty_report_render_and_fragment():
    report = analyze_sessions([])
    assert report["gaps"] == []
    assert "No gaps detected" in render_text(report)
    assert as_context_fragment(report) == ""


def test_report_is_json_serializable():
    report = analyze_sessions([("s1", _rich_session())])
    json.loads(json.dumps(report))


# ── analyze_workspace：端到端读会话文件 ──────────────────────────


@pytest.fixture
def sessions_dir(tmp_path, monkeypatch):
    d = tmp_path / "sessions"
    monkeypatch.setattr("openx.orchestration.sessions.SESSIONS_DIR", d)
    return d


def _write_session(sessions_dir, workspace: str, session_id: str, lines: list[dict]):
    sub = sessions_dir / SessionStore.workspace_hash(workspace)
    sub.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "meta", "version": 1, "session_id": session_id,
             "workspace": workspace, "model": "m", "group": "default",
             "created_at": "2026-01-01T00:00:00+00:00",
             "updated_at": "2026-01-01T00:01:00+00:00"}]
    for line in lines:
        if line.get("type") == "message":
            rows.append(line)
        else:  # 账本行（信封）
            rows.append({"seq": len(rows), "ts": 0.0, "session": session_id,
                         "type": line["type"], "payload": line, "cause": None,
                         "origin": "kernel", "digest": ""})
    (sub / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )


def test_analyze_workspace_end_to_end(tmp_path, sessions_dir):
    ws = str(tmp_path / "ws")
    _write_session(sessions_dir, ws, "s1", _rich_session())
    report = analyze_workspace(ws)
    assert report["sessions"] == 1
    assert any(g["kind"] == "permission_friction" for g in report["gaps"])


def test_analyze_workspace_empty(tmp_path, sessions_dir):
    report = analyze_workspace(str(tmp_path / "nope"))
    assert report["sessions"] == 0 and report["gaps"] == []


def test_analyze_workspace_limit(tmp_path, sessions_dir, monkeypatch):
    ws = str(tmp_path / "ws")
    for i in range(3):
        _write_session(sessions_dir, ws, f"s{i}", [
            _msg("assistant", content="", tool_calls=[
                {"id": "a", "function": {"name": "shell", "arguments": "{}"}},
            ]),
        ])
    report = analyze_workspace(ws, limit=2)
    assert report["sessions"] == 2


# ── CLI 命令面：/gaps ────────────────────────────────────────────


class _CapConsole:
    def __init__(self):
        self.infos: list[str] = []
        self.raw_lines: list[str] = []
        self.raw = self

    def print(self, *args, **kwargs):
        self.raw_lines.append(" ".join(str(a) for a in args))

    def print_info(self, message):
        self.infos.append(message)


class _FakeAgent:
    def __init__(self, ws):
        self.workspace = ws


async def test_gaps_command_renders_report(tmp_path, sessions_dir):
    from openx.app.cli import commands

    ws = str(tmp_path / "ws")
    _write_session(sessions_dir, ws, "s1", _rich_session())
    agent = _FakeAgent(ws)

    stub = _CapConsole()
    assert await commands.handle_slash_command("gaps", agent, stub, []) is True
    assert any("tool_failure" in line and "shell" in line for line in stub.raw_lines)

    stub2 = _CapConsole()
    await commands.handle_slash_command("gaps", agent, stub2, ["json"])
    assert json.loads("\n".join(stub2.raw_lines))["schema"] == SCHEMA

    stub3 = _CapConsole()
    await commands.handle_slash_command("gaps", agent, stub3, ["context"])
    assert any("Observed capability gaps" in line for line in stub3.raw_lines)

