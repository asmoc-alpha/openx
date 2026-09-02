"""结构化输出（structured_output）回归测试 —— v0.3.2。

覆盖：
- validate：类型（含 bool⊄int）/ enum / required / properties 递归 /
  items 递归 / 类型数组 / 坏 schema 宽容降级；
- StructuredOutputTool：校验通过写入 agent 并成功、失败返错误消息且
  绝不写入、缺 data 报错；动态参数表携带调用方 schema；
- OpenXAgent 集成（FakeLLM）：一次调用即捕获并返回 JSON、校验失败后
  模型重试第二轮成功、从不调用则保留文本语义且属性抛错；工具注册与
  系统提示；子代理白名单裁剪不得裁掉 structured_output；
- TaskTool：schema 非对象早失败、捕获 → JSON 输出、未捕获 → 错误；
- Workflow：agent(prompt, schema=...) 返回校验过的 Python 对象；
  未捕获 → None 且计入 failed；坏 schema → WorkflowError。

风格：pytest-asyncio auto、手写 FakeLLM、禁 unittest.mock。

运行：``python -m pytest tests/test_structured_output.py -q``
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from openx.config import OpenXConfig
from openx.orchestration import workflow as workflow_mod
from openx.orchestration.workflow import WorkflowEngine, WorkflowError
from openx.llm import StreamDone
from openx.tools import subagent_tool
from openx.tools.structured_output import StructuredOutputTool
from openx.utils.jsonschema import validate


SCHEMA = {
    "type": "object",
    "required": ["answer"],
    "properties": {
        "answer": {"type": "string"},
        "confidence": {"type": "number"},
    },
}


# ── validate 单元 ────────────────────────────────────────────────

class TestValidate:
    def test_pass_and_type_errors(self):
        assert validate({"answer": "x"}, SCHEMA) is None
        assert "expected string" in validate({"answer": 1}, SCHEMA)
        assert "missing required property 'answer'" in validate({}, SCHEMA)

    def test_bool_is_not_integer(self):
        assert "expected integer, got boolean" in validate(True, {"type": "integer"})
        assert validate(3, {"type": "integer"}) is None

    def test_number_accepts_int_and_float(self):
        assert validate(3, {"type": "number"}) is None
        assert validate(3.5, {"type": "number"}) is None
        assert "got string" in validate("3", {"type": "number"})

    def test_type_array(self):
        s = {"type": ["string", "null"]}
        assert validate("a", s) is None
        assert validate(None, s) is None
        assert "expected string/null" in validate(1, s)

    def test_enum(self):
        assert validate("bug", {"enum": ["bug", "feature"]}) is None
        assert "not in enum" in validate("task", {"enum": ["bug", "feature"]})

    def test_nested_properties_and_items(self):
        s = {
            "type": "object",
            "properties": {
                "files": {"type": "array", "items": {"type": "string"}},
                "meta": {
                    "type": "object",
                    "required": ["lines"],
                    "properties": {"lines": {"type": "integer"}},
                },
            },
        }
        good = {"files": ["a.py"], "meta": {"lines": 3}}
        assert validate(good, s) is None
        assert "files[1]" in validate({"files": ["a", 2]}, s)
        assert "meta.lines" in validate({"meta": {"lines": "x"}}, s)
        assert "meta: missing required" in validate({"meta": {}}, s)

    def test_non_dict_schema_is_permissive(self):
        assert validate({"anything": 1}, None) is None
        assert validate(5, "oops") is None


# ── StructuredOutputTool 单元 ────────────────────────────────────

class FakeAgentSlot:
    _structured_result = None


class TestStructuredOutputTool:
    def _tool(self, agent=None):
        return StructuredOutputTool(agent or FakeAgentSlot(), SCHEMA)

    async def test_valid_data_captures(self):
        agent = FakeAgentSlot()
        result = await self._tool(agent).execute(data={"answer": "42"})
        assert result.success
        assert agent._structured_result == {"answer": "42"}

    async def test_invalid_data_rejected_not_captured(self):
        agent = FakeAgentSlot()
        result = await self._tool(agent).execute(data={"answer": 42})
        assert not result.success
        assert "Schema validation failed" in result.error
        assert "expected string" in result.error
        assert agent._structured_result is None

    async def test_missing_data(self):
        result = await self._tool().execute()
        assert not result.success and "Missing required field" in result.error

    def test_dynamic_parameters_carry_schema(self):
        schema = self._tool().to_openai_schema()["function"]["parameters"]
        assert schema["required"] == ["data"]
        assert schema["properties"]["data"]["required"] == ["answer"]


# ── OpenXAgent 集成（FakeLLM）────────────────────────────────────

class FakeLLM:
    """脚本化假 LLM：按序返回 (content, tool_calls)。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.call_count = 0

    async def stream_chat(self, messages, tools=None):
        yield StreamDone(response=self._next(), token_count=5, input_tokens=10)

    async def chat(self, messages, tools=None, stream=True):
        return self._next()

    def _next(self):
        content, tool_calls = self.responses[self.call_count]
        self.call_count += 1
        resp = {"role": "assistant", "content": content or None}
        if tool_calls:
            resp["tool_calls"] = tool_calls
        return resp


def _so_call(data: object, tc_id: str = "c1") -> list:
    """构造一次 structured_output 工具调用（data 经外层包装传递）。"""
    return [{
        "id": tc_id, "type": "function",
        "function": {
            "name": "structured_output",
            "arguments": json.dumps({"data": data}),
        },
    }]


def _make_agent(tmp_path, responses, structured_schema=None, **kw):
    from openx.agent import OpenXAgent
    config = OpenXConfig()
    config.workspace = str(tmp_path)
    config.api_key = "sk-test"
    config.api_base = "https://example.com/v1"
    config.model = "test-model"
    agent = OpenXAgent(config, structured_schema=structured_schema, **kw)
    agent.llm = FakeLLM(responses)
    return agent


class TestAgentStructuredRun:
    async def test_single_valid_call_returns_json(self, tmp_path):
        agent = _make_agent(
            tmp_path,
            [(None, _so_call({"answer": "hi", "confidence": 0.9}))],
            structured_schema=SCHEMA,
        )
        out = await agent.run("give answer")
        assert json.loads(out) == {"answer": "hi", "confidence": 0.9}
        assert agent.has_structured_result()
        assert agent.structured_result == {"answer": "hi", "confidence": 0.9}

    async def test_validation_failure_then_retry_succeeds(self, tmp_path):
        agent = _make_agent(
            tmp_path,
            [
                (None, _so_call({"answer": 42}, "c1")),   # 校验失败
                (None, _so_call({"answer": "fixed"}, "c2")),  # 模型修正
            ],
            structured_schema=SCHEMA,
        )
        out = await agent.run("give answer")
        assert json.loads(out) == {"answer": "fixed"}
        # 两轮消息都进了状态流：第一轮 tool 结果含校验错误
        assert agent.llm.call_count == 2

    async def test_never_called_keeps_text_semantics(self, tmp_path):
        agent = _make_agent(
            tmp_path,
            [("plain text final", None)],
            structured_schema=SCHEMA,
        )
        out = await agent.run("give answer")
        assert out == "plain text final"
        assert not agent.has_structured_result()
        with pytest.raises(RuntimeError):
            _ = agent.structured_result

    def test_tool_registered_and_in_schemas(self, tmp_path):
        agent = _make_agent(tmp_path, [], structured_schema=SCHEMA)
        assert "structured_output" in agent.tools
        names = {s["function"]["name"] for s in agent.tool_schemas}
        assert "structured_output" in names

    def test_absent_without_schema(self, tmp_path):
        agent = _make_agent(tmp_path, [])
        assert "structured_output" not in agent.tools

    def test_system_prompt_carries_contract_and_schema(self, tmp_path):
        agent = _make_agent(tmp_path, [], structured_schema=SCHEMA)
        assert "structured_output" in agent._system_prompt
        assert '"answer"' in agent._system_prompt
        assert "DISCARDED" in agent._system_prompt

    async def test_allowlist_never_drops_structured_output(self, tmp_path):
        """子代理白名单交集之后注入 → 规格裁剪不得裁掉契约出口。"""
        from openx.agent import OpenXAgent
        from openx.orchestration.subagent import BUILTIN_SUBAGENTS
        parent = _make_agent(tmp_path, [])
        spec = next(s for s in BUILTIN_SUBAGENTS if s.name == "explore")
        assert spec.tools  # explore 带显式白名单
        child = subagent_tool.build_child_agent(
            parent, spec, structured_schema=SCHEMA
        )
        assert "structured_output" in child.tools
        # 常规结构性排除依旧生效
        assert "task" not in child.tools
        # 白名单内工具保留
        assert "read_file" in child.tools


# ── TaskTool schema 路径 ─────────────────────────────────────────

class FakeStructuredChild:
    def __init__(self, captured):
        self._captured = captured  # None = 未捕获
        self.tool_executor = type("E", (), {
            "on_prompt_start": None, "on_prompt_end": None,
            "_prompt_lock": None,
        })()
        # 终值重建自历史（TaskTool 的 _child_final_text 读末条 assistant）
        self.history = SimpleNamespace(messages=[
            {"role": "assistant", "content": "text-that-must-be-ignored"},
        ])

    async def stream_run(self, prompt):
        yield "text-that-must-be-ignored"

    def has_structured_result(self):
        return self._captured is not None

    @property
    def structured_result(self):
        return self._captured


def _task_tool_with_child(monkeypatch, tmp_path, child):
    from openx.orchestration.subagent import BUILTIN_SUBAGENTS
    from openx.tools.subagent_tool import TaskTool

    class FakeParent:
        tool_executor = type("E", (), {
            "on_prompt_start": None, "on_prompt_end": None,
            "_prompt_lock": None,
        })()

    monkeypatch.setattr(
        subagent_tool, "build_child_agent",
        lambda parent, spec, structured_schema=None: child,
    )
    specs = {s.name: s for s in BUILTIN_SUBAGENTS}
    return TaskTool(FakeParent(), specs)


class TestTaskToolSchema:
    async def test_schema_must_be_object(self, tmp_path, monkeypatch):
        tool = _task_tool_with_child(monkeypatch, tmp_path,
                                     FakeStructuredChild(None))
        result = await tool.execute(
            description="d", prompt="p", schema=["not", "a", "schema"],
        )
        assert not result.success
        assert "JSON Schema object" in result.error

    async def test_captured_result_returned_as_json(self, tmp_path, monkeypatch):
        tool = _task_tool_with_child(
            monkeypatch, tmp_path,
            FakeStructuredChild({"files": ["a.py"], "count": 1}),
        )
        result = await tool.execute(
            description="d", prompt="p",
            schema={"type": "object"},
        )
        assert result.success
        assert json.loads(result.output) == {"files": ["a.py"], "count": 1}

    async def test_not_captured_is_error(self, tmp_path, monkeypatch):
        tool = _task_tool_with_child(monkeypatch, tmp_path,
                                     FakeStructuredChild(None))
        result = await tool.execute(
            description="d", prompt="p", schema={"type": "object"},
        )
        assert not result.success
        assert "without producing structured output" in result.error

    async def test_no_schema_keeps_text_path(self, tmp_path, monkeypatch):
        tool = _task_tool_with_child(
            monkeypatch, tmp_path, FakeStructuredChild(None),
        )
        result = await tool.execute(description="d", prompt="p")
        assert result.success
        assert "text-that-must-be-ignored" in result.output


# ── Workflow schema 路径 ─────────────────────────────────────────

class FakeWorkflowChild:
    def __init__(self, captured=None):
        self._captured = captured
        self.total_output_tokens = 3
        self.tool_executor = type("E", (), {
            "on_prompt_start": None, "on_prompt_end": None,
        })()
        self.history = SimpleNamespace(messages=[
            {"role": "assistant", "content": "child-text"},
        ])

    async def stream_run(self, prompt):
        await asyncio.sleep(0)
        yield "child-text"

    def has_structured_result(self):
        return self._captured is not None

    @property
    def structured_result(self):
        return self._captured


class FakeWfParent:
    workspace = "."
    console = None
    tool_executor = None


class TestWorkflowSchema:
    async def test_agent_returns_validated_object(self, tmp_path, monkeypatch):
        child = FakeWorkflowChild({"n": 42, "ok": True})
        monkeypatch.setattr(
            workflow_mod, "_build_workflow_child",
            lambda parent, st, lock, structured_schema=None: child,
        )
        engine = WorkflowEngine(FakeWfParent())
        script = (
            "async def main(agent, parallel, pipeline, phase, log, args):\n"
            "    return await agent('count', schema={'type': 'object'})\n"
        )
        result, stats = await engine.run(script)
        # 脚本拿到的是 Python 对象，不是文本
        assert result == {"n": 42, "ok": True}
        assert stats.agents_run == 1 and stats.agents_failed == 0

    async def test_uncaptured_schema_agent_is_none(self, tmp_path, monkeypatch):
        child = FakeWorkflowChild(None)  # 从未调用 structured_output
        monkeypatch.setattr(
            workflow_mod, "_build_workflow_child",
            lambda parent, st, lock, structured_schema=None: child,
        )
        engine = WorkflowEngine(FakeWfParent())
        script = (
            "async def main(agent, parallel, pipeline, phase, log, args):\n"
            "    return await agent('x', schema={'type': 'object'})\n"
        )
        result, stats = await engine.run(script)
        assert result is None
        assert stats.agents_failed == 1 and stats.agents_run == 0

    async def test_no_schema_still_returns_text(self, tmp_path, monkeypatch):
        child = FakeWorkflowChild(None)
        monkeypatch.setattr(
            workflow_mod, "_build_workflow_child",
            lambda parent, st, lock, structured_schema=None: child,
        )
        engine = WorkflowEngine(FakeWfParent())
        script = (
            "async def main(agent, parallel, pipeline, phase, log, args):\n"
            "    return await agent('x')\n"
        )
        result, _ = await engine.run(script)
        assert result == "child-text"

    async def test_bad_schema_type_raises(self, tmp_path, monkeypatch):
        engine = WorkflowEngine(FakeWfParent())
        script = (
            "async def main(agent, parallel, pipeline, phase, log, args):\n"
            "    return await agent('x', schema='nope')\n"
        )
        with pytest.raises(WorkflowError, match="JSON Schema object"):
            await engine.run(script)
