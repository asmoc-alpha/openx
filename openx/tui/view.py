"""转录视图管线：流事件 → widget（**只在 app 定时器里被调用**）。

分工与行内实现一一对应，但落点从"重绘一块 Live 区域"变成"mount/更新控件"：

| 行内 ``StreamingService`` | 全屏 ``StreamView`` |
|---|---|
| ``_reasoning_buffer`` + 指示行 + Ctrl+R | :class:`ThinkingBlock`（``collapsed`` 是控件属性） |
| ``_segments`` 的 text 段 + Markdown 重渲 | :class:`RichEntry`，按门控 ``update()`` |
| ``_segments`` 的 tool 段 + 三态渲染 | :class:`ToolBlock`，``set_result()`` 改状态点 |
| ``done()`` 追加 ``✻ Cooked for Ns`` | ``TurnEnd`` → 写一行 ``done_line`` |
| Ctrl+R / Ctrl+T 在 Live drain 循环里消费 | app 的 key binding（见 ``app.py``） |

两条硬纪律：

1. **单一消费者**：本类只在 :meth:`openx.tui.app.OpenXTui._drain` 里被调用，
   也就是只在 app 主循环线程改 widget。``TuiStream.feed`` 只入队。
2. **答案分段**：工具块一插进来，当前答案段就结束了——后续 token 必须
   **新开**一条 ``RichEntry``，否则新文本会接在旧 Markdown 里、把工具块
   "埋"在中间（行内 ``_segments`` 的交替语义，等价实现）。
"""

from __future__ import annotations

import time
from typing import Any, Optional

from rich.text import Text

from ..ui._helpers import done_line
from ..ui._style import DIM
from .blocks import ThinkingBlock, ToolBlock, markdown_answer
from .stream import (
    Answer,
    Note,
    Reasoning,
    Status,
    StreamStart,
    ToolFinished,
    ToolStarted,
    TurnAborted,
    TurnEnd,
)

#: 空闲态输入框提示（``app.py`` 的 compose 与本模块共用，避免两处文案漂移）
IDLE_PLACEHOLDER = "Ask OpenX…  (Ctrl+Q 退出 / Esc 打断)"
#: 流式期输入框提示：回车的内容会排队，等本回合结束再发
BUSY_PLACEHOLDER = "Queue a message…  (Esc 打断本回合)"

#: 答案 Markdown 的重渲最小间隔（秒）。
#: token 逐字到达而 Markdown 每次都要整段重解析——30Hz 排空节拍下无限流
#: 重渲纯属浪费，10Hz 已远超可感知的字速。
_ANSWER_RERENDER_S = 0.1

#: 打断收尾的固定动词（随机动词属于"好好干完了"的语义，用在这里是错的）
_ABORT_VERB = "Interrupted"


class StreamView:
    """把一回合的流事件落成 transcript 里的控件。"""

    def __init__(self, app: Any) -> None:
        self._app = app
        self._reset()

    def _reset(self) -> None:
        self._t_start = 0.0
        self._thinking: Optional[ThinkingBlock] = None
        self._answer: Optional[Any] = None          # 当前答案段（RichEntry）
        self._answer_text = ""
        self._last_render = 0.0
        self._blocks: list[ToolBlock] = []
        self._by_call_id: dict[str, ToolBlock] = {}
        # 本回合是否产出过任何东西（推理/答案/工具）——决定收尾行写不写：
        # 空回合（模型什么都没说就被打断）打一行 "Cooked for 0s" 是噪音。
        self._produced = False

    # ── 入口 ────────────────────────────────────────────────────

    def apply(self, event: Any) -> None:
        """单个事件 → widget 变更。未知事件静默忽略（向前兼容）。"""
        if isinstance(event, StreamStart):
            self._reset()
            self._t_start = time.monotonic()
            self._app.set_busy("Thinking…")
            self._set_placeholder(BUSY_PLACEHOLDER)
        elif isinstance(event, Reasoning):
            self._on_reasoning(event.text)
        elif isinstance(event, Answer):
            self._on_answer(event.text)
        elif isinstance(event, ToolStarted):
            self._on_tool_started(event)
        elif isinstance(event, ToolFinished):
            self._on_tool_finished(event)
        elif isinstance(event, Status):
            self._app.set_busy(event.label)
        elif isinstance(event, TurnEnd):
            self._finish(event.elapsed, verb=None)
        elif isinstance(event, TurnAborted):
            self._finish(event.elapsed, verb=_ABORT_VERB)
        elif isinstance(event, Note):
            self._app.write(Text(f"  {event.text}", style=DIM))

    # ── 推理 ────────────────────────────────────────────────────

    def _on_reasoning(self, text: str) -> None:
        if not text:
            return
        if self._thinking is None:
            # 惰性 mount：没有推理内容的后端一条控件都不多出
            self._thinking = ThinkingBlock()
            self._app.write_widget(self._thinking)
        self._produced = True
        self._thinking.feed(text)

    def _freeze_thinking(self) -> None:
        """首个非推理内容到达 → 冻结推理阶段（标题改静态 ``Thought for Ns``）。"""
        block = self._thinking
        if block is None or block.frozen:
            return
        block.freeze(max(0.0, time.monotonic() - self._t_start))

    # ── 答案 ────────────────────────────────────────────────────

    def _on_answer(self, text: str) -> None:
        if not text:
            return
        self._freeze_thinking()
        if self._answer is None:
            # 新的一段（回合开头，或上一个工具块之后）：行内 _segments 的
            # text/tool 交替语义在此等价实现。
            self._answer = self._app.write(markdown_answer(""))
            self._answer_text = ""
            self._last_render = 0.0
        self._answer_text += text
        self._produced = True
        now = time.monotonic()
        if now - self._last_render >= _ANSWER_RERENDER_S:
            self._last_render = now
            self._render_answer()
        self._app.set_busy("Answering…")

    def _render_answer(self) -> None:
        if self._answer is None:
            return
        self._answer.update(markdown_answer(self._answer_text))

    def _close_answer(self) -> None:
        """当前文本段结束（工具块插入 / 回合收尾）：补最后一次重渲。"""
        if self._answer is None:
            return
        self._render_answer()
        self._answer = None
        self._answer_text = ""

    # ── 工具块 ──────────────────────────────────────────────────

    def _on_tool_started(self, event: ToolStarted) -> None:
        self._freeze_thinking()
        self._close_answer()
        block = ToolBlock(event.name, event.summary)
        self._app.write_widget(block)
        self._blocks.append(block)
        self._produced = True
        if event.call_id:
            self._by_call_id[event.call_id] = block

    def _on_tool_finished(self, event: ToolFinished) -> None:
        block = self._match(event)
        if block is None:
            # 无匹配（旧后端 / 事件缺失）：兜底建一条已完成块——结果绝不丢
            block = ToolBlock(event.name)
            self._app.write_widget(block)
            self._blocks.append(block)
        block.set_result(event.output, is_error=event.is_error)

    def _match(self, event: ToolFinished) -> Optional[ToolBlock]:
        """结果 → 起始块：``call_id`` 优先，退回同名首个 running（行内语义）。"""
        if event.call_id:
            block = self._by_call_id.pop(event.call_id, None)
            if block is not None:
                return block
        for block in self._blocks:
            if block.tool_name == event.name and block.status == "running":
                return block
        return None

    # ── 收尾 ────────────────────────────────────────────────────

    def _finish(self, elapsed: float, *, verb: Optional[str]) -> None:
        self._freeze_thinking()
        self._close_answer()
        # 空回合（什么都没有就结束/被打断）不收尾：一行 "Cooked for 0s"
        # 只会读成"干了点什么"，而它什么都没干。
        if self._produced:
            self._app.write(done_line(elapsed, verb=verb))
        self._app.set_busy(None)
        self._set_placeholder(IDLE_PLACEHOLDER)

    def _set_placeholder(self, text: str) -> None:
        try:
            self._app.set_placeholder(text)
        except Exception:
            pass  # 输入框未就绪（测试/首帧）→ 提示文案是锦上添花


__all__ = [
    "BUSY_PLACEHOLDER",
    "IDLE_PLACEHOLDER",
    "StreamView",
]
