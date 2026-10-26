"""E4 评测门执行器 · 纯逻辑面：任务集解析 / 确定性判定 / 证据渲染。

执行编排（带 vs 摘两侧、探针复原、账本零决策）在内核面测：
``tests/kernel/test_eval_gate_runner.py``。这里不造内核——假 kernel 验证
前置守卫与编排顺序即可。

运行：``python -m pytest tests/services/test_eval_runner.py -q``
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from openx.services.eval_runner import (
    DEFAULT_TASK_TIMEOUT,
    SCHEMA,
    EvalSetError,
    EvalTask,
    evaluate_scaffold,
    judge_output,
    load_eval_set,
    render_text,
    resolve_eval_set_path,
)


@pytest.fixture
def eval_set(tmp_path) -> Path:
    p = tmp_path / "evals" / "tasks.jsonl"
    p.parent.mkdir()
    p.write_text(
        "\n".join(
            [
                json.dumps({"id": "a", "task": "say hi", "expect": {"contains": "hi"}}),
                json.dumps(
                    {
                        "task": "plain",
                        "expect": {"contains": ["x"], "not_contains": ["y"], "regex": r"\d+"},
                        "tools": ["read"],
                        "timeout": 5,
                    }
                ),
                "",
            ]
        ),
        encoding="utf-8",
    )
    return p


def test_load_eval_set_valid(eval_set):
    tasks = load_eval_set(eval_set)
    assert [t.id for t in tasks] == ["a", "t2"]  # 缺 id 按行号补
    t2 = tasks[1]
    assert t2.expect["not_contains"] == ["y"]
    assert t2.tools == ("read",)
    assert t2.timeout == 5.0
    assert tasks[0].timeout == DEFAULT_TASK_TIMEOUT
    assert tasks[0].tools == ()


@pytest.mark.parametrize(
    "line",
    [
        "{not json}",
        json.dumps({"task": "x"}),  # 无 expect
        json.dumps({"task": "x", "expect": {}}),  # 空判定
        json.dumps({"task": "x", "expect": {"vague": 1}}),  # 未知判定键
        json.dumps({"task": "x", "expect": {"contains": ""}}),  # 空串判定
        json.dumps({"task": "x", "expect": {"contains": 1}}),  # 非串
        json.dumps({"task": "x", "expect": {"regex": "("}}),  # 编译不过
        json.dumps({"task": "", "expect": {"contains": "x"}}),  # 空任务
        json.dumps({"task": "  ", "expect": {"contains": "x"}}),
        json.dumps({"task": "x", "expect": {"contains": "ok"}, "timeout": -1}),
        json.dumps({"task": "x", "expect": {"contains": "ok"}, "tools": [1]}),
        json.dumps({"task": "x", "expect": {"contains": "ok"}, "id": 7}),
        "null",
    ],
)
def test_load_eval_set_rejects_bad_rows(tmp_path, line):
    p = tmp_path / "bad.jsonl"
    p.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(EvalSetError):
        load_eval_set(p)


def test_load_eval_set_rejects_empty_and_missing(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n\n", encoding="utf-8")
    with pytest.raises(EvalSetError, match="no tasks"):
        load_eval_set(empty)
    with pytest.raises(EvalSetError, match="cannot read"):
        load_eval_set(tmp_path / "absent.jsonl")


def test_resolve_eval_set_path(tmp_path):
    assert resolve_eval_set_path(str(tmp_path), "evals/x.jsonl") == tmp_path / "evals" / "x.jsonl"
    assert resolve_eval_set_path(str(tmp_path), "/abs/x.jsonl") == Path("/abs/x.jsonl")


def test_judge_output_deterministic():
    t = EvalTask("t", "q", {"contains": ["Hello", "world"]})
    assert judge_output(t, "hello, WORLD!") == []
    assert judge_output(t, "hello") == ["missing: 'world'"]
    t = EvalTask("t", "q", {"not_contains": ["no", "never"]})
    assert judge_output(t, "fine") == []
    assert judge_output(t, "No way") == ["forbidden hit: 'no'"]
    t = EvalTask("t", "q", {"regex": r"summary: \d+ words"})
    assert judge_output(t, "summary: 42 words") == []
    assert judge_output(t, "summary: words") == [r"regex miss: 'summary: \\d+ words'"]
    # 组合判定：失败逐条列出
    t = EvalTask("t", "q", {"contains": "a", "not_contains": "b", "regex": r"\d"})
    failed = judge_output(t, "b no digits")
    assert len(failed) == 3  # 缺 contains + 命中 not_contains + regex miss
    assert judge_output(t, "a ok 7") == []
    # None / 非 str 输出容错
    assert judge_output(EvalTask("t", "q", {"contains": "none"}), None) != []


class _FakeKernel:
    """前置守卫与探针调用的鸭子 kernel（不做真组合）。"""

    def __init__(self, *, scaffold=True, retired=False, eval_set="evals/x.jsonl"):
        self.row = {
            "id": "s",
            "phase": "retired" if retired else "active",
            "scaffold": scaffold,
            "retired": retired,
            "skip_reason": "scaffold-retired" if retired else "",
        }
        self.help = (
            {"scaffold": {"compensates": "c", "exit_when": "e", "eval_set": eval_set}}
            if scaffold
            else {}
        )
        self.probes: list[tuple[str, dict]] = []

    def list_plugins(self):
        return [dict(self.row)]

    def plugin_help(self, name):
        return dict(self.help)

    @contextmanager
    def scaffold_probe(self, name, decl=None):
        self.probes.append((name, dict(decl or {})))
        yield


def _patch(monkeypatch, batches: list[list[dict]]):
    calls: list[list[str]] = []

    async def _fake(agent, tasks):
        calls.append([t.id for t in tasks])
        return batches[len(calls) - 1]

    monkeypatch.setattr("openx.services.eval_runner._execute_batch", _fake)
    return calls


async def test_evaluate_orchestration_and_payload(monkeypatch):
    tasks = [EvalTask("t1", "q", {"contains": "x"}), EvalTask("t2", "q", {"contains": "y"})]
    calls = _patch(
        monkeypatch,
        [
            [{"id": "t1", "ok": True, "detail": ""}, {"id": "t2", "ok": False, "detail": "z"}],
            [{"id": "t1", "ok": True, "detail": ""}, {"id": "t2", "ok": True, "detail": ""}],
        ],
    )
    k = _FakeKernel()
    report = await evaluate_scaffold(None, k, "s", tasks, eval_set="evals/x.jsonl")
    # 带侧先跑、摘侧后跑（各一遍、同序）
    assert calls == [["t1", "t2"], ["t1", "t2"]]
    assert k.probes == [("s", {"compensates": "c", "exit_when": "e", "eval_set": "evals/x.jsonl"})]
    assert report["schema"] == SCHEMA
    assert report["plugin"] == "s" and report["eval_set"] == "evals/x.jsonl"
    assert report["tasks"] == 2
    assert report["gate"]["verdict"] == "retire"
    assert report["gate"]["delta"] == pytest.approx(0.5)
    assert report["runs"]["with_scaffold"][1]["detail"] == "z"
    json.loads(json.dumps(report))


async def test_evaluate_guard_messages(monkeypatch):
    tasks = [EvalTask("t1", "q", {"contains": "x"})]
    _patch(monkeypatch, [[]])
    with pytest.raises(ValueError, match="plugin not found: ghost"):
        await evaluate_scaffold(None, _FakeKernel(), "ghost", tasks)
    with pytest.raises(ValueError, match="not a scaffold"):
        await evaluate_scaffold(None, _FakeKernel(scaffold=False), "s", tasks)
    with pytest.raises(ValueError, match="already retired"):
        await evaluate_scaffold(None, _FakeKernel(retired=True), "s", tasks)
    with pytest.raises(ValueError, match="no eval_set"):
        await evaluate_scaffold(None, _FakeKernel(eval_set=""), "s", tasks)


def test_render_text_both_verdicts():
    keep = {
        "schema": SCHEMA,
        "plugin": "histcompact",
        "eval_set": "evals/long-session.jsonl",
        "tolerance": 0.0,
        "tasks": 2,
        "gate": {
            "verdict": "keep",
            "delta": -0.5,
            "tolerance": 0.0,
            "with_scaffold": {"success_rate": 1.0},
            "without_scaffold": {"success_rate": 0.5},
        },
        "runs": {
            "with_scaffold": [
                {"id": "t1", "ok": True, "detail": ""},
                {"id": "t2", "ok": True, "detail": ""},
            ],
            "without_scaffold": [
                {"id": "t1", "ok": True, "detail": ""},
                {"id": "t2", "ok": False, "detail": "missing: 'COMPACTED'"},
            ],
        },
    }
    text = render_text(keep)
    assert "histcompact" in text and "KEEP" in text
    assert "1/2" in text and "2/2" in text
    assert "[without] t2: missing: 'COMPACTED'" in text

    retire = json.loads(json.dumps(keep))
    retire["gate"] = {
        "verdict": "retire",
        "delta": 0.0,
        "tolerance": 0.0,
        "with_scaffold": {"success_rate": 1.0},
        "without_scaffold": {"success_rate": 1.0},
    }
    assert "RETIRE" in render_text(retire)
