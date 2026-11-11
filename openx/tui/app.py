"""Textual app —— 全屏界面主体（转录 / 状态行 / 输入框 / 按键）。

组成（对应行内实现的四段式布局，但由框架托管）：

    ┌ transcript (VerticalScroll) ─ 会话流，块可折叠、可鼠标点开
    │ status line                  转帧 + Thinking…/Answering… + 耗时
    │ Input                        输入行（Enter 提交；多行/历史是 Stage 3）
    └ Footer                       键位提示

三条缝在这里落地：

1. **输出缝**：``Console._console`` 被换成 :class:`openx.tui.sink.TuiSink`，
   它只往 :class:`~openx.tui.sink.SinkQueue` 投递；本 app 用 ``set_interval``
   定时排空并 mount widget。**同一时间只有一个消费者改 widget**（沿用
   ``ui/resize.py`` 的写屏纪律，只是消费者从 Live 线程换成 app 线程）。
2. **输入缝**：:meth:`ask` 是 ``print_user_prompt`` 的 TUI 实现——聚焦输入
   框后 ``await`` 一个队列；返回 ``None`` 表示 EOF/退出，与行内语义一致
   （``interactive.py`` 见到 ``None`` 就干净退出，不改一行）。
3. **会话缝**：:func:`run_tui` 让现有 REPL 协程与 app 在**同一个 asyncio
   loop** 里并发跑，任一先结束就收掉另一个——不需要 ``call_from_thread``。

流式回合走第二条投递通道：:class:`openx.tui.stream.TuiStream` 与行内
``StreamingService`` 同面，但把 chunk 归一成事件投进 ``stream_queue``；
:class:`openx.tui.view.StreamView` 在同一个排空节拍里把事件落成
thinking / 答案 / 工具块。**两路都只由 :meth:`_drain` 消费**——这是本文件
唯一的 widget 写入口（除首帧 compose）。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, ClassVar, Callable, Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Input, Static

from ..ui._style import ACCENT
from .blocks import StatusLine, ThinkingBlock
from .sink import SinkQueue, TuiSink
from .stream import Note, StreamStart, TuiStream, TurnAborted, TurnEnd
from .view import IDLE_PLACEHOLDER, StreamView


class RichEntry(Static):
    """转录里的一条 Rich 内容，**保留源对象**。

    两用：① 测试/诊断可读（Textual 的 ``Static.visual`` 对 Markdown 之类
    已不保留原 renderable）；② 宽度变化时按新宽重渲染——行内实现靠 Rich
    自己重排，全屏实现必须自己兜这一层。

    ``update()`` 必须一起换掉 ``source``：``Static.update`` 只替换内部
    renderable，不保留传入对象——不同步就会出现"控件显示新内容、``source``
    还是旧对象"的静默不一致（流式答案的每个 token 都走这条路）。
    """

    def __init__(self, source: Any, **kwargs: Any) -> None:
        self.source = source
        super().__init__(source, classes="entry", **kwargs)

    def update(self, source: Any = None, **kwargs: Any) -> None:
        if source is not None:
            self.source = source
        super().update(source, **kwargs)


class OpenXTui(App[None]):
    """OpenX 的全屏终端界面：转录 + 状态行 + 输入框 + 键位。"""

    CSS = """
    Screen {
        layout: vertical;
    }
    #transcript {
        height: 1fr;
        padding: 0 1;
        scrollbar-size-vertical: 1;
    }
    #spinner {
        height: 1;
        padding: 0 1;
    }
    #prompt {
        height: 3;
    }
    Footer {
        height: 1;
    }
    .thinking-block > .block-body {
        color: grey;
        padding: 0 1;
    }
    .tool-block > .block-body {
        color: grey;
        padding: 0 1;
    }
    .entry {
        padding: 0 0;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+q", "quit_tui", "退出", show=True, priority=True),
        Binding("ctrl+r", "toggle_thinking", "thinking", show=True),
        Binding("ctrl+t", "toggle_all", "全部折叠/展开", show=True),
        # priority：焦点在 Input 里也要能打断（否则 Esc 被输入框吞掉，
        # 流式期只剩 Ctrl+C 一条路——与行内"esc to interrupt"承诺不符）
        Binding("escape", "interrupt", "打断", show=False, priority=True),
    ]

    #: 排空间隔（秒）：sink/stream 投递 → widget 挂载的合并窗口（≈30Hz）
    DRAIN_INTERVAL = 1 / 30
    #: 状态行刷新间隔（转帧，与行内 Live 的 5Hz 同量级）
    SPINNER_INTERVAL = 0.12

    def __init__(self, *, demo: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.sink_queue = SinkQueue()
        #: 流式回合的事件通道（``TuiStream`` 投递，``StreamView`` 消费）
        self.stream_queue = SinkQueue()
        self._demo = demo
        self._mounted_ready = asyncio.Event()
        self._input_q: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self._busy = False
        self._sink: Optional[TuiSink] = None
        #: 视图管线：**只**在 :meth:`_drain` 里被调用（单一消费者）
        self._view = StreamView(self)
        #: ``Console`` 引用（排队输入要写回 ``console._input_queue``）
        self._openx_console: Optional[Any] = None
        #: 流式回合进行中（决定 Enter 是"排队"还是"提交给 ask()"）
        self._stream_active = False
        #: Esc 打断回调（``TuiStream.start()`` 接线，收尾解绑）
        self._interrupt_cb: Optional[Callable[[], None]] = None

    # ── 生命周期 ────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="transcript")
        yield StatusLine(id="spinner")
        yield Input(placeholder=IDLE_PLACEHOLDER, id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        self._mounted_ready.set()
        self.set_interval(self.DRAIN_INTERVAL, self._drain)
        self.set_interval(self.SPINNER_INTERVAL, self._tick_spinner)
        self.query_one("#prompt", Input).focus()
        if self._demo:
            self.run_worker(self._demo_script(), name="demo", exit_on_error=False)

    def on_resize(self) -> None:
        self._sync_sink_size()

    # ── 输出缝 ──────────────────────────────────────────────────

    def write(self, renderable: Any) -> None:
        """挂载一个 Rich renderable，返回包装它的控件（可后续 ``update``）。

        调用方是 :class:`~openx.tui.view.StreamView`（只在 :meth:`_drain`
        里被驱动）或演示脚本；两者都在 app 主循环线程，不构成并发写。
        """
        entry = RichEntry(renderable)
        if not self._closed:
            self._transcript.mount(entry)
            self._scroll_to_end()
        return entry

    def write_widget(self, widget: Any) -> None:
        """挂载一个 widget（块类：ThinkingBlock / ToolBlock …）。"""
        if self._closed:
            return
        self._transcript.mount(widget)
        self._scroll_to_end()

    @property
    def _transcript(self) -> VerticalScroll:
        return self.query_one("#transcript", VerticalScroll)

    def _drain(self) -> None:
        """排空两路投递 → widget。**本 app 唯一的常规 widget 写入口。**

        先流式事件（块/答案/状态），后普通输出——同一拍里两者的时间顺序
        就是各自入队顺序，跨通道的相对顺序不保证，但流式回合期间另一路
        基本只有零散打印，肉眼不可辨。

        ``_closed`` 早退：定时器可能在 app 卸载之后仍被投递一次（控件树
        已拆），此时任何 ``query``/``mount`` 都会抛。
        """
        if self._closed:
            return
        events = self.stream_queue.drain()
        for event in events:
            if isinstance(event, StreamStart):
                self._stream_active = True
            elif isinstance(event, (TurnEnd, TurnAborted)):
                self._stream_active = False
            self._view.apply(event)
        items = self.sink_queue.drain()
        if items:
            for renderable in items:
                self._transcript.mount(RichEntry(renderable))
            self._scroll_to_end()

    def _scroll_to_end(self) -> None:
        """贴底跟随；用户回看历史时不抢滚动位置。"""
        try:
            scroller = self._transcript
            offset = getattr(scroller, "scroll_offset", None)
            max_y = getattr(scroller, "max_scroll_y", 0)
            if offset is not None and max_y - offset.y > 2:
                return  # 用户已上翻：不打断
            scroller.scroll_end(animate=False)
        except Exception:
            pass  # 布局未就绪（首帧）→ 下一拍再贴底

    def _sync_sink_size(self) -> None:
        sink = getattr(self, "_sink", None)
        if sink is None:
            return
        with contextlib.suppress(Exception):
            sink.update_size(self.size.width, self.size.height)

    def install_sink(self, sink: TuiSink) -> None:
        """由 ``Console.attach_tui`` 回填（resize 时同步量测宽度用）。"""
        self._sink = sink
        self._sync_sink_size()

    def attach_console(self, console: Any) -> None:
        """由 ``Console.attach_tui`` 回填：排队输入要写回 ``_input_queue``。

        复用行内主循环已有的排队通道（``run_interactive`` 在每次询问前
        先弹 ``console._input_queue``），排队消息因此无需新机制。
        """
        self._openx_console = console

    def set_interrupt(self, callback: Optional[Callable[[], None]]) -> None:
        """接线/解绑 Esc 打断回调（``TuiStream.start()`` / 收尾时调用）。"""
        self._interrupt_cb = callback

    def set_placeholder(self, text: str) -> None:
        """输入框占位文案（流式期提示"回车 = 排队"）。"""
        with contextlib.suppress(Exception):
            self.query_one("#prompt", Input).placeholder = text

    # ── 状态行 ──────────────────────────────────────────────────

    def set_busy(self, label: Optional[str]) -> None:
        """``label`` 非空 → 显示转帧 + 耗时；None → 清空。"""
        self._busy = label is not None
        if label is None:
            self.query_one("#spinner", StatusLine).set_busy(None)
        else:
            self.query_one("#spinner", StatusLine).set_busy(label,
                                                            started_at=time.monotonic())

    def _tick_spinner(self) -> None:
        # shutdown 竞态：定时器可能在控件树拆除后再被投递一次（回合没跑完
        # 就退出时最典型）。转帧是纯装饰，绝不能让它把退出路径炸掉。
        if not self._busy or self._closed:
            return
        with contextlib.suppress(Exception):
            self.query_one("#spinner", StatusLine)._render_frame()

    # ── 输入缝 ──────────────────────────────────────────────────

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter：流式期排队等本回合结束，空闲期交给 ``ask()``。

        与行内同语义（流式期回车 = "该回合结束后立即发送"）：行内由
        ``InputCapture`` 把行塞进 ``console._input_queue``，此处直接复用
        那个通道——主循环的下一拍就会取走它。
        """
        text = event.value
        event.input.value = ""
        if not text.strip():
            return
        if self._stream_active and self._openx_console is not None:
            self._openx_console._input_queue.append(text)
            self.stream_queue.push(Note(f"↳ queued: {text}"))
            return
        self._input_q.put_nowait(text)

    async def ask(self) -> Optional[str]:
        """``print_user_prompt`` 的 TUI 实现：聚焦输入框并等一条输入。

        返回 ``None`` 表示界面已关闭（用户 Ctrl+Q / app 退出）——
        与行内路径的 EOF 语义一致，``interactive.py`` 据此干净退出。
        """
        await self._mounted_ready.wait()
        self.query_one("#prompt", Input).focus()
        value = await self._input_q.get()
        return value

    # ── 交互 ────────────────────────────────────────────────────

    def action_quit_tui(self) -> None:
        # 唤醒在等的 ask()（None = EOF），再退出 app。
        self._input_q.put_nowait(None)  # type: ignore[arg-type]
        self._close()

    def action_interrupt(self) -> None:
        """Esc：交给接线好的打断回调（``TuiStream.interrupt``）。

        空闲期按 Esc 什么都不该发生——回调在回合收尾时已解绑，这里不写
        任何提示行（占位提示是阶段 0 的产物，早已退休）。
        """
        callback = self._interrupt_cb
        if callback is None:
            return
        with contextlib.suppress(Exception):
            callback()

    def action_toggle_thinking(self) -> None:
        blocks = list(self.query(ThinkingBlock))
        for block in blocks:
            block.collapsed = not block.collapsed

    def action_toggle_all(self) -> None:
        from textual.widgets import Collapsible

        blocks = list(self.query(Collapsible))
        if not blocks:
            return
        # 任一折叠 → 全展开；否则全折叠（一次键切换两种全量态）
        expand = any(b.collapsed for b in blocks)
        for block in blocks:
            block.collapsed = not expand

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self.exit()

    # ── 离线演示脚本 ────────────────────────────────────────────

    async def _demo_script(self) -> None:
        """离线演示：用**真实事件**喂一遍 thinking / 工具块 / 答案。

        ``TuiStream`` 与生产路径共用同一条管线——演示即管线的自检，不另
        造一条仿造路径（否则演示跑通证明不了任何事）。
        """
        from ..agent import ToolResultEvent, ToolStartEvent
        from ..kernel.reasoning.provider import StreamReasoning

        stream = TuiStream(self._console_stub(), app=self)
        exit_phrase = Text(
            "❯ 解释一下这个仓库的流式渲染是怎么做的", style=f"bold {ACCENT}"
        )
        self.write(exit_phrase)

        stream.start()
        for chunk in (
            "用户要的是「thinking 默认折叠、点开可看」。\n",
            "行内实现里这件事很贵：scrollback 不可重写，\n",
            "所以只能靠未固化窗口 + Ctrl+R + 就地重印。\n",
            "换到 widget 树之后，折叠只是 Collapsible 的一个参数。",
        ):
            stream.feed(StreamReasoning(chunk))
            await asyncio.sleep(0.18)
        stream.feed(ToolStartEvent(
            name="read_file", arguments='{"file_path": "services/streaming.py"}',
            call_id="demo-1"))
        await asyncio.sleep(0.3)
        stream.feed(ToolResultEvent(
            name="read_file", call_id="demo-1",
            output="1 2200 行 / 逐行固化水印 / _ResizeAwareLive"))
        stream.feed(ToolStartEvent(
            name="grep", arguments='{"pattern": "_commit_watermark"}',
            call_id="demo-2"))
        await asyncio.sleep(0.3)
        stream.feed(ToolResultEvent(
            name="grep", call_id="demo-2",
            output="openx/services/streaming.py:1370\n"
                   "openx/services/streaming.py:1576"))
        for chunk in (
            "## 结论\n\n",
            "全屏实现把「折叠」从**状态机**降级成**控件属性**：\n\n",
            "```python\nCollapsible(body, title='Thought for 3s', collapsed=True)\n```\n\n",
            "剩下的成本都在迁移，而不在这个功能本身。",
        ):
            stream.feed(chunk)
            await asyncio.sleep(0.2)
        stream.done()
        # 演示"点开看"的等价状态：默认折叠的 thinking 块在此展开
        thinking = list(self.query(ThinkingBlock))
        if thinking:
            thinking[0].collapsed = False

    def _console_stub(self) -> Any:
        """演示用的最小 console 存根（``TuiStream`` 只用到 ``_input_queue``）。"""
        from types import SimpleNamespace

        return SimpleNamespace(_input_queue=[])


async def run_tui(agent: Any, console: Any) -> None:
    """``--tui`` 入口：app 与既有 REPL 协程在同一 loop 内并发。

    任一先结束就收掉另一个：REPL 里 ``/quit`` 或 EOF → 关 app；用户在 TUI
    里 Ctrl+Q → ``ask()`` 拿到 ``None`` → REPL 走既有的 EOF 退出路径。
    """
    from ..app.cli.interactive import run_interactive

    app = OpenXTui()
    console.attach_tui(app)
    app_task = asyncio.create_task(app.run_async(), name="openx-tui")
    repl_task = asyncio.create_task(run_interactive(agent, console),
                                    name="openx-repl")
    try:
        await asyncio.wait({app_task, repl_task},
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (repl_task, app_task):
            if not task.done():
                with contextlib.suppress(Exception):
                    app.exit()
                task.cancel()
        await asyncio.gather(repl_task, app_task, return_exceptions=True)
        console.detach_tui()


__all__ = ["OpenXTui", "run_tui"]
