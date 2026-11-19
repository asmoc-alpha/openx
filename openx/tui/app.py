"""Textual app —— 全屏界面主体（转录 / 状态行 / 输入框 / 按键）。

组成（对应行内实现的界面分段，但由框架托管；**没有 Footer**——键位提示
只在弹窗的提示行里出现，`Ctrl+Q`/`Ctrl+R`/`Ctrl+T` 照常生效，不在屏幕上
常驻占一行）：

    ┌ transcript (VerticalScroll) ─ 会话流，块可折叠、可鼠标点开
    │ detail (DetailView)          子代理详情（Ctrl+O），顶掉转录的位置
    │ plan-deck (DeckView)         计划 + 排队队列（输入框之上，空则隐藏）
    │ status line                  转帧 + Thinking…/Answering… + 耗时
    │ PromptEditor                 输入行（Enter 提交 / Ctrl+J 换行 / ↑↓ 历史）
    │ SlashMenu                    输入行下方的 `/` 候选浮层
    └ agents-deck (DeckView)       Agents + 插件面板（输入框之下，空则隐藏）

三条缝在这里落地：

1. **输出缝**：``Console._console`` 被换成 :class:`openx.tui.sink.TuiSink`，
   它只往 :class:`~openx.tui.sink.SinkQueue` 投递；本 app 用 ``set_interval``
   定时排空并 mount widget。**同一时间只有一个消费者改 widget**（沿用
   ``ui/resize.py`` 的写屏纪律，只是消费者从 Live 线程换成 app 线程）。
2. **输入缝**：:meth:`ask` 是 ``print_user_prompt`` 的 TUI 实现——聚焦输入
   行（:class:`~openx.tui.prompt.PromptEditor`）后 ``await`` 一个队列；返回
   ``None`` 表示 EOF/退出，与行内语义一致（``interactive.py`` 见到 ``None``
   就干净退出，不改一行）。
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
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Static

from ..ui._style import ACCENT
from .blocks import StatusLine, ThinkingBlock
from .deck import DeckSource, DeckView, DetailView
from .history import PromptHistory
from .modals import (
    _OTHER,
    ChoiceScreen,
    TextScreen,
    ask_user_options,
    permission_body,
    plan_body,
)
from .prompt import PromptEditor
from .sink import SinkQueue, TuiSink
from .slash import SlashMenu
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


class Transcript(VerticalScroll):
    """转录滚动容器 —— **不可聚焦**（``can_focus = False``）。

    Textual 的可滚动容器默认可聚焦（为了键盘滚动）。但聊天界面里点一下
    转录（想选段文字，或纯属肌肉记忆）就会把焦点从输入行抢到 ``#transcript``
    上——此后**所有**按键（字符、Enter、``/quit``）都到不了输入行，界面对
    用户表现为"无法输入"；而且回看历史是滚轮的事（事件按指针位置派发，
    不需要焦点），键盘滚动在这里没有意义。整体关掉可聚焦性，输入行永不
    因点击失焦。
    """

    can_focus = False


class OpenXTui(App[None]):
    """OpenX 的全屏终端界面：转录 + 状态行 + 输入框 + 键位。"""

    CSS = """
    Screen {
        layout: vertical;
    }
    /* 滚动条一律不显示（scrollbar-size-vertical: 0）：终端里那一列可拖动
       的竖条太扎眼，也没有用处——回看历史靠滚轮（事件按指针位置派发，不需要
       可见的滑块），键位里没有任何东西去拖它。副作用是好的：Textual 会给可
       见的滚动条留一列槽位，隐藏后这列归正文（80 列终端里 77 → 78 可用）。 */
    #transcript {
        height: 1fr;
        padding: 0 1;
        scrollbar-size-vertical: 0;
    }
    /* 子代理详情（Ctrl+O）顶掉转录的位置。两个都是 1fr，但同一时刻只有一个
       在 display 里，所以不存在"抢高度"——空则整块不占（含 padding）。 */
    #detail {
        height: 1fr;
        padding: 0 1;
        scrollbar-size-vertical: 0;
        display: none;
    }
    /* 详情头行：单行不折（Agent N: 标签 · 状态 · 工具数 · 耗时 · 键位提示），
       超宽由 Text 的 ellipsis 收尾——与 deck 行同一几何纪律。正文不设高度：
       它可折行，高度归框架（滚动容器里）。 */
    #detail-head {
        height: 1;
    }
    #spinner {
        height: 1;
        padding: 0 1;
    }
    /* 状态层（deck）两个槽位：计划/队列在输入行**之上**、Agents 与插件面板
       在其**之下**（与行内同序）。各自与相邻区域隔 1 空行——行内靠
       _DECK_SPINNER_GAP 显式占位，这里用 margin：deck 不在场时
       ``display: none`` 会把 margin 一并带走，正是"仅 deck 在场才占位"。
       高度由 DeckView.set_rows 显式写成 len(rows)（行恒 no_wrap，
       一行 ≡ 一行），CSS 不写 height。 */
    #plan-deck {
        padding: 0 1;
        margin-bottom: 1;
    }
    #agents-deck {
        padding: 0 1;
        margin-top: 1;
    }
    /* 输入行 = 行内同款的"两条横线包着"（``print_user_prompt`` 的框）：
       顶、底各一条细线，**左右无边框**——不是 Textual 默认的长方形盒。
       #585858 = 256 色 240 号，即行内框线的 ``_style.CHROME``（grey35，
       Textual 的 CSS 不认 Rich 颜色名，故写十六进制）。
       ``#prompt`` 是 ``PromptEditor``（TextArea）：TextArea 自带长方形边框
       与 ``$surface`` 底色，这里逐项改写；高度由控件自己算（1–8 行 + 框线，
       见 ``tui/prompt.py::_sync_height``），故此处不写死 height。 */
    #prompt, #prompt:focus {
        height: 3;
        border: none;
        border-top: tall #585858;
        border-bottom: tall #585858;
        background: transparent;
        padding: 0 1;
    }
    #prompt .text-area--placeholder {
        color: $text-muted;
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
    /* 弹窗：行内的开放式格式（与 ``_raw_select`` 菜单同构）——无遮罩
       （screen 背景透明，转录在弹窗后面照常可见）、通栏左对齐、无卡片
       无边框：标题一行亮字 → 正文 → 裸 ``●`` 选项列 → dim 提示行，像行内
       输出一样铺在屏幕底部。模态语义保留：挡住下方的输入行、Esc 取消。 */
    ChoiceScreen, TextScreen {
        align: left bottom;
        background: transparent;
    }
    ChoiceScreen #modal-card, TextScreen #modal-card {
        width: 100%;
        height: auto;
        max-height: 80%;
        padding: 0 1;
    }
    ChoiceScreen #modal-title, TextScreen #modal-title {
        text-style: bold;
        padding: 0 0 1 0;
    }
    #modal-body {
        height: auto;
        max-height: 16;
        padding: 0 0 1 0;
    }
    /* 选项表：裸列。行内菜单选中行 = ``● label``（cyan 圆点 + 亮白字），
       其余 = ``  label`` dim。OptionList 默认的反白高亮块与边框全数剥掉。 */
    ChoiceScreen #modal-options {
        height: auto;
        max-height: 14;
        border: none;
        background: transparent;
        padding: 0;
        scrollbar-size-vertical: 0;
    }
    ChoiceScreen #modal-options > .option-list--option-highlighted {
        background: transparent;
        color: $foreground;
        text-style: bold;
    }
    ChoiceScreen #modal-options:focus > .option-list--option-highlighted {
        background: transparent;
        color: $foreground;
        text-style: bold;
    }
    ChoiceScreen #modal-hint, TextScreen #modal-hint {
        padding: 1 0 0 0;
    }
    /* 弹窗里的自由输入行：与主输入行同一视觉身份——两条横线包着。
       同一个 ``PromptEditor``，所以 ``Enter`` 确认、``Ctrl+J`` 换行，
       高度同样按内容长高（多行 instructions 因此能在一个弹窗里写完）。 */
    TextScreen #modal-input, TextScreen #modal-input:focus {
        margin: 1 0 0 0;
        background: transparent;
        padding: 0 1;
        height: 3;
        border: none;
        border-top: tall #585858;
        border-bottom: tall #585858;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        # 全部 show=False：界面没有 Footer，键位不在屏幕上常驻展示——
        # `Ctrl+Q`/`Ctrl+R`/`Ctrl+T` 照常生效，提示只在弹窗的提示行出现。
        Binding("ctrl+q", "quit_tui", "退出", show=False, priority=True),
        # `Ctrl+R`/`Ctrl+T` 提权：输入行是 TextArea，自带一整套编辑键位
        # （`ctrl+z`/`ctrl+y`/`ctrl+k`/…）。这两个是**全局界面**动作，必须
        # 先于控件绑定求值——否则哪天 TextArea 给 `ctrl+r` 也绑一件事，
        # 折叠就被静默吃掉（行内实现里 `ctrl+r` 恰好是历史反搜，语义更该
        # 由 app 说了算）。模态屏不用这两个键，不会重犯 Esc 的覆辙。
        Binding("ctrl+r", "toggle_thinking", "thinking", show=False,
                priority=True),
        Binding("ctrl+t", "toggle_all", "全部折叠/展开", show=False,
                priority=True),
        # 舰队导航：`Ctrl+O` 循环切换主回答 ⇄ 各子代理详情（行内同一个键、
        # 同一语义），`Alt+0..9` 直选（0 = 主视图）。同理提权——输入行是
        # TextArea，全局动作必须先于控件绑定求值。
        # **不用 ↑/↓**：全屏的输入行始终可编辑，方向键的第一含义是翻历史/
        # 移光标（tests/tui/test_input.py 钉着）；行内那套"↓ 选中 + ↵ 确认"
        # 能成立只是因为流式期输入行不接收编辑。
        Binding("ctrl+o", "fleet_cycle", "子代理视图", show=False,
                priority=True),
        Binding("alt+0", "fleet_select(0)", "主回答", show=False,
                priority=True),
        Binding("alt+1", "fleet_select(1)", "子代理 1", show=False,
                priority=True),
        Binding("alt+2", "fleet_select(2)", "子代理 2", show=False,
                priority=True),
        Binding("alt+3", "fleet_select(3)", "子代理 3", show=False,
                priority=True),
        Binding("alt+4", "fleet_select(4)", "子代理 4", show=False,
                priority=True),
        Binding("alt+5", "fleet_select(5)", "子代理 5", show=False,
                priority=True),
        Binding("alt+6", "fleet_select(6)", "子代理 6", show=False,
                priority=True),
        Binding("alt+7", "fleet_select(7)", "子代理 7", show=False,
                priority=True),
        Binding("alt+8", "fleet_select(8)", "子代理 8", show=False,
                priority=True),
        Binding("alt+9", "fleet_select(9)", "子代理 9", show=False,
                priority=True),
        # **不加 priority**：输入行没有 escape 绑定，按键自然冒泡到 app，
        # 输入行聚焦时照样能打断。而加上 priority 会让 app 抢在**模态屏**
        # 之前吃掉 Esc——弹窗就再也取消不了了（priority 绑定先于屏幕绑定
        # 求值）。屏幕级绑定天然优先于 app 级绑定，正是这里要的分层。
        Binding("escape", "interrupt", "打断", show=False),
    ]

    #: 排空间隔（秒）：sink/stream 投递 → widget 挂载的合并窗口（≈30Hz）
    DRAIN_INTERVAL = 1 / 30
    #: 状态行刷新间隔（转帧，与行内 Live 的 5Hz 同量级）
    SPINNER_INTERVAL = 0.12
    #: deck 刷新间隔（秒）：与行内 ``Live`` 的 5Hz 同节拍——deck 里的转帧、
    #: 子代理耗时、插件面板（``refresh_hz`` 默认 5）都按这个节奏走。
    DECK_INTERVAL = 0.2

    def __init__(self, *, demo: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.sink_queue = SinkQueue()
        #: 流式回合的事件通道（``TuiStream`` 投递，``StreamView`` 消费）
        self.stream_queue = SinkQueue()
        self._demo = demo
        self._mounted_ready = asyncio.Event()
        self._input_q: asyncio.Queue = asyncio.Queue()
        #: "本界面已收摊" —— **绝不能叫 ``_closed``**。``MessagePump._closed``
        #: 是 Textual 自己的开关，``post_message`` 在它为真时**静默丢弃**
        #: 消息（``message_pump.py``: ``if self._closing or self._closed:
        #: return False``）。早期版本直接复用了这个名字，于是 ``_close()``
        #: 一置位，紧接着的 ``exit()`` 投递的 ``ExitApp`` 就被丢掉——退出
        #: 消息永远到不了 app，``_process_messages`` 挂在 ``_get_message()``
        #: 上不退（run_test 收尾 ``await app_task`` 遂永久挂死）。
        self._tui_closed = False
        self._busy = False
        self._busy_label: Optional[str] = None
        self._sink: Optional[TuiSink] = None
        #: 视图管线：**只**在 :meth:`_drain` 里被调用（单一消费者）
        self._view = StreamView(self)
        #: ``Console`` 引用（排队输入要写回 ``console._input_queue``）
        self._openx_console: Optional[Any] = None
        #: 流式回合进行中（决定 Enter 是"排队"还是"提交给 ask()"）
        self._stream_active = False
        #: Esc 打断回调（``TuiStream.start()`` 接线，收尾解绑）
        self._interrupt_cb: Optional[Callable[[], None]] = None
        #: 输入历史（与行内共用 ``~/.openx/history``，见 ``tui/history.py``）
        self._history = PromptHistory()
        #: 状态层数据源（``TuiStream.start()`` 接线；行格式见 ``ui/_deck.py``）
        self._decks = DeckSource()
        #: 当前在看的视图：0 = 主回答，1..N = 第 N 个子代理的详情（``Ctrl+O``）
        self._fleet_focus = 0
        #: 挂起的弹窗：future → 取消默认值。app 被外部关掉时按默认值
        #: resolve——一个 await 着的工具绝不能永远挂着（那是死锁，不是退出）。
        self._pending_dialogs: dict[Any, Any] = {}

    # ── 生命周期 ────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Transcript(id="transcript")
        # 子代理详情（Ctrl+O）：与转录**互斥**——同一处位置，谁在看谁显示
        yield DetailView(id="detail")
        # 输入框之上的状态层：计划 + 队列
        yield DeckView(id="plan-deck")
        yield StatusLine(id="spinner")
        yield PromptEditor(
            placeholder=IDLE_PLACEHOLDER,
            id="prompt",
            prompt_history=self._history,
        )
        yield SlashMenu()
        # 输入框之下的状态层：Agents + 插件面板
        yield DeckView(id="agents-deck")

    def on_mount(self) -> None:
        self._mounted_ready.set()
        self.set_interval(self.DRAIN_INTERVAL, self._drain)
        self.set_interval(self.SPINNER_INTERVAL, self._tick_spinner)
        self.set_interval(self.DECK_INTERVAL, self._tick_decks)
        self.query_one("#prompt", PromptEditor).focus()
        if self._demo:
            self.run_worker(self._demo_script(), name="demo", exit_on_error=False)

    def on_resize(self) -> None:
        self._sync_sink_size()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        """主屏上的点击永不把焦点从输入行带走（聊天界面惯例）。

        Textual 会把焦点交给被点的可聚焦控件（``CollapsibleTitle`` 等；
        转录容器已在 :class:`Transcript` 里关掉可聚焦性），输入行从此
        收不到任何按键——用户表现为“点了一下就打不了字”。滚轮滚动、
        块标题点击都按指针位置分发、不需要焦点，所以功能照旧；这里在
        冒泡到 app 的 MouseDown 上把焦点还给 ``#prompt``。模态屏有
        自己的焦点语义（选项表 / ``#modal-input``），不介入。
        """
        if len(self.screen_stack) > 1:
            return
        with contextlib.suppress(Exception):
            prompt = self.query_one("#prompt", PromptEditor)
            if self.focused is not prompt:
                prompt.focus()

    # ── 输出缝 ──────────────────────────────────────────────────

    def write(self, renderable: Any) -> None:
        """挂载一个 Rich renderable，返回包装它的控件（可后续 ``update``）。

        调用方是 :class:`~openx.tui.view.StreamView`（只在 :meth:`_drain`
        里被驱动）或演示脚本；两者都在 app 主循环线程，不构成并发写。
        """
        entry = RichEntry(renderable)
        if not self._tui_closed:
            self._transcript.mount(entry)
            self._scroll_to_end()
        return entry

    def write_widget(self, widget: Any) -> None:
        """挂载一个 widget（块类：ThinkingBlock / ToolBlock …）。"""
        if self._tui_closed:
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
        if self._tui_closed:
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

    def _active_scroller(self) -> VerticalScroll:
        """当前"看得见"的滚动面：详情开着就是它，否则是转录。

        贴底跟随只该作用于用户正看着的那一屏——详情开着时把转录滚到底既
        不可见，又会在退回来时把用户"回看历史"的位置悄悄冲掉。
        """
        if self._fleet_focus > 0:
            try:
                detail = self.query_one("#detail", DetailView)
                if detail.showing:
                    return detail
            except Exception:
                pass
        return self._transcript

    def _scroll_to_end(self) -> None:
        """贴底跟随；用户回看历史时不抢滚动位置。"""
        try:
            scroller = self._active_scroller()
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
        """输入框占位文案（流式期提示"回车 = 排队"；空内容时才显示）。"""
        with contextlib.suppress(Exception):
            self.query_one("#prompt", PromptEditor).placeholder = text

    # ── 状态行 ──────────────────────────────────────────────────

    def set_busy(self, label: Optional[str]) -> None:
        """``label`` 非空 → 显示转帧 + 耗时；None → 清空。"""
        self._busy = label is not None
        self._busy_label = label
        if label is None:
            self.query_one("#spinner", StatusLine).set_busy(None)
        else:
            self.query_one("#spinner", StatusLine).set_busy(label,
                                                            started_at=time.monotonic())

    # ── 状态层（deck：计划 / 队列 / Agents / 插件面板）──────────────

    def attach_decks(
        self,
        *,
        todos: Optional[Callable[[], Any]] = None,
        stale: Optional[Callable[[], bool]] = None,
        fleet: Optional[Any] = None,
        panels: Optional[Any] = None,
    ) -> None:
        """接线四路 deck 数据源（``TuiStream.start()`` 每回合调一次）。

        回合结束**不解线**：计划面板与插件面板要留在屏上（``agent.todos``
        本来就跨轮存活，``ui.panel`` 的桌面宠物/监控件按定义该常驻）。
        Agents 的显隐不看这里，看 :attr:`_stream_active`。
        ``stale`` 是计划新鲜度（``agent.todos_stale``）：清单过期时 Plan
        头行标 ``▲ stale``——面板照旧渲染，但不再冒充实时进度。

        每个回合把视图焦点**归零**（回主回答）：与行内 ``StreamingService.
        start()`` 的"每轮干净起点"同语义——新回合的第一帧绝不能还停在上一轮
        的某个子代理详情上（那个视图甚至已经不在新快照里了）。
        """
        self._decks.configure(todos=todos, stale=stale, fleet=fleet, panels=panels)
        self._fleet_focus = 0
        self._tick_decks()   # 立刻画一帧：别让 deck 晚 200ms 才出现

    def detach_decks(self) -> None:
        """解线（app 收摊时；此后 ``_tick_decks`` 也就早退了）。"""
        self._decks.configure()

    def _queued_items(self) -> list[str]:
        """排队待发的跟进消息（``console._input_queue`` 的只读快照）。"""
        console = self._openx_console
        queue = getattr(console, "_input_queue", None) if console else None
        return [str(item) for item in list(queue or [])]

    def _tick_decks(self) -> None:
        """5Hz：四路数据 → 两段行（上半进输入框之上，下半进其下）。

        单一消费者纪律照旧——只有本定时器（app 主循环线程）写这几个控件。
        高度有变化时补一次贴底滚动：deck 变高会压缩转录，用户正停在底部
        时必须继续跟着底走（``_scroll_to_end`` 自己会放过"正在回看历史"
        的情形）。焦点在子代理详情时同一拍里把它一并刷新（同一份舰队快照）。
        """
        if self._tui_closed:
            return
        try:
            top, bottom = self._decks.rows(
                height=self.size.height,
                turn_active=self._stream_active,
                queue=self._queued_items(),
                marked=self._fleet_focus,
            )
            changed = self.query_one("#plan-deck", DeckView).set_rows(top)
            changed = self.query_one(
                "#agents-deck", DeckView).set_rows(bottom) or changed
            changed = self._sync_detail() or changed
        except Exception:
            return   # 控件树拆除后的迟到一拍：状态层不值得把退出路径炸掉
        if changed:
            self._scroll_to_end()

    # ── 子代理详情视图（Ctrl+O）──────────────────────────────────

    def _sync_detail(self) -> bool:
        """焦点 → 详情视图的显隐与内容。返回**是否发生了可见变化**。

        焦点 ≤ 0 且详情本就没开 → 直接返回（不白取一份舰队快照：详情关着
        的时候这条路径每 200ms 都会走到）。焦点越界（子代理减少、舰队被
        ``reset()`` 清空）→ 归零回主视图：绝不显示一个已经不存在的代理。
        """
        detail = self.query_one("#detail", DetailView)
        if self._fleet_focus <= 0 and not detail.showing:
            return False
        snap = self._decks.fleet_snapshot()
        focus = self._fleet_focus
        if focus > len(snap):
            focus = 0
            self._fleet_focus = 0
        if focus <= 0:
            changed = detail.hide()
            if not self._transcript.display:
                self._transcript.display = True
                changed = True
            return changed
        changed = detail.show(snap[focus - 1])
        if self._transcript.display:
            self._transcript.display = False
            changed = True
        return changed

    # ── 弹窗（ModalScreen）──────────────────────────────────────

    async def _modal(self, screen: Any, default: Any) -> Any:
        """压一个模态屏并 await 它的结果（``default`` 兼作取消/关闭兜底）。

        不用 ``push_screen_wait``：它要求 worker 上下文，而这里的调用方是
        任意协程（工具执行栈深处）。自建 Future 的好处是能兜底——app 若在
        等待期间被关掉（Ctrl+Q / ``/quit``），``_close`` 会按 ``default``
        把它 resolve 掉，绝不让工具栈永远挂着。

        弹窗期间状态行改说 ``Waiting for you…``：流式回合此时静止（在等
        工具返回），继续显示 ``Answering…`` 会读成"已经答完了"。

        已收摊（``_close()`` 之后）直接返回 ``default``：这时压屏只会压进
        一棵已拆除的控件树，而 await 一个有主的 future 等于把工具栈钉死。
        """
        if self._tui_closed:
            return default
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending_dialogs[fut] = default
        prev_label = self._busy_label
        if self._busy_label is not None:
            self.set_busy("Waiting for you…")

        def _done(result: Any) -> None:
            if not fut.done():
                fut.set_result(default if result is None else result)

        try:
            self.push_screen(screen, _done)
            return await fut
        finally:
            self._pending_dialogs.pop(fut, None)
            if prev_label is not None and not self._tui_closed:
                with contextlib.suppress(Exception):
                    self.set_busy(prev_label)

    async def ask_permission(
        self,
        tool_name: str,
        reason: str,
        details: str = "",
        args_summary: str = "",
        can_remember: bool = True,
        diff: Optional[tuple[str, str, str]] = None,
    ) -> tuple[bool, bool]:
        """权限审批 → ``(approved, remember)``。取消/关闭 = ``(False, False)``。

        "不再询问"只在**有参数摘要可落规则**且调用方允许持久化时出现——
        与行内实现的判据逐字一致（manual 模式传 ``can_remember=False``）。
        """
        options: list[tuple[str, tuple[bool, bool]]] = [
            ("Yes, allow once", (True, False)),
            ("No, don't run", (False, False)),
        ]
        if args_summary and can_remember:
            options.insert(1, ("Yes, and don't ask again for this", (True, True)))
        body = permission_body(reason, details, diff, theme="monokai")
        return await self._modal(
            ChoiceScreen(
                f"Allow {tool_name}?",
                options,
                body=body,
                cancel_value=(False, False),
            ),
            (False, False),
        )

    async def ask_question(
        self, question: str, options: list[dict], multi_select: bool = False
    ) -> Any:
        """``ask_user`` 提问 → ``str``（单选）或 ``list[str]``（多选）。

        取消语义：单选 ``""``、多选 ``[]``——"未作答"在工具侧是合法输入
        （``ask_user`` 会把空答案如实回给模型，而不是编一个默认选项）。
        """
        pairs = ask_user_options(options, multi_select)
        choice = await self._modal(
            ChoiceScreen(
                question,
                pairs,
                cancel_value=[] if multi_select else "",
                multi=multi_select,
            ),
            [] if multi_select else "",
        )
        if multi_select:
            chosen = list(choice) if isinstance(choice, list) else []
            if _OTHER in chosen:
                chosen.remove(_OTHER)
                typed = await self.ask_text("Your answer")
                if typed:
                    chosen.append(typed)
            return chosen
        if choice == _OTHER:
            typed = await self.ask_text("Your answer")
            return typed or ""
        return choice

    async def ask_text(self, title: str, *, placeholder: str = "") -> str:
        """自由文本弹窗（返回 ``""`` 表示取消/空输入）。"""
        value = await self._modal(TextScreen(title, placeholder=placeholder), None)
        return (value or "").strip()

    async def select(
        self,
        title: str,
        options: list[tuple[str, Any]],
        *,
        default_index: int = 0,
    ) -> Any:
        """通用选择模态（slash 命令菜单的 TUI 后端）。

        ``options`` 是 ``(label, value)`` 序列，``value`` 原样返回。取消
        （Esc / app 关闭）→ ``None``——与行内各菜单 "Cancel/Done → None"
        的既有语义天然对齐，调用方无需分叉判断。
        """
        return await self._modal(
            ChoiceScreen(
                title,
                options,
                default_index=default_index,
                cancel_value=None,
            ),
            None,
        )

    def clear_transcript(self) -> None:
        """清空转录（``/clear`` 的 TUI 后端）。

        行内实现是 ``console.raw.clear()``（清整屏）；全屏界面里等价操作
        是移除 transcript 的全部子控件——``raw`` 在 TUI 下是 ``TuiSink``，
        它的 ``clear()`` 只会落进测量用的 StringIO，屏清不掉。
        """
        if self._tui_closed:
            return
        with contextlib.suppress(Exception):
            self._transcript.remove_children()

    async def ask_plan(self, plan: str = "") -> bool:
        """计划审批 → 是否执行。取消/关闭 = ``False``（不执行，fail-closed）。"""
        return bool(await self._modal(
            ChoiceScreen(
                "Plan approval  计划审批",
                [
                    ("Approve and execute (批准并执行)", True),
                    ("Reject (拒绝)", False),
                ],
                body=plan_body(plan),
                cancel_value=False,
            ),
            False,
        ))

    def _tick_spinner(self) -> None:
        # shutdown 竞态：定时器可能在控件树拆除后再被投递一次（回合没跑完
        # 就退出时最典型）。转帧是纯装饰，绝不能让它把退出路径炸掉。
        if not self._busy or self._tui_closed:
            return
        with contextlib.suppress(Exception):
            self.query_one("#spinner", StatusLine)._render_frame()

    # ── 输入缝 ──────────────────────────────────────────────────

    def on_prompt_editor_submitted(self, event: PromptEditor.Submitted) -> None:
        """Enter：流式期排队等本回合结束，空闲期交给 ``ask()``。

        与行内同语义（流式期回车 = "该回合结束后立即发送"）：行内由
        ``InputCapture`` 把行塞进 ``console._input_queue``，此处直接复用
        那个通道——主循环的下一拍就会取走它。

        菜单开着时先应用候选（行内 ``_enter_with_menu``：Enter = 提交所选
        命令而不是原文）——那一步在 :meth:`PromptEditor.action_submit` 里
        完成，到这里 ``event.value`` 已是定型文本。

        提交即清空输入行（内容不属于"历史输入框"，它作为用户回合重绘在
        转录里）并把这一条记进历史——空行不记，相邻重复也不记。
        """
        text = event.value
        editor = self.query_one("#prompt", PromptEditor)
        editor.load_text("")
        # 提交后菜单即收（候选已应用或命令已定型）
        menu = self.query_one("#slash-menu", SlashMenu)
        menu.hide()
        if not text.strip():
            return
        self._history.add(text)
        if self._stream_active and self._openx_console is not None:
            self._openx_console._input_queue.append(text)
            self.stream_queue.push(Note(f"↳ queued: {text}"))
            return
        self._input_q.put_nowait(text)

    async def ask(self) -> Optional[str]:
        """``print_user_prompt`` 的 TUI 实现：聚焦输入行并等一条输入。

        返回 ``None`` 表示界面已关闭（用户 Ctrl+Q / app 退出）——
        与行内路径的 EOF 语义一致，``interactive.py`` 据此干净退出。
        """
        await self._mounted_ready.wait()
        self.query_one("#prompt", PromptEditor).focus()
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

    # ── 舰队导航（Ctrl+O / Alt+0..9）───────────────────────────────

    def action_fleet_cycle(self) -> None:
        """``Ctrl+O``：主回答 → 子代理 1 → … → 子代理 N → 主回答。

        与行内 ``Ctrl-O`` 逐字同语义（那里的注释是"主视图 ⇄ 各子代理直接
        循环切换"）。没有子代理时是 no-op——按键**总有响应**但绝不假装有东西
        可看。
        """
        snap = self._decks.fleet_snapshot()
        if not snap:
            return
        self._fleet_focus = (self._fleet_focus + 1) % (len(snap) + 1)
        self._after_fleet_nav()

    def action_fleet_select(self, index: Any) -> None:
        """``Alt+0..9``：直选某个视图（``0`` = 主回答）。

        越界钳到最后一个（同行内"按键必须总有响应，静默失败最困惑"）；没有
        子代理时连主视图也不切——``0`` 的语义本来就是"回主回答"，此时已经在
        主回答上。
        """
        snap = self._decks.fleet_snapshot()
        if not snap:
            return
        n = int(index)
        self._fleet_focus = 0 if n <= 0 else min(n, len(snap))
        self._after_fleet_nav()

    def _after_fleet_nav(self) -> None:
        """切完视图立刻重画（不等下一拍 200ms），并让焦点留在输入行。"""
        if self._tui_closed:
            return
        if len(self.screen_stack) > 1:
            # 弹窗在场时不动主视图：详情会把转录顶掉，而弹窗正文正是压
            # 转录之上的——两个可见变化叠在一起只会让人不知道发生了什么。
            return
        self._tick_decks()

    def _close(self) -> None:
        """收摊：先按取消默认值了结挂起的弹窗，再让 app 退出。

        顺序是承重的——**先 resolve，再 exit**：挂起弹窗的 ``await`` 属于
        工具栈，若先退出，工具栈会在 app 已经死掉之后再被唤醒（写已拆除
        的控件树）。
        """
        if self._tui_closed:
            return
        self._tui_closed = True
        self.detach_decks()   # 双保险：迟到的定时器拍子已被 _tui_closed 挡住
        for fut, default in list(self._pending_dialogs.items()):
            if not fut.done():
                with contextlib.suppress(Exception):
                    fut.set_result(default)
        self._pending_dialogs.clear()
        # 逐层摘掉模态屏只是为了让退出过程干净（转录不留半块浮层）；
        # 即便摘不掉也不影响退出：``App._shutdown()`` 的 ``_close_all()``
        # 会收掉所有屏幕栈。所以这里一律兜住异常。
        while len(self.screen_stack) > 1:
            try:
                self.pop_screen()
            except Exception:
                break
        with contextlib.suppress(Exception):
            self.exit()

    # ── 离线演示脚本 ────────────────────────────────────────────

    async def _demo_script(self) -> None:
        """离线演示：用**真实事件**喂一遍 thinking / 工具块 / 答案 + 四块 deck。

        ``TuiStream`` 与生产路径共用同一条管线——演示即管线的自检，不另
        造一条仿造路径（否则演示跑通证明不了任何事）。状态层同理：喂进去的
        是 ``agent.todos`` 形状的列表、``FleetMonitor.snapshot()`` 形状的快照
        与 ``UiPanelCollector.panels()`` 形状的收集器，于是计划 / 队列 /
        Agents / 插件面板四块在演示里都能看到。
        """
        from ..agent import ToolResultEvent, ToolStartEvent
        from ..kernel.reasoning.provider import StreamReasoning

        todos: list[dict] = [
            {"content": "抽出 deck 行格式", "status": "completed",
             "activeForm": ""},
            {"content": "接全屏 deck 面板", "status": "in_progress",
             "activeForm": "接全屏 deck 面板"},
            {"content": "补测试与文档", "status": "pending", "activeForm": ""},
        ]
        console = self._console_stub()
        # 队列面板读的是 console._input_queue（与真 REPL 同一条通道）
        console._input_queue.append("顺手把 Agents 行也接上")
        self.attach_console(console)
        stream = TuiStream(
            console, app=self,
            todos_provider=lambda: todos,
            fleet=_DemoFleet(),
            panels=_DemoPanels(),
            tool_lookup=_demo_tool_lookup,
        )
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
        # 文本操作：标题用路径、正文是彩色 diff（绿增/红删/语言高亮）——
        # 演示"edit 的变更看得见区别"，也顺带自检 tool_lookup → preview_diff
        # → ui/_diff 这条链。
        stream.feed(ToolStartEvent(
            name="edit_file",
            arguments='{"file_path": "openx/services/streaming.py", '
                      '"old_text": "return Group(*parts)", '
                      '"new_text": "# deck 在 spinner 之上\\nreturn Group(*parts)"}',
            call_id="demo-3"))
        await asyncio.sleep(0.35)
        stream.feed(ToolResultEvent(
            name="edit_file", call_id="demo-3",
            output="Replaced 1 occurrence(s) in openx/services/streaming.py\n"
                   "--- a/openx/services/streaming.py\n"
                   "+++ b/openx/services/streaming.py\n"
                   "@@ -1157,3 +1157,4 @@ def _build_renderable(self):\n"
                   "     def _build_renderable(self):\n"
                   "-        return Group(*parts)\n"
                   "+        # deck 在 spinner 之上\n"
                   "+        return Group(*parts)\n"))
        # 计划面板推进一格（演示"下一拍就反映变化"）
        todos[1]["status"] = "completed"
        todos[2]["status"] = "in_progress"
        todos[2]["activeForm"] = "补测试与文档"
        for chunk in (
            "## 结论\n\n",
            "全屏实现把「折叠」从**状态机**降级成**控件属性**：\n\n",
            "```python\nCollapsible(body, title='Thought for 3s', collapsed=True)\n```\n\n",
            "剩下的成本都在迁移，而不在这个功能本身。",
        ):
            stream.feed(chunk)
            await asyncio.sleep(0.2)
        stream.done()
        # 收尾：计划全部完成 → 面板折叠成单行汇总
        for item in todos:
            item["status"] = "completed"
        # 演示"点开看"的等价状态：默认折叠的 thinking 块在此展开
        thinking = list(self.query(ThinkingBlock))
        if thinking:
            thinking[0].collapsed = False
        # 演示舰队导航：切进子代理 1 的详情看一眼再回主回答（Ctrl+O 的真实
        # 路径——演示即管线的自检，不另造一条仿造路径）
        self.action_fleet_cycle()
        await asyncio.sleep(2.0)
        self.action_fleet_select(0)

    def _console_stub(self) -> Any:
        """演示用的最小 console 存根（``TuiStream`` 只用到 ``_input_queue``）。"""
        from types import SimpleNamespace

        return SimpleNamespace(_input_queue=[])


class _DemoFleet:
    """演示用舰队：形状与 ``FleetMonitor.snapshot()`` 逐键一致（含 ``lines``）。"""

    _SEEDS = (
        {"id": 1, "label": "explore streaming", "status": "running",
         "tools_count": 3, "elapsed": 4.2,
         "last_tool": "read_file(services/streaming.py)",
         "lines": [
             "[dim]● read_file[/dim][dim](file_path=services/streaming.py)[/dim]",
             "[dim]  ⎿  [/]1 2200 行 / 逐行固化水印 / _ResizeAwareLive",
             "[dim]● grep[/dim][dim](pattern=_commit_watermark)[/dim]",
         ],
         "pending": "正在核对 deck 预算与视口裁剪…"},
        {"id": 2, "label": "review docs", "status": "done",
         "tools_count": 5, "elapsed": 9.1, "last_tool": "",
         "lines": ["[dim]● read_file[/dim][dim](docs/subsystems/tui.md)[/dim]",
                   "文档与实现一致，Decks 一节需补按键说明。"],
         "pending": ""},
    )

    def __init__(self) -> None:
        self.reset()

    def snapshot(self) -> list[dict]:
        return [dict(view) for view in self._views]

    def reset(self) -> None:
        """演示里的"新一轮"= 回到初始两条（生产侧是清空）。"""
        self._views = [dict(view) for view in self._SEEDS]


class _DemoEdit:
    """演示用的 ``edit_file`` 预览桩：只实现 ``preview_diff``。

    形状与 ``tools/file_tools.py::EditFileTool.preview_diff`` 一致
    （``(path, old, new)``）——演示因此能画出真实的三色 diff，而不是造一条
    仿造渲染路径。
    """

    def preview_diff(self, args: dict) -> Any:
        path = str(args.get("file_path") or "services/streaming.py")
        old = "    def _build_renderable(self):\n        return Group(*parts)\n"
        new = ("    def _build_renderable(self):\n"
               "        # deck 在 spinner 之上，隔 1 空行\n"
               "        return Group(*parts)\n")
        return (path, old, new)


def _demo_tool_lookup(name: str) -> Any:
    """演示用的工具查找：只有 ``edit_file`` 有变更预览。"""
    return _DemoEdit() if name == "edit_file" else None


class _DemoPanels:
    """演示用插件面板收集器：形状与 ``UiPanelCollector.panels()`` 一致。"""

    def __init__(self) -> None:
        self._frame = 0

    def panels(self) -> list[tuple[str, str, list[str]]]:
        self._frame += 1
        return [("pet", "demo-pet",
                 [f"[dim](^_^)[/dim] [bold]demo-pet[/bold]"
                  f" · 第 {self._frame} 帧"])]


async def run_tui(agent: Any, console: Any) -> None:
    """``--tui`` 入口：app 与既有 REPL 协程在同一 loop 内并发。

    任一先结束就收掉另一个：REPL 里 ``/quit`` 或 EOF → 关 app；用户在 TUI
    里 Ctrl+Q → ``ask()`` 拿到 ``None`` → REPL 走既有的 EOF 退出路径。

    REPL task 必须带着 ``active_app`` 上下文出生：Textual 的 ``push_screen``
    → 屏 ``compose`` 依赖这个 ContextVar（``Widget.app`` 在控件尚未挂上父
    节点时靠它兜底）。``run_tui`` 自身在 ``app._context()`` 之外运行，裸
    ``create_task`` 出生的 REPL 及其后代 task（工具执行栈里的弹窗调用都
    在其中）没有它——弹窗一开就在 compose 的 ``Vertical.__enter__`` 处
    ``NoActiveAppError``。测试没抓到是因为 ``run_test`` 的测试体本身跑在
    上下文里。
    """
    from ..app.cli.interactive import run_interactive

    app = OpenXTui()
    console.attach_tui(app)
    app_task = asyncio.create_task(app.run_async(), name="openx-tui")
    try:
        spawn_ctx = app._context()
    except AttributeError:  # pragma: no cover - 防御 textual 版本变动
        spawn_ctx = contextlib.nullcontext()
    with spawn_ctx:
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
