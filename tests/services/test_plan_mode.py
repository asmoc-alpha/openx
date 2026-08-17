"""Phase 4 plan-mode 测试。

覆盖：schema 过滤（模型看不见写入工具）、executor 第二道防线（硬拦截）、
exit_plan_mode 批准/拒绝两条路径、/mode 切换与 auto-approve 保存/还原、
/workspace 重建后过滤仍生效、系统提示注入 PLAN_MODE_INSTRUCTIONS。

运行：``python -m pytest tests/test_plan_mode.py -q``
"""

from __future__ import annotations

import pytest

from openx.config import OpenXConfig
from openx.instructions import PLAN_MODE_INSTRUCTIONS
from openx.llm import StreamDone
from openx.permissions import Permission, PermissionRules
from openx.services.tool_executor import ToolExecutor
from openx.tools.base import Tool, ToolResult
from openx.tools.plan_tools import ExitPlanModeTool


# ── Fakes ────────────────────────────────────────────────────────

class FakeLLM:
    """可脚本化的假 LLM：plan-mode 测试不会真的调用它，仅为构造一致性。"""

    def __init__(self, responses):
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


class FakeRaw:
    """Duck-typed console.raw：记录 print 调用，绝不触碰终端。"""

    def __init__(self):
        self.printed: list = []

    def print(self, *args, **kwargs):
        self.printed.append(args)


class FakePlanConsole:
    """Duck-typed console：confirm_plan 可脚本化（批准/拒绝）。"""

    def __init__(self, approve: bool = True):
        self.mode = "plan"
        self.raw = FakeRaw()
        self._approve = approve
        self.confirm_calls = 0

    def confirm_plan(self) -> bool:
        self.confirm_calls += 1
        return self._approve


class FakeModeConsole:
    """Duck-typed console：/mode 处理器只需 mode 属性与 print_info/warning。"""

    def __init__(self, mode: str = "auto"):
        self.mode = mode
        self.infos: list[str] = []
        self.warnings: list[str] = []

    def print_info(self, msg: str):
        self.infos.append(msg)

    def print_warning(self, msg: str):
        self.warnings.append(msg)


class SpyWriteTool(Tool):
    """间谍写入工具：ASK 级，记录 execute 是否真的被调用。"""

    name = "write_file"
    description = "spy write tool"
    parameters = {
        "type": "object",
        "properties": {"file_path": {"type": "string"}},
        "required": ["file_path"],
    }

    def __init__(self):
        self.calls = 0

    @property
    def permission(self):
        return Permission.ask("Writing files")

    async def execute(self, file_path: str, content: str = ""):
        self.calls += 1
        return ToolResult(output=f"wrote {file_path}")


class SpyDenyTool(Tool):
    """DENY 级工具：plan mode 下同样应被闸门拦截。"""

    name = "danger"
    description = "spy deny tool"
    parameters = {"type": "object", "properties": {}}

    @property
    def permission(self):
        return Permission.deny("dangerous")

    async def execute(self):
        return ToolResult(output="ran")


def _make_agent(tmp_path, monkeypatch, responses=()):
    """构造一个挂载 FakeLLM 的 OpenXAgent（绕过真实 API）。"""
    from openx.agent import OpenXAgent
    config = OpenXConfig()
    config.workspace = str(tmp_path)
    config.api_key = "sk-test"
    config.api_base = "https://example.com/v1"
    config.model = "test-model"
    agent = OpenXAgent(config)
    agent.llm = FakeLLM(responses)
    agent.tool_executor._rules = PermissionRules()  # 忽略真实 settings.json
    return agent


def _schema_names(agent) -> set[str]:
    """当前 tool_schemas 里模型可见的工具名集合。"""
    return {s["function"]["name"] for s in agent.tool_schemas}


# ── 1. Schema 过滤 ───────────────────────────────────────────────

class TestSchemaFiltering:
    """plan mode 下 ASK/DENY 工具从 schema 中消失，只读工具与新出口保留。"""

    def test_exit_plan_mode_registered(self, tmp_path, monkeypatch):
        agent = _make_agent(tmp_path, monkeypatch)
        assert "exit_plan_mode" in agent.tools
        assert "exit_plan_mode" in _schema_names(agent)

    def test_enter_plan_hides_write_tools(self, tmp_path, monkeypatch):
        agent = _make_agent(tmp_path, monkeypatch)
        agent.set_plan_mode(True)
        names = _schema_names(agent)
        # 写入类工具不可见
        assert "write_file" not in names
        assert "edit_file" not in names
        assert "shell" not in names
        # 只读工具与审批出口仍可见
        assert "read_file" in names
        assert "grep" in names
        assert "glob" in names
        assert "exit_plan_mode" in names

    def test_exit_plan_restores_all_tools(self, tmp_path, monkeypatch):
        agent = _make_agent(tmp_path, monkeypatch)
        agent.set_plan_mode(True)
        agent.set_plan_mode(False)
        # 恢复后：每个注册工具都重新可见
        assert _schema_names(agent) == set(agent.tools)
        assert agent.plan_mode is False


# ── 2. Executor 第二道防线 ───────────────────────────────────────

class TestExecutorGate:
    """即便模型臆造写入工具调用，prepare 也硬拦截、绝不执行。"""

    def _executor(self, plan_mode: bool = True, auto_approve: bool = False):
        console = FakePlanConsole(approve=True)
        executor = ToolExecutor(console, auto_approve=auto_approve, plan_mode=plan_mode)
        executor._rules = PermissionRules()
        return executor

    @pytest.mark.asyncio
    async def test_gate_blocks_write_tool(self):
        executor = self._executor(plan_mode=True)
        spy = SpyWriteTool()
        result, approved = await executor.execute(
            "write_file", spy, '{"file_path": "a.py", "content": "x"}'
        )
        assert not approved
        assert result.error
        assert "exit_plan_mode" in result.error
        assert spy.calls == 0  # 工具绝未被执行

    @pytest.mark.asyncio
    async def test_gate_blocks_prepare_only(self):
        executor = self._executor(plan_mode=True)
        spy = SpyWriteTool()
        pc = await executor.prepare("write_file", spy, '{"file_path": "a.py"}', "tc-1")
        assert pc.pre_result is not None
        assert "exit_plan_mode" in pc.pre_result.error
        assert pc.approved is False

    @pytest.mark.asyncio
    async def test_gate_blocks_deny_level_tool(self):
        executor = self._executor(plan_mode=True, auto_approve=True)
        pc = await executor.prepare("danger", SpyDenyTool(), "{}", "tc-2")
        assert pc.pre_result is not None
        assert "exit_plan_mode" in pc.pre_result.error

    @pytest.mark.asyncio
    async def test_exit_plan_mode_not_gated(self):
        # 审批出口按名字豁免：prepare 放行（无 pre_result）
        executor = self._executor(plan_mode=True)
        console = FakePlanConsole(approve=True)

        class _DuckAgent:
            plan_mode = True
            tool_executor = executor

            def set_plan_mode(self, on):
                self.plan_mode = on

        tool = ExitPlanModeTool(_DuckAgent(), console)
        pc = await executor.prepare("exit_plan_mode", tool, '{"plan": "# P"}', "tc-3")
        assert pc.pre_result is None

    @pytest.mark.asyncio
    async def test_no_gate_when_plan_mode_off(self):
        # plan mode 关闭 → ASK 工具在 auto_approve 下正常执行
        executor = self._executor(plan_mode=False, auto_approve=True)
        spy = SpyWriteTool()
        result, approved = await executor.execute(
            "write_file", spy, '{"file_path": "a.py", "content": "x"}'
        )
        assert approved and result.success
        assert spy.calls == 1


# ── 3/4. ExitPlanModeTool 批准 / 拒绝路径 ────────────────────────

class TestExitPlanModeTool:
    """真实 agent + duck-typed console.confirm_plan 走通两条路径。"""

    @pytest.mark.asyncio
    async def test_approve_path(self, tmp_path, monkeypatch):
        agent = _make_agent(tmp_path, monkeypatch)
        agent.set_plan_mode(True)
        agent.console.confirm_plan = lambda: True  # 用户批准

        result = await agent.tools["exit_plan_mode"].execute(plan="# Plan\n- step 1")

        assert result.success
        assert "approved" in result.output
        assert agent.plan_mode is False
        # schema 恢复：除 choose_mode（仅 manual 可见）外所有工具重新可见
        assert _schema_names(agent) == set(agent.tools) - {"choose_mode"}
        # Claude-Code 式：批准后自动执行
        assert agent.tool_executor.auto_approve is True
        assert agent.console.mode == "auto"

    @pytest.mark.asyncio
    async def test_reject_path(self, tmp_path, monkeypatch):
        agent = _make_agent(tmp_path, monkeypatch)
        agent.set_plan_mode(True)
        agent.console.confirm_plan = lambda: False  # 用户拒绝

        result = await agent.tools["exit_plan_mode"].execute(plan="# Plan")

        # 非错误输出，让模型继续推理、修订计划
        assert result.success
        assert "rejected" in result.output
        assert "Revise" in result.output
        assert agent.plan_mode is True
        assert "write_file" not in _schema_names(agent)


# ── 5. /mode 处理器 ──────────────────────────────────────────────

class TestModeCommand:
    """/mode [manual|auto|plan]：显式切换；无参显示当前；非法值警告。"""

    @pytest.mark.asyncio
    async def test_switch_plan_and_back_with_auto_approve_roundtrip(
        self, tmp_path, monkeypatch
    ):
        from openx.app.cli.commands import handle_slash_command

        agent = _make_agent(tmp_path, monkeypatch)
        agent.tool_executor.auto_approve = True  # 起始开启
        console = FakeModeConsole(mode=agent.mode)
        agent.console = console  # 与生产一致：REPL 与 agent 共用同一 console
        assert agent.mode == "manual"  # 启动默认 manual

        # → plan：schema 过滤生效，executor auto_approve 强制关闭
        keep = await handle_slash_command("mode", agent, console, ["plan"])
        assert keep is True
        assert agent.plan_mode is True
        assert console.mode == "plan"
        assert agent.tool_executor.auto_approve is False
        assert "write_file" not in _schema_names(agent)
        assert any("exit_plan_mode" in m for m in console.infos)

        # → auto：auto_approve 还原起始值；choose_mode 仅 manual 可见
        await handle_slash_command("mode", agent, console, ["auto"])
        assert agent.mode == "auto" and agent.plan_mode is False
        assert console.mode == "auto"
        assert agent.tool_executor.auto_approve is True  # 还原起始值
        assert _schema_names(agent) == set(agent.tools) - {"choose_mode"}

        # → manual：choose_mode 重新可见
        await handle_slash_command("mode", agent, console, ["manual"])
        assert agent.mode == "manual"
        assert _schema_names(agent) == set(agent.tools)

    @pytest.mark.asyncio
    async def test_no_args_shows_current_mode(self, tmp_path, monkeypatch):
        from openx.app.cli.commands import handle_slash_command

        agent = _make_agent(tmp_path, monkeypatch)
        console = FakeModeConsole(mode=agent.mode)
        agent.console = console  # 与生产一致：REPL 与 agent 共用同一 console
        await handle_slash_command("mode", agent, console, [])
        assert agent.mode == "manual"  # 未切换
        assert any("manual" in m and "Usage" in m for m in console.infos)

    @pytest.mark.asyncio
    async def test_invalid_arg_warns_and_keeps_mode(self, tmp_path, monkeypatch):
        from openx.app.cli.commands import handle_slash_command

        agent = _make_agent(tmp_path, monkeypatch)
        console = FakeModeConsole(mode=agent.mode)
        agent.console = console  # 与生产一致：REPL 与 agent 共用同一 console
        keep = await handle_slash_command("mode", agent, console, ["bogus"])
        assert keep is True
        assert agent.mode == "manual"  # 不变
        assert any("bogus" in w for w in console.warnings)


# ── 6. /workspace 重建回归 ───────────────────────────────────────

class TestWorkspaceRebuildRegression:
    """/workspace 重建工具注册表后，plan-mode 过滤依然生效。"""

    def test_filter_survives_rebuild(self, tmp_path, monkeypatch):
        agent = _make_agent(tmp_path, monkeypatch)
        agent.set_plan_mode(True)

        # 模拟 /workspace 处理器：重建工具 + 中心 schema 计算
        agent.tools = agent._build_tools()
        agent.tool_schemas = agent._compute_tool_schemas()

        names = _schema_names(agent)
        assert "write_file" not in names
        assert "edit_file" not in names
        assert "shell" not in names
        assert "read_file" in names
        assert "grep" in names
        assert "glob" in names
        assert "exit_plan_mode" in names


# ── 7. 系统提示注入 ──────────────────────────────────────────────

class TestSystemPrompt:
    """plan mode 开关驱动 PLAN_MODE_INSTRUCTIONS 的注入/移除。"""

    def test_instructions_injected_when_on(self, tmp_path, monkeypatch):
        agent = _make_agent(tmp_path, monkeypatch)
        assert PLAN_MODE_INSTRUCTIONS not in agent._system_prompt

        agent.set_plan_mode(True)
        assert PLAN_MODE_INSTRUCTIONS in agent._system_prompt
        assert "exit_plan_mode" in agent._system_prompt

    def test_instructions_removed_when_off(self, tmp_path, monkeypatch):
        agent = _make_agent(tmp_path, monkeypatch)
        agent.set_plan_mode(True)
        agent.set_plan_mode(False)
        assert PLAN_MODE_INSTRUCTIONS not in agent._system_prompt
