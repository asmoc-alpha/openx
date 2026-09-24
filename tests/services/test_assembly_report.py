"""E5 装配策略学习 · ``assembly_report``（当前组合 × 近期用量 → 配对命中率）。

覆盖：
- 插件调用归因（工具调用按插件工具集求和）、命中标记；
- 未使用者（装了却零调用的**带工具非内置**插件）；无工具插件不误报；
- 使用频率序 + 最常用工具；建议文本（人终审）；
- ``build_workspace_report`` 端到端读会话文件。

运行：``python -m pytest tests/services/test_assembly_report.py -q``
"""

from __future__ import annotations

import json

import pytest

from openx.orchestration.sessions import SessionStore
from openx.services.assembly_report import (
    SCHEMA,
    build_report,
    build_workspace_report,
    render_text,
    suggestions,
)

PLUGINS = [
    {"id": "builtin-tools", "phase": "active", "builtin": True,
     "tools": ["read_file", "grep", "shell"]},
    {"id": "dataviz", "phase": "active", "builtin": False, "scope": "session",
     "tools": ["plot"]},
    {"id": "auto-helper", "phase": "active", "builtin": False, "scope": "session",
     "tools": ["helper"]},
    {"id": "histcompact", "phase": "active", "builtin": False,
     "tools": [], "type": "context.memory"},
    {"id": "old", "phase": "disabled", "builtin": False, "tools": ["x"]},
]


def _msg(role, **kw):
    return {"type": "message", "ts": 0.0, "message": {"role": role, **kw}}


def _calls(*names):
    return _msg("assistant", content="", tool_calls=[
        {"id": f"c{i}", "function": {"name": n, "arguments": "{}"}}
        for i, n in enumerate(names)
    ])


def test_build_report_attributes_calls_to_plugins():
    sessions = [("s1", [_calls("read_file", "grep", "plot")]),
                ("s2", [_calls("shell", "grep")])]
    report = build_report(PLUGINS, sessions, workspace="/tmp/ws")
    assert report["schema"] == SCHEMA
    assert report["sessions"] == 2 and report["tool_calls"] == 5

    by_id = {r["id"]: r for r in report["plugins"]}
    assert "old" not in by_id                                  # 非 active 不入
    assert by_id["builtin-tools"]["calls"] == 4 and by_id["builtin-tools"]["hit"]
    assert by_id["dataviz"]["calls"] == 1
    assert by_id["auto-helper"]["calls"] == 0 and not by_id["auto-helper"]["hit"]
    assert by_id["histcompact"]["tracked"] is False            # 无工具 → 不可测


def test_unused_excludes_builtin_and_untracked():
    report = build_report(PLUGINS, [("s1", [_calls("grep")])])
    # 未调用的带工具非内置插件：dataviz 与 auto-helper（builtin/无工具不列）
    assert report["unused"] == ["auto-helper", "dataviz"]
    assert report["ranked"][0]["id"] == "builtin-tools"
    assert report["tools"][0] == {"name": "grep", "calls": 1}


def test_render_and_suggestions():
    report = build_report(PLUGINS, [("s1", [_calls("grep", "plot")])])
    text = render_text(report)
    assert "dataviz: 1" in text and "auto-helper" in text and "grep×1" in text
    sug = suggestions(report)
    assert any("auto-helper" in s and "roll back" in s for s in sug)  # auto-* 提示回滚


def test_suggestions_when_assembly_matches_usage():
    report = build_report(PLUGINS, [("s1", [_calls("grep", "plot", "helper")])])
    assert report["unused"] == []
    assert suggestions(report) == ["assembly matches usage — no unused plugins"]


def test_empty_sessions_all_unused():
    report = build_report(PLUGINS, [])
    assert report["tool_calls"] == 0
    assert report["unused"] == ["auto-helper", "dataviz"]
    assert "never used" in render_text(report)


def test_report_json_serializable():
    report = build_report(PLUGINS, [("s1", [_calls("grep")])])
    json.loads(json.dumps(report))


# ── build_workspace_report：端到端读会话文件 ─────────────────────


@pytest.fixture
def sessions_dir(tmp_path, monkeypatch):
    d = tmp_path / "sessions"
    monkeypatch.setattr("openx.orchestration.sessions.SESSIONS_DIR", d)
    return d


def _write_session(sessions_dir, workspace, session_id, lines):
    sub = sessions_dir / SessionStore.workspace_hash(workspace)
    sub.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "meta", "version": 1, "session_id": session_id,
             "workspace": workspace, "model": "m", "group": "default",
             "created_at": "2026-01-01T00:00:00+00:00",
             "updated_at": "2026-01-01T00:01:00+00:00"}]
    rows.extend(lines)
    (sub / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )


def test_build_workspace_report_end_to_end(tmp_path, sessions_dir):
    ws = str(tmp_path / "ws")
    _write_session(sessions_dir, ws, "s1", [_calls("grep", "plot")])
    report = build_workspace_report(ws, PLUGINS)
    assert report["sessions"] == 1 and report["tool_calls"] == 2
    assert report["unused"] == ["auto-helper"]


def test_build_workspace_report_empty(tmp_path, sessions_dir):
    report = build_workspace_report(str(tmp_path / "nope"), PLUGINS)
    assert report["sessions"] == 0 and report["tool_calls"] == 0


# ── CLI 命令面：/assembly ───────────────────────────────────────


class _CapConsole:
    def __init__(self):
        self.infos = []
        self.raw_lines = []
        self.raw = self

    def print(self, *args, **kwargs):
        self.raw_lines.append(" ".join(str(a) for a in args))

    def print_info(self, message):
        self.infos.append(message)


class _FakeKernel:
    def ensure_loaded(self, workspace):
        pass

    def list_plugins(self):
        return list(PLUGINS)


async def test_assembly_command(tmp_path, sessions_dir, monkeypatch):
    from openx.app.cli import commands

    monkeypatch.setattr("openx.kernel.get_kernel", lambda: _FakeKernel())
    ws = str(tmp_path / "ws")
    _write_session(sessions_dir, ws, "s1", [_calls("grep", "plot")])

    class _Agent:
        pass

    agent = _Agent()
    agent.workspace = ws

    stub = _CapConsole()
    assert await commands.handle_slash_command("assembly", agent, stub, []) is True
    assert any("auto-helper" in line for line in stub.raw_lines)

    stub2 = _CapConsole()
    await commands.handle_slash_command("assembly", agent, stub2, ["json"])
    assert json.loads("\n".join(stub2.raw_lines))["schema"] == SCHEMA

    stub3 = _CapConsole()
    await commands.handle_slash_command("assembly", agent, stub3, ["suggestions"])
    assert any("auto-helper" in line for line in stub3.raw_lines)
