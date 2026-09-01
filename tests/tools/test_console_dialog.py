"""console_dialog：async 优先对话框通道 + 三个交互工具的接线。

serve（web）下 console 有 ``*_async`` 协程变体（ServeConsole）→ 走交互通道
（广播 + 等待客户端应答）；TUI / 测试桩只有同步版 → 回退同步调用。本测试
锁定两条通道的分发与三个工具（ask_user / choose_mode / exit_plan_mode）
确实经该通道提问——而不是直连同步版（那会在 serve 下拿到保守默认）。
"""

from __future__ import annotations

import pytest

from openx.tools.ask_user_tool import AskUserTool
from openx.tools.console_dialog import ask_user_question, confirm_plan
from openx.tools.mode_tools import ChooseModeTool
from openx.tools.plan_tools import ExitPlanModeTool


class SyncConsole:
    """无 async 变体（TUI / 测试桩）→ 回退同步版。"""

    def __init__(self, answer="A"):
        self.answer = answer
        self.calls: list[tuple] = []

    def ask_user_question(self, question, options, multi_select=False):
        self.calls.append(("ask", question, options, multi_select))
        return [self.answer]

    def confirm_plan(self) -> bool:
        self.calls.append(("plan",))
        return True


class AsyncConsole:
    """有协程变体（ServeConsole）→ 走交互通道，绝不碰同步版。"""

    class _Raw:
        def print(self, *a, **k):
            pass

    def __init__(self):
        self.raw = self._Raw()
        self.calls: list[tuple] = []

    async def ask_user_question_async(
        self, question, options, multi_select=False
    ):
        self.calls.append(("ask", question, options, multi_select))
        return options[0]["label"]

    async def confirm_plan_async(self, plan: str = "") -> bool:
        self.calls.append(("plan", plan))
        return True

    def ask_user_question(self, *a, **k):
        raise AssertionError("async console must not use sync ask path")

    def confirm_plan(self, *a, **k):
        raise AssertionError("async console must not use sync plan path")


# ── 通道分发 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_console_falls_back_to_sync():
    c = SyncConsole(answer="B")
    assert await ask_user_question(c, "q", [{"label": "A"}, {"label": "B"}]) == ["B"]
    assert await confirm_plan(c) is True
    assert [x[0] for x in c.calls] == ["ask", "plan"]


@pytest.mark.asyncio
async def test_async_console_prefers_coroutine():
    c = AsyncConsole()
    assert await ask_user_question(
        c, "q", [{"label": "A", "description": "d"}, {"label": "B"}],
        multi_select=True,
    ) == "A"
    assert await confirm_plan(c, "# plan") is True
    assert c.calls[0][3] is True      # multi_select 透传
    assert c.calls[1][1] == "# plan"  # plan 透传


# ── 工具接线 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ask_user_tool_uses_async_channel():
    c = AsyncConsole()
    r = await AskUserTool(c).execute(
        question="q", options=[{"label": "A"}, {"label": "B"}]
    )
    assert r.success and "A" in r.output
    assert c.calls[0][0] == "ask"


@pytest.mark.asyncio
async def test_choose_mode_tool_uses_async_channel():
    class _Agent:
        mode = "manual"
        mode_choice_offered = False

        def set_mode(self, m):
            self.mode = m

    c = AsyncConsole()  # 首项 "Auto" → set_mode("auto")
    agent = _Agent()
    r = await ChooseModeTool(agent, c).execute(summary="edit foo.py")
    assert agent.mode == "auto" and r.success
    assert c.calls[0][0] == "ask"


@pytest.mark.asyncio
async def test_exit_plan_mode_tool_uses_async_channel():
    class _Executor:
        auto_approve = False

    class _Agent:
        mode = "plan"
        tool_executor = _Executor()

        def set_mode(self, m):
            self.mode = m

    agent = _Agent()
    c = AsyncConsole()
    r = await ExitPlanModeTool(agent, c).execute(plan="# plan")
    assert r.success and "approved" in r.output
    assert agent.mode == "auto" and agent.tool_executor.auto_approve is True
    assert c.calls[0][0] == "plan"
