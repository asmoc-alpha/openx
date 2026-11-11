"""阶段 2：流式 widget 管线（``TuiStream`` + ``StreamView``）+ Esc 打断。

覆盖的三件事：

1. **事件 → 控件**：答案 token / 推理 token / 工具起止事件分别落到
   ``RichEntry``(Markdown) / ``ThinkingBlock`` / ``ToolBlock``，收尾写
   ``✻ … for Ns``——全部经 app 的排空节拍，而不是直写控件（单一消费者）。
2. **Esc 打断**：单回合闩、只取消登记的消费子任务、``esc_interrupted``
   供交互层决定吞取消还是上抛；收尾解绑后误按不再波及下一回合。
3. **接线**：``_stream_response`` 在 ``console._tui`` 就位时选 ``TuiStream``
   （而不是行内 ``StreamingService``），且**不起** InputCapture（cbreak
   会与 Textual 抢 stdin）。

喂给管线的是**真实事件对象**（``ToolStartEvent`` / ``ToolResultEvent`` /
``StreamReasoning``），不是本模块自定义的私有结构——否则测的就不是生产路径。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from rich.markdown import Markdown

pytest.importorskip("textual", reason="--tui 依赖 textual")

from openx.agent import ToolResultEvent, ToolStartEvent                 # noqa: E402
from openx.config import OpenXConfig                                    # noqa: E402
from openx.kernel.reasoning.provider import StreamReasoning             # noqa: E402
from openx.tui.app import OpenXTui, RichEntry                           # noqa: E402
from openx.tui.blocks import ThinkingBlock, ToolBlock                   # noqa: E402
from openx.tui.stream import TuiStream, arg_summary                     # noqa: E402
from openx.tui.view import BUSY_PLACEHOLDER, IDLE_PLACEHOLDER           # noqa: E402
from openx.ui.console import Console                                    # noqa: E402


# ── 夹具 ──────────────────────────────────────────────────────────


def _console() -> Console:
    return Console(config=OpenXConfig(workspace="/tmp"))


async def _settle(pilot, ticks: int = 2) -> None:
    """排空 app 定时器（30Hz）——事件入队后需要至少两拍才落到控件。"""
    for _ in range(ticks):
        await pilot.pause()


def _entries(app):
    return list(app.query(RichEntry))


def _answers(app):
    """纯答案 entry（排掉收尾行 ``✻ …`` 与排队提示行 ``↳ …``）。"""
    return [
        e for e in _entries(app)
        if "✻" not in _plain(e) and not _plain(e).strip().startswith("↳")
    ]


def _plain(entry) -> str:
    """entry 的纯文本：Markdown 取源 markup，其余取 ``str``（Text → plain）。"""
    source = entry.source
    return source.markup if isinstance(source, Markdown) else str(source)


async def _run_turn(app, pilot, chunks, *, end=True):
    """把 chunks 依次喂给一个 ``TuiStream`` 并排空。"""
    stream = TuiStream(_console(), app=app)
    stream.start()
    for chunk in chunks:
        stream.feed(chunk)
    await _settle(pilot)
    if end:
        stream.done()
        await _settle(pilot)
    return stream


# ── 1. 事件 → 控件 ────────────────────────────────────────────────


class TestAnswerStreaming:
    async def test_tokens_accumulate_into_one_markdown_entry(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            await _run_turn(app, pilot, ["**bold** ", "and ", "`code`"])
            entries = _answers(app)
            assert len(entries) == 1, "同一文本段必须是**一条** entry"
            assert "bold" in _plain(entries[0]) and "code" in _plain(entries[0])

    async def test_tool_block_splits_the_text_segment(self):
        """工具块之后的新 token 必须**新开**一条 entry——否则工具块会被
        埋在旧 Markdown 中间（行内 _segments 交替语义的等价实现）。"""
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            await _run_turn(app, pilot, [
                "before", ToolStartEvent(name="grep", arguments='{"pattern":"x"}'),
                ToolResultEvent(name="grep", output="hit"), "after",
            ])
            entries = _answers(app)
            assert len(entries) == 2
            assert "before" in _plain(entries[0])
            assert "after" in _plain(entries[1])

    async def test_busy_label_switches_then_clears(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            stream = TuiStream(_console(), app=app)
            stream.start()
            await _settle(pilot)
            line = app.query_one("#spinner")
            assert "Thinking" in line.visual.plain

            stream.feed("hello")
            await _settle(pilot)
            assert "Answering" in line.visual.plain

            stream.done()
            await _settle(pilot)
            assert line.visual.plain.strip() == ""

    async def test_placeholder_hints_queueing_during_stream(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt")
            assert prompt.placeholder == IDLE_PLACEHOLDER
            stream = TuiStream(_console(), app=app)
            stream.start()
            await _settle(pilot)
            assert prompt.placeholder == BUSY_PLACEHOLDER
            stream.done()
            await _settle(pilot)
            assert prompt.placeholder == IDLE_PLACEHOLDER


class TestReasoning:
    async def test_reasoning_block_is_collapsed_and_feeds(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            await _run_turn(app, pilot, [
                StreamReasoning("let me "), StreamReasoning("think"), "done",
            ])
            blocks = list(app.query(ThinkingBlock))
            assert len(blocks) == 1
            assert blocks[0].text == "let me think"
            # 首个正文 token 到达 → 推理阶段冻结（标题带静态耗时）
            assert blocks[0].frozen is True
            assert "Thought for" in blocks[0].title

    async def test_no_reasoning_block_without_reasoning(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            await _run_turn(app, pilot, ["just an answer"])
            assert not list(app.query(ThinkingBlock)), "无推理内容不该多出控件"


class TestToolBlocks:
    async def test_start_then_result_sets_state_and_body(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            stream = TuiStream(_console(), app=app)
            stream.start()
            stream.feed(ToolStartEvent(
                name="read_file", arguments='{"file_path": "a.py"}', call_id="c1"))
            await _settle(pilot)
            block = list(app.query(ToolBlock))[0]
            assert block.status == "running"
            assert "a.py" in block.title          # 参数摘要进了标题

            stream.feed(ToolResultEvent(
                name="read_file", output="line 1\nline 2", call_id="c1"))
            await _settle(pilot)
            assert block.status == "done"
            assert block.body.visual.plain.startswith("line 1")

    async def test_error_result_marks_block(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            await _run_turn(app, pilot, [
                ToolStartEvent(name="bash", arguments='{"command":"exit 1"}',
                               call_id="c1"),
                ToolResultEvent(name="bash", output="boom", is_error=True,
                                call_id="c1"),
            ])
            block = list(app.query(ToolBlock))[0]
            assert block.status == "error"
            assert "boom" in block.body.visual.plain

    async def test_parallel_same_name_paired_by_call_id(self):
        """并行同名工具：两个 ``read_file`` 各自的结果必须落到自己块里。

        行内实现按"同名首个 running"配对（并行同名是它的已知弱点）；
        事件带 ``call_id`` 时这里能精确配对——这条测试钉住那个差别。
        """
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            stream = TuiStream(_console(), app=app)
            stream.start()
            stream.feed(ToolStartEvent(name="read_file", arguments='{"file_path":"a"}',
                                       call_id="A"))
            stream.feed(ToolStartEvent(name="read_file", arguments='{"file_path":"b"}',
                                       call_id="B"))
            await _settle(pilot)
            # 结果**乱序**回传：B 先到
            stream.feed(ToolResultEvent(name="read_file", output="content-b",
                                        call_id="B"))
            stream.feed(ToolResultEvent(name="read_file", output="content-a",
                                        call_id="A"))
            await _settle(pilot)

            blocks = list(app.query(ToolBlock))
            assert len(blocks) == 2
            by_title = {b.title: b for b in blocks}
            a_block = next(b for t, b in by_title.items() if "(a)" in t)
            b_block = next(b for t, b in by_title.items() if "(b)" in t)
            assert a_block.body.visual.plain == "content-a"
            assert b_block.body.visual.plain == "content-b"

    async def test_result_without_call_id_falls_back_to_fifo(self):
        """旧 provider 不带 call_id → 退回同名首个 running（行内语义）。"""
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            await _run_turn(app, pilot, [
                ToolStartEvent(name="grep", arguments='{"pattern":"a"}'),
                ToolResultEvent(name="grep", output="first"),
            ])
            blocks = list(app.query(ToolBlock))
            assert len(blocks) == 1 and blocks[0].status == "done"
            assert blocks[0].body.visual.plain == "first"

    async def test_unmatched_result_is_not_dropped(self):
        """没有起始事件的结果也要落成一条已完成块——结果绝不丢。"""
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            await _run_turn(app, pilot, [
                ToolResultEvent(name="orphan", output="orphan output"),
            ])
            blocks = list(app.query(ToolBlock))
            assert len(blocks) == 1 and blocks[0].status == "done"

    async def test_task_result_echoes_with_description(self):
        """``task`` 起始不落块（舰队面板的活），结果回显带描述。"""
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            stream = TuiStream(_console(), app=app)
            stream.start()
            stream.feed(ToolStartEvent(
                name="task", arguments='{"description": "audit auth"}', call_id="t1"))
            await _settle(pilot)
            assert not list(app.query(ToolBlock)), "task 起始不该落块"

            stream.feed(ToolResultEvent(name="task", output="report", call_id="t1"))
            await _settle(pilot)
            blocks = list(app.query(ToolBlock))
            assert len(blocks) == 1 and blocks[0].status == "done"
            assert "audit auth" in blocks[0].title


class TestTurnEnd:
    async def test_done_writes_cook_line_and_returns_elapsed(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            stream = TuiStream(_console(), app=app)
            stream.start()
            stream.feed("hi")
            await _settle(pilot)
            elapsed = stream.done()
            await _settle(pilot)

            assert elapsed >= 0.0
            texts = [_plain(e) for e in _entries(app)]
            assert any("for" in t and "✻" in t for t in texts), texts

    async def test_empty_turn_has_no_cook_line(self):
        """空回合（什么都没产出就结束）不留收尾行——"Cooked for 0s" 是噪音。"""
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            stream = TuiStream(_console(), app=app)
            stream.start()
            await _settle(pilot)
            stream.done()
            await _settle(pilot)
            assert not _entries(app)

    async def test_cancel_writes_interrupted_line(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            stream = TuiStream(_console(), app=app)
            stream.start()
            stream.feed("partial")
            await _settle(pilot)
            stream.cancel()
            await _settle(pilot)
            texts = " ".join(_plain(e) for e in _entries(app))
            assert "Interrupted" in texts
            # 部分回答保真：已有内容不因打断消失
            assert "partial" in texts

    async def test_next_turn_starts_clean(self):
        """两回合之间视图状态必须归零（折叠态、块引用、收尾判断）。"""
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            await _run_turn(app, pilot, ["first turn"])
            await _run_turn(app, pilot, ["second turn"])
            entries = _answers(app)
            assert len(entries) == 2
            assert "first turn" in _plain(entries[0])
            assert "second turn" in _plain(entries[1])


# ── 2. Esc 打断 ───────────────────────────────────────────────────


class TestEscInterrupt:
    async def test_esc_cancels_registered_task_once(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            stream = TuiStream(_console(), app=app)
            stream.start()
            task = asyncio.create_task(asyncio.sleep(30))
            stream.set_cancel_target(task)
            await _settle(pilot)

            await pilot.press("escape")     # 经 app 的 key binding 真走一遍
            await _settle(pilot)
            assert stream.esc_interrupted is True
            with pytest.raises(asyncio.CancelledError):
                await task

            # 闩：连按不再触发（第二次不会抛、也不会重复通知）
            await pilot.press("escape")
            await _settle(pilot)

    async def test_esc_without_target_is_safe(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            stream = TuiStream(_console(), app=app)
            stream.start()
            await _settle(pilot)
            await pilot.press("escape")     # 未登记消费任务
            await _settle(pilot)
            assert stream.esc_interrupted is True

    async def test_esc_after_done_does_not_touch_next_turn(self):
        """收尾解绑 Esc：回合结束后的误按绝不波及下一回合（行内同款纪律）。"""
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            first = TuiStream(_console(), app=app)
            first.start()
            first.done()
            await _settle(pilot)
            assert app._interrupt_cb is None

            second = TuiStream(_console(), app=app)
            second.start()
            task = asyncio.create_task(asyncio.sleep(30))
            second.set_cancel_target(task)
            await _settle(pilot)
            await pilot.press("escape")
            await _settle(pilot)
            with pytest.raises(asyncio.CancelledError):
                await task
            assert first.esc_interrupted is False, "上一回合的闩不该被复用"

    async def test_esc_idle_is_noop(self):
        """没有回合在跑时按 Esc：什么都不发生（不写提示行、不抛）。"""
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("escape")
            await _settle(pilot)
            assert not _entries(app)

    async def test_interrupt_notice_fires_and_swallows_errors(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            stream = TuiStream(_console(), app=app)
            stream.start()

            def _boom():
                raise RuntimeError("notice failed")

            stream.set_interrupt_notice(_boom)
            await _settle(pilot)
            await pilot.press("escape")     # 通知炸了也不能影响打断
            await _settle(pilot)
            assert stream.esc_interrupted is True


# ── 3. 排队输入 ───────────────────────────────────────────────────


class TestQueuedInput:
    async def test_enter_during_stream_queues_for_next_turn(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            console = _console()
            console.attach_tui(app)
            stream = TuiStream(console, app=app)
            stream.start()
            await _settle(pilot)

            await pilot.press("n", "e", "x", "t", "enter")
            await _settle(pilot)
            assert console._input_queue == ["next"]
            # 排队行在转录里回显（否则用户看不到自己排了什么）
            assert any("queued" in _plain(e).lower() for e in _entries(app))

    async def test_enter_when_idle_goes_to_ask(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            console = _console()
            console.attach_tui(app)
            task = asyncio.create_task(app.ask())
            await pilot.pause()
            await pilot.press("h", "i", "enter")
            assert await asyncio.wait_for(task, 2) == "hi"
            assert console._input_queue == []


# ── 4. 接线：_stream_response 选中 TuiStream ──────────────────────


class _FakeAgent:
    """流式假 agent：吐 token 后挂起（等被打断）。"""

    def __init__(self, chunks):
        self.config = SimpleNamespace(stream=True)
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.tool_executor = SimpleNamespace(on_prompt_start=None,
                                             on_prompt_end=None)
        self.todos: list = []
        self.fleet = None
        self.tools: dict = {}
        self.interrupted = False
        self._chunks = chunks

    async def stream_run(self, content):
        for chunk in self._chunks:
            yield chunk
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.interrupted = True
            raise


class TestWiring:
    def test_make_display_picks_tui_stream_when_attached(self):
        from openx.app.cli.interactive import _make_display

        console = _console()
        app = OpenXTui()
        console.attach_tui(app)
        try:
            display = _make_display(_FakeAgent([]), console)
            assert isinstance(display, TuiStream)
        finally:
            console.detach_tui()

    def test_make_display_falls_back_to_inline(self):
        from openx.app.cli.interactive import _make_display
        from openx.services.streaming import StreamingService

        assert isinstance(_make_display(_FakeAgent([]), _console()),
                          StreamingService)

    async def test_stream_response_feeds_widgets_without_capture(self):
        """端到端：``_stream_response`` 走 TUI 管线——转录有内容，且
        **没有**起行内 cbreak 捕获（那会与 Textual 抢 stdin）。"""
        from openx.app.cli.interactive import _stream_response

        agent = _FakeAgent(["hello ", "world"])
        console = _console()
        app = OpenXTui()
        console.attach_tui(app)
        try:
            async with app.run_test(size=(100, 30)) as pilot:
                task = asyncio.create_task(
                    _stream_response(agent, console, "hi"))
                await pilot.pause(0.4)
                assert console._input_capture is None
                app.action_interrupt()          # Esc 等价（不依赖按键路径）
                await asyncio.wait_for(task, 5)
                await _settle(pilot)
                # 必须在 app 上下文内取值：退出 run_test 后控件树已卸载
                texts = " ".join(_plain(e) for e in _entries(app))
        finally:
            console.detach_tui()

        assert agent.interrupted, "消费子任务应被取消"
        assert "hello" in texts and "world" in texts
        assert "Interrupted" in texts


# ── 5. 小工具函数 ─────────────────────────────────────────────────


class TestArgSummary:
    def test_prefers_known_keys(self):
        assert arg_summary('{"file_path": "a/b.py"}') == "a/b.py"
        assert arg_summary('{"pattern": "_commit"}') == "_commit"
        assert arg_summary('{"description": "audit auth"}') == "audit auth"

    def test_malformed_json_falls_back_to_raw(self):
        assert arg_summary("not json") == "not json"

    def test_empty_and_long(self):
        assert arg_summary("") == ""
        assert len(arg_summary("x" * 200)) <= 48

    def test_non_dict_json(self):
        assert arg_summary("[1, 2]") == "[1, 2]"
