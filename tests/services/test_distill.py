"""E6 经验沉淀闭环 · ``distill``（会话账本 → 候选经验 → 记忆写入 / 召回回账）。

覆盖：
- 三类候选：workflow（反复成功的 shell 命令）、debug_pattern（同名工具先错后成）、
  project_fact（反复编辑的热点文件）；
- 阈值过滤（只跑一次的命令不成候选）；去重；类序排序；
- ``save_candidates`` 写进 coding_memory（source=distill、幂等）；
- ``recall_report`` 汇总 ``memory_recall`` 事件；
- ``CodingMemoryStore.build_context_prompt(collect=...)`` 出参；
- ``distill_workspace`` 端到端读会话文件；``/distill`` 命令面。

运行：``python -m pytest tests/services/test_distill.py -q``
"""

from __future__ import annotations

import json

import pytest

from openx.coding_memory import CodingMemoryStore
from openx.orchestration.sessions import SessionStore
from openx.services.distill import (
    SCHEMA,
    distill_sessions,
    distill_workspace,
    recall_report,
    recall_report_workspace,
    render_recall,
    render_text,
    save_candidates,
)


def _msg(role, **kw):
    return {"type": "message", "ts": 0.0, "message": {"role": role, **kw}}


def _call(cid, name, **args):
    return {"id": cid, "function": {"name": name, "arguments": json.dumps(args)}}


def _rich_session():
    return [
        _msg("assistant", content="", tool_calls=[
            _call("a", "shell", command="pytest -q"),
            _call("b", "shell", command="pytest  -q"),   # 空白差异 → 归一后同命令
        ]),
        _msg("tool", tool_call_id="a", content="ok"),
        _msg("tool", tool_call_id="b", content="2 passed"),
        _msg("assistant", content="", tool_calls=[
            _call("c", "shell", command="ruff check ."),  # 只跑一次 → 不成候选
        ]),
        _msg("tool", tool_call_id="c", content="All checks passed"),
        _msg("assistant", content="", tool_calls=[
            _call("d", "edit_file", path="openx/services/distill.py"),
            _call("e", "edit_file", path="openx/services/distill.py"),
        ]),
        _msg("tool", tool_call_id="d", content="patched"),
        _msg("tool", tool_call_id="e", content="patched"),
    ]


def _debug_session():
    return [
        _msg("assistant", content="", tool_calls=[_call("f", "grep", pattern="foo")]),
        _msg("tool", tool_call_id="f", content="Error: pattern invalid"),
        _msg("assistant", content="", tool_calls=[_call("g", "grep", pattern="bar")]),
        _msg("tool", tool_call_id="g", content="bar: 3 matches"),
    ]


# ── distill_sessions：三类候选 ───────────────────────────────────


def test_distill_all_three_kinds():
    report = distill_sessions(
        [("s1", _rich_session()), ("s2", _debug_session())], workspace="/tmp/ws"
    )
    assert report["schema"] == SCHEMA and report["sessions"] == 2
    by_kind = {c["kind"]: c for c in report["candidates"]}
    assert set(by_kind) == {"workflow", "debug_pattern", "project_fact"}

    wf = by_kind["workflow"]
    assert wf["content"].endswith("pytest -q") and wf["occurrences"] == 2
    assert "pytest" in wf["keywords"] and wf["category"] == "workflow"

    dbg = by_kind["debug_pattern"]
    assert dbg["evidence"]["tool"] == "grep" and "Error" in dbg["content"]
    assert dbg["category"] == "debug_pattern"

    pf = by_kind["project_fact"]
    assert pf["related_paths"] == ["openx/services/distill.py"] and pf["occurrences"] == 2


def test_threshold_filters_single_run_command():
    report = distill_sessions([("s1", _rich_session())])
    assert not any("ruff" in c["content"] for c in report["candidates"])


def test_kind_order_debug_first():
    report = distill_sessions(
        [("s1", _rich_session()), ("s2", _debug_session())]
    )
    assert report["candidates"][0]["kind"] == "debug_pattern"


def test_empty_report():
    report = distill_sessions([])
    assert report["candidates"] == []
    assert "No candidate experiences found" in render_text(report)


def test_report_json_serializable():
    report = distill_sessions([("s1", _rich_session())])
    json.loads(json.dumps(report))


def test_render_lists_candidates():
    report = distill_sessions([("s1", _rich_session()), ("s2", _debug_session())])
    text = render_text(report)
    assert "workflow" in text and "debug_pattern" in text
    assert "/distill save" in text


# ── save_candidates：写入 coding_memory ──────────────────────────


@pytest.fixture
def tmp_store(tmp_path):
    return CodingMemoryStore(
        workspace=str(tmp_path / "ws"),
        global_dir=tmp_path / "cm-global",
        projects_root=tmp_path / "cm-projects",
    )


def test_save_candidates_writes_and_is_idempotent(tmp_store):
    report = distill_sessions([("s1", _rich_session()), ("s2", _debug_session())])
    cands = report["candidates"]
    saved = save_candidates(tmp_store, cands)
    assert len(saved) == len(cands)
    assert all(m.source == "distill" for m in saved)

    # 幂等：同内容再存不新增
    save_candidates(tmp_store, cands)
    assert len(tmp_store.list_all()) == len(cands)


def test_save_candidates_skips_empty_content(tmp_store):
    saved = save_candidates(tmp_store, [{"category": "project_fact", "content": "  "}])
    assert saved == []


# ── recall 回账 ──────────────────────────────────────────────────


def test_recall_report_aggregates():
    events = [
        {"type": "memory_recall", "ids": ["m1", "m2"]},
        {"type": "memory_recall", "ids": ["m1"]},
    ]
    rr = recall_report([("s1", events)])
    assert rr["schema"] == "openx-recall-report/v1"
    assert rr["prompt_builds"] == 2 and rr["distinct"] == 2
    assert rr["ranked"][0] == {"id": "m1", "recalls": 2}


def test_recall_report_empty_render():
    rr = recall_report([])
    assert rr["ranked"] == []
    assert "No memories recalled yet" in render_recall(rr)


def test_build_context_prompt_collect(tmp_store):
    tmp_store.remember("use pytest not unittest", category="code_convention",
                       keywords=["pytest"], related_paths=["tests/**"])
    collected: list = []
    prompt = tmp_store.build_context_prompt(collect=collected)
    assert prompt and len(collected) == 1
    assert collected[0].category == "code_convention"
    # 不传 collect：无副作用
    assert tmp_store.build_context_prompt() == prompt


# ── 端到端：读会话文件 ───────────────────────────────────────────


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


def test_distill_workspace_end_to_end(tmp_path, sessions_dir):
    ws = str(tmp_path / "ws")
    _write_session(sessions_dir, ws, "s1", _rich_session())
    report = distill_workspace(ws)
    assert report["sessions"] == 1
    assert any(c["kind"] == "workflow" for c in report["candidates"])


def test_recall_report_workspace_end_to_end(tmp_path, sessions_dir):
    ws = str(tmp_path / "ws")
    _write_session(sessions_dir, ws, "s1", [
        {"type": "memory_recall", "ids": ["m1"], "categories": ["debug_pattern"]},
        {"type": "memory_recall", "ids": ["m1", "m2"]},
    ])
    rr = recall_report_workspace(ws)
    assert rr["prompt_builds"] == 2 and rr["ranked"][0]["id"] == "m1"


# ── CLI 命令面：/distill ─────────────────────────────────────────


class _CapConsole:
    def __init__(self):
        self.infos = []
        self.successes = []
        self.raw_lines = []
        self.raw = self

    def print(self, *args, **kwargs):
        self.raw_lines.append(" ".join(str(a) for a in args))

    def print_info(self, message):
        self.infos.append(message)

    def print_success(self, message):
        self.successes.append(message)


class _FakeAgent:
    def __init__(self, ws, store):
        self.workspace = ws
        self.coding_memory = store


async def test_distill_command_list_and_json(tmp_path, sessions_dir, tmp_store):
    from openx.app.cli import commands

    ws = str(tmp_path / "ws")
    _write_session(sessions_dir, ws, "s1", _rich_session())
    agent = _FakeAgent(ws, tmp_store)

    stub = _CapConsole()
    assert await commands.handle_slash_command("distill", agent, stub, []) is True
    assert any("workflow" in line for line in stub.raw_lines)

    stub2 = _CapConsole()
    await commands.handle_slash_command("distill", agent, stub2, ["json"])
    assert json.loads("\n".join(stub2.raw_lines))["schema"] == SCHEMA


async def test_distill_command_save(tmp_path, sessions_dir, tmp_store):
    from openx.app.cli import commands

    ws = str(tmp_path / "ws")
    _write_session(sessions_dir, ws, "s1", _rich_session())
    agent = _FakeAgent(ws, tmp_store)

    stub = _CapConsole()
    await commands.handle_slash_command("distill", agent, stub, ["save"])
    assert stub.successes and tmp_store.list_all()


async def test_distill_command_recall(tmp_path, sessions_dir, tmp_store):
    from openx.app.cli import commands

    ws = str(tmp_path / "ws")
    _write_session(sessions_dir, ws, "s1", [
        {"type": "memory_recall", "ids": ["m1"]},
    ])
    agent = _FakeAgent(ws, tmp_store)
    stub = _CapConsole()
    await commands.handle_slash_command("distill", agent, stub, ["recall"])
    assert any("m1" in line for line in stub.raw_lines)


async def test_distill_command_save_no_candidates(tmp_path, sessions_dir, tmp_store):
    from openx.app.cli import commands

    ws = str(tmp_path / "ws")
    _write_session(sessions_dir, ws, "s1", [_msg("assistant", content="hi")])
    agent = _FakeAgent(ws, tmp_store)
    stub = _CapConsole()
    await commands.handle_slash_command("distill", agent, stub, ["save"])
    assert stub.infos and not stub.successes
