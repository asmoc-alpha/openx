"""复杂任务自动进入 plan 模式：EnterPlanModeTool + 回合中途提示重同步。

覆盖三块：

1. 工具语义——manual/auto 下切进 plan、已在 plan 下是空操作、headless
   （``plan_entry_enabled=False``）下不切且明确告知；
2. 可见性——manual/auto 可见、plan 与 headless 不可见（schema 层第一道防线，
   kernel guard 是第二道，见 tests/kernel/test_guard.py）；
3. **端到端**（本功能的地基）：模型在回合中途调 enter_plan_mode 之后，
   *下一次请求* 里的系统提示必须已含 ``PLAN_MODE_INSTRUCTIONS``、工具 schema
   里写入类工具必须已消失——否则模型"写工具没了、提示还是旧的"，既不知道
   自己被切进了计划模式，也不知道该去提交计划。

运行：``python -m pytest tests/services/test_plan_entry.py -q``
"""

from __future__ import annotations

import pytest

from openx.config import OpenXConfig
from openx.instructions import (
    AUTO_MODE_INSTRUCTIONS,
    MANUAL_MODE_INSTRUCTIONS,
    PLAN_MODE_INSTRUCTIONS,
)
from openx.llm import StreamDone
from openx.permissions import PermissionRules
from openx.tools.plan_tools import EnterPlanModeTool


# ── Fakes ────────────────────────────────────────────────────────

class RecordingLLM:
    """脚本化流式假 LLM：**留下每次请求的 messages 与 tools**（断言用）。"""

    def __init__(self, script):
        """script 元素为 ``(content, tool_calls)``。"""
        self.script = list(script)
        self.call_count = 0
        self.requests: list[dict] = []

    async def stream_chat(self, messages, tools=None):
        self.requests.append({
            "messages": [dict(m) for m in messages],
            "tools": [t["function"]["name"] for t in (tools or [])],
        })
        content, tool_calls = self.script[self.call_count]
        self.call_count += 1
        if content:
            for tok in content.split():
                yield tok + " "
        resp = {"role": "assistant", "content": content or None}
        if tool_calls:
            resp["tool_calls"] = tool_calls
        yield StreamDone(response=resp, token_count=5, input_tokens=10)

    async def chat(self, messages, tools=None, stream=True):
        self.requests.append({
            "messages": [dict(m) for m in messages],
            "tools": [t["function"]["name"] for t in (tools or [])],
        })
        content, tool_calls = self.script[self.call_count]
        self.call_count += 1
        resp = {"role": "assistant", "content": content}
        if tool_calls:
            resp["tool_calls"] = tool_calls
        return resp

    def system_prompt(self, index: int) -> str:
        return str(self.requests[index]["messages"][0].get("content") or "")


class FakeRaw:
    def __init__(self):
        self.printed: list = []

    def print(self, *args, **kwargs):
        self.printed.append(" ".join(str(a) for a in args))


class FakeConsole:
    """Duck-typed console：raw.print 只记录（不碰终端）。"""

    def __init__(self, mode: str = "manual"):
        self.mode = mode
        self.raw = FakeRaw()


def _make_agent(tmp_path, responses=()):
    from openx.agent import OpenXAgent

    config = OpenXConfig()
    config.workspace = str(tmp_path)
    config.model = "test-model"
    agent = OpenXAgent(config)
    agent.llm = RecordingLLM(responses)
    agent.tool_executor._rules = PermissionRules()  # 忽略真实 settings.json
    return agent


def _schema_names(agent) -> set[str]:
    return {s["function"]["name"] for s in agent.tool_schemas}


def _call_enter_plan_mode() -> dict:
    return {
        "id": "call-1",
        "type": "function",
        "function": {"name": "enter_plan_mode", "arguments": '{"reason": "多文件"}'},
    }


def _bind_console(agent, console) -> None:
    """把工具的 console 换成假 console（真 console 会往终端打提示）。"""
    agent.tools["enter_plan_mode"]._console = console
    agent.console = console
    agent.tool_executor.console = console


# ── 1. 工具语义 ──────────────────────────────────────────────────

class TestEnterPlanModeTool:
    async def test_manual_switches_to_plan_and_announces(self, tmp_path):
        agent = _make_agent(tmp_path)
        console = FakeConsole()
        _bind_console(agent, console)
        assert agent.mode == "manual"

        result = await agent.tools["enter_plan_mode"].execute(reason="要改 4 个文件")

        assert result.success
        assert agent.mode == "plan"
        assert "exit_plan_mode" in result.output
        assert any("要改 4 个文件" in line for line in console.raw.printed), (
            console.raw.printed
        )

    async def test_auto_switches_to_plan(self, tmp_path):
        """auto 下同样生效：复杂任务的确认点前移到计划本身。"""
        agent = _make_agent(tmp_path)
        _bind_console(agent, FakeConsole(mode="auto"))
        agent.set_mode("auto")

        result = await agent.tools["enter_plan_mode"].execute(reason="新功能")

        assert result.success and agent.mode == "plan"

    async def test_already_in_plan_is_noop(self, tmp_path):
        agent = _make_agent(tmp_path)
        console = FakeConsole(mode="plan")
        _bind_console(agent, console)
        agent.set_mode("plan")

        result = await agent.tools["enter_plan_mode"].execute(reason="再来一次")

        assert result.success and agent.mode == "plan"
        assert "Already in plan mode" in result.output
        assert console.raw.printed == []          # 不重复打提示

    async def test_headless_is_unavailable(self, tmp_path):
        """非交互运行没人能审批计划 → 不切模式，并明确告诉模型别规划。"""
        agent = _make_agent(tmp_path)
        console = FakeConsole()
        _bind_console(agent, console)
        agent.plan_entry_enabled = False

        result = await agent.tools["enter_plan_mode"].execute(reason="x")

        assert result.success and agent.mode == "manual"
        assert "non-interactive" in result.output
        assert console.raw.printed == []

    async def test_permission_is_allow(self, tmp_path):
        """进入计划模式本身无副作用：真正的闸门是出口的审批弹窗。"""
        agent = _make_agent(tmp_path)
        assert agent.tools["enter_plan_mode"].permission.level.value == "allow"


# ── 2. 可见性 ────────────────────────────────────────────────────

class TestVisibility:
    def test_visible_in_manual_and_auto(self, tmp_path):
        agent = _make_agent(tmp_path)
        assert "enter_plan_mode" in _schema_names(agent)          # manual
        agent.set_mode("auto")
        assert "enter_plan_mode" in _schema_names(agent)

    def test_hidden_in_plan_mode(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent.set_mode("plan")
        assert "enter_plan_mode" not in _schema_names(agent)

    def test_hidden_when_headless(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent.plan_entry_enabled = False
        agent.set_mode("auto")
        assert "enter_plan_mode" not in _schema_names(agent)

    def test_auto_prompt_mentions_planning_only_when_enabled(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent.set_mode("auto")
        assert AUTO_MODE_INSTRUCTIONS in agent._system_prompt
        agent.plan_entry_enabled = False
        agent.set_mode("auto")            # 重建提示
        assert AUTO_MODE_INSTRUCTIONS not in agent._system_prompt

    def test_manual_prompt_teaches_the_routing(self, tmp_path):
        agent = _make_agent(tmp_path)
        assert MANUAL_MODE_INSTRUCTIONS in agent._system_prompt
        assert "enter_plan_mode" in agent._system_prompt
        assert "choose_mode" in agent._system_prompt


# ── 3. 端到端：回合中途切模式，提示必须跟上 ──────────────────────

class TestMidTurnPromptSync:
    async def test_next_round_sees_plan_prompt_and_no_write_tools(self, tmp_path):
        """核心回归：模型自己切进 plan 之后，下一轮请求必须是真正的 plan 轮。"""
        agent = _make_agent(tmp_path, responses=[
            ("这个任务有点大，先出计划。", [_call_enter_plan_mode()]),
            ("计划如下……", None),
        ])
        _bind_console(agent, FakeConsole())

        async for _ in agent.stream_run("给 config 加一层校验并补测试"):
            pass

        assert len(agent.llm.requests) == 2, agent.llm.requests
        # 第一轮：manual 提示 + 写工具可见
        assert PLAN_MODE_INSTRUCTIONS not in agent.llm.system_prompt(0)
        assert "write_file" in agent.llm.requests[0]["tools"]
        # 第二轮：plan 提示已生效、写工具已消失
        second_prompt = agent.llm.system_prompt(1)
        assert PLAN_MODE_INSTRUCTIONS in second_prompt
        assert MANUAL_MODE_INSTRUCTIONS not in second_prompt
        assert "write_file" not in agent.llm.requests[1]["tools"]
        assert "shell" not in agent.llm.requests[1]["tools"]
        assert "exit_plan_mode" in agent.llm.requests[1]["tools"]
        assert agent.mode == "plan"

    async def test_round_without_mode_change_keeps_the_seeded_prompt(self, tmp_path):
        """钉住重同步不是"每轮无条件重写"：没变化时提示原样。"""
        agent = _make_agent(tmp_path, responses=[
            ("先看一眼。", [{
                "id": "call-1", "type": "function",
                "function": {"name": "glob", "arguments": '{"pattern": "*.py"}'},
            }]),
            ("看完了。", None),
        ])
        _bind_console(agent, FakeConsole())

        async for _ in agent.stream_run("看看有哪些 py 文件"):
            pass

        assert len(agent.llm.requests) == 2
        assert agent.llm.system_prompt(1) == agent.llm.system_prompt(0)
        assert agent.mode == "manual"
        assert MANUAL_MODE_INSTRUCTIONS in agent.llm.system_prompt(1)

    async def test_single_shot_turns_plan_entry_off(self):
        """接线回归：headless 入口确实关掉了计划入口（标志位不是摆设）。"""
        from openx.app.cli.single_shot import run_single_shot

        class _FakeConsole:
            def show_startup_single_shot(self, prompt):
                pass

            def print_streaming_start(self):
                pass

            def print_assistant(self, text):
                pass

            def print_streaming_done(self, elapsed, tokens):
                pass

            def print_error(self, msg):
                pass

        class _FakeAgent:
            plan_entry_enabled = True    # 默认允许，single-shot 必须关掉它
            total_output_tokens = 0
            session_id = "s"
            config = type("C", (), {"model": "m"})()
            tools: dict = {}

            def __init__(self):
                self.modes: list[str] = []

            def set_mode(self, mode):
                self.modes.append(mode)

            async def startup(self):
                pass

            async def shutdown(self):
                pass

            async def run(self, content):
                return "ok"

        agent = _FakeAgent()
        code = await run_single_shot(agent, _FakeConsole(), "改个东西")

        assert code == 0
        assert agent.plan_entry_enabled is False
        assert agent.modes == ["auto"]   # headless 仍强制 auto

    async def test_non_stream_path_syncs_too(self, tmp_path):
        """非流式 run() 走同一条重同步（两条循环都要跟上模式变化）。"""
        agent = _make_agent(tmp_path, responses=[
            ("", [_call_enter_plan_mode()]),
            ("计划。", None),
        ])
        _bind_console(agent, FakeConsole())

        await agent.run("重构一下")

        assert len(agent.llm.requests) == 2
        assert PLAN_MODE_INSTRUCTIONS in agent.llm.system_prompt(1)
        assert "write_file" not in agent.llm.requests[1]["tools"]
