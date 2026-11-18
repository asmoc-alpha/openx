"""流式回合的显示后端：同一份事件流，落到 widget 而不是 Rich ``Live``。

行内实现的 :class:`openx.services.streaming.StreamingService` 把 token 累积成
Markdown 重渲进一块 ``Live`` 区域，并为此维护一整套"视口预算 + 逐行固化水印
+ 光标算术"（见该模块）。全屏实现里这些全部不需要：转录是一棵 widget 树，
追加一条就是 mount 一个控件。

本模块提供**与 ``StreamingService`` 同面的** :class:`TuiStream`——交互层
（``app/cli/interactive.py``）用它替换行内服务时，接线代码一行不改：

    start() / feed(chunk) / done() / cancel()
    pause() / resume() / pause_capture() / resume_capture()
    set_cancel_target(task) / set_interrupt_notice(cb) / esc_interrupted

线程模型（沿用 ``tui/sink.py`` 的纪律）
------------------------------------
:meth:`TuiStream.feed` **只入队**（``SinkQueue``，有界、线程安全）。真正的
widget 变更发生在 app 的定时器里（见 :class:`openx.tui.view.StreamView`）：
**同一时间只有一个消费者改 widget**。行内实现靠"写屏只在单一消费者"守
同一条不变量（原 ``ui/resize.py``），这里只是把消费者从 Live 刷新线程换成
app 主循环。

Esc 打断
--------
行内由 ``InputCapture`` 的 cbreak 读键线程捕获 Esc；全屏实现里 stdin 归
Textual，于是 Esc 是 app 的 key binding（:meth:`openx.tui.app.OpenXTui
.action_interrupt`），它调 :meth:`TuiStream.interrupt`——语义与行内逐点
一致：单回合闩、只取消登记的子任务、绝不误杀下一回合、``esc_interrupted``
供交互层决定吞掉取消还是上抛。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass
from typing import Any, Optional

from .sink import SinkQueue

# ── 事件（stream → view 的唯一语言）────────────────────────────────


@dataclass
class StreamStart:
    """回合开始：状态行进入 ``Thinking…``，每轮视图状态归零。"""


@dataclass
class Reasoning:
    """推理 token（``StreamReasoning.text`` 的载荷）。"""

    text: str


@dataclass
class Answer:
    """答案 token（模型正文）。"""

    text: str


@dataclass
class ToolStarted:
    """一次工具调用开始。

    ``call_id`` 非空时结果按 id 精确配对（``ToolResultEvent`` 自带）；为空
    （旧 provider / 测试）退回"同名先到先配"，与行内实现同语义。

    ``diff``：文本操作（``write_file`` / ``edit_file``）的变更预览
    ``(path, old, new)``——ToolStart 发生在执行**之前**，所以此刻取到的是
    真实的"变更前"内容，工具块据此渲染彩色 diff（行内 ``record.diff`` 的
    对位）。探测失败/非文本操作 → ``None``（回退纯输出渲染）。
    """

    name: str
    summary: str = ""
    call_id: str = ""
    diff: Optional[tuple[str, str, str]] = None


@dataclass
class ToolFinished:
    name: str
    output: str = ""
    is_error: bool = False
    call_id: str = ""


@dataclass
class Status:
    """状态行文案（``None`` = 清空）。"""

    label: Optional[str]


@dataclass
class TurnEnd:
    """正常收尾：``elapsed`` 秒，写一行 ``✻ Cooked for Ns``。"""

    elapsed: float


@dataclass
class TurnAborted:
    """被打断的收尾：动词固定 ``Interrupted``（随机动词属于"好好干完了"）。"""

    elapsed: float


@dataclass
class Note:
    """转录里的一行 dim 提示（如"消息已排队"）。"""

    text: str


# ── 工具摘要 ──────────────────────────────────────────────────────

#: 参数里最适合做标题摘要的键，按优先级取第一个非空值。
_SUMMARY_KEYS = (
    "description", "file_path", "path", "pattern", "command",
    "query", "url", "prompt", "name",
)


def arg_summary(arguments: str, limit: int = 48) -> str:
    """``arguments``(JSON 串) → 一句人读摘要（失败一律退回截断原文）。

    只做展示：任何解析异常都必须降级成"原文截断"，绝不让畸形参数打断
    回合——与行内渲染层 fail-open 的纪律一致。
    """
    raw = (arguments or "").strip()
    if not raw:
        return ""
    try:
        args = json.loads(raw)
    except (ValueError, TypeError):
        return _clip(raw.replace("\n", " "), limit)
    if not isinstance(args, dict):
        return _clip(raw, limit)
    for key in _SUMMARY_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return _clip(" ".join(value.split()), limit)
    # 无已知键：退回紧凑的 JSON 原文
    return _clip(raw, limit)


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── 显示服务 ──────────────────────────────────────────────────────


class TuiStream:
    """``StreamingService`` 的全屏对位：事件入队，widget 由 app 变更。

    三件事与行内同源：

    - **事件**：``feed()`` 把 chunk 归一成 :class:`Answer` / :class:`Reasoning`
      / :class:`ToolStarted` / :class:`ToolFinished` 入队，``StreamView`` 在
      app 的排空节拍里落成 widget；
    - **状态层（deck）**：``todos_provider`` / ``fleet`` / ``panels`` 在
      ``start()`` 时交给 ``app.attach_decks``——计划、队列、Agents、插件面板
      从此由 app 的 5Hz 定时器渲染（行格式与行内共用 ``ui/_deck.py``）；
    - **舰队生命周期**：构造时 ``fleet.reset()``（与行内 ``StreamingService``
      逐字同位置同理由——实例每轮新建，等于每轮重置）。

    ``tool_lookup`` 仍是接口兼容参数（交互层的构造点与行内共用一份调用），
    TUI 的工具块还没有内联 diff 预览。
    """

    def __init__(
        self,
        console: Any,
        app: Any,
        input_tokens: int = 0,
        todos_provider: Optional[Any] = None,
        fleet: Optional[Any] = None,
        panels: Optional[Any] = None,
        tool_lookup: Optional[Any] = None,
    ) -> None:
        self._console = console
        self._app = app
        # 不能写成 ``getattr(app, "stream_queue", None) or SinkQueue()``：
        # SinkQueue 有 ``__len__``，空队列是**假值**——那样会另建一条没人
        # 排空的队列，事件静默蒸发（widget 永远不出现）。
        queue = getattr(app, "stream_queue", None)
        self._queue: SinkQueue = queue if queue is not None else SinkQueue()
        # 状态层数据源：start() 时交给 app 的 deck 定时器渲染
        self._todos_provider = todos_provider
        self._fleet = fleet
        self._panels = panels
        self._input_tokens = input_tokens
        # 接口兼容参数（见类 docstring）：保留是为了让交互层的构造点无需分叉
        self._tool_lookup = tool_lookup
        # 舰队每轮归零（与行内 ``StreamingService.__init__`` 同位置同理由：
        # 显示服务实例每轮新建，构造即新一轮）。不做这一步，TUI 下 fleet
        # 会跨轮累积——上一轮的子代理留在面板上，永远不下场。
        if self._fleet is not None:
            reset = getattr(self._fleet, "reset", None)
            if callable(reset):
                with contextlib.suppress(Exception):
                    reset()
        self._t0 = 0.0
        self._done = False
        # ── Esc 打断（语义对齐 StreamingService）────────────────────
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._cancel_target: Optional[asyncio.Future] = None
        self._interrupt_notice: Optional[Any] = None
        self._interrupt_fired = False
        self.esc_interrupted = False
        # task 工具起始不落块（结果回显才落）——暂存描述供回显标题
        self._pending_task_desc: list[str] = []
        # 暂停计数：弹窗在 Stage 5 迁移，此处只维持引用计数平衡
        self._pause_count = 0

    # ── public API（与 StreamingService 同面）─────────────────────

    def start(self) -> None:
        """回合开始：复位打断状态、接线 Esc、入队 ``StreamStart``。"""
        self._t0 = time.monotonic()
        self._done = False
        self._interrupt_fired = False
        self.esc_interrupted = False
        self._pending_task_desc = []
        self._pause_count = 0
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None  # 同步调用（测试）→ 打断降级 no-op
        self._app.set_interrupt(self.interrupt)
        # 状态层接线（计划 / 队列 / Agents / 插件面板）：回合结束**不解线**
        # ——计划与插件面板要继续留在屏上，Agents 的显隐由 app 看回合状态。
        attach = getattr(self._app, "attach_decks", None)
        if callable(attach):
            with contextlib.suppress(Exception):
                attach(todos=self._todos_provider, fleet=self._fleet,
                       panels=self._panels)
        self._queue.push(StreamStart())

    def feed(self, chunk: Any) -> None:
        """收到一个 chunk → 归一成事件入队（不触碰任何 widget）。

        chunk 类型与行内一致：``str`` 正文 token、``StreamReasoning`` 推理
        token、``ToolStartEvent`` / ``ToolResultEvent`` 工具事件。未知类型
        静默丢弃（与行内 ``feed`` 的 ``else: return`` 同）。
        """
        from ..agent import ToolResultEvent, ToolStartEvent
        from ..kernel.reasoning.provider import StreamReasoning

        if isinstance(chunk, StreamReasoning):
            self._queue.push(Reasoning(chunk.text or ""))
        elif isinstance(chunk, ToolStartEvent):
            if chunk.name == "task":
                # 起始不落块：子代理运行动态归 Agents 面板（``tui/deck.py``）。
                # 描述暂存供结果回显的标题行——回显是该报告在转录里的唯一
                # 记录。
                self._pending_task_desc.append(arg_summary(chunk.arguments))
            else:
                # 文本操作：执行前探测变更预览（彩色 diff 的来源）。有预览
                # 时标题摘要用路径——比 ``file_path=…`` 的 key=value 清楚。
                preview = self._preview_text_op(chunk.name, chunk.arguments)
                self._queue.push(ToolStarted(
                    name=chunk.name,
                    summary=(preview[0] if preview
                             else arg_summary(chunk.arguments)),
                    call_id=getattr(chunk, "call_id", "") or "",
                    diff=preview,
                ))
        elif isinstance(chunk, ToolResultEvent):
            if chunk.name == "task":
                desc = (self._pending_task_desc.pop(0)
                        if self._pending_task_desc else "")
                self._queue.push(ToolStarted(
                    name="task", summary=desc,
                    call_id=getattr(chunk, "call_id", "") or "",
                ))
                self._queue.push(ToolFinished(
                    name="task", output=chunk.output or "",
                    is_error=chunk.is_error,
                    call_id=getattr(chunk, "call_id", "") or "",
                ))
            else:
                self._queue.push(ToolFinished(
                    name=chunk.name, output=chunk.output or "",
                    is_error=chunk.is_error,
                    call_id=getattr(chunk, "call_id", "") or "",
                ))
        elif isinstance(chunk, str):
            self._queue.push(Answer(chunk))

    def _preview_text_op(self, name: str, arguments: str
                         ) -> Optional[tuple[str, str, str]]:
        """文本操作的变更预览 ``(path, old, new)``（只读、fail-open）。

        与行内 ``StreamingService._preview_text_op`` 逐条同纪律：经
        ``tool_lookup`` 取工具实例、调它自己的 ``preview_diff(args)``
        （base 默认 None，仅 ``write_file`` / ``edit_file`` 覆写，且与
        ``execute`` 的校验语义严格镜像）。无 tool_lookup / 坏 JSON / 探测
        抛错 → None（工具块回退纯输出渲染，绝不因装饰性预览而炸掉回合）。
        """
        lookup = self._tool_lookup
        if lookup is None:
            return None
        try:
            tool = lookup(name)
            if tool is None:
                return None
            preview = getattr(tool, "preview_diff", None)
            if preview is None:
                return None
            try:
                args = json.loads(arguments) if arguments else {}
            except (ValueError, TypeError):
                return None
            if not isinstance(args, dict):
                return None
            result = preview(args)
        except Exception:
            return None
        if not result:
            return None
        try:
            path, old, new = result
        except (TypeError, ValueError):
            return None
        return (str(path), str(old), str(new))

    def done(self) -> float:
        """正常收尾：入队 ``TurnEnd``、解绑 Esc，返回耗时。"""
        elapsed = time.monotonic() - self._t0
        self._done = True
        self._queue.push(TurnEnd(elapsed))
        self._app.set_interrupt(None)
        return elapsed

    def cancel(self) -> None:
        """打断收尾：入队 ``TurnAborted``（保留已产出的部分回答）。"""
        elapsed = time.monotonic() - self._t0
        self._done = True
        self._queue.push(TurnAborted(elapsed))
        self._app.set_interrupt(None)

    # ── 暂停（弹窗钩子占位）───────────────────────────────────────

    def pause(self) -> None:
        """引用计数式暂停（executor 权限钩子与 console 弹窗钩子共用）。

        全屏界面没有需要停的 ``Live`` 重绘线程，也没有 cbreak 读键线程，
        因此这里只维持计数平衡：**接口在、语义空**——弹窗迁移是 Stage 5，
        在那之前 ``--tui`` 配 ``-y`` / auto 模式跑（见文档「限制」）。
        """
        self._pause_count += 1

    def resume(self) -> None:
        if self._pause_count:
            self._pause_count -= 1

    # 行内别名（executor 的权限钩子接的是这两个名字）
    pause_capture = pause
    resume_capture = resume

    # ── Esc 打断 ──────────────────────────────────────────────────

    def set_cancel_target(self, task: Any) -> None:
        """登记 Esc 要取消的**流消费子任务**（交互层在 await 前调用）。"""
        self._cancel_target = task

    def set_interrupt_notice(self, callback: Optional[Any]) -> None:
        """登记 Esc 的附加通知（容灾把 checkpoint 的 reason 记成 ``esc``）。"""
        self._interrupt_notice = callback

    def interrupt(self) -> None:
        """Esc：取消登记的消费子任务 → 打断当前回合。

        与行内 ``StreamingService._interrupt`` 逐点对齐：
        - 单回合闩，连按只触发一次；
        - 只取消登记的**子任务**，主任务不受影响（子任务已完成后
          ``cancel()`` 即 no-op，回合结束后的误按不会波及下一回合）；
        - 取消经 ``call_soon_threadsafe`` 投递——即便未来从其它线程调用，
          真正的取消动作仍落在事件循环线程；
        - 通知回调异常一律吞掉（通知是锦上添花，打断本身不能因为它失败）。
        """
        if self._interrupt_fired:
            return
        self._interrupt_fired = True
        self.esc_interrupted = True
        notice = self._interrupt_notice
        if notice is not None:
            try:
                notice()
            except Exception:
                pass
        task = self._cancel_target
        loop = self._loop
        if task is not None and loop is not None and not task.done():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except (RuntimeError, AttributeError):
                pass  # loop 已关闭 → 降级 no-op

    # 行内捕获线程入口的同名对位（测试与兼容调用方）
    _interrupt = interrupt


__all__ = [
    "Answer",
    "Note",
    "Reasoning",
    "Status",
    "StreamStart",
    "ToolFinished",
    "ToolStarted",
    "TuiStream",
    "TurnAborted",
    "TurnEnd",
    "arg_summary",
]
