"""E4 退场评测门 · **执行器**——真正跑 ``eval_set``：带 vs 摘两侧实测 → 度量 → 裁决。

设计（`docs/design/openx-self-evolution-design.md` §3.2）：退场三步的第①步
是**评测门**——在 ``scaffold.eval_set`` 上对比"带脚手架 vs 摘除"的任务成功率：
**不掉点 → 准许退场；掉点 → 保持**。裁决面在 ``services/retirement_gate``
（``compare_success``：给定两侧度量算 verdict + 证据）；本模块补上它注释里
预留的另一半——**把任务真的跑出来**：

- 任务集：``eval_set`` 是 JSONL（一行一任务，``openx-eval-run/v1``），相对
  **工作区**解析（绝对路径与 ``~`` 展开亦可）。任务 = 提示 + **确定性判定**
  （``contains`` / ``not_contains`` / ``regex``，大小写不敏感）——先离线，
  模型评审型判定留给后续切片；
- 执行：每个任务派一个**无头子代理**（``tools/subagent_tool.build_child_agent``，
  ``role="main"`` 与被评模型同绑定），单任务超时即判负。缺省**无工具**（纯
  问答，最安全也最便宜）；任务行可声明 ``tools`` 白名单放开；
- 摘除侧：``kernel.scaffold_probe``——**内存级**临时摘除（走与真实退场相同
  的 PHASE_RETIRED 组合跳过路径），退出时复原，**绝不写全局账本**：探针是
  测量不是裁决；
- 裁决：两侧度量喂 ``compare_success`` → ``{verdict, delta, ...}``，连同
  逐任务明细打包成证据 payload——直接作为 ``kernel.retire_scaffold(
  evidence=...)`` 的证据落 ``scaffold_retired``。

跑评测 = 真实模型调用（任务数 × 2 次代理回合），**命令调用即授权**（同
``/distill save`` 的人审纪律）；人终审摘除（详设 §5.1）仍由
``/scaffolds retire`` 完成。
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

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .retirement_gate import DEFAULT_TOLERANCE, compare_success

#: 证据 schema 版本（消费方据此判断字段形状）。
SCHEMA = "openx-eval-run/v1"

#: 单任务缺省超时秒（子代理回合无响应即判负；任务行可用 ``timeout`` 覆盖）。
DEFAULT_TASK_TIMEOUT = 120.0

#: 确定性判定键（至少一个；先离线——模型评审型判定后续切片）。
JUDGE_KEYS = ("contains", "not_contains", "regex")

#: 评测子代理的角色指令（追加进系统提示）：直接作答，判定器只看最终文本。
EVAL_SYSTEM_EXTRA = (
    "You are part of an automated benchmark run. Answer the user's task "
    "directly and completely in your final response. Do not ask "
    "clarifying questions; a deterministic checker scores the final text."
)


class EvalSetError(ValueError):
    """评测任务集形状错误（文件缺失 / 行非法 / 判定键不合法）。"""


@dataclass(frozen=True)
class EvalTask:
    """一行评测任务：提示 + 确定性判定 +（可选）工具白名单与超时。"""

    id: str
    task: str
    expect: dict[str, Any]
    tools: tuple[str, ...] = ()       # 空 = 无工具（纯问答子代理）
    timeout: float = DEFAULT_TASK_TIMEOUT


def resolve_eval_set_path(workspace: str, raw: str) -> Path:
    """``scaffold.eval_set`` 声明 → 任务集文件路径：相对**工作区**，``~``/绝对路径原样。"""
    p = Path(raw).expanduser()
    return p if p.is_absolute() else Path(workspace) / p


def load_eval_set(path: Path) -> list[EvalTask]:
    """解析 JSONL 任务集（一行一任务）；任何一行不合法即整体拒绝（``EvalSetError``）。

    行形状：``{"id"?: str, "task": str(非空), "expect": {...判定键...},
    "tools"?: [str], "timeout"?: 秒}``。``id`` 缺省按行号 ``t<n>``。整体拒绝
    而非跳过坏行——评测门是回归测试，静默丢任务等于把门拆掉一块。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EvalSetError(f"cannot read eval_set {path}: {exc}") from exc

    tasks: list[EvalTask] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvalSetError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise EvalSetError(f"{path}:{lineno}: must be a JSON object")
        task = row.get("task")
        if not isinstance(task, str) or not task.strip():
            raise EvalSetError(f"{path}:{lineno}: 'task' must be a non-empty string")
        expect = row.get("expect")
        if not isinstance(expect, dict) or not any(k in expect for k in JUDGE_KEYS):
            raise EvalSetError(
                f"{path}:{lineno}: 'expect' needs at least one of {list(JUDGE_KEYS)}"
            )
        unknown = set(expect) - set(JUDGE_KEYS)
        if unknown:
            raise EvalSetError(
                f"{path}:{lineno}: unknown judge key(s) {sorted(unknown)}; "
                f"supported: {list(JUDGE_KEYS)}"
            )
        for key in ("contains", "not_contains"):
            if key in expect:
                needles = expect[key]
                if isinstance(needles, str):
                    needles = [needles]
                if (
                    not isinstance(needles, list)
                    or not needles
                    or not all(isinstance(n, str) and n for n in needles)
                ):
                    raise EvalSetError(
                        f"{path}:{lineno}: expect.{key} must be a non-empty "
                        "string or list of strings"
                    )
        if "regex" in expect:
            pattern = expect["regex"]
            if not isinstance(pattern, str) or not pattern:
                raise EvalSetError(f"{path}:{lineno}: expect.regex must be a string")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise EvalSetError(
                    f"{path}:{lineno}: expect.regex does not compile: {exc}"
                ) from exc
        tools = row.get("tools", [])
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise EvalSetError(f"{path}:{lineno}: 'tools' must be a list of strings")
        timeout = row.get("timeout", DEFAULT_TASK_TIMEOUT)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise EvalSetError(f"{path}:{lineno}: 'timeout' must be a positive number")
        task_id = row.get("id")
        if task_id is not None and not isinstance(task_id, str):
            raise EvalSetError(f"{path}:{lineno}: 'id' must be a string")
        tasks.append(
            EvalTask(
                id=task_id or f"t{lineno}",
                task=task,
                expect=expect,
                tools=tuple(tools),
                timeout=float(timeout),
            )
        )
    if not tasks:
        raise EvalSetError(f"{path}: no tasks (empty eval_set)")
    return tasks


def judge_output(task: EvalTask, output: str) -> list[str]:
    """确定性判定：返回**未通过**的检查描述（空列表 = 通过）。

    ``contains``（全部命中）/ ``not_contains``（一命中即败）均为大小写不敏感
    子串匹配；``regex`` 为 ``re.search``（字面大小写）。模型输出措辞漂移靠
    判定键的写法吸收（任务作者知道在测什么）。
    """
    text = str(output or "")
    low = text.lower()
    failed: list[str] = []
    expect = task.expect
    for needle in _as_list(expect.get("contains")):
        if needle.lower() not in low:
            failed.append(f"missing: {needle!r}")
    for needle in _as_list(expect.get("not_contains")):
        if needle.lower() in low:
            failed.append(f"forbidden hit: {needle!r}")
    pattern = expect.get("regex")
    if pattern is not None and re.search(pattern, text) is None:
        failed.append(f"regex miss: {pattern!r}")
    return failed


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [v for v in value if isinstance(v, str)]


async def _run_one(child: Any, task: EvalTask) -> dict[str, Any]:
    """跑一个任务：子代理回合（超时即负）→ 确定性判定 → 逐任务结果行。"""
    try:
        output = await asyncio.wait_for(child.run(task.task), timeout=task.timeout)
    except asyncio.TimeoutError:
        return {"id": task.id, "ok": False, "detail": f"timeout (>{task.timeout:.0f}s)"}
    except Exception as exc:  # 代理崩溃 = 任务失败（评测不容例外）
        return {"id": task.id, "ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    failed = judge_output(task, str(output))
    return {"id": task.id, "ok": not failed, "detail": "; ".join(failed)}


async def _execute_batch(agent: Any, tasks: list[EvalTask]) -> list[dict[str, Any]]:
    """跑一侧的全部任务（每任务一个无头子代理，顺序执行）。

    模块级函数 = 测试接缝（monkeypatch 本函数即可注入假执行器，无需真模型）。
    子代理 ``role="main"``——与被评脚手架服务的模型同绑定，评测才对得上
    "这个模型还需不需要它"。
    """
    from ..orchestration.subagent import SubagentSpec
    from ..tools.subagent_tool import build_child_agent

    results: list[dict[str, Any]] = []
    for task in tasks:
        spec = SubagentSpec(
            "eval-runner",
            "Retirement-gate eval run",
            tools=list(task.tools),  # 空 = 无工具：纯问答，最安全也最便宜
            system_prompt_extra=EVAL_SYSTEM_EXTRA,
        )
        child = build_child_agent(agent, spec, role="main")
        results.append(await _run_one(child, task))
    return results


def _metrics(runs: list[dict[str, Any]]) -> dict[str, int]:
    return {"success": sum(1 for r in runs if r["ok"]), "total": len(runs)}


def _check_preconditions(kernel: Any, name: str) -> dict[str, Any]:
    """评测前置：active + 带 scaffold 声明 + 声明了 eval_set + 未退场。返回声明。"""
    rows = {r["id"]: r for r in kernel.list_plugins()}
    row = rows.get(name)
    if row is None:
        raise ValueError(f"plugin not found: {name}")
    if row.get("retired"):
        reason = row.get("skip_reason") or "retired"
        raise ValueError(
            f"scaffold already retired: {name} ({reason}); "
            "no with/without contrast to run — restore it first"
        )
    if not row.get("scaffold"):
        raise ValueError(f"{name} is not a scaffold (no scaffold declaration)")
    decl = (kernel.plugin_help(name) or {}).get("scaffold") or {}
    if not decl.get("eval_set"):
        raise ValueError(f"{name} declares no eval_set; nothing to run")
    return decl


async def evaluate_scaffold(
    agent: Any,
    kernel: Any,
    name: str,
    tasks: list[EvalTask],
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    eval_set: str = "",
) -> dict[str, Any]:
    """评测门完整执行：带侧实测 → 探针摘除侧实测 → ``compare_success`` 裁决。

    返回证据 payload（``openx-eval-run/v1``）：``gate``（裁决 + 两侧度量）、
    ``runs``（逐任务明细）。直接作为 ``kernel.retire_scaffold(evidence=...)``
    的证据。前置不满足 / 任务跑不了 → ``ValueError``。摘除侧包在
    ``kernel.scaffold_probe`` 里——退出时组合必然复原，账本不添决策。
    """
    decl = _check_preconditions(kernel, name)
    with_runs = await _execute_batch(agent, tasks)
    with kernel.scaffold_probe(name, decl):
        without_runs = await _execute_batch(agent, tasks)
    gate = compare_success(_metrics(with_runs), _metrics(without_runs), tolerance)
    return {
        "schema": SCHEMA,
        "plugin": name,
        "eval_set": eval_set or decl.get("eval_set", ""),
        "tolerance": float(tolerance),
        "tasks": len(tasks),
        "gate": gate,
        "runs": {"with_scaffold": with_runs, "without_scaffold": without_runs},
    }


def render_text(report: dict[str, Any]) -> str:
    """证据 payload → 人读报告（``/scaffolds eval`` 缺省输出）。"""
    gate = report.get("gate") or {}
    runs = report.get("runs") or {}
    lines = [
        f"Retirement gate: {report.get('plugin')}"
        f"  (eval_set: {report.get('eval_set')}, "
        f"{report.get('tasks')} task(s) x 2 sides)"
    ]
    for side, label in (("with_scaffold", "with scaffold"),
                        ("without_scaffold", "without scaffold")):
        side_runs = runs.get(side) or []
        ok = sum(1 for r in side_runs if r.get("ok"))
        rate = gate.get(side, {}).get("success_rate", 0.0)
        lines.append(f"  {label:<18} {ok}/{len(side_runs)}  ({rate:.2f})")
    delta = gate.get("delta", 0.0)
    tol = gate.get("tolerance", 0.0)
    verdict = gate.get("verdict", "?")
    tail = (
        "no drop — scaffold may retire"
        if verdict == "retire"
        else "drop exceeds tolerance — scaffold stays"
    )
    lines.append(f"  delta: {delta:+.2f}  (tolerance {tol})")
    lines.append(f"  verdict: {verdict.upper()} — {tail}")
    failures = [
        (side, r)
        for side in ("with_scaffold", "without_scaffold")
        for r in runs.get(side) or []
        if not r.get("ok")
    ]
    if failures:
        lines.append("  failures:")
        for side, r in failures:
            tag = "with" if side == "with_scaffold" else "without"
            lines.append(f"    [{tag}] {r.get('id')}: {r.get('detail')}")
    lines.append(
        "next: /scaffolds eval %s --retire  (gate + retire in one step)"
        % report.get("plugin")
    )
    return "\n".join(lines)


if __name__ == "__main__":
    import tempfile

    # 任务集解析：合法 / 坏 JSON / 缺 expect / 坏 regex / 空集
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "evals.jsonl"
        p.write_text(
            "\n".join(
                [
                    json.dumps(
                        {"id": "a", "task": "say hi", "expect": {"contains": "hi"}}
                    ),
                    json.dumps(
                        {"task": "no-tools ok", "expect": {"regex": "ok"}, "tools": []}
                    ),
                    "",
                ]
            ),
            encoding="utf-8",
        )
        _tasks = load_eval_set(p)
        assert [t.id for t in _tasks] == ["a", "t2"]
        assert _tasks[0].timeout == DEFAULT_TASK_TIMEOUT

        def _bad(content: str) -> None:
            q = Path(td) / "bad.jsonl"
            q.write_text(content, encoding="utf-8")
            try:
                load_eval_set(q)
            except EvalSetError:
                pass
            else:
                raise AssertionError("should have rejected")

        _bad("{not json}\n")
        _bad(json.dumps({"task": "x", "expect": {}}) + "\n")
        _bad(json.dumps({"task": "x", "expect": {"regex": "("}}) + "\n")
        _bad(json.dumps({"task": "x", "expect": {"vague": 1}}) + "\n")
        _bad(json.dumps({"task": "", "expect": {"contains": "x"}}) + "\n")
        _bad("\n\n")
        assert load_eval_set.__doc__  # 有一条即够，语义在上

    # 确定性判定
    from openx.services.eval_runner import EvalTask as _T

    _t = _T("t", "q", {"contains": ["Hello"], "not_contains": "nope"})
    assert judge_output(_t, "hello world") == []
    assert judge_output(_t, "hello nope") == ["forbidden hit: 'nope'"]
    assert judge_output(_t, "nothing") == ["missing: 'Hello'"]
    _t = _T("t", "q", {"regex": r"\d+ items"})
    assert judge_output(_t, "we found 42 items") == []
    assert judge_output(_t, "no digits") != []

    # 裁决编排：两侧假执行器（不造真子代理）→ keep / retire / 探针复原由内核测试覆盖
    class _FakeKernel:
        def __init__(self, scaffold: bool = True):
            self._scaffold = scaffold
            self.probe_names: list[str] = []

        def list_plugins(self):
            return [
                {
                    "id": "hist-comp",
                    "phase": "active",
                    "scaffold": self._scaffold,
                    "retired": False,
                    "skip_reason": "",
                }
            ]

        def plugin_help(self, name):
            return {
                "scaffold": {
                    "compensates": "ctx",
                    "exit_when": "eval passes",
                    "eval_set": "evals/x.jsonl",
                }
            } if self._scaffold else {}

        from contextlib import contextmanager as _cm

        @_cm
        def scaffold_probe(self, name, decl=None):
            self.probe_names.append(name)
            yield

    import openx.services.eval_runner as _mod

    async def _fake_batch(agent, tasks):
        return [{"id": t.id, "ok": True, "detail": ""} for t in tasks]

    _real = _mod._execute_batch
    _mod._execute_batch = _fake_batch  # type: ignore[assignment]
    try:
        # 直跑时本文件是 __main__，与导入副本不同对象——patch 与调用须经
        # 同一模块（_mod），否则 patch 落不到被调函数身上。
        _rep = asyncio.run(
            _mod.evaluate_scaffold(None, _FakeKernel(), "hist-comp", _tasks)
        )
    finally:
        _mod._execute_batch = _real  # type: ignore[assignment]
    assert _rep["schema"] == SCHEMA
    assert _rep["gate"]["verdict"] == "retire" and _rep["gate"]["delta"] == 0.0
    assert _rep["runs"]["with_scaffold"][0]["ok"]
    assert _rep["eval_set"] == "evals/x.jsonl"
    _json = __import__("json")
    _json.loads(_json.dumps(_rep))  # 证据要可序列化（落账本）
    assert "RETIRE" in render_text(_rep)

    # 前置守卫：非脚手架 / 未声明 eval_set / 已退场
    try:
        asyncio.run(evaluate_scaffold(None, _FakeKernel(scaffold=False), "hist-comp", _tasks))
    except ValueError as e:
        assert "not a scaffold" in str(e)
    else:
        raise AssertionError("should have refused")

    print("openx/services/eval_runner.py OK ✓")
