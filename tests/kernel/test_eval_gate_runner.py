"""E4 评测门执行器 × 内核：``scaffold_probe`` 探针 + ``evaluate_scaffold`` 全流程。

覆盖：
- 探针：内存级临时摘除走与真实退场相同的组合跳过路径（工具消失），退出复原
  （工具回来）；**全局账本不添任何决策事件**——探针是测量不是裁决；
- 评测门全流程：带/摘两侧各跑一遍（假执行器按"compact 工具在不在册"辨认
  自己在哪一侧——顺带验证探针真的改变了组合）→ compare_success 裁决 →
  证据 payload；摘除侧掉点 → keep；
- 前置守卫：未声明 eval_set / 已退场（须先回挂）被拒；
- CLI：``/scaffolds eval`` 报告渲染；``--retire`` 仅在裁决为准许时退场且
  ``scaffold_retired`` 证据带上本次评测 payload。

环境：kernel_env 已把 SETTINGS_PATH 与 GLOBAL_LEDGER_PATH 隔离到 tmp。
运行：``python -m pytest tests/kernel/test_eval_gate_runner.py -q``
"""

from __future__ import annotations

import json
import textwrap

import pytest

from openx.kernel import get_kernel
from openx.services.eval_runner import (
    EvalSetError,
    evaluate_scaffold,
    load_eval_set,
    resolve_eval_set_path,
)

from ._helpers import write_plugin

SCAFFOLD_SRC = textwrap.dedent('''\
    """E4：带脚手架声明 + eval_set 的工具插件（评测门的被评对象）。"""
    __openx_meta__ = {
        "type": "capability.tool", "mount": "loop.tool-call", "trust": "user",
        "summary": "历史压缩",
        "scaffold": {
            "compensates": "模型上下文有限",
            "exit_when": "长会话免压缩评测通过",
            "eval_set": "evals/long-session.jsonl",
        },
    }

    from openx.tools.base import Tool, ToolResult

    class CompactTool(Tool):
        name = "compact"
        description = "compress history"
        async def execute(self, **kw):
            return ToolResult(output="ok")

    def factory(host):
        return [CompactTool()]

    def apply(ctx):
        ctx.register_tool_factory("compact", factory)
''')

EVAL_SET_ROWS = [
    {"id": "t1", "task": "summarize a long session", "expect": {"contains": "COMPACTED"}},
    {"id": "t2", "task": "count words", "expect": {"regex": r"summary: \d+ words"}},
]


class Sink:
    """收集决策事件的全局账本 sink（test_scaffold_retirement 同款）。"""

    def __init__(self):
        self.events: list = []

    def __call__(self, event) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]

    def of(self, type_: str) -> list:
        return [e for e in self.events if e.type == type_]


def _tool_names(k) -> set[str]:
    return {e.name for e in k.registry("tools").entries()}


@pytest.fixture
def gated_workspace(kernel_env):
    """装好脚手架插件 + 写好 eval_set 的工作区，内核已装载、账本已挂 sink。"""
    ws, _ = kernel_env
    write_plugin(ws, "histcompact", SCAFFOLD_SRC)
    evals = ws / "evals"
    evals.mkdir()
    (evals / "long-session.jsonl").write_text(
        "\n".join(json.dumps(r) for r in EVAL_SET_ROWS), encoding="utf-8"
    )
    k = get_kernel()
    sink = Sink()
    k.attach_global_ledger(sink)
    k.ensure_loaded(str(ws))
    return ws, k, sink


def _patch_batch(monkeypatch, sides: dict[bool, list[dict]]):
    """假执行器：按"compact 工具在不在册"辨认带/摘侧，返回预置结果行。"""

    async def _fake(agent, tasks):
        present = "compact" in _tool_names(get_kernel())
        rows = sides[present]
        return [dict(r, id=t.id) for r, t in zip(rows, tasks)]

    monkeypatch.setattr("openx.services.eval_runner._execute_batch", _fake)


async def test_probe_skips_and_restores_without_ledger_decision(gated_workspace):
    ws, k, sink = gated_workspace
    assert "compact" in _tool_names(k)

    decl = (k.plugin_help("histcompact") or {}).get("scaffold") or {}
    with k.scaffold_probe("histcompact", decl):
        # 探针内：组合跳过（与真实退场同路径），声明经账本回填仍可读
        assert "compact" not in _tool_names(k)
        entry = next(c for c in k.list_plugins() if c["id"] == "histcompact")
        assert entry["retired"] is True and entry["phase"] == "retired"
        assert k.plugin_help("histcompact")["scaffold"]["compensates"] == "模型上下文有限"
    # 探针外：复原
    assert "compact" in _tool_names(k)
    entry = next(c for c in k.list_plugins() if c["id"] == "histcompact")
    assert entry["retired"] is False and entry["phase"] == "active"

    # 探针是测量不是裁决：账本零决策事件
    assert sink.of("scaffold_retired") == []
    assert sink.of("scaffold_restored") == []

    # 探针不对已退场脚手架负责（前置由调用方把关），这里只验证不崩
    k.retire_scaffold("histcompact")
    with k.scaffold_probe("histcompact", decl):
        pass
    k.restore_scaffold("histcompact")
    assert "compact" in _tool_names(k)


async def test_evaluate_runs_both_sides_and_verdicts(gated_workspace, monkeypatch):
    ws, k, sink = gated_workspace
    tasks = load_eval_set(ws / "evals" / "long-session.jsonl")
    assert [t.id for t in tasks] == ["t1", "t2"]

    # 摘除侧全军覆没 → 掉点 → keep（假执行器按工具在册与否辨认侧：
    # 这同时验证探针真的改变了组合，否则两侧结果无差别）
    _patch_batch(
        monkeypatch,
        {
            True: [{"ok": True, "detail": ""}, {"ok": True, "detail": ""}],
            False: [{"ok": False, "detail": "missing: 'COMPACTED'"},
                    {"ok": False, "detail": "regex miss"}],
        },
    )
    report = await evaluate_scaffold(None, k, "histcompact", tasks, eval_set="evals/long-session.jsonl")
    assert report["schema"] == "openx-eval-run/v1"
    assert report["plugin"] == "histcompact"
    assert report["tasks"] == 2
    assert report["gate"]["verdict"] == "keep"
    assert report["gate"]["delta"] == pytest.approx(-1.0)
    assert report["runs"]["with_scaffold"][0]["ok"]
    assert not report["runs"]["without_scaffold"][0]["ok"]

    # 评测完毕：组合复原、账本零决策
    assert "compact" in _tool_names(k)
    assert sink.types() == []

    # 容差放行小幅掉点 → retire
    _patch_batch(
        monkeypatch,
        {
            True: [{"ok": True, "detail": ""}, {"ok": True, "detail": ""}],
            False: [{"ok": True, "detail": ""}, {"ok": False, "detail": "regex miss"}],
        },
    )
    report = await evaluate_scaffold(None, k, "histcompact", tasks, tolerance=0.5)
    assert report["gate"]["verdict"] == "retire"
    assert report["tolerance"] == 0.5
    json.loads(json.dumps(report))  # 证据可序列化（要落账本）


async def test_evaluate_precondition_guards(gated_workspace, monkeypatch):
    ws, k, sink = gated_workspace
    tasks = load_eval_set(ws / "evals" / "long-session.jsonl")
    _patch_batch(monkeypatch, {True: [], False: []})

    # 未知名
    with pytest.raises(ValueError, match="plugin not found"):
        await evaluate_scaffold(None, k, "ghost", tasks)
    # 已退场：没有"带/摘"可对比，须先回挂
    k.retire_scaffold("histcompact")
    with pytest.raises(ValueError, match="already retired"):
        await evaluate_scaffold(None, k, "histcompact", tasks)
    k.restore_scaffold("histcompact")

    # 声明了 scaffold 但没声明 eval_set
    write_plugin(ws, "declonly", textwrap.dedent('''\
        __openx_meta__ = {
            "type": "capability.tool", "trust": "user", "summary": "s",
            "scaffold": {"compensates": "c", "exit_when": "e"},
        }
        def apply(ctx):
            pass
    '''))
    k._force_reload()
    with pytest.raises(ValueError, match="no eval_set"):
        await evaluate_scaffold(None, k, "declonly", tasks)

    # 任务集文件缺失
    with pytest.raises(EvalSetError):
        load_eval_set(resolve_eval_set_path(str(ws), "evals/absent.jsonl"))


# ── CLI 命令面：/scaffolds eval ───────────────────────────────────


class _CapConsole:
    def __init__(self):
        self.infos: list[str] = []
        self.successes: list[str] = []
        self.warnings: list[str] = []
        self.raw_lines: list[str] = []
        self.raw = self

    def print(self, *args, **kwargs):
        self.raw_lines.append(" ".join(str(a) for a in args))

    def print_info(self, message): self.infos.append(message)
    def print_success(self, message): self.successes.append(message)
    def print_warning(self, message): self.warnings.append(message)


class _FakeAgent:
    def __init__(self, ws):
        self.workspace = ws
        self.rebuilds = 0

    def _rebuild_tools(self):
        self.rebuilds += 1


async def test_scaffolds_eval_command_and_retire_flag(gated_workspace, monkeypatch):
    from openx.app.cli import commands

    ws, k, sink = gated_workspace
    agent = _FakeAgent(ws)

    # keep：报告渲染，--retire 不摘
    _patch_batch(
        monkeypatch,
        {
            True: [{"ok": True, "detail": ""}, {"ok": True, "detail": ""}],
            False: [{"ok": False, "detail": "missing"}, {"ok": False, "detail": "x"}],
        },
    )
    stub = _CapConsole()
    assert await commands.handle_slash_command(
        "scaffolds", agent, stub, ["eval", "histcompact", "--retire"]
    ) is True
    blob = "\n".join(stub.raw_lines)
    assert "histcompact" in blob and "KEEP" in blob
    assert stub.warnings and "not retiring" in stub.warnings[-1]
    assert sink.of("scaffold_retired") == []
    assert agent.rebuilds == 0

    # retire：裁决准许 + --retire → 退场且证据带上本次评测 payload
    _patch_batch(
        monkeypatch,
        {True: [{"ok": True, "detail": ""}, {"ok": True, "detail": ""}],
         False: [{"ok": True, "detail": ""}, {"ok": True, "detail": ""}]},
    )
    stub2 = _CapConsole()
    assert await commands.handle_slash_command(
        "scaffolds", agent, stub2, ["eval", "histcompact", "--retire", "--json"]
    ) is True
    assert stub2.successes and agent.rebuilds == 1
    payload = sink.of("scaffold_retired")[0].payload
    assert payload["evidence"]["schema"] == "openx-eval-run/v1"
    assert payload["evidence"]["gate"]["verdict"] == "retire"
    entry = next(c for c in k.list_plugins() if c["id"] == "histcompact")
    assert entry["retired"] is True

    # 坏 flag / 无 eval_set 声明：警告不崩
    stub3 = _CapConsole()
    assert await commands.handle_slash_command(
        "scaffolds", agent, stub3, ["eval", "histcompact", "--bogus"]
    ) is True
    assert stub3.warnings and "unknown flag" in stub3.warnings[-1]
