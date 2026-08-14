"""Phase 1 bugfix 回归测试。

覆盖：--no-stream 接线、allowed_commands 预批准、validate_args 生效、
tool.execute 异常兜底、工作区边界前缀绕过、compact 按轮裁剪、__all__ 星号导入。

运行：``python -m pytest tests/test_bugfixes.py -q``
"""

from __future__ import annotations

import pytest

from openx.config import OpenXConfig
from openx.core.history import ConversationHistory
from openx.llm import StreamDone
from openx.permissions import PermissionRules
from openx.services.tool_executor import ToolExecutor
from openx.tools.base import Tool, ToolResult
from openx.tools.file_tools import EditFileTool, WriteFileTool
from openx.tools.shell_tools import ShellTool


# ── Fakes ────────────────────────────────────────────────────────

class FakeLLM:
    """可脚本化的假 LLM：额外记录 chat() 收到的 stream 参数。"""

    def __init__(self, responses):
        self.responses = list(responses)  # list of (content, tool_calls)
        self.call_count = 0
        self.stream_calls: list[bool] = []

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
        self.stream_calls.append(stream)
        content, tool_calls = self.responses[self.call_count]
        self.call_count += 1
        resp = {"role": "assistant", "content": content}
        if tool_calls:
            resp["tool_calls"] = tool_calls
        return resp


class FakeConsole:
    """Duck-typed console：记录权限询问，返回预设的批准结果。"""

    def __init__(self, approve: bool = True):
        self.approve = approve
        self.asked: list[str] = []

    async def ask_permission(self, tool_name, reason, details="", args_summary="",
                       can_remember=True, diff=None):
        self.asked.append(tool_name)
        return (self.approve, False)


def _make_agent(tmp_path, monkeypatch, responses):
    """构造一个挂载 FakeLLM 的 OpenXAgent（绕过真实 API）。"""
    from openx.agent import OpenXAgent
    config = OpenXConfig()
    config.workspace = str(tmp_path)
    config.api_key = "sk-test"
    config.api_base = "https://example.com/v1"
    config.model = "test-model"
    agent = OpenXAgent(config)
    agent.llm = FakeLLM(responses)
    agent.tool_executor._rules = PermissionRules()  # 忽略真实 settings.json，保证确定性
    return agent


# ── Bug 1: --no-stream 接线 ─────────────────────────────────────

class TestNoStreamWiring:
    """config.stream 传到 LLMClient.chat(stream=...)。"""

    def test_config_stream_default_true(self):
        assert OpenXConfig().stream is True

    @pytest.mark.asyncio
    async def test_agent_run_passes_stream_false(self, tmp_path, monkeypatch):
        agent = _make_agent(tmp_path, monkeypatch, [("final answer", None)])
        agent.config.stream = False
        out = await agent.run("hello")
        assert out == "final answer"
        assert agent.llm.stream_calls == [False]

    @pytest.mark.asyncio
    async def test_agent_run_default_stream_true(self, tmp_path, monkeypatch):
        agent = _make_agent(tmp_path, monkeypatch, [("final answer", None)])
        await agent.run("hello")
        assert agent.llm.stream_calls == [True]


# ── Bug 2: allowed_commands 预批准 ──────────────────────────────

class TestAllowedCommands:
    """白名单命令跳过 ASK 询问；未列出的命令仍会询问（非硬拦截）。"""

    def test_auto_allowed_first_token(self, tmp_path):
        tool = ShellTool(str(tmp_path), allowed_commands=["pytest"])
        assert tool.auto_allowed({"command": "pytest -q"}) is True

    def test_auto_allowed_skips_leading_env_var(self, tmp_path):
        tool = ShellTool(str(tmp_path), allowed_commands=["pytest"])
        assert tool.auto_allowed({"command": "FOO=1 pytest -q"}) is True

    def test_unlisted_not_auto_allowed(self, tmp_path):
        tool = ShellTool(str(tmp_path), allowed_commands=["pytest"])
        assert tool.auto_allowed({"command": "rm foo"}) is False
        assert tool.auto_allowed({"command": ""}) is False
        # 引号不平衡等解析错误 → 保守返回 False，退回交互式确认
        assert tool.auto_allowed({"command": "pytest 'unbalanced"}) is False

    @pytest.mark.asyncio
    async def test_executor_skips_prompt_for_allowed(self, tmp_path):
        console = FakeConsole(approve=True)
        executor = ToolExecutor(console, auto_approve=False)
        executor._rules = PermissionRules()
        tool = ShellTool(str(tmp_path), allowed_commands=["echo"])
        result, approved = await executor.execute(
            "shell", tool, '{"command": "echo hi"}'
        )
        assert approved and result.success
        assert "hi" in result.output
        assert console.asked == []  # 白名单命令：未询问

    @pytest.mark.asyncio
    async def test_executor_prompts_for_unlisted(self, tmp_path):
        console = FakeConsole(approve=False)  # 用户拒绝
        executor = ToolExecutor(console, auto_approve=False)
        executor._rules = PermissionRules()
        tool = ShellTool(str(tmp_path), allowed_commands=["pytest"])
        result, approved = await executor.execute(
            "shell", tool, '{"command": "whoami"}'
        )
        assert not approved
        assert "denied" in result.error.lower()
        assert console.asked == ["shell"]  # 未列出：弹出询问


# ── Bug 3: validate_args 生效 ───────────────────────────────────

class TestValidateArgs:
    """参数校验失败 / 非 dict 参数 → 错误结果，且不执行工具。"""

    @staticmethod
    def _executor():
        executor = ToolExecutor(FakeConsole(), auto_approve=True)
        executor._rules = PermissionRules()
        return executor

    @pytest.mark.asyncio
    async def test_validation_error_string_blocks_execution(self):
        ran = {"n": 0}

        class _StrictTool(Tool):
            name = "strict"

            def validate_args(self, **kw):
                return "value must be positive"

            async def execute(self, **kw):
                ran["n"] += 1
                return ToolResult(output="ran")

        result, approved = await self._executor().execute(
            "strict", _StrictTool(), '{"x": -1}'
        )
        assert not approved
        assert result.error == "Invalid arguments: value must be positive"
        assert ran["n"] == 0

    @pytest.mark.asyncio
    async def test_non_dict_json_args_rejected(self):
        ran = {"n": 0}

        class _AnyTool(Tool):
            name = "anytool"

            async def execute(self, *a, **kw):
                ran["n"] += 1
                return ToolResult(output="ran")

        # "5" 解析成 int，不是合法的参数对象
        result, approved = await self._executor().execute("anytool", _AnyTool(), "5")
        assert not approved
        assert "Invalid arguments" in result.error
        assert ran["n"] == 0

    @pytest.mark.asyncio
    async def test_missing_required_kwarg_is_typeerror(self):
        class _TypedTool(Tool):
            name = "typed"

            def validate_args(self, required_arg, **kw):  # 缺 required_arg → TypeError
                return None

            async def execute(self, **kw):
                return ToolResult(output="ran")

        result, approved = await self._executor().execute("typed", _TypedTool(), "{}")
        assert not approved
        assert "Invalid arguments" in result.error
        assert "required_arg" in result.error


# ── Bug 4: tool.execute 异常兜底 ────────────────────────────────

class _BoomTool(Tool):
    """execute 必抛 RuntimeError 的工具。"""

    name = "boom"

    async def execute(self, **kw):
        raise RuntimeError("kaboom")


class TestToolExceptionGuard:
    """工具异常转成错误结果，整个对话轮不中断。"""

    @pytest.mark.asyncio
    async def test_executor_wraps_exception(self):
        executor = ToolExecutor(FakeConsole(), auto_approve=True)
        executor._rules = PermissionRules()
        result, _ = await executor.execute("boom", _BoomTool(), "{}")
        assert not result.success
        assert "RuntimeError" in result.error
        assert "kaboom" in result.error

    @pytest.mark.asyncio
    async def test_agent_turn_survives_tool_crash(self, tmp_path, monkeypatch):
        agent = _make_agent(tmp_path, monkeypatch, [
            (None, [{"id": "c1", "type": "function",
                     "function": {"name": "boom", "arguments": "{}"}}]),
            ("recovered fine", None),
        ])
        agent.tools["boom"] = _BoomTool()
        out = await agent.run("do something risky")
        assert out == "recovered fine"


# ── Bug 5: 工作区边界前缀绕过 ───────────────────────────────────

class TestWorkspaceBoundary:
    """同前缀兄弟目录（ws vs ws-evil）必须被拦下。"""

    @pytest.mark.asyncio
    async def test_write_sibling_prefix_dir_blocked(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        evil = tmp_path / "ws-evil"
        evil.mkdir()
        tool = WriteFileTool(str(ws))
        result = await tool.execute("../ws-evil/f.txt", "pwned")
        assert not result.success
        assert "outside workspace" in result.error.lower()
        assert not (evil / "f.txt").exists()  # 文件未被创建

    @pytest.mark.asyncio
    async def test_edit_sibling_prefix_dir_blocked(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        evil = tmp_path / "ws-evil"
        evil.mkdir()
        target = evil / "f.txt"
        target.write_text("original")
        tool = EditFileTool(str(ws))
        result = await tool.execute("../ws-evil/f.txt", "original", "pwned")
        assert not result.success
        assert "outside workspace" in result.error.lower()
        assert target.read_text() == "original"  # 文件未被修改


# ── Bug 6: compact 按轮裁剪 ─────────────────────────────────────

class _SummaryLLM:
    """compact() 只 await llm.chat(...) 的离线替身。"""

    async def chat(self, messages, tools=None, stream=True):
        return {"content": "summary of older turns"}


class TestCompactTurns:
    """keep_last 现在数“轮”（user 边界），不拆 tool_call/tool_result 对。"""

    @staticmethod
    def _turn(n: int) -> list[dict]:
        # 完整一轮：user / assistant(tool_calls) / tool / assistant 最终回复
        return [
            {"role": "user", "content": f"question {n}"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"call_{n}", "type": "function",
                 "function": {"name": "shell", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": f"call_{n}", "content": f"result {n}"},
            {"role": "assistant", "content": f"answer {n}"},
        ]

    @pytest.mark.asyncio
    async def test_keep_last_counts_turns(self):
        h = ConversationHistory()
        for n in (1, 2, 3):
            h.messages.extend(self._turn(n))
        summary = await h.compact(_SummaryLLM(), keep_last=2)
        assert "summary of older turns" in summary
        # 压缩后历史仍合法：validate() 通过、无孤立 tool 消息
        assert h.validate()
        user_contents = [m["content"] for m in h.messages if m["role"] == "user"]
        assert "question 1" not in user_contents  # 第 1 轮已被摘要
        assert "question 2" in user_contents and "question 3" in user_contents
        # 第 1 轮的 tool_call/tool_result 成对移除，绝不拆散
        tool_ids = [m["tool_call_id"] for m in h.messages if m["role"] == "tool"]
        assert tool_ids == ["call_2", "call_3"]
        assert h.messages[0]["content"].startswith("[Previous conversation summary]")

    @pytest.mark.asyncio
    async def test_too_few_turns_noop(self):
        h = ConversationHistory()
        h.messages.extend(self._turn(1))
        result = await h.compact(_SummaryLLM(), keep_last=2)
        assert "too short" in result
        assert len(h.messages) == 4  # 缓冲未变

    def test_validate_detects_orphan_tool_message(self):
        h = ConversationHistory()
        h.messages = [{"role": "tool", "tool_call_id": "nope", "content": "x"}]
        assert h.validate() is False
        h.messages = [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "shell", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ]
        assert h.validate() is True


# ── Bug 7: __all__ 星号导入 ─────────────────────────────────────

class TestStarExport:
    """from openx import * 可用且包含真实导出的名字。"""

    def test_star_import_includes_openx_config(self):
        import openx
        assert "OpenXConfig" in openx.__all__
        assert "OpenX" not in openx.__all__  # 旧的不存在符号已移除
        ns: dict = {}
        exec("from openx import *", ns)
        assert ns["OpenXConfig"] is OpenXConfig
        assert ns["SETTINGS_PATH"] is not None
