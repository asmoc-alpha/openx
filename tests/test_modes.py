"""三模式权限系统（manual/auto/plan）回归测试 —— 2026-07 v0.3.0。

覆盖：
- 启动默认 manual；子代理快照父模式；choose_mode 仅顶层且仅 manual 可见；
- set_mode 全迁移同步（executor/console/schemas/系统提示）、非法值、
  auto_approve 在 plan 进出的保存/还原、set_plan_mode 兼容包装；
- manual 闸门：ASK 工具永远逐项弹窗——绕过存储 allow 规则、shell 白名单
  与 auto_approve/-y；can_remember=False 且绝不落盘规则；ALLOW 工具免弹；
  deny 规则与 PreToolUse 钩子否决仍生效；
- 高危闸门：is_high_risk（shell 命中 dangerous_patterns）任何模式永远
  弹窗，规则/白名单/auto_approve 都不得跳过；记住后再来仍弹；
  批准可执行、拒绝报错；
- choose_mode 工具：三选项分发、非 manual 执行器防线、防重复闩、
  "Other" 安全默认；
- 指令注入：MANUAL/PLAN 仅对应模式、仅顶层；
- headless：run_single_shot 强制 auto。

风格：pytest-asyncio auto、手写 FakeConsole/FakeLLM、禁 unittest.mock。

运行：``python -m pytest tests/test_modes.py -q``
"""

from __future__ import annotations

import pytest

from openx.config import OpenXConfig
from openx.instructions import MANUAL_MODE_INSTRUCTIONS, PLAN_MODE_INSTRUCTIONS
from openx.llm import StreamDone
from openx.permissions import Permission, PermissionLevel, PermissionRules
from openx.services.tool_executor import ToolExecutor
from openx.tools.base import Tool, ToolResult


# ── Fakes ────────────────────────────────────────────────────────


class FakeLLM:
    """可脚本化假 LLM（agent.run 走 chat 路径）。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.call_count = 0

    async def stream_chat(self, messages, tools=None):
        content, tool_calls = self.responses[self.call_count]
        self.call_count += 1
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


class FakeAskConsole:
    """记录 ask_permission 调用（含 can_remember），返回脚本化结果。"""

    def __init__(self, approve: bool = True, remember: bool = False):
        self.approve = approve
        self.remember = remember
        self.calls: list[dict] = []
        self.mode = "manual"

    async def ask_permission(self, tool_name, reason, details="", args_summary="",
                       can_remember=True, diff=None):
        self.calls.append({
            "tool": tool_name, "args_summary": args_summary,
            "can_remember": can_remember,
        })
        return (self.approve, self.remember)


class FakeQuestionConsole:
    """脚本化 ask_user_question（choose_mode 弹窗）+ raw.print。"""

    class _Raw:
        def __init__(self):
            self.printed: list = []

        def print(self, *args, **kwargs):
            self.printed.append(args)

    def __init__(self, answer: str = "Auto"):
        self._answer = answer
        self.questions: list[str] = []
        self.raw = FakeQuestionConsole._Raw()
        self.mode = "manual"

    def ask_user_question(self, question, options, multi_select=False):
        self.questions.append(question)
        return [self._answer]


class SpyAskTool(Tool):
    """ASK 级间谍工具：记录 execute 是否真的被调用。"""

    name = "spy_write"
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


class SpyAllowTool(Tool):
    """ALLOW 级间谍工具（只读语义）。"""

    name = "spy_read"
    description = "spy read tool"
    parameters = {"type": "object", "properties": {}}

    def __init__(self):
        self.calls = 0

    async def execute(self):
        self.calls += 1
        return ToolResult(output="read ok")


class SpyHighRiskTool(SpyAskTool):
    """永远高风险的 ASK 工具；白名单也声明免询问（须被 force_prompt 压倒）。"""

    name = "spy_high_risk"

    def auto_allowed(self, args):
        return True

    def is_high_risk(self, args):
        return True


def _make_agent(tmp_path, monkeypatch=None, responses=()):
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


def _executor(console, *, mode="auto", auto_approve=False):
    ex = ToolExecutor(console, auto_approve=auto_approve, mode=mode)
    ex._rules = PermissionRules()
    return ex


def _schema_names(agent) -> set[str]:
    return {s["function"]["name"] for s in agent.tool_schemas}


# ── 1. 默认模式与继承 ────────────────────────────────────────────


class TestDefaultMode:
    def test_fresh_agent_starts_in_manual(self, tmp_path):
        agent = _make_agent(tmp_path)
        assert agent.mode == "manual"
        assert agent.plan_mode is False
        assert agent.tool_executor.mode == "manual"
        assert agent.console.mode == "manual"
        assert agent.mode_choice_offered is False

    def test_manual_schemas_show_all_tools(self, tmp_path):
        agent = _make_agent(tmp_path)
        names = _schema_names(agent)
        # manual 不过滤 schema——写入工具可见（仅弹窗行为不同）
        assert {"write_file", "shell", "choose_mode"} <= names
        assert names == set(agent.tools)

    def test_child_inherits_parent_mode_snapshot(self, tmp_path):
        from openx.agent import OpenXAgent
        parent = _make_agent(tmp_path)
        parent.set_mode("auto")
        config = OpenXConfig()
        config.workspace = str(tmp_path)
        config.api_key = "sk-test"
        config.api_base = "https://example.com/v1"
        config.model = "test-model"
        child = OpenXAgent(config, parent=parent)
        assert child.mode == "auto"
        assert child.tool_executor.mode == "auto"
        # choose_mode 是顶层专属工具，子代理结构性排除
        assert "choose_mode" not in child.tools


# ── 2. set_mode 迁移与同步 ───────────────────────────────────────


class TestSetMode:
    def test_full_cycle_syncs_all_state(self, tmp_path):
        agent = _make_agent(tmp_path)
        for target in ("auto", "plan", "manual"):
            agent.set_mode(target)
            assert agent.mode == target
            assert agent.tool_executor.mode == target
            assert agent.console.mode == target
            assert agent.plan_mode == (target == "plan")
            # choose_mode 仅 manual 可见；write_file 仅 plan 隐藏
            names = _schema_names(agent)
            assert ("choose_mode" in names) == (target == "manual")
            assert ("write_file" in names) == (target != "plan")

    def test_invalid_mode_raises(self, tmp_path):
        agent = _make_agent(tmp_path)
        with pytest.raises(ValueError):
            agent.set_mode("yolo")
        assert agent.mode == "manual"  # 未变

    def test_auto_approve_restored_across_plan(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent.tool_executor.auto_approve = True  # 模拟 -y
        agent.set_mode("plan")
        assert agent.tool_executor.auto_approve is False  # 强制关闭
        agent.set_mode("auto")
        assert agent.tool_executor.auto_approve is True  # 原样还原

    def test_manual_does_not_touch_auto_approve(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent.tool_executor.auto_approve = True
        agent.set_mode("manual")
        # manual 不动 auto_approve（闸门层忽略它）
        assert agent.tool_executor.auto_approve is True

    def test_set_plan_mode_compat_wrapper(self, tmp_path):
        agent = _make_agent(tmp_path)
        assert agent.mode == "manual"
        agent.set_plan_mode(True)
        assert agent.mode == "plan"
        agent.set_plan_mode(False)
        assert agent.mode == "manual"  # 还原进入前的模式（非 auto）

    def test_set_plan_mode_false_is_noop_outside_plan(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent.set_mode("auto")
        agent.set_plan_mode(False)  # 当前非 plan → 空操作
        assert agent.mode == "auto"

    def test_reenter_manual_resets_choice_latch(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent.mode_choice_offered = True
        agent.set_mode("auto")
        assert agent.mode_choice_offered is True  # 非 manual 切换不复位
        agent.set_mode("manual")
        assert agent.mode_choice_offered is False  # 主动回切 manual → 复位


# ── 3. manual 闸门 ───────────────────────────────────────────────


class TestManualGate:
    @pytest.mark.asyncio
    async def test_ask_tool_prompts_despite_auto_approve(self):
        console = FakeAskConsole(approve=True)
        ex = _executor(console, mode="manual", auto_approve=True)
        spy = SpyAskTool()
        pc = await ex.prepare("spy_write", spy, '{"file_path": "a.py"}', "t1")
        assert pc.pre_result is None and pc.approved
        assert len(console.calls) == 1  # -y 也必弹窗

    @pytest.mark.asyncio
    async def test_stored_allow_rule_bypassed(self):
        console = FakeAskConsole(approve=True)
        ex = _executor(console, mode="manual", auto_approve=True)
        ex._rules.add_allow("spy_write(a.py)")  # 精确匹配的规则
        pc = await ex.prepare("spy_write", SpyAskTool(), '{"file_path": "a.py"}', "t2")
        assert pc.pre_result is None
        assert len(console.calls) == 1  # 存储规则被绕过

    @pytest.mark.asyncio
    async def test_whitelist_bypassed(self):
        from openx.tools.shell_tools import ShellTool
        console = FakeAskConsole(approve=True)
        ex = _executor(console, mode="manual")
        shell = ShellTool("/tmp", allowed_commands=["echo"])
        pc = await ex.prepare("shell", shell, '{"command": "echo hi"}', "t3")
        assert pc.pre_result is None
        assert len(console.calls) == 1  # 白名单被绕过

    @pytest.mark.asyncio
    async def test_can_remember_false_and_rule_never_persisted(self):
        console = FakeAskConsole(approve=True, remember=True)  # 故意返回"记住"
        ex = _executor(console, mode="manual")
        await ex.prepare("spy_write", SpyAskTool(), '{"file_path": "a.py"}', "t4")
        assert console.calls[0]["can_remember"] is False
        assert ex._rules.check("spy_write", "a.py") is None  # 绝不落盘

    @pytest.mark.asyncio
    async def test_allow_tool_runs_without_prompt(self):
        console = FakeAskConsole()
        ex = _executor(console, mode="manual")
        spy = SpyAllowTool()
        pc = await ex.prepare("spy_read", spy, '{}', "t5")
        assert pc.pre_result is None and pc.approved
        assert console.calls == []  # 只读免弹
        r = await ex.execute_prepared(pc)
        assert r.success and spy.calls == 1

    @pytest.mark.asyncio
    async def test_stored_deny_still_blocks(self):
        console = FakeAskConsole()
        ex = _executor(console, mode="manual")
        ex._rules.add_deny("spy_write(a.py)")
        pc = await ex.prepare("spy_write", SpyAskTool(), '{"file_path": "a.py"}', "t6")
        assert pc.pre_result is not None and "deny rule" in pc.pre_result.error
        assert console.calls == []  # 否决先于弹窗

    @pytest.mark.asyncio
    async def test_deny_in_manual_never_executes(self):
        console = FakeAskConsole(approve=False)  # 用户拒绝
        ex = _executor(console, mode="manual")
        spy = SpyAskTool()
        pc = await ex.prepare("spy_write", spy, '{"file_path": "a.py"}', "t7")
        r = await ex.execute_prepared(pc)
        assert not r.success and "denied by user" in r.error
        assert spy.calls == 0


# ── 4. 高危闸门（is_high_risk）───────────────────────────────────


class TestHighRiskGate:
    def test_shell_is_high_risk_by_pattern(self):
        from openx.tools.shell_tools import ShellTool
        sh = ShellTool("/tmp", dangerous_patterns=["rm -rf", "sudo"])
        assert sh.is_high_risk({"command": "rm -rf /"}) is True
        assert sh.is_high_risk({"command": "sudo reboot"}) is True
        assert sh.is_high_risk({"command": "echo hi"}) is False
        assert sh.is_high_risk({}) is False

    def test_base_tool_default_not_high_risk(self):
        assert SpyAskTool().is_high_risk({"file_path": "a"}) is False

    @pytest.mark.asyncio
    async def test_high_risk_prompts_despite_everything(self):
        """auto_approve + 存储规则 + 白名单三重豁免都不得跳过弹窗。"""
        console = FakeAskConsole(approve=True, remember=True)
        ex = _executor(console, mode="auto", auto_approve=True)
        ex._rules.add_allow("spy_high_risk(a.py)")  # 已记住的放行规则
        pc = await ex.prepare(
            "spy_high_risk", SpyHighRiskTool(), '{"file_path": "a.py"}', "h1")
        assert pc.pre_result is None and pc.approved
        assert len(console.calls) == 1

    @pytest.mark.asyncio
    async def test_remembered_high_risk_still_prompts_next_time(self):
        console = FakeAskConsole(approve=True, remember=True)
        ex = _executor(console, mode="auto")
        for i in range(2):
            pc = await ex.prepare(
                "spy_high_risk", SpyHighRiskTool(), '{"file_path": "a.py"}', f"h2-{i}")
            assert pc.pre_result is None
        # 第一次记住落盘了规则（auto 允许），第二次仍弹窗
        assert ex._rules.check("spy_high_risk", "a.py") == PermissionLevel.ALLOW
        assert len(console.calls) == 2

    @pytest.mark.asyncio
    async def test_high_risk_denied_never_executes(self):
        console = FakeAskConsole(approve=False)
        ex = _executor(console, mode="auto", auto_approve=True)
        spy = SpyHighRiskTool()
        pc = await ex.prepare(
            "spy_high_risk", spy, '{"file_path": "a.py"}', "h3")
        r = await ex.execute_prepared(pc)
        assert not r.success and "denied by user" in r.error
        assert spy.calls == 0

    @pytest.mark.asyncio
    async def test_non_dangerous_whitelisted_still_skips_prompt(self):
        """回归：auto 下非危险白名单命令照旧免询问。"""
        from openx.tools.shell_tools import ShellTool
        console = FakeAskConsole()
        ex = _executor(console, mode="auto")
        shell = ShellTool("/tmp", allowed_commands=["echo"])
        pc = await ex.prepare("shell", shell, '{"command": "echo hi"}', "h4")
        assert pc.pre_result is None and pc.approved
        assert console.calls == []


# ── 5. choose_mode 工具 ──────────────────────────────────────────


class TestChooseModeTool:
    def test_registered_top_level_and_manual_only_schema(self, tmp_path):
        agent = _make_agent(tmp_path)
        assert "choose_mode" in agent.tools
        assert "choose_mode" in _schema_names(agent)  # manual 可见
        agent.set_mode("auto")
        assert "choose_mode" not in _schema_names(agent)  # auto 隐藏
        agent.set_mode("plan")
        assert "choose_mode" not in _schema_names(agent)

    @pytest.mark.asyncio
    async def test_executor_backstop_outside_manual(self):
        console = FakeAskConsole()
        ex = _executor(console, mode="auto")
        agent_stub = type("A", (), {"mode": "auto", "mode_choice_offered": False})()
        tool = __import__(
            "openx.tools.mode_tools", fromlist=["ChooseModeTool"]
        ).ChooseModeTool(agent_stub, FakeQuestionConsole())
        pc = await ex.prepare("choose_mode", tool, '{}', "c0")
        assert pc.pre_result is not None and "manual mode" in pc.pre_result.error

    @pytest.mark.asyncio
    async def test_auto_choice_switches_mode(self, tmp_path):
        agent = _make_agent(tmp_path)
        console = FakeQuestionConsole(answer="Auto")
        agent.tools["choose_mode"]._console = console  # 工具持有构造时的 console
        r = await agent.tools["choose_mode"].execute(summary="edit foo.py")
        assert r.success and "AUTO" in r.output
        assert agent.mode == "auto"
        assert agent.mode_choice_offered is True
        assert len(console.questions) == 1

    @pytest.mark.asyncio
    async def test_plan_choice_switches_mode(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent.tools["choose_mode"]._console = FakeQuestionConsole(answer="Plan")
        r = await agent.tools["choose_mode"].execute()
        assert agent.mode == "plan" and "exit_plan_mode" in r.output

    @pytest.mark.asyncio
    async def test_stay_manual_keeps_mode_and_latches(self, tmp_path):
        agent = _make_agent(tmp_path)
        console = FakeQuestionConsole(answer="Stay in manual")
        agent.tools["choose_mode"]._console = console
        r = await agent.tools["choose_mode"].execute()
        assert agent.mode == "manual" and "again" in r.output
        # 第二次调用：不再弹窗（防重复闩）
        r2 = await agent.tools["choose_mode"].execute()
        assert len(console.questions) == 1 and "already asked" in r2.output

    @pytest.mark.asyncio
    async def test_other_free_text_safe_default_manual(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent.tools["choose_mode"]._console = FakeQuestionConsole(answer="just do it")
        r = await agent.tools["choose_mode"].execute()
        assert agent.mode == "manual"  # 安全默认
        assert "just do it" in r.output  # 回显用户原话


# ── 6. 系统提示指令注入 ──────────────────────────────────────────


class TestInstructionsInjection:
    def test_manual_instructions_in_prompt_by_default(self, tmp_path):
        agent = _make_agent(tmp_path)
        assert MANUAL_MODE_INSTRUCTIONS in agent._system_prompt
        assert PLAN_MODE_INSTRUCTIONS not in agent._system_prompt

    def test_plan_instructions_after_switch(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent.set_mode("plan")
        assert PLAN_MODE_INSTRUCTIONS in agent._system_prompt
        assert MANUAL_MODE_INSTRUCTIONS not in agent._system_prompt

    def test_auto_has_neither_mode_block(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent.set_mode("auto")
        assert MANUAL_MODE_INSTRUCTIONS not in agent._system_prompt
        assert PLAN_MODE_INSTRUCTIONS not in agent._system_prompt

    def test_child_prompt_has_no_mode_block(self, tmp_path):
        from openx.agent import OpenXAgent
        parent = _make_agent(tmp_path)
        config = OpenXConfig()
        config.workspace = str(tmp_path)
        config.api_key = "sk-test"
        config.api_base = "https://example.com/v1"
        config.model = "test-model"
        child = OpenXAgent(config, parent=parent)
        assert MANUAL_MODE_INSTRUCTIONS not in child._system_prompt
        assert PLAN_MODE_INSTRUCTIONS not in child._system_prompt


# ── 7. headless 强制 auto ────────────────────────────────────────


class TestSingleShotMode:
    @pytest.mark.asyncio
    async def test_single_shot_forces_auto(self, tmp_path, monkeypatch):
        from openx.cli.single_shot import run_single_shot

        class MinimalConsole:
            mode = "manual"

            def show_startup_single_shot(self, *a, **k):
                pass

            def print_streaming_start(self, *a, **k):
                pass

            def print_streaming_done(self, *a, **k):
                pass

            def print_assistant(self, *a, **k):
                pass

            def print_error(self, *a, **k):
                pass

            def print_warning(self, *a, **k):
                pass

        agent = _make_agent(tmp_path, responses=[("done", None)])
        assert agent.mode == "manual"
        await run_single_shot(agent, MinimalConsole(), "hi")
        assert agent.mode == "auto"  # headless 强制 auto（弹窗会阻塞 stdin）
