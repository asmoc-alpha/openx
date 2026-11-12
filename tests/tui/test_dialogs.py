"""阶段 5：弹窗迁移成 ModalScreen + 未迁移入口的干净报错。

三条承重行为：

1. **三个阻塞弹窗不再碰终端**：权限审批 / ``ask_user`` 提问 / 计划批准各压
   一块 ``ModalScreen``，取消一律 fail-closed（拒绝 / 不执行 / 未作答）。
2. **接缝是真的接上了**：真 ``Console`` + ``attach_tui`` 下，
   ``console.ask_permission`` 与工具侧的 ``tools.console_dialog`` 入口都自动
   走模态；``detach_tui`` 后自动退回行内同步实现（迁移期逃生门）。
3. **未迁移入口干净报错**：slash 命令里的同步选择器 / 自由输入框在 TUI 下
   抛 ``TuiInputNeeded``，由 ``handle_slash_command`` / ``_stream_response``
   转成一条警告——会话继续，界面不被 raw-mode 写花。
"""

from __future__ import annotations

import asyncio
import io
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("textual", reason="--tui 依赖 textual")

from openx.config import OpenXConfig                                    # noqa: E402
from openx.tui.app import OpenXTui                                      # noqa: E402
from openx.tui.modals import (                                           # noqa: E402
    ChoiceScreen,
    TextScreen,
    ask_user_options,
    permission_body,
    plan_body,
)
from openx.ui._components.dialogs import TuiInputNeeded                  # noqa: E402
from openx.ui._components.prompt import paste_aware_input                # noqa: E402
from openx.ui.console import Console                                     # noqa: E402


def _console() -> Console:
    return Console(config=OpenXConfig(workspace="/tmp"))


def _modal(app):
    """当前压在最上层的屏幕（没有 → None）。"""
    stack = app.screen_stack
    return stack[-1] if len(stack) > 1 else None


def _render(renderable, width: int = 100) -> str:
    """Rich renderable → 纯文本（``Group`` 的 ``str()`` 只有 repr）。"""
    from rich.console import Console as RichConsole

    buf = io.StringIO()
    RichConsole(file=buf, width=width, force_terminal=False).print(renderable)
    return buf.getvalue()


async def _settle(pilot, ticks: int = 2) -> None:
    for _ in range(ticks):
        await pilot.pause()


async def _answer(app, pilot, coro, *, keys=()):
    """跑一个弹窗调用、按 keys 作答、返回结果。"""
    task = asyncio.create_task(coro)
    await _settle(pilot)
    for key in keys:
        await pilot.press(key)
    result = await asyncio.wait_for(task, 5)
    await _settle(pilot)
    return result


# ── 1. 权限审批 ───────────────────────────────────────────────────


class TestPermissionModal:
    async def test_enter_approves_once(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 34)) as pilot:
            result = await _answer(
                app, pilot, app.ask_permission("bash", "run tests"),
                keys=("enter",),
            )
            assert result == (True, False)

    async def test_dont_ask_again_option_persists(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 34)) as pilot:
            result = await _answer(
                app, pilot,
                app.ask_permission("bash", "run", args_summary="ls -la"),
                keys=("down", "enter"),
            )
            assert result == (True, True)

    async def test_remember_option_sits_above_reject(self):
        """选项次序与行内实现逐字一致：允许一次 / 不再询问 / 拒绝。

        次序是安全语义的一部分——"不再询问"夹在同意与拒绝之间，误按 ↓ 一次
        落到的是**更窄**的授权，而不是把拒绝按成永久放行。
        """
        app = OpenXTui()
        async with app.run_test(size=(100, 34)) as pilot:
            task = asyncio.create_task(
                app.ask_permission("bash", "run", args_summary="ls -la"))
            await _settle(pilot)
            labels = [label for label, _ in _modal(app)._options]
            assert labels == [
                "Yes, allow once",
                "Yes, and don't ask again for this",
                "No, don't run",
            ]
            await pilot.press("escape")
            await asyncio.wait_for(task, 5)

    async def test_esc_is_fail_closed(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 34)) as pilot:
            assert await _answer(
                app, pilot, app.ask_permission("bash", "rm"), keys=("escape",)
            ) == (False, False)

    async def test_manual_mode_hides_remember_option(self):
        """``can_remember=False`` 时绝不产生持久化规则——只给两项。"""
        app = OpenXTui()
        async with app.run_test(size=(100, 34)) as pilot:
            task = asyncio.create_task(app.ask_permission(
                "bash", "run", args_summary="ls", can_remember=False))
            await _settle(pilot)
            screen = _modal(app)
            assert isinstance(screen, ChoiceScreen)
            assert len(screen._options) == 2
            await pilot.press("escape")
            await asyncio.wait_for(task, 5)

    async def test_no_args_summary_hides_remember_option(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 34)) as pilot:
            task = asyncio.create_task(app.ask_permission("bash", "run"))
            await _settle(pilot)
            assert len(_modal(app)._options) == 2
            await pilot.press("escape")
            await asyncio.wait_for(task, 5)

    async def test_body_renders_the_diff(self):
        """审批要看的是"改什么"——diff 预览必须在正文里。"""
        body = permission_body(
            "edit a file", "",
            ("a.py", "x = 1\n", "x = 2\n"), "monokai",
        )
        rendered = _render(body)
        assert "a.py" in rendered and "+1" in rendered and "-1" in rendered

    async def test_body_without_diff_uses_details(self):
        assert "boom" in _render(permission_body("r", "boom", None, "monokai"))

    async def test_status_line_says_waiting_then_restores(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 34)) as pilot:
            app.set_busy("Answering…")
            task = asyncio.create_task(app.ask_permission("bash", "run"))
            await _settle(pilot)
            assert "Waiting" in app.query_one("#spinner").visual.plain
            await pilot.press("escape")
            await asyncio.wait_for(task, 5)
            await _settle(pilot)
            assert "Answering" in app.query_one("#spinner").visual.plain


# ── 2. ask_user ───────────────────────────────────────────────────


class TestAskUserModal:
    _OPTS = [{"label": "Alpha", "description": "first"},
             {"label": "Beta"}]

    async def test_single_select_returns_label(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 34)) as pilot:
            result = await _answer(
                app, pilot, app.ask_question("Which?", self._OPTS),
                keys=("down", "enter"),
            )
            assert result == "Beta"

    async def test_single_select_cancelled_is_empty_string(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 34)) as pilot:
            assert await _answer(
                app, pilot, app.ask_question("Which?", self._OPTS),
                keys=("escape",),
            ) == ""

    async def test_other_opens_text_screen(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 34)) as pilot:
            task = asyncio.create_task(app.ask_question("Which?", self._OPTS))
            await _settle(pilot)
            # 光标移到最后的 "Other" 项
            await pilot.press("down", "down", "enter")
            await _settle(pilot)
            assert isinstance(_modal(app), TextScreen)
            await pilot.press("m", "i", "n", "e", "enter")
            assert await asyncio.wait_for(task, 5) == "mine"

    async def test_multi_select_toggles_with_space(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 34)) as pilot:
            task = asyncio.create_task(
                app.ask_question("Which?", self._OPTS, multi_select=True))
            await _settle(pilot)
            await pilot.press("space", "down", "space", "enter")
            assert await asyncio.wait_for(task, 5) == ["Alpha", "Beta"]

    async def test_multi_select_cancelled_is_empty_list(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 34)) as pilot:
            assert await _answer(
                app, pilot,
                app.ask_question("Which?", self._OPTS, multi_select=True),
                keys=("escape",),
            ) == []

    async def test_option_labels_carry_description(self):
        pairs = ask_user_options(self._OPTS, False)
        assert pairs[0][0] == "Alpha — first"
        assert pairs[0][1] == "Alpha"       # 返回值是纯 label
        assert pairs[-1][1] == "__other__"


# ── 3. 计划批准 ───────────────────────────────────────────────────


class TestPlanModal:
    async def test_approve(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 34)) as pilot:
            assert await _answer(
                app, pilot, app.ask_plan("# Plan\n- step"), keys=("enter",)
            ) is True

    async def test_reject(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 34)) as pilot:
            assert await _answer(
                app, pilot, app.ask_plan("# Plan"), keys=("down", "enter")
            ) is False

    async def test_esc_rejects(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 34)) as pilot:
            assert await _answer(
                app, pilot, app.ask_plan("# Plan"), keys=("escape",)
            ) is False

    def test_long_plan_is_truncated(self):
        body = plan_body("\n".join(f"line {i}" for i in range(100)))
        # 截断提示在源里，完整计划留在转录（body 只是预览）
        assert "more lines" in body.markup
        assert "line 41" not in body.markup


# ── 4. 接缝：真 Console 自动走模态 ────────────────────────────────


class TestConsoleSeam:
    async def test_ask_permission_uses_modal(self):
        console = _console()
        app = OpenXTui()
        console.attach_tui(app)
        try:
            async with app.run_test(size=(100, 34)) as pilot:
                task = asyncio.create_task(
                    console.ask_permission("bash", "run tests"))
                await _settle(pilot)
                assert isinstance(_modal(app), ChoiceScreen)
                await pilot.press("enter")
                assert await asyncio.wait_for(task, 5) == (True, False)
        finally:
            console.detach_tui()

    async def test_tool_side_ask_user_routes_to_modal(self):
        """``tools.console_dialog`` 的 async 优先入口自动选中模态——工具侧
        代码零改动是这条迁移的核心承诺，故钉死。"""
        from openx.tools.console_dialog import ask_user_question

        console = _console()
        app = OpenXTui()
        console.attach_tui(app)
        try:
            async with app.run_test(size=(100, 34)) as pilot:
                task = asyncio.create_task(
                    ask_user_question(console, "Pick",
                                      [{"label": "A"}, {"label": "B"}]))
                await _settle(pilot)
                assert isinstance(_modal(app), ChoiceScreen)
                await pilot.press("down", "enter")
                assert await asyncio.wait_for(task, 5) == "B"
        finally:
            console.detach_tui()

    async def test_tool_side_confirm_plan_routes_to_modal(self):
        from openx.tools.console_dialog import confirm_plan

        console = _console()
        app = OpenXTui()
        console.attach_tui(app)
        try:
            async with app.run_test(size=(100, 34)) as pilot:
                task = asyncio.create_task(confirm_plan(console, "# plan"))
                await _settle(pilot)
                assert isinstance(_modal(app), ChoiceScreen)
                await pilot.press("enter")
                assert await asyncio.wait_for(task, 5) is True
        finally:
            console.detach_tui()

    async def test_detached_console_falls_back_to_inline(self):
        """detach 之后两个 async 变体原样委托同步方法（行内行为不变）。"""
        console = _console()
        seen = {}
        console.ask_user_question = lambda q, o, m=False: "inline-answer"
        console.confirm_plan = lambda: True
        seen["async"] = await console.ask_user_question_async("q", [])
        seen["plan"] = await console.confirm_plan_async("# plan")
        assert seen == {"async": "inline-answer", "plan": True}

    def test_closing_app_resolves_pending_dialog_with_default(self):
        """app 被关掉时挂起的弹窗必须按取消默认值 resolve——否则工具栈
        永远挂着（死锁，不是退出）。"""

        async def _run():
            app = OpenXTui()
            async with app.run_test(size=(100, 34)) as pilot:
                task = asyncio.create_task(app.ask_permission("bash", "run"))
                await _settle(pilot)
                app._close()
                await _settle(pilot, 4)
                return await asyncio.wait_for(task, 5)

        assert asyncio.run(_run()) == (False, False)

    def test_closed_flag_does_not_shadow_message_pump(self):
        """自己的"已收摊"标记**不能**叫 ``_closed``。

        ``MessagePump._closed`` 为真时 ``post_message`` 静默丢弃消息，于是
        ``_close()`` 后紧跟的 ``exit()`` 投递的 ``ExitApp`` 会被吃掉，app
        的 ``_process_messages`` 挂在 ``_get_message()`` 上永不返回。这条
        断言是这个 bug 的唯一快速探针（其余表现是"测试挂死"，不是失败）。
        """
        app = OpenXTui()
        app._close()
        assert app._tui_closed is True
        assert app._closed is False, "别劫持 MessagePump._closed"

    def test_closing_stops_the_app(self):
        """``_close()`` 之后 app 必须**自己**退出来（有界等待：挂死即失败）。"""

        async def _run():
            app = OpenXTui()
            async with app.run_test(size=(100, 34)) as pilot:
                app._close()
                await _settle(pilot)

        asyncio.run(asyncio.wait_for(_run(), 10))

    async def test_dialog_after_close_returns_default_immediately(self):
        """收摊之后再来问一句 → 立刻拿到取消值，绝不压屏、绝不挂着。"""
        app = OpenXTui()
        async with app.run_test(size=(100, 34)) as pilot:
            app._close()
            await _settle(pilot)
            assert await asyncio.wait_for(
                app.ask_permission("bash", "run"), 2) == (False, False)
            assert await asyncio.wait_for(app.ask_plan("# p"), 2) is False
            assert await asyncio.wait_for(app.ask_text("q"), 2) == ""


# ── 5. 未迁移入口：干净报错 ───────────────────────────────────────


class TestUnmigratedGuards:
    def test_interactive_select_raises_under_tui(self):
        console = _console()
        console._tui = object()          # 只判存在性，不需真 app
        with pytest.raises(TuiInputNeeded) as exc:
            console._interactive_select([("Yes", True)])
        assert "--tui" in str(exc.value)

    def test_paste_aware_input_raises_under_tui(self):
        console = _console()
        console._tui = object()
        with pytest.raises(TuiInputNeeded):
            paste_aware_input(console, "▸ ")

    def test_inline_console_still_selects_normally(self, monkeypatch):
        """无 ``_tui``：守卫不生效，行内选择器照旧（脚本化替身）。

        ``_raw_select`` 只在 tty 上启用（否则退数字菜单），故这里把 stdin
        换成一个只会回答 ``isatty()`` 的假 tty——替身 ``_raw_select`` 不
        读键，永远不会真去碰它。
        """
        console = _console()
        console._raw_select = lambda opts, idx, prompt, **kw: opts[1][1]
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
        assert console._interactive_select([("Yes", True), ("No", False)],
                                           cancel_chars="") is False

    async def test_slash_command_swallows_and_warns(self):
        """斜杠命令里撞到守卫 → 打警告 + 返回 True（会话继续）。"""
        from openx.app.cli.commands import handle_slash_command, register

        warnings: list = []

        @register("tui-guard-probe", description="test only")
        async def _probe(agent, console, args):
            raise TuiInputNeeded("not available in --tui yet")

        console = _console()
        console.print_warning = warnings.append
        result = await handle_slash_command(
            "tui-guard-probe", SimpleNamespace(), console, [])
        assert result is True
        assert warnings and "--tui" in warnings[0]

    async def test_stream_response_warns_instead_of_erroring(self):
        from openx.app.cli.interactive import _stream_response

        class _Boom:
            config = SimpleNamespace(stream=True)
            total_input_tokens = 0
            total_output_tokens = 0
            todos: list = []
            fleet = None
            tools: dict = {}
            tool_executor = SimpleNamespace(
                on_prompt_start=None, on_prompt_end=None)

            async def stream_run(self, content):
                raise TuiInputNeeded("not available in --tui yet")
                yield  # pragma: no cover

        console = _console()
        app = OpenXTui()
        console.attach_tui(app)
        warnings: list = []
        errors: list = []
        console.print_warning = warnings.append
        console.print_error = errors.append
        try:
            async with app.run_test(size=(100, 34)) as pilot:
                await _stream_response(_Boom(), console, "hi")
                await _settle(pilot)
        finally:
            console.detach_tui()
        assert warnings and "--tui" in warnings[0]
        assert not errors, "未迁移入口不该被报成 Error"


# ── 6. 控件级细节 ─────────────────────────────────────────────────


class TestScreenDetails:
    async def test_choice_screen_hint_mentions_escape(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 34)) as pilot:
            task = asyncio.create_task(app.ask_plan("p"))
            await _settle(pilot)
            hint = _modal(app).query_one("#modal-hint").visual.plain
            assert "Esc" in hint
            await pilot.press("escape")
            await asyncio.wait_for(task, 5)

    async def test_text_screen_empty_input_is_none(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 34)) as pilot:
            task = asyncio.create_task(app.ask_text("Your answer"))
            await _settle(pilot)
            assert isinstance(_modal(app), TextScreen)
            await pilot.press("enter")
            assert await asyncio.wait_for(task, 5) == ""

    async def test_modal_is_removed_after_answering(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 34)) as pilot:
            await _answer(app, pilot, app.ask_plan("p"), keys=("enter",))
            await _settle(pilot)
            assert _modal(app) is None
            assert not list(app.query(ChoiceScreen))
