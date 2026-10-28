"""``/auto-approve`` 修复回归：manual 模式下 ON 同步退到 auto，开关真正生效。

背景（S1c 之后暴露）：serve 默认 manual 模式，Guard station ⑤ 把所有 ASK
级工具 ``forced``（压过 auto_approve——manual 的逐项弹窗契约）。``/auto-approve``
此前只切 ``executor.auto_approve``，不动模式 → serve 里开了照样逐项弹。
修复：ON 且 mode==manual 时同步 ``set_mode("auto")``。

经真实 ToolExecutor + Guard 栈断言：manual 下被 cap 的 ASK 工具切换前
逐项弹、切换后直放。
运行：``python -m pytest tests/tools/test_auto_approve_toggle.py -q``
"""

from __future__ import annotations

import asyncio

import pytest

from openx.app.cli import commands
from openx.kernel.audit.guard import Guard
from openx.permissions import Permission, PermissionLevel, PermissionRules
from openx.services.tool_executor import ToolExecutor
from openx.tools.base import Tool, ToolResult


class _NullConsole:
    """无人可问的 console：prompter 走测试注入，不经这里。"""

    async def ask_permission(self, *a, **k):  # pragma: no cover
        raise AssertionError("prompter should be intercepted by the test spy")

    def print_info(self, m): pass
    def print_success(self, m): pass
    def print_warning(self, m): pass


class _AskTool(Tool):
    """自声明 ALLOW 的工具（S1c cap 后会变 ASK——这里直接用 ASK 等价态）。"""

    name = "askme"
    description = "asks"
    parameters = {"type": "object", "properties": {}}

    @property
    def permission(self):
        return Permission(level=PermissionLevel.ASK, reason="because")

    async def execute(self, **kw):
        return ToolResult(output="ran")


class _FakeAgent:
    """鸭子 agent：_mode/set_mode 与真实 agent 同形（set_mode 翻两边）。"""

    def __init__(self, executor, mode="manual"):
        self.config = type("C", (), {"auto_approve": False})()
        self.tool_executor = executor
        self._mode = mode

    def set_mode(self, mode):
        self._mode = mode
        self.tool_executor.mode = mode


@pytest.fixture
def harness():
    prompts: list[str] = []

    async def spy_prompter(call, perm):
        prompts.append(call.tool_name)
        return (True, False)

    ex = ToolExecutor(_NullConsole(), auto_approve=False, mode="manual")
    ex._guard = Guard(
        emit=lambda *a, **k: None,
        rules=lambda: PermissionRules.load(),
        hooks=None,
        prompter=spy_prompter,
        mode=lambda: ex.mode,
        auto_approve=lambda: ex.auto_approve,
    )
    tool = _AskTool()
    agent = _FakeAgent(ex, mode="manual")
    return prompts, ex, tool, agent


async def _gate(ex, tool, prompts):
    before = len(prompts)
    v = await ex._guard.gate(type("C", (), {
        "tool": tool, "tool_name": tool.name, "args": {},
        "args_summary": ""})())
    return v, len(prompts) > before


async def test_toggle_on_in_manual_mode_flips_to_auto(harness):
    prompts, ex, tool, agent = harness
    # manual + auto_approve off：ASK 工具逐项弹
    v, prompted = await _gate(ex, tool, prompts)
    assert prompted and v.verdict.name in ("ASK", "ALLOW_ONCE")

    # /auto-approve ON：模式同步退到 auto
    assert await commands._cmd_auto_approve(agent, _NullConsole(), []) is True
    assert agent.config.auto_approve is True
    assert ex.auto_approve is True
    assert agent._mode == "auto" and ex.mode == "auto"

    # 同一 ASK 工具：直放，不再弹
    v, prompted = await _gate(ex, tool, prompts)
    assert not prompted
    assert v.approved and v.verdict.name == "ALLOW"


async def test_toggle_off_keeps_mode_and_resumes_prompting(harness):
    prompts, ex, tool, agent = harness
    await commands._cmd_auto_approve(agent, _NullConsole(), [])  # ON（manual→auto）
    assert agent._mode == "auto"
    await commands._cmd_auto_approve(agent, _NullConsole(), [])  # OFF
    assert agent.config.auto_approve is False and ex.auto_approve is False
    assert agent._mode == "auto"  # OFF 不动模式：auto 下 ASK 本就走询问
    _v, prompted = await _gate(ex, tool, prompts)
    assert prompted  # 恢复逐项确认


async def test_toggle_on_in_auto_mode_no_mode_flip(harness):
    prompts, ex, tool, agent = harness
    agent._mode = "auto"
    ex.mode = "auto"
    assert await commands._cmd_auto_approve(agent, _NullConsole(), []) is True
    assert agent._mode == "auto"  # 本就是 auto：无模式变化提示之扰
    _v, prompted = await _gate(ex, tool, prompts)
    assert not prompted
