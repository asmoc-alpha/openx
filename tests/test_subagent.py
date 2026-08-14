"""Phase 8 子代理系统测试：task 工具、子代理构建、规格加载、共享接线。

覆盖 ``openx.core.subagent``（规格加载）与 ``openx.tools.subagent_tool``
（TaskTool + build_child_agent）以及 ``OpenXAgent`` 的子模式：工具裁剪、
PermissionRules 共享、系统提示注入、弹窗回调传播、禁套娃。

TASKS_DIR 与 hooks SETTINGS_PATH 均 monkeypatch 到 tmp_path，
绝不触碰真实用户数据。运行：``python -m pytest tests/test_subagent.py -q``
"""

from __future__ import annotations

import json

import pytest

from openx.config import OpenXConfig
from openx.core.subagent import (
    BUILTIN_SUBAGENTS,
    SubagentSpec,
    load_subagent_specs,
)
from openx.instructions import SUBAGENT_INSTRUCTIONS
from openx.llm import StreamDone
from openx.permissions import PermissionLevel, PermissionRules
import openx.tools.subagent_tool as subagent_tool
from openx.tools.subagent_tool import TaskTool, build_child_agent


# ── 隔离与假 LLM ─────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    """hooks settings 与后台任务目录隔离到 tmp，绝不碰真实 home。"""
    monkeypatch.setattr(
        "openx.core.hooks.SETTINGS_PATH", tmp_path / "no-such-settings.json"
    )
    monkeypatch.setattr("openx.core.tasks.TASKS_DIR", tmp_path / "tasks")


class FakeLLM:
    """可脚本化的假 LLM：按顺序返回预设响应（stream_chat + chat 双实现）。"""

    def __init__(self, responses):
        # responses: list of (content, tool_calls)
        self.responses = list(responses)
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
    # 确定性：忽略真实 ~/.openx/settings.json 里可能存在的存储规则
    agent.tool_executor._rules = PermissionRules()
    return agent


def _builtin_specs() -> dict[str, SubagentSpec]:
    return {s.name: s for s in BUILTIN_SUBAGENTS}


# ── 端到端：task 工具委派 → 子代理结果回流父历史 ─────────────────


class TestEndToEnd:
    """父 agent 经 task 工具委派，子代理结果作为工具结果回流。"""

    @pytest.mark.asyncio
    async def test_task_tool_delegates_and_result_flows_back(
        self, tmp_path, monkeypatch
    ):
        task_call = [{
            "id": "call-1",
            "type": "function",
            "function": {
                "name": "task",
                "arguments": json.dumps({
                    "description": "find X",
                    "prompt": "find X in the codebase",
                    "subagent_type": "general-purpose",
                }),
            },
        }]
        parent = _make_agent(tmp_path, [
            (None, task_call),
            ("Done — child-found: file.py:42 confirmed.", None),
        ])
        assert "task" in parent.tools  # 顶层持有 task 工具

        created: dict = {}

        def fake_build(parent_agent, spec, structured_schema=None):
            # 真接线、假大脑：子代理走真实构建路径，只替换 LLM
            child = build_child_agent(
                parent_agent, spec, structured_schema=structured_schema
            )
            child.llm = FakeLLM([("child-found: file.py:42", None)])
            created["child"] = child
            return child

        monkeypatch.setattr(subagent_tool, "build_child_agent", fake_build)

        final = await parent.run("where is X?")
        assert "child-found: file.py:42" in final

        # 父历史里的 task 工具结果同时包含完成标题与子代理返回值
        tool_msgs = [m for m in parent.history.messages if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        content = tool_msgs[0]["content"]
        assert "Subagent 'general-purpose' finished" in content
        assert "child-found: file.py:42" in content

        # 子代理收到的是完整委派 prompt（它看不见父对话）
        child = created["child"]
        assert child.history.messages[0]["content"] == "find X in the codebase"


# ── 工具注册表裁剪 ───────────────────────────────────────────────


class TestChildToolRegistry:
    """子代理工具集 = 全集 − 结构性排除，再与规格白名单取交集。"""

    def test_general_purpose_child_excludes_structural_tools(self, tmp_path):
        parent = _make_agent(tmp_path, [])
        child = build_child_agent(parent, _builtin_specs()["general-purpose"])
        for excluded in ("task", "ask_user", "exit_plan_mode"):
            assert excluded not in child.tools
        # 写入类工具保留
        for kept in ("write_file", "shell", "read_file", "edit_file"):
            assert kept in child.tools

    def test_explore_child_has_exactly_readonly_tools(self, tmp_path):
        parent = _make_agent(tmp_path, [])
        child = build_child_agent(parent, _builtin_specs()["explore"])
        assert set(child.tools) == {
            "read_file", "grep", "glob", "list_directory",
            "git_status", "git_diff", "git_log", "git_branch",
        }

    def test_top_agent_has_task_tool_and_specs(self, tmp_path):
        parent = _make_agent(tmp_path, [])
        assert isinstance(parent.tools["task"], TaskTool)
        assert set(parent._subagent_specs) >= {"general-purpose", "explore"}


# ── 规格加载 ─────────────────────────────────────────────────────


class TestSpecLoading:
    """.openx/agents/*.md 规格：追加、覆盖、坏文件降级。"""

    def test_project_spec_extends_builtins(self, tmp_path):
        agents = tmp_path / ".openx" / "agents"
        agents.mkdir(parents=True)
        (agents / "reviewer.md").write_text(
            "---\nname: reviewer\ndescription: Reviews code.\n"
            "tools: read_file, grep\n---\nYou review code.\n",
            encoding="utf-8",
        )
        specs = load_subagent_specs(str(tmp_path))
        assert set(specs) >= {"general-purpose", "explore", "reviewer"}
        assert specs["reviewer"].tools == ["read_file", "grep"]
        assert "You review code." in specs["reviewer"].system_prompt_extra

    def test_malformed_file_skipped_without_raising(self, tmp_path):
        agents = tmp_path / ".openx" / "agents"
        agents.mkdir(parents=True)
        (agents / "broken.md").write_text(
            "---\nname: broken\ndescription: no closing fence\n",
            encoding="utf-8",
        )
        specs = load_subagent_specs(str(tmp_path))  # 不得抛异常
        assert "broken" not in specs
        assert set(specs) == {"general-purpose", "explore"}

    def test_custom_spec_overrides_builtin_by_name(self, tmp_path):
        agents = tmp_path / ".openx" / "agents"
        agents.mkdir(parents=True)
        (agents / "explore.md").write_text(
            "---\nname: explore\ndescription: Custom explore.\n"
            "tools: read_file\n---\nCustom body.\n",
            encoding="utf-8",
        )
        specs = load_subagent_specs(str(tmp_path))
        assert specs["explore"].tools == ["read_file"]
        assert specs["explore"].description == "Custom explore."

    def test_missing_agents_dir_returns_builtins_only(self, tmp_path):
        specs = load_subagent_specs(str(tmp_path))
        assert set(specs) == {"general-purpose", "explore"}


# ── PermissionRules 共享 ─────────────────────────────────────────


class TestRulesSharing:
    """父子 executor 共享同一 PermissionRules 对象（双向传播）。"""

    def test_parent_and_child_share_rules_object(self, tmp_path):
        parent = _make_agent(tmp_path, [])
        child = build_child_agent(parent, _builtin_specs()["general-purpose"])
        assert parent.tool_executor.rules is child.tool_executor.rules
        # 父侧新增的 allow 规则对子侧立即可见（同一对象，无需同步）
        parent.tool_executor.rules.allow.append("shell(echo)")
        assert (
            child.tool_executor.rules.check("shell", "echo")
            == PermissionLevel.ALLOW
        )


# ── 禁套娃 ───────────────────────────────────────────────────────


class TestNestingGuard:
    """子代理无 task 工具 → 无法派生孙代理。"""

    def test_child_cannot_spawn_grandchildren(self, tmp_path):
        parent = _make_agent(tmp_path, [])
        child = build_child_agent(parent, _builtin_specs()["general-purpose"])
        assert "task" not in child.tools
        assert child._subagent_specs == {}


# ── 未知 subagent_type ───────────────────────────────────────────


class TestUnknownSubagentType:
    @pytest.mark.asyncio
    async def test_unknown_type_error_lists_available(self, tmp_path):
        parent = _make_agent(tmp_path, [])
        result = await parent.tools["task"].execute(
            description="x", prompt="y", subagent_type="nope"
        )
        assert not result.success
        assert "Unknown subagent_type 'nope'" in result.error
        assert "general-purpose" in result.error and "explore" in result.error


# ── 系统提示 ─────────────────────────────────────────────────────


class TestSystemPrompt:
    def test_child_prompt_has_contract_and_extra_parent_has_neither(
        self, tmp_path
    ):
        parent = _make_agent(tmp_path, [])
        spec = SubagentSpec(
            name="custom",
            description="d",
            tools=None,
            system_prompt_extra="EXTRA_MARKER: review with care.",
        )
        child = build_child_agent(parent, spec)
        child_prompt = child._build_system_prompt()
        assert "## Sub-agent mode" in child_prompt          # 契约标记
        assert SUBAGENT_INSTRUCTIONS in child_prompt
        assert "EXTRA_MARKER: review with care." in child_prompt

        parent_prompt = parent._build_system_prompt()
        assert "## Sub-agent mode" not in parent_prompt
        assert "EXTRA_MARKER" not in parent_prompt


# ── 弹窗回调传播（Phase 3 bug-10 契约）──────────────────────────


class TestPromptCallbackPropagation:
    @pytest.mark.asyncio
    async def test_child_inherits_parent_prompt_callbacks(
        self, tmp_path, monkeypatch
    ):
        parent = _make_agent(tmp_path, [])

        def sentinel_start():
            pass

        def sentinel_end():
            pass

        parent.tool_executor.on_prompt_start = sentinel_start
        parent.tool_executor.on_prompt_end = sentinel_end

        created: dict = {}

        def fake_build(parent_agent, spec, structured_schema=None):
            child = build_child_agent(
                parent_agent, spec, structured_schema=structured_schema
            )
            child.llm = FakeLLM([("done", None)])
            created["child"] = child
            return child

        monkeypatch.setattr(subagent_tool, "build_child_agent", fake_build)

        result = await parent.tools["task"].execute(
            description="d", prompt="p"
        )
        assert result.success
        child = created["child"]
        assert child.tool_executor.on_prompt_start is sentinel_start
        assert child.tool_executor.on_prompt_end is sentinel_end

    @pytest.mark.asyncio
    async def test_child_failure_becomes_error_result(self, tmp_path, monkeypatch):
        parent = _make_agent(tmp_path, [])

        def exploding_build(parent_agent, spec):
            raise RuntimeError("boom")

        monkeypatch.setattr(subagent_tool, "build_child_agent", exploding_build)
        result = await parent.tools["task"].execute(description="d", prompt="p")
        assert not result.success and "Subagent failed" in result.error


# ── 舰队视图：子代理流捕获（v0.4.0）──────────────────────────────


class BoomLLM:
    """每次都抛异常的假 LLM。"""

    async def chat(self, messages, tools=None, stream=True):
        raise RuntimeError("api exploded")

    async def stream_chat(self, messages, tools=None):
        raise RuntimeError("api exploded")
        yield  # pragma: no cover —— 使其成为异步生成器


def _task_call(description: str, tc_id: str = "call-1", **extra) -> list:
    args = {
        "description": description,
        "prompt": f"task prompt: {description}",
        "subagent_type": "general-purpose",
        **extra,
    }
    return [{
        "id": tc_id, "type": "function",
        "function": {"name": "task", "arguments": json.dumps(args)},
    }]


class TestFleetCapture:
    """task 子代理的流事件捕获进父 agent.fleet（状态层数据源）。"""

    @pytest.mark.asyncio
    async def test_child_stream_captured_into_fleet(self, tmp_path, monkeypatch):
        parent = _make_agent(tmp_path, [])
        (tmp_path / "f.txt").write_text("data0")
        child_tcs = [{"id": "ct1", "type": "function", "function": {
            "name": "read_file",
            "arguments": json.dumps({"file_path": "f.txt"}),
        }}]
        created = {}

        def fake_build(parent_agent, spec, structured_schema=None):
            child = build_child_agent(
                parent_agent, spec, structured_schema=structured_schema
            )
            child.llm = FakeLLM([(None, child_tcs), ("child report here", None)])
            created["child"] = child
            return child

        monkeypatch.setattr(subagent_tool, "build_child_agent", fake_build)
        result = await parent.tools["task"].execute(
            description="find X", prompt="find X",
        )
        # 终值经 history 重建流入工具结果
        assert result.success
        assert "child report here" in result.output
        # fleet 视图：标签、状态、工具计数、捕获行
        views = parent.fleet.snapshot()
        assert len(views) == 1
        v = views[0]
        assert v["label"] == "find X"
        assert v["status"] == "done"
        assert v["tools_count"] == 1
        assert any("read_file" in ln for ln in v["lines"])
        # prompt 锁已从父 executor 传播（并行子代理弹窗串行化）
        assert (
            created["child"].tool_executor._prompt_lock
            is parent.tool_executor._prompt_lock
        )

    @pytest.mark.asyncio
    async def test_child_error_marks_view(self, tmp_path, monkeypatch):
        parent = _make_agent(tmp_path, [])

        def fake_build(parent_agent, spec, structured_schema=None):
            child = build_child_agent(
                parent_agent, spec, structured_schema=structured_schema
            )
            child.llm = BoomLLM()
            return child

        monkeypatch.setattr(subagent_tool, "build_child_agent", fake_build)
        result = await parent.tools["task"].execute(description="doomed", prompt="p")
        assert not result.success and "Subagent failed" in result.error
        views = parent.fleet.snapshot()
        assert len(views) == 1 and views[0]["status"] == "error"

    @pytest.mark.asyncio
    async def test_max_rounds_fallback_text(self, tmp_path, monkeypatch):
        parent = _make_agent(tmp_path, [])
        tcs = [{"id": "ct1", "type": "function", "function": {
            "name": "read_file",
            "arguments": json.dumps({"file_path": "missing.txt"}),
        }}]

        def fake_build(parent_agent, spec, structured_schema=None):
            child = build_child_agent(
                parent_agent, spec, structured_schema=structured_schema
            )
            child.config.max_tool_rounds = 2
            child.llm = FakeLLM([(None, tcs)] * 4)
            return child

        monkeypatch.setattr(subagent_tool, "build_child_agent", fake_build)
        result = await parent.tools["task"].execute(description="loopy", prompt="p")
        # run() 同款回退串（不经 token 拼接）
        assert result.success
        assert "Reached maximum tool call rounds" in result.output

    @pytest.mark.asyncio
    async def test_parallel_children_get_separate_views(
        self, tmp_path, monkeypatch
    ):
        parent = _make_agent(tmp_path, [
            (None, _task_call("A", "c-a") + _task_call("B", "c-b")),
            ("done", None),
        ])
        counter = {"n": 0}

        def fake_build(parent_agent, spec, structured_schema=None):
            child = build_child_agent(
                parent_agent, spec, structured_schema=structured_schema
            )
            child.llm = FakeLLM([(f"report-{counter['n']}", None)])
            counter["n"] += 1
            return child

        monkeypatch.setattr(subagent_tool, "build_child_agent", fake_build)
        await parent.run("go")
        views = parent.fleet.snapshot()
        assert len(views) == 2
        assert {v["label"] for v in views} == {"A", "B"}
        assert all(v["status"] == "done" for v in views)

    @pytest.mark.asyncio
    async def test_schema_path_completes_view(self, tmp_path, monkeypatch):
        parent = _make_agent(tmp_path, [])
        schema = {
            "type": "object", "required": ["n"],
            "properties": {"n": {"type": "integer"}},
        }
        so_tcs = [{"id": "s1", "type": "function", "function": {
            "name": "structured_output",
            "arguments": json.dumps({"data": {"n": 42}}),
        }}]

        def fake_build(parent_agent, spec, structured_schema=None):
            child = build_child_agent(
                parent_agent, spec, structured_schema=structured_schema
            )
            child.llm = FakeLLM([(None, so_tcs)])
            return child

        monkeypatch.setattr(subagent_tool, "build_child_agent", fake_build)
        result = await parent.tools["task"].execute(
            description="d", prompt="p", schema=schema,
        )
        assert result.success
        assert json.loads(result.output) == {"n": 42}
        views = parent.fleet.snapshot()
        assert len(views) == 1 and views[0]["status"] == "done"
