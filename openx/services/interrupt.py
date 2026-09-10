"""中断控制器——把"用户/系统要停下"翻译成**可恢复的**取消。

四类中断汇入同一条路径（统一意味着只有一个地方需要推理正确）：

| 来源 | 入口 | 落盘 reason |
|---|---|---|
| Ctrl-C | ``SIGINT``（链回 ``KeyboardInterrupt``） | ``signal`` |
| ``kill`` | ``SIGTERM``（链回 ``SIG_DFL``） | ``signal`` |
| Esc（REPL） | ``StreamingService._interrupt`` → ``note`` | ``esc`` |
| 客户端打断（Web） | ``ServeSession.interrupt`` → ``request`` | ``esc`` |

**本控制器对信号只做加法，不改任何一条既有退出路径。** 信号处理器只做
两件事：登记来源，然后把控制权**交回原处理器**。于是：

- Ctrl-C 仍然是 ``KeyboardInterrupt``，REPL 的清理（Live/termios 恢复 +
  goodbye）逐字不变；
- SIGTERM 仍是默认终止，进程行为与今天一致。

**这里的顺序纪律与直觉相反，值得说清**：不是"信号来了先落盘"，而是
--**落盘早就发生了**。每个工具轮结束都提交过 checkpoint，所以 ``kill -9``、
断电、SIGTERM 全都能从最近一个已提交的轮次恢复，且不重放已完成的调用。
信号路径需要补的只是"为什么停下"这条因果（``interrupt`` 事件），以及
把当前**在途**轮次的最佳努力快照写下来--后者由取消路径调用
``CheckpointManager.flush_current()`` 完成，**不在信号处理器里**：
信号处理器运行在主线程的任意两条字节码之间，可能打断一次正在进行的
文件写入或列表变更，在那里做 IO 是不安全的。

**为什么没有"二次信号升级/强制退出"**：直觉会想加一个"第二次 Ctrl-C 就
``os._exit``"，但这里既无必要也有害。无必要，是因为信号处理器不做 IO，
没有可能卡住的落盘流程（真正会写盘的是退出路径里的 ``flush_current``，
而它一旦被打断，第二次 Ctrl-C 会自然地在那个 ``except`` 块里再抛一次
``KeyboardInterrupt``，进程照常退出）；有害，是因为同一套升级逻辑若被
客户端的重复 Esc 触发，就会把整个 web 服务 ``os._exit`` 掉。宁可少一层
机制，也不要一个能让服务端失控的开关。
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
from enum import Enum
from typing import Any, Optional

_log = logging.getLogger("openx")


class InterruptKind(str, Enum):
    """中断来源。取值同时是账本 ``interrupt`` 事件的 ``kind`` 字段。"""

    SIGINT = "sigint"
    SIGTERM = "sigterm"
    ESC = "esc"
    CLIENT = "client"


class InterruptController:
    """单进程的中断协调器：登记来源 → 取消 → 由取消侧落盘。

    与 ``StreamingService.set_cancel_target`` 同款接线：控制器不认识 agent，
    只持有一个"当前回合任务"的句柄，取消它即可。任务被取消后，``agent``
    的取消处理分支负责取景并调用 ``CheckpointManager.flush_current()``。
    """

    def __init__(self, *, console: Any = None) -> None:
        self._console = console
        self._turn_task: Optional["asyncio.Task[Any]"] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._pending: Optional[InterruptKind] = None
        self._count = 0
        self._installed: list[int] = []       # 装过的信号
        self._previous: dict[int, Any] = {}   # 原处理器（链式交回用）

    # ── 探测 ────────────────────────────────────────────────

    @property
    def pending(self) -> Optional[InterruptKind]:
        """当前待处理的中断（None = 本回合没人打断）。"""
        return self._pending

    @property
    def requested(self) -> bool:
        """本回合是否收到过任何中断。"""
        return self._count > 0

    def kind_name(self, default: str = "signal") -> str:
        """待处理中断的种类名（供 ``flush_current(kind)`` 直接使用）。"""
        return self._pending.value if self._pending is not None else default

    # ── 接线 ────────────────────────────────────────────────

    def set_turn_task(self, task: Optional["asyncio.Task[Any]"]) -> None:
        """登记当前回合任务（取消的目标）。回合开始/结束都要重设。"""
        self._turn_task = task
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def clear(self) -> None:
        """回合收尾：清掉本回合的中断状态（下一次中断重新计数）。"""
        self._turn_task = None
        self._pending = None
        self._count = 0

    # ── 信号安装 ────────────────────────────────────────────

    def install_signals(self) -> bool:
        """安装 SIGINT/SIGTERM 处理器。成功返回 True。

        **只在主线程可用**（``signal.signal`` 的硬约束，同 ``ui/resize.py``
        的 SIGWINCH）。装不上就安静返回 False--单发/嵌入式用法本来也不需要。
        """
        if threading.current_thread() is not threading.main_thread():
            _log.debug("signal install skipped: not the main thread")
            return False
        installed = False
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous[sig] = signal.getsignal(sig)
                signal.signal(sig, self._handle_signal)
                self._installed.append(sig)
                installed = True
            except (ValueError, OSError, RuntimeError):
                _log.debug("signal %s install failed", sig, exc_info=True)
        return installed

    def disarm(self) -> None:
        """还原原处理器（退出时调用；best-effort，绝不抛）。"""
        for sig in list(self._installed):
            try:
                signal.signal(sig, self._previous.get(sig, signal.SIG_DFL))
            except (ValueError, OSError, RuntimeError, TypeError):
                pass
        self._installed.clear()
        self._previous.clear()

    # ── 中断请求 ────────────────────────────────────────────

    def note(self, kind: str | InterruptKind) -> None:
        """只**登记来源**、不发起取消（取消已由调用方完成）。

        Esc 路径用：``StreamingService._interrupt`` 已经用
        ``call_soon_threadsafe(task.cancel)`` 取消了回合任务，控制器不必
        再取消一次；这里只让取消处理分支知道"是 Esc 打断的"，好把
        ``reason`` 记成 ``esc`` 而不是含糊的 ``signal``。
        """
        if self._count == 0:
            self._pending = InterruptKind(kind) if isinstance(kind, str) else kind
        self._count += 1

    def request(self, kind: str | InterruptKind) -> None:
        """登记来源**并**取消当前回合任务（serve 的客户端打断用）。

        线程安全：只改 Python 标量 + 一次 ``call_soon_threadsafe``，可由
        信号处理器直接调用。**绝不碰 agent 状态、绝不写文件。**
        """
        self.note(kind)
        self._cancel_turn()

    def _handle_signal(self, signum: int, frame: Any) -> None:
        """信号处理器：登记来源，然后把控制权交回原处理器。

        ``InterruptKind`` 由信号编号映射；未知编号按 SIGINT 处理（保守：
        当作"用户想停下"）。
        """
        kind = (
            InterruptKind.SIGTERM
            if signum == getattr(signal, "SIGTERM", None)
            else InterruptKind.SIGINT
        )
        self.note(kind)
        self._chain_previous(signum, frame)

    def _chain_previous(self, signum: int, frame: Any) -> None:
        """把信号交回原处理器（尽力而为；失败则退回默认行为）。"""
        previous = self._previous.get(signum)
        if callable(previous):
            try:
                previous(signum, frame)
                return
            except Exception:
                _log.debug("previous signal handler failed", exc_info=True)
        # SIG_DFL / SIG_IGN 或原处理器异常：还原默认并重发，不吞掉信号。
        try:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
        except (ValueError, OSError, RuntimeError):
            _log.debug("signal re-raise failed", exc_info=True)

    def _cancel_turn(self) -> None:
        """取消当前回合任务。

        优先走 ``loop.call_soon_threadsafe``（线程安全）；没有事件循环时
        退回直接 ``cancel()``。
        """
        task = self._turn_task
        if task is None or task.done():
            return
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(task.cancel)
                return
            except RuntimeError:
                pass
        try:
            task.cancel()
        except RuntimeError:
            _log.debug("turn task cancel failed", exc_info=True)
