"""Textual app —— 阶段 0 的最小可运行骨架。

组成（对应行内实现的四段式布局，但由框架托管）：

    ┌ transcript (VerticalScroll) ─ 会话流，块可折叠、可鼠标点开
    │ status line                  转帧 + Thinking…/Answering… + 耗时
    │ Input                        输入行（Enter 提交；多行/历史是 Stage 3）
    └ Footer                       键位提示

三条缝在这里落地（迁移计划的 Stage 0/1 部分）：

1. **输出缝**：``Console._console`` 被换成 :class:`openx.tui.sink.TuiSink`，
   它只往 :class:`~openx.tui.sink.SinkQueue` 投递；本 app 用 ``set_interval``
   定时排空并 mount widget。**同一时间只有一个消费者改 widget**（沿用
   ``ui/resize.py`` 的写屏纪律，只是消费者从 Live 线程换成 app 线程）。
2. **输入缝**：:meth:`ask` 是 ``print_user_prompt`` 的 TUI 实现——聚焦输入
   框后 ``await`` 一个队列；返回 ``None`` 表示 EOF/退出，与行内语义一致
   （``interactive.py`` 见到 ``None`` 就干净退出，不改一行）。
3. **会话缝**：:func:`run_tui` 让现有 REPL 协程与 app 在**同一个 asyncio
   loop** 里并发跑，任一先结束就收掉另一个——不需要 ``call_from_thread``。

阶段 0 的限制（写在明处）：``--tui`` 强制非流式回合（``config.stream =
False``），因为行内 ``StreamingService`` 的 Rich ``Live`` 与 Textual 争同一
个终端。流式 widget 管线是 Stage 2 的交付物。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, ClassVar, Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Input, Static

from ..ui._style import ACCENT, DIM
from .blocks import StatusLine
from .sink import SinkQueue, TuiSink


class RichEntry(Static):
    """转录里的一条 Rich 内容，**保留源对象**。

    两用：① 测试/诊断可读（Textual 的 ``Static.visual`` 对 Markdown 之类
    已不保留原 renderable）；② Stage 1 起宽度变化时按新宽重渲染——行内
    实现靠 Rich 自己重排，全屏实现必须自己兜这一层。
    """

    def __init__(self, source: Any, **kwargs: Any) -> None:
        self.source = source
        super().__init__(source, classes="entry", **kwargs)


class OpenXTui(App[None]):
    """OpenX 的全屏终端界面（阶段 0：转录 + 状态行 + 输入框）。"""

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
        Binding("escape", "interrupt", "打断", show=False),
    ]

    #: 排空间隔（秒）：sink 投递 → widget 挂载的合并窗口（≈30Hz）
    DRAIN_INTERVAL = 1 / 30
    #: 状态行刷新间隔（转帧，与行内 Live 的 5Hz 同量级）
    SPINNER_INTERVAL = 0.12

    def __init__(self, *, demo: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.sink_queue = SinkQueue()
        self._demo = demo
        self._mounted_ready = asyncio.Event()
        self._input_q: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self._busy = False
        self._sink: Optional[TuiSink] = None

    # ── 生命周期 ────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="transcript")
        yield StatusLine(id="spinner")
        yield Input(placeholder="Ask OpenX…  (Ctrl+Q 退出 / Esc 打断)",
                    id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        self._mounted_ready.set()
        self.set_interval(self.DRAIN_INTERVAL, self._drain_sink)
        self.set_interval(self.SPINNER_INTERVAL, self._tick_spinner)
        self.query_one("#prompt", Input).focus()
        if self._demo:
            self.run_worker(self._demo_script(), name="demo", exit_on_error=False)

    def on_resize(self) -> None:
        self._sync_sink_size()

    # ── 输出缝 ──────────────────────────────────────────────────

    def write(self, renderable: Any) -> None:
        """直接挂载一个 Rich renderable（供未来的流式管线调用）。"""
        if self._closed:
            return
        self._transcript.mount(RichEntry(renderable))
        self._scroll_to_end()

    def write_widget(self, widget: Any) -> None:
        """挂载一个 widget（块类：ThinkingBlock / ToolBlock …）。"""
        if self._closed:
            return
        self._transcript.mount(widget)
        self._scroll_to_end()

    @property
    def _transcript(self) -> VerticalScroll:
        return self.query_one("#transcript", VerticalScroll)

    def _drain_sink(self) -> None:
        items = self.sink_queue.drain()
        if not items:
            return
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
        if self._busy:
            self.query_one("#spinner", StatusLine)._render_frame()

    # ── 输入缝 ──────────────────────────────────────────────────

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value
        event.input.value = ""
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
        # 阶段 0：Esc 打断尚未接入流式管线（Stage 2），先只做提示。
        self.write(Text("  esc: 流式管线接入后生效（Stage 2）", style=DIM))

    def action_toggle_thinking(self) -> None:
        from .blocks import ThinkingBlock

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

    # ── 阶段 0 演示脚本 ─────────────────────────────────────────

    async def _demo_script(self) -> None:
        """离线演示：真流式节奏喂一遍 thinking / 工具块 / 答案。

        这不是"假数据演示"，而是把 Stage 2 要用的 widget 操作先跑通：
        折叠 → 展开 → 工具块三态 → Markdown 增量 → 回合收尾行。
        """
        from .blocks import ToolBlock, markdown_answer

        self.write(Text("❯ 解释一下这个仓库的流式渲染是怎么做的",
                        style=f"bold {ACCENT}"))
        self.set_busy("Thinking…")
        thinking = _make_thinking()
        self.write_widget(thinking)
        for chunk in (
            "用户要的是「thinking 默认折叠、点开可看」。\n",
            "行内实现里这件事很贵：scrollback 不可重写，\n",
            "所以只能靠未固化窗口 + Ctrl+R + 就地重印。\n",
            "换到 widget 树之后，折叠只是 Collapsible 的一个参数。",
        ):
            thinking.feed(chunk)
            await asyncio.sleep(0.18)
        thinking.freeze(3.4)
        thinking.collapsed = False  # 演示"点开看"的等价状态

        self.set_busy("Answering…")
        tools = [
            ToolBlock("read_file", "services/streaming.py"),
            ToolBlock("grep", "pattern=_commit_watermark"),
        ]
        for block in tools:
            self.write_widget(block)
            await asyncio.sleep(0.25)
        tools[0].set_result("1 2200 行 / 逐行固化水印 / _ResizeAwareLive",
                            is_error=False)
        tools[1].set_result("openx/services/streaming.py:1370\n"
                            "openx/services/streaming.py:1576")
        await asyncio.sleep(0.3)

        answer = RichEntry(markdown_answer(""))
        self.write_widget(answer)
        text = ""
        for chunk in (
            "## 结论\n\n",
            "全屏实现把「折叠」从**状态机**降级成**控件属性**：\n\n",
            "```python\nCollapsible(body, title='Thought for 3s', collapsed=True)\n```\n\n",
            "剩下的成本都在迁移，而不在这个功能本身。",
        ):
            text += chunk
            answer.update(markdown_answer(text))
            await asyncio.sleep(0.2)
        self.set_busy(None)
        self.write(Text("✻ Cooked for 12s", style=DIM))


def _make_thinking():
    from .blocks import ThinkingBlock

    return ThinkingBlock()


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
