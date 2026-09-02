"""Phase 10 工作流测试：WorkflowEngine 钩子语义、WorkflowTool、/workflow。

覆盖 ``openx.orchestration.workflow``（引擎五钩子、parallel/pipeline 语义、并发
上限、MAX_AGENTS 兜底、脚本错误收敛、保存工作流的 ast 列举与加载、
共享 prompt 锁）与 ``openx.tools.workflow_tool`` 及 ``/workflow`` 斜杠
命令；外加顶层 agent 的禁套娃接线。

fake 子代理走 monkeypatch ``openx.orchestration.workflow._build_workflow_child``
（轻量鸭子）；至少一个端到端用例走**真实** ``build_child_agent`` +
FakeLLM 证明接线。SETTINGS_PATH 与 TASKS_DIR 均隔离到 tmp_path，
绝不触碰真实用户数据。运行：``python -m pytest tests/test_workflow.py -q``
"""

from __future__ import annotations

import asyncio
import io
import json
import time
from types import SimpleNamespace

import pytest
from rich.console import Console as RichConsole

from openx.config import OpenXConfig
from openx.orchestration.subagent import BUILTIN_SUBAGENTS
import openx.orchestration.workflow as workflow_mod
from openx.orchestration.workflow import (
    DEFAULT_CONCURRENCY,
    WorkflowEngine,
    WorkflowError,
    list_workflows,
    load_workflow,
)
from openx.llm import StreamDone
from openx.permissions import PermissionLevel, PermissionRules
from openx.services.tool_executor import ToolExecutor
from openx.tools.subagent_tool import build_child_agent
from openx.tools.workflow_tool import WorkflowTool
from openx.ui.console import Console


# ── 隔离与假 LLM ─────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    """hooks settings 与后台任务目录隔离到 tmp，绝不碰真实 home。"""
    monkeypatch.setattr(
        "openx.kernel.audit.hooks.SETTINGS_PATH", tmp_path / "no-such-settings.json"
    )
    monkeypatch.setattr("openx.orchestration.tasks.TASKS_DIR", tmp_path / "tasks")


class FakeLLM:
    """可脚本化的假 LLM：按顺序返回预设响应（stream_chat + chat 双实现）。"""

    def __init__(self, responses):
        self.responses = list(responses)  # list of (content, tool_calls)
        self.call_count = 0

    async def stream_chat(self, messages, tools=None):
        content, tool_calls = self.responses[self.call_count]
        self.call_count += 1
        if content:
            for tok in content.split():
                yield tok + " "
        resp = {"role": "assistant", "content": content or None}
        if tool_calls:
            resp["tool_calls"] = tool_calls
        yield StreamDone(response=resp, token_count=5, input_tokens=10)

    async def chat(self, messages, tools=None, stream=True):
        content, tool_calls = self.responses[self.call_count]
        self.call_count += 1
        resp = {"role": "assistant", "content": content}
        if tool_calls:
            resp["tool_calls"] = tool_calls
        return resp


def _make_agent(tmp_path, responses):
    """构造一个挂载 FakeLLM 的顶层 OpenXAgent（绕过真实 API）。"""
    from openx.agent import OpenXAgent

    config = OpenXConfig()
    config.workspace = str(tmp_path)
    config.api_key = "sk-test"
    config.api_base = "https://example.com/v1"
    config.model = "test-model"
    agent = OpenXAgent(config)
    agent.llm = FakeLLM(responses)
    agent.tool_executor._rules = PermissionRules()  # 忽略真实存储规则
    return agent


# ── 鸭子子代理 ───────────────────────────────────────────────────


class FakeExecutor:
    on_prompt_start = None
    on_prompt_end = None


class EchoChild:
    """轻量鸭子子代理：echo 回 prompt，固定 token 数。"""

    def __init__(self):
        self.tool_executor = FakeExecutor()
        self.total_output_tokens = 5
        self.history = SimpleNamespace(messages=[])

    async def stream_run(self, prompt):
        yield f"echo:{prompt}"
        self.history.messages.append(
            {"role": "assistant", "content": f"echo:{prompt}"}
        )


class RecordingChild:
    """记录 (start/end, prompt, 时刻) 的鸭子子代理，供并发语义断言。"""

    def __init__(self, events, delay=0.05):
        self.tool_executor = FakeExecutor()
        self.total_output_tokens = 3
        self.events = events
        self.delay = delay
        self.history = SimpleNamespace(messages=[])

    async def stream_run(self, prompt):
        start = time.monotonic()
        self.events.append(("start", prompt, start))
        await asyncio.sleep(self.delay)
        end = time.monotonic()
        self.events.append(("end", prompt, end))
        yield f"done:{prompt}"
        self.history.messages.append(
            {"role": "assistant", "content": f"done:{prompt}"}
        )


def _other_pending_tasks() -> list:
    """当前测试任务之外仍在悬挂的任务（孤儿任务检测）。"""
    current = asyncio.current_task()
    return [t for t in asyncio.all_tasks() if t is not current and not t.done()]


# ── 1. 端到端：WorkflowTool + 真实子代理接线 ────────────────────


class TestEndToEnd:
    """WorkflowTool 内联脚本 → 真实 build_child_agent + FakeLLM 子代理。"""

    @pytest.mark.asyncio
    async def test_tool_runs_inline_script_with_real_child(
        self, tmp_path, monkeypatch
    ):
        parent = _make_agent(tmp_path, [])
        assert "workflow" in parent.tools  # 顶层持有 workflow 工具

        created = {}

        def fake_build(parent_agent, subagent_type, prompt_lock,
                       structured_schema=None):
            # 真接线、假大脑：走真实 build_child_agent，只替换 LLM
            specs = {s.name: s for s in BUILTIN_SUBAGENTS}
            child = build_child_agent(
                parent_agent, specs[subagent_type],
                structured_schema=structured_schema,
            )
            child.llm = FakeLLM([("42 — the answer", None)])
            child.tool_executor._prompt_lock = prompt_lock
            created["child"] = child
            return child

        monkeypatch.setattr(workflow_mod, "_build_workflow_child", fake_build)

        script = (
            "meta = {'name': 'tiny', 'description': 'd'}\n"
            "async def main(agent, parallel, pipeline, phase, log, args):\n"
            "    answer = await agent('what is 6*7?')\n"
            "    return {'ok': 1, 'answer': answer}\n"
        )
        result = await WorkflowTool(parent).execute(script=script)
        assert result.success
        data = json.loads(result.output.split("\n[workflow:")[0])
        assert data == {"ok": 1, "answer": "42 — the answer"}
        assert "[workflow: 1 agents, 0 failed," in result.output
        # 真实子代理确实收到了工作流的 prompt
        assert created["child"].history.messages[0]["content"] == "what is 6*7?"


# ── 2. parallel 语义：屏障、顺序、重叠、失败槽位 ─────────────────


class TestParallelSemantics:
    @pytest.mark.asyncio
    async def test_overlap_order_and_failed_thunk_slot(self, tmp_path, monkeypatch):
        parent = _make_agent(tmp_path, [])
        events = []
        monkeypatch.setattr(
            workflow_mod,
            "_build_workflow_child",
            lambda parent_a, st, lock, structured_schema=None: RecordingChild(events),
        )
        engine = WorkflowEngine(parent, concurrency=8)
        script = (
            "async def main(agent, parallel, pipeline, phase, log, args):\n"
            "    def boom():\n"
            "        raise RuntimeError('kaput')\n"
            "    return await parallel([\n"
            "        lambda: agent('alpha', label='a'),\n"
            "        lambda: agent('beta', label='b'),\n"
            "        boom,\n"
            "    ])\n"
        )
        result, stats = await engine.run(script)
        # 顺序保持 + 失败 thunk 落 None
        assert result == ["done:alpha", "done:beta", None]
        # 并发重叠证明：最晚的 start 也早于最早的 end
        starts = [t for k, _, t in events if k == "start"]
        ends = [t for k, _, t in events if k == "end"]
        assert len(starts) == 2 and max(starts) < min(ends)
        assert stats.agents_run == 2 and stats.agents_failed == 0
        assert not _other_pending_tasks()


# ── 3. pipeline 语义：逐项独立链、阶段顺序、异常隔离 ─────────────


class TestPipelineSemantics:
    @pytest.mark.asyncio
    async def test_stages_sequential_per_item_items_independent(self, tmp_path):
        parent = _make_agent(tmp_path, [])
        calls = []  # (stage, prev, orig, index, 时刻)

        async def stage1(prev, orig, i):
            calls.append(("s1", prev, orig, i, time.monotonic()))
            await asyncio.sleep(0.03)
            if orig == 2:
                raise ValueError("stage1 rejects 2")
            return prev * 10

        async def stage2(prev, orig, i):
            calls.append(("s2", prev, orig, i, time.monotonic()))
            await asyncio.sleep(0.005)
            return prev + 1

        engine = WorkflowEngine(parent)
        result = await engine._pipeline([1, 2, 3], stage1, stage2)
        # item2 在 stage1 抛异常 → None；其余按 (prev, orig, index) 链式传递
        assert result == [11, None, 31]

        s2_i0 = next(c for c in calls if c[0] == "s2" and c[3] == 0)
        assert s2_i0[1] == 10 and s2_i0[2] == 1 and s2_i0[3] == 0

        # 每个 item 内部阶段严格顺序：s1(i) < s2(i)
        for i in (0, 2):
            t1 = next(c[4] for c in calls if c[0] == "s1" and c[3] == i)
            t2 = next(c[4] for c in calls if c[0] == "s2" and c[3] == i)
            assert t1 < t2
        # item 之间互相独立（无屏障）：item2 的 s1 早于 item0 的 s2
        s1_i2 = next(c[4] for c in calls if c[0] == "s1" and c[3] == 2)
        assert s1_i2 < s2_i0[4]

    @pytest.mark.asyncio
    async def test_pipeline_hook_wired_through_script_args(self, tmp_path):
        """闭包阶段经 args 注入脚本 → pipeline 钩子端到端可用。"""
        parent = _make_agent(tmp_path, [])
        engine = WorkflowEngine(parent)
        script = (
            "async def main(agent, parallel, pipeline, phase, log, args):\n"
            "    return await pipeline(args['items'], *args['stages'])\n"
        )

        async def double(prev, orig, i):
            return prev * 2

        result, stats = await engine.run(
            script, args={"items": [1, 2], "stages": [double]}
        )
        assert result == [2, 4]
        assert stats.agents_run == 0


# ── 4. 并发上限 ─────────────────────────────────────────────────


class TestConcurrencyCap:
    @pytest.mark.asyncio
    async def test_semaphore_bounds_inflight_agents(self, tmp_path, monkeypatch):
        parent = _make_agent(tmp_path, [])
        state = {"active": 0, "peak": 0}

        class SlowChild:
            def __init__(self):
                self.tool_executor = FakeExecutor()
                self.total_output_tokens = 1
                self.history = SimpleNamespace(messages=[])

            async def stream_run(self, prompt):
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                await asyncio.sleep(0.03)
                state["active"] -= 1
                yield "x"
                self.history.messages.append(
                    {"role": "assistant", "content": "x"}
                )

        monkeypatch.setattr(
            workflow_mod, "_build_workflow_child",
            lambda p, st, lock, structured_schema=None: SlowChild(),
        )
        engine = WorkflowEngine(parent, concurrency=2)
        script = (
            "async def main(agent, parallel, pipeline, phase, log, args):\n"
            "    return await parallel([lambda i=i: agent(f'job{i}') for i in range(4)])\n"
        )
        result, stats = await engine.run(script)
        assert result == ["x"] * 4
        assert state["peak"] == 2  # 同时在飞的最多 2 个
        assert stats.agents_run == 4

    def test_default_concurrency_within_claude_code_bounds(self):
        assert 2 <= DEFAULT_CONCURRENCY <= 16


# ── 5. MAX_AGENTS 兜底闸 ────────────────────────────────────────


class TestAgentBackstop:
    @pytest.mark.asyncio
    async def test_cap_surfaces_as_error(self, tmp_path, monkeypatch):
        parent = _make_agent(tmp_path, [])
        monkeypatch.setattr(
            workflow_mod, "_build_workflow_child",
            lambda p, st, lock, structured_schema=None: EchoChild(),
        )
        monkeypatch.setattr(workflow_mod, "MAX_AGENTS_PER_RUN", 3)
        script = (
            "async def main(agent, parallel, pipeline, phase, log, args):\n"
            "    for i in range(4):\n"
            "        await agent(f'job {i}')\n"
            "    return 'unreachable'\n"
        )
        with pytest.raises(WorkflowError, match="safety cap"):
            await WorkflowEngine(parent).run(script)
        # 工具路径 → 错误 ToolResult
        result = await WorkflowTool(parent).execute(script=script)
        assert not result.success and "safety cap" in result.error

    @pytest.mark.asyncio
    async def test_parallel_backstop_cancels_siblings_no_orphans(
        self, tmp_path, monkeypatch
    ):
        parent = _make_agent(tmp_path, [])
        monkeypatch.setattr(
            workflow_mod, "_build_workflow_child",
            lambda p, st, lock, structured_schema=None: RecordingChild([], delay=0.05),
        )
        monkeypatch.setattr(workflow_mod, "MAX_AGENTS_PER_RUN", 2)
        script = (
            "async def main(agent, parallel, pipeline, phase, log, args):\n"
            "    return await parallel([lambda i=i: agent(f'job{i}') for i in range(4)])\n"
        )
        with pytest.raises(WorkflowError, match="safety cap"):
            await WorkflowEngine(parent).run(script)
        await asyncio.sleep(0.1)
        assert not _other_pending_tasks()  # 兄弟协程已全部取消落地


# ── 6. 脚本错误收敛 ─────────────────────────────────────────────


class TestScriptErrors:
    @pytest.mark.parametrize(
        "script, needle",
        [
            ("def broken(:", "syntax"),
            ("x = 1\n", "must define"),
            ("def main(**kw):\n    return 1\n", "async"),
            (
                "async def main(agent, parallel, pipeline, phase, log, args):\n"
                "    raise ValueError('boom')\n",
                "Workflow raised",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_error_surfaces_as_error_result(
        self, tmp_path, monkeypatch, script, needle
    ):
        parent = _make_agent(tmp_path, [])
        monkeypatch.setattr(
            workflow_mod, "_build_workflow_child",
            lambda p, st, lock, structured_schema=None: EchoChild(),
        )
        result = await WorkflowTool(parent).execute(script=script)
        assert not result.success
        assert needle in result.error
        assert not _other_pending_tasks()

    @pytest.mark.asyncio
    async def test_main_raising_includes_type_and_message(self, tmp_path):
        parent = _make_agent(tmp_path, [])
        script = (
            "async def main(agent, parallel, pipeline, phase, log, args):\n"
            "    raise KeyError('missing-thing')\n"
        )
        result = await WorkflowTool(parent).execute(script=script)
        assert not result.success
        assert "KeyError" in result.error and "missing-thing" in result.error

    @pytest.mark.asyncio
    async def test_exactly_one_of_script_name(self, tmp_path):
        parent = _make_agent(tmp_path, [])
        tool = WorkflowTool(parent)
        neither = await tool.execute()
        assert not neither.success and "script" in neither.error
        both = await tool.execute(script="x = 1", name="y")
        assert not both.success and "not both" in both.error


# ── 7. 保存的工作流：按名运行 / ast 列举 / 缺失报错 ──────────────


class TestSavedWorkflows:
    def _write(self, tmp_path):
        wf = tmp_path / ".openx" / "workflows"
        wf.mkdir(parents=True)
        (wf / "hello.py").write_text(
            "meta = {'name': 'hello', 'description': 'Says hi.'}\n"
            "async def main(agent, parallel, pipeline, phase, log, args):\n"
            "    reply = await agent('greet')\n"
            "    return {'greeting': reply, 'args': args}\n",
            encoding="utf-8",
        )
        return wf

    @pytest.mark.asyncio
    async def test_tool_runs_saved_workflow_by_name(self, tmp_path, monkeypatch):
        self._write(tmp_path)
        parent = _make_agent(tmp_path, [])
        monkeypatch.setattr(
            workflow_mod, "_build_workflow_child",
            lambda p, st, lock, structured_schema=None: EchoChild(),
        )
        result = await WorkflowTool(parent).execute(
            name="hello", args={"who": "world"}
        )
        assert result.success
        data = json.loads(result.output.split("\n[workflow:")[0])
        assert data == {"greeting": "echo:greet", "args": {"who": "world"}}
        assert "[workflow: 1 agents, 0 failed," in result.output

    def test_list_workflows_parses_meta_and_falls_back(self, tmp_path):
        wf = self._write(tmp_path)
        # meta 值非常量 → 解析降级为文件名主干，绝不执行脚本
        (wf / "weird.py").write_text("meta = {'name': compute()}\n", encoding="utf-8")
        rows = list_workflows(str(tmp_path))
        assert [r["name"] for r in rows] == ["hello", "weird"]
        assert rows[0]["description"] == "Says hi."
        assert rows[1]["description"] == ""

    def test_list_workflows_missing_dir_is_empty(self, tmp_path):
        assert list_workflows(str(tmp_path)) == []

    @pytest.mark.asyncio
    async def test_missing_name_is_error(self, tmp_path):
        parent = _make_agent(tmp_path, [])
        result = await WorkflowTool(parent).execute(name="ghost")
        assert not result.success and "not found" in result.error

    def test_load_workflow_rejects_traversal(self, tmp_path):
        with pytest.raises(WorkflowError):
            load_workflow(str(tmp_path), "../evil")
        with pytest.raises(WorkflowError):
            load_workflow(str(tmp_path), ".hidden")


# ── 8. 共享 prompt 锁 ───────────────────────────────────────────


class TestSharedPromptLock:
    @pytest.mark.asyncio
    async def test_all_children_of_a_run_share_one_lock(self, tmp_path, monkeypatch):
        parent = _make_agent(tmp_path, [])
        seen = []

        def spy(parent_a, st, lock, structured_schema=None):
            seen.append(lock)
            return EchoChild()

        monkeypatch.setattr(workflow_mod, "_build_workflow_child", spy)
        engine = WorkflowEngine(parent)
        script = (
            "async def main(agent, parallel, pipeline, phase, log, args):\n"
            "    return await parallel([lambda i=i: agent(f'j{i}') for i in range(3)])\n"
        )
        await engine.run(script)
        assert len(seen) == 3
        assert all(lock is seen[0] for lock in seen)
        assert seen[0] is engine._prompt_lock

    def test_tool_executor_accepts_injected_lock(self):
        lock = asyncio.Lock()
        shared = ToolExecutor(Console(config=OpenXConfig()), prompt_lock=lock)
        assert shared._prompt_lock is lock
        own = ToolExecutor(Console(config=OpenXConfig()))
        assert own._prompt_lock is not lock

    @pytest.mark.asyncio
    async def test_real_build_resolves_spec_and_injects_lock(self, tmp_path):
        parent = _make_agent(tmp_path, [])
        lock = asyncio.Lock()
        child = workflow_mod._build_workflow_child(parent, "general-purpose", lock)
        assert child.tool_executor._prompt_lock is lock
        # 未知类型 → WorkflowError 列出可用规格
        with pytest.raises(WorkflowError, match="Unknown subagent_type"):
            workflow_mod._build_workflow_child(parent, "nope", lock)


# ── 9. 权限 ─────────────────────────────────────────────────────


class TestPermissionAndSchema:
    def test_permission_is_ask(self, tmp_path):
        parent = _make_agent(tmp_path, [])
        tool = WorkflowTool(parent)
        assert tool.permission.level is PermissionLevel.ASK

    def test_schema_exposes_script_name_args(self, tmp_path):
        parent = _make_agent(tmp_path, [])
        schema = WorkflowTool(parent).to_openai_schema()
        assert schema["function"]["name"] == "workflow"
        props = schema["function"]["parameters"]["properties"]
        assert {"script", "name", "args"} <= set(props)


# ── 10. 禁套娃 ──────────────────────────────────────────────────


class TestNestingGuard:
    def test_child_agent_has_no_workflow_tool(self, tmp_path):
        parent = _make_agent(tmp_path, [])
        assert "workflow" in parent.tools
        child = build_child_agent(
            parent, {s.name: s for s in BUILTIN_SUBAGENTS}["general-purpose"]
        )
        assert "workflow" not in child.tools
        assert "task" not in child.tools


# ── 11. /workflow 斜杠命令 ──────────────────────────────────────


class TestSlashCommand:
    def _console(self, agent):
        """StringIO 捕获 console（沿用 tests/test_console.py 的模式）。"""
        console = Console(config=agent.config)
        console._console = RichConsole(
            file=io.StringIO(), force_terminal=True, highlight=False
        )
        console._terminal_width = 100
        return console

    @staticmethod
    def _capture(console) -> str:
        return console._console.file.getvalue()

    @pytest.mark.asyncio
    async def test_no_args_lists_saved_workflows(self, tmp_path):
        from openx.app.cli.commands import handle_slash_command

        wf = tmp_path / ".openx" / "workflows"
        wf.mkdir(parents=True)
        for name, desc in [("alpha", "First."), ("beta", "Second.")]:
            (wf / f"{name}.py").write_text(
                f"meta = {{'name': '{name}', 'description': '{desc}'}}\n"
                "async def main(agent, parallel, pipeline, phase, log, args):\n"
                "    return 1\n",
                encoding="utf-8",
            )
        agent = _make_agent(tmp_path, [])
        console = self._console(agent)
        result = await handle_slash_command("workflow", agent, console, [])
        assert result is True
        out = self._capture(console)
        assert "alpha" in out and "beta" in out
        assert "First." in out and "Second." in out

    @pytest.mark.asyncio
    async def test_no_args_empty_dir_shows_hint(self, tmp_path):
        from openx.app.cli.commands import handle_slash_command

        agent = _make_agent(tmp_path, [])
        console = self._console(agent)
        result = await handle_slash_command("workflows", agent, console, [])  # 别名
        assert result is True
        assert ".openx/workflows" in self._capture(console)

    @pytest.mark.asyncio
    async def test_run_by_name(self, tmp_path, monkeypatch):
        from openx.app.cli.commands import handle_slash_command

        wf = tmp_path / ".openx" / "workflows"
        wf.mkdir(parents=True)
        (wf / "echo.py").write_text(
            "meta = {'name': 'echo', 'description': 'Echoes.'}\n"
            "async def main(agent, parallel, pipeline, phase, log, args):\n"
            "    return {'reply': await agent('ping')}\n",
            encoding="utf-8",
        )
        agent = _make_agent(tmp_path, [])
        monkeypatch.setattr(
            workflow_mod, "_build_workflow_child",
            lambda p, st, lock, structured_schema=None: EchoChild(),
        )
        console = self._console(agent)
        result = await handle_slash_command("workflow", agent, console, ["echo"])
        assert result is True
        out = self._capture(console)
        assert "echo:ping" in out and "finished" in out

    @pytest.mark.asyncio
    async def test_unknown_name_prints_error_and_continues(self, tmp_path):
        from openx.app.cli.commands import handle_slash_command

        agent = _make_agent(tmp_path, [])
        console = self._console(agent)
        result = await handle_slash_command("workflow", agent, console, ["ghost"])
        assert result is True  # 报错但 REPL 继续
        assert "not found" in self._capture(console)


# ── 舰队视图：工作流子代理登记（v0.4.0）──────────────────────────


class TestFleetViews:
    @pytest.mark.asyncio
    async def test_agent_registers_and_completes_views(self, tmp_path, monkeypatch):
        parent = _make_agent(tmp_path, [])
        monkeypatch.setattr(
            workflow_mod, "_build_workflow_child",
            lambda p, st, lock, structured_schema=None: EchoChild(),
        )
        engine = WorkflowEngine(parent)
        script = (
            "async def main(agent, parallel, pipeline, phase, log, args):\n"
            "    await agent('alpha', label='A')\n"
            "    return await parallel([lambda: agent('b1'), lambda: agent('b2')])\n"
        )
        result, stats = await engine.run(script)
        assert result == ["echo:b1", "echo:b2"]
        assert stats.agents_run == 3 and stats.agents_failed == 0
        views = parent.fleet.snapshot()
        assert [v["label"] for v in views] == ["A", "b1", "b2"]
        assert all(v["status"] == "done" for v in views)

    @pytest.mark.asyncio
    async def test_failed_child_marks_error_view(self, tmp_path, monkeypatch):
        class BoomChild(EchoChild):
            async def stream_run(self, prompt):
                raise RuntimeError("kaput")
                yield  # pragma: no cover —— 使其成为异步生成器

        parent = _make_agent(tmp_path, [])
        monkeypatch.setattr(
            workflow_mod, "_build_workflow_child",
            lambda p, st, lock, structured_schema=None: BoomChild(),
        )
        engine = WorkflowEngine(parent)
        script = (
            "async def main(agent, parallel, pipeline, phase, log, args):\n"
            "    return await agent('x', label='boom')\n"
        )
        result, stats = await engine.run(script)
        assert result is None and stats.agents_failed == 1
        views = parent.fleet.snapshot()
        assert len(views) == 1 and views[0]["status"] == "error"
