"""P-E 轨迹升级 · ``eval_export``（账本 → 评测任务集 + 对照数据）。

覆盖：
- ``export_session``：回合配对（user 开启、assistant 累计、``turn_usage`` 按序
  回填成本字段）、工具名去重、totals 汇总、空会话 → None；
- ``export_workspace``：按工作区枚举会话写 JSONL（一行一会话），跳过无回合的
  空会话；输出可用 ``json.loads`` 解析。

SESSIONS_DIR 隔离到 tmp_path，绝不触碰真实 ``~/.openx``。
运行：``python -m pytest tests/services/test_eval_export.py -q``
"""

from __future__ import annotations

import json

import pytest

from openx.orchestration.sessions import SessionStore
from openx.services.eval_export import SCHEMA, export_session, export_workspace


def _session_lines(session_id: str, *, with_turn: bool = True) -> list[dict]:
    lines: list[dict] = [
        {"type": "meta", "version": 1, "session_id": session_id,
         "workspace": "/tmp/ws", "model": "m-1", "group": "default",
         "created_at": "2026-01-01T00:00:00+00:00",
         "updated_at": "2026-01-01T00:01:00+00:00"},
    ]
    if not with_turn:
        return lines
    lines += [
        {"type": "message", "ts": 1.0,
         "message": {"role": "user", "content": "do the thing"}},
        {"type": "message", "ts": 2.0,
         "message": {"role": "assistant", "content": "",
                     "tool_calls": [{"function": {"name": "read_file"}},
                                    {"function": {"name": "grep"}}]}},
        {"type": "message", "ts": 3.0,
         "message": {"role": "tool", "tool_call_id": "c1", "content": "big output"}},
        {"type": "message", "ts": 4.0,
         "message": {"role": "assistant", "content": "finished"}},
        {"seq": 1, "ts": 4.5, "origin": "kernel",
         "payload": {"type": "turn_usage", "session_id": session_id,
                     "turn_index": 1, "input_tokens": 1200, "output_tokens": 300,
                     "cached_tokens": 40, "plugin_tokens": 400, "duration_ms": 3400}},
    ]
    return lines


def _write(path, lines: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n",
        encoding="utf-8",
    )


# ── export_session ───────────────────────────────────────────────


def test_export_session_pairs_turn_and_usage(tmp_path):
    path = tmp_path / "s1.jsonl"
    _write(path, _session_lines("s1"))

    rec = export_session(path)
    assert rec is not None
    assert rec["schema"] == SCHEMA
    assert rec["session_id"] == "s1" and rec["model"] == "m-1"
    assert len(rec["turns"]) == 1
    turn = rec["turns"][0]
    assert turn["user"] == "do the thing" and turn["assistant"] == "finished"
    assert turn["tools"] == ["read_file", "grep"] and turn["tool_calls"] == 2
    assert (turn["input_tokens"], turn["output_tokens"]) == (1200, 300)
    assert turn["cached_tokens"] == 40 and turn["plugin_tokens"] == 400
    assert turn["duration_ms"] == 3400
    assert rec["totals"]["turns"] == 1
    assert rec["totals"]["input_tokens"] == 1200
    json.loads(json.dumps(rec))  # 可序列化


def test_export_session_empty_returns_none(tmp_path):
    path = tmp_path / "empty.jsonl"
    _write(path, _session_lines("empty", with_turn=False))
    assert export_session(path) is None


def test_export_session_tolerates_missing_meta(tmp_path):
    """无 meta 行（只有消息）时用文件名兜底，不抛。"""
    path = tmp_path / "nometa.jsonl"
    _write(path, [
        {"type": "message", "ts": 1.0,
         "message": {"role": "user", "content": "hi"}},
        {"type": "message", "ts": 2.0,
         "message": {"role": "assistant", "content": "yo"}},
    ])
    rec = export_session(path)
    assert rec is not None and rec["session_id"] == "nometa"
    assert rec["turns"][0]["user"] == "hi"
    assert rec["turns"][0]["input_tokens"] == 0  # 无 turn_usage → 全 0


# ── export_workspace ─────────────────────────────────────────────


@pytest.fixture
def sessions_dir(tmp_path, monkeypatch):
    d = tmp_path / "sessions"
    monkeypatch.setattr("openx.orchestration.sessions.SESSIONS_DIR", d)
    return d


def test_export_workspace_writes_jsonl_and_skips_empty(tmp_path, sessions_dir):
    ws = str(tmp_path / "ws")
    sub = sessions_dir / SessionStore.workspace_hash(ws)
    sub.mkdir(parents=True)
    _write(sub / "s1.jsonl", _session_lines("s1"))
    _write(sub / "s2.jsonl", _session_lines("s2"))
    _write(sub / "empty.jsonl", _session_lines("empty", with_turn=False))

    out = tmp_path / "eval.jsonl"
    target, count = export_workspace(ws, out_path=out)

    assert target == out and count == 2  # 空会话被跳过
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [r["session_id"] for r in rows] == ["s1", "s2"]
    assert all(r["schema"] == SCHEMA for r in rows)


def test_export_workspace_empty_workspace(tmp_path, sessions_dir):
    out = tmp_path / "eval.jsonl"
    target, count = export_workspace(str(tmp_path / "nope"), out_path=out)
    assert count == 0
    assert target.read_text(encoding="utf-8") == ""
