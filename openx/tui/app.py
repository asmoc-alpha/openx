"""Textual app —— 全屏界面主体（转录 / 状态行 / 输入框 / 按键）。

组成（对应行内实现的界面分段，但由框架托管；**没有 Footer**——键位提示
只在弹窗的提示行里出现，`Ctrl+Q`/`Ctrl+R`/`Ctrl+T` 照常生效，不在屏幕上
常驻占一行）：

    ┌ transcript (VerticalScroll) ─ 会话流，块可折叠、可鼠标点开
    │ status line                  转帧 + Thinking…/Answering… + 耗时
    │ Input                        输入行（Enter 提交；斜杠菜单见 slash.py）
    └ SlashMenu                    输入行下方的 `/` 候选浮层

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
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Input, Static

from ..ui._style import ACCENT
from .blocks import StatusLine, ThinkingBlock
from .modals import (
    _OTHER,
    ChoiceScreen,
    TextScreen,
    ask_user_options,
    permission_body,
    plan_body,
)
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


class PromptInput(Input):
    """输入行 + 斜杠命令菜单的键位路由。

    行内 ptk 的语义（``editor.build_key_bindings``）逐条搬过来：

    - 菜单开时 ``↑``/``↓`` 移动候选光标（不到输入历史里去——TUI 输入行
      暂无历史，方向键不挪光标也符合"菜单优先"的行内行为）；
    - ``Tab``：应用当前候选 + 尾随空格（参数位起编）；
    - ``Enter``：应用当前候选并**提交**（行内 ``_enter_with_menu``）；
    - ``Esc``：先收菜单（有菜单关菜单），无菜单时冒泡给 app 打断回合
      （行内 ``_esc_closes_menu`` 同款分层）。

    菜单本体是兄弟节点 ``#slash-menu``（compose 顺序紧贴输入行下方）；
    路由方法在这里，弹与收由 :meth:`_refresh_slash_menu` 驱动。
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("up", "slash_up", "上一个候选", show=False),
        Binding("down", "slash_down", "下一个候选", show=False),
        Binding("tab", "slash_tab", "应用候选", show=False),
        Binding("escape", "slash_esc", "收菜单", show=False),
    ]

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """菜单关时全部 slash 绑定整体失效——Esc 等键自然冒泡给 app。

        不做这一层门控，Esc 绑定会先于 app 的 interrupt 吃掉按键：流式
        打断永远不触发（回归于 test_esc_cancels_registered_task_once）。
        """
        if action.startswith("slash_"):
            return self._menu_open()
        return super().check_action(action, parameters)

    async def _on_key(self, event: events.Key) -> None:
        """菜单开时吃掉 ↑/↓/Tab/Esc 的**默认行为**，但放行冒泡。

        Textual 的键路是两条：① MRO 分发的 ``_on_key``（Input 在这里把
        可打印字符插入、把 ↑/↓ 交给光标/历史）；② 事件冒泡到 ``App`` 后
        的 ``_check_bindings``（我们注册的 slash_* 绑定在这里触发）。
        ``prevent_default`` 只断 ①（MRO 在本类之后 break），``stop`` 会
        把 ② 也断掉——按键永远到不了绑定 action（实测：菜单开按 ↓ 光标
        纹丝不动）。所以这里**只 prevent_default、绝不 stop**。
        """
        if self._menu_open() and event.key in ("up", "down", "tab", "escape"):
            event.prevent_default()

    @property
    def _slash_menu(self) -> SlashMenu | None:
        try:
            return self.app.query_one("#slash-menu", SlashMenu)
        except Exception:
            return None

    def _menu_open(self) -> bool:
        menu = self._slash_menu
        return menu is not None and bool(menu.matches)

    def action_slash_up(self) -> None:
        self._slash_menu.move(-1)

    def action_slash_down(self) -> None:
        self._slash_menu.move(1)

    def action_slash_tab(self) -> None:
        menu = self._slash_menu
        if menu is None or not menu.matches:
            return  # 无候选：不拦截（Textual Input 默认 Tab 是焦点移动）
        applied = menu.apply(with_space=True)
        if applied is not None:
            self.value = applied
            self.cursor_position = len(applied)
            menu.hide()

    def action_slash_esc(self) -> None:
        self._slash_menu.hide()

    async def action_submit(self) -> None:
        """Enter：菜单开时先应用候选再提交（行内 ``_enter_with_menu``）。

        菜单未开：默认提交原文。应用后 value 已是规范名（``/quit`` 而非
        ``/qu``），随后的提交事件携带的就是它。
        """
        menu = self._slash_menu
        if menu is not None and menu.matches:
            applied = menu.apply()
            if applied is not None:
                self.value = applied
                self.cursor_position = len(applied)
        await super().action_submit()

    def _refresh_slash_menu(self) -> None:
        """输入变化 → 弹/收菜单（``Input.Changed`` 经 app 转发驱动）。"""
        menu = self._slash_menu
        if menu is None:
            return
        menu.refresh_for(self.value)


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
    /* 输入行 = 行内同款的"两条横线包着"（``print_user_prompt`` 的框）：
       顶、底各一条细线，**左右无边框**——不是 Textual 默认的长方形盒。
       #585858 = 256 色 240 号，即行内框线的 ``_style.CHROME``（grey35，
       Textual 的 CSS 不认 Rich 颜色名，故写十六进制）。 */
    #prompt {
        height: 3;
        border: none;
        border-top: tall #585858;
        border-bottom: tall #585858;
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
    /* 弹窗里的自由输入行：与主输入行同一视觉身份——两条横线包着。 */
    TextScreen #modal-input {
        margin: 1 0 0 0;
        background: transparent;
        padding: 0;
        height: auto;
        border: none;
        border-top: tall #585858;
        border-bottom: tall #585858;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        # 全部 show=False：界面没有 Footer，键位不在屏幕上常驻展示——
        # `Ctrl+Q`/`Ctrl+R`/`Ctrl+T` 照常生效，提示只在弹窗的提示行出现。
        Binding("ctrl+q", "quit_tui", "退出", show=False, priority=True),
        Binding("ctrl+r", "toggle_thinking", "thinking", show=False),
        Binding("ctrl+t", "toggle_all", "全部折叠/展开", show=False),
        # **不加 priority**：`Input` 没有 escape 绑定，按键自然冒泡到 app，
        # 输入行聚焦时照样能打断。而加上 priority 会让 app 抢在**模态屏**
        # 之前吃掉 Esc——弹窗就再也取消不了了（priority 绑定先于屏幕绑定
        # 求值）。屏幕级绑定天然优先于 app 级绑定，正是这里要的分层。
        Binding("escape", "interrupt", "打断", show=False),
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
        #: 挂起的弹窗：future → 取消默认值。app 被外部关掉时按默认值
        #: resolve——一个 await 着的工具绝不能永远挂着（那是死锁，不是退出）。
        self._pending_dialogs: dict[Any, Any] = {}

    # ── 生命周期 ────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="transcript")
        yield StatusLine(id="spinner")
        yield PromptInput(placeholder=IDLE_PLACEHOLDER, id="prompt")
        yield SlashMenu()

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
        self._busy_label = label
        if label is None:
            self.query_one("#spinner", StatusLine).set_busy(None)
        else:
            self.query_one("#spinner", StatusLine).set_busy(label,
                                                            started_at=time.monotonic())

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

    def on_input_changed(self, event: Input.Changed) -> None:
        """输入变化 → 刷新斜杠菜单（``PromptInput`` 转发来的联动）。"""
        if isinstance(event.input, PromptInput):
            event.input._refresh_slash_menu()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter：流式期排队等本回合结束，空闲期交给 ``ask()``。

        与行内同语义（流式期回车 = "该回合结束后立即发送"）：行内由
        ``InputCapture`` 把行塞进 ``console._input_queue``，此处直接复用
        那个通道——主循环的下一拍就会取走它。

        菜单开着时先应用候选（行内 ``_enter_with_menu``：Enter = 提交所选
        命令而不是原文）。
        """
        text = event.value
        event.input.value = ""
        # 提交后菜单即收（候选已应用或命令已定型）
        menu = self.query_one("#slash-menu", SlashMenu)
        menu.hide()
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
        """收摊：先按取消默认值了结挂起的弹窗，再让 app 退出。

        顺序是承重的——**先 resolve，再 exit**：挂起弹窗的 ``await`` 属于
        工具栈，若先退出，工具栈会在 app 已经死掉之后再被唤醒（写已拆除
        的控件树）。
        """
        if self._tui_closed:
            return
        self._tui_closed = True
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
