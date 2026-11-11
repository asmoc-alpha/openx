"""输出缝（sink）：``Console._console.print`` 的去向抽象。

行内实现在等 `openx --tui` 时把 ``Console._console`` 从 Rich ``Console``
换成 :class:`TuiSink`：同一批 ``console.print_*`` 调用（89 处）不再写
stdout，而是投递成 transcript 里的 widget。**渲染层代码零改动**——Rich
renderable 原样进 ``Static``。

线程模型
--------
:meth:`TuiSink.print` **只入队**（线程安全、有界、溢出丢最旧并计数），
由 app 的定时器在主事件循环里排空并 mount widget。工具线程 / REPL 协程
永不直接触碰 widget——沿用行内实现里"写屏只在单一消费者"的纪律
（原 ``ui/resize.py`` 的不变量，TUI 版换成"改 widget 只在 app 线程"）。

量测面
------
``render_lines`` / ``capture`` / ``width`` / ``height`` 仍需要 Rich 的能力
（diff 着色、行数估算、宽度换算都在用它）。:class:`TuiSink` 持有一个
**file 指向 StringIO 的量测 Console**：任何漏网的直接写调用都会落进
缓冲区而不是终端——TUI 界面永不被内部代码弄脏。
"""

from __future__ import annotations

import io
import threading
from collections import deque
from typing import Any

from rich.console import Console as RichConsole
from rich.console import Group


class SinkQueue:
    """线程安全的有界投递队列（溢出丢最旧 + 计数，绝不阻塞调用方）。"""

    def __init__(self, limit: int = 4000) -> None:
        self._items: deque = deque()
        self._lock = threading.Lock()
        self._limit = limit
        #: 因溢出被丢弃的条目数（永不静默：测试/诊断可读）
        self.dropped = 0

    def push(self, item: Any) -> None:
        with self._lock:
            if len(self._items) >= self._limit:
                self._items.popleft()
                self.dropped += 1
            self._items.append(item)

    def drain(self) -> list[Any]:
        with self._lock:
            items = list(self._items)
            self._items.clear()
            return items

    def __len__(self) -> int:  # pragma: no cover - 诊断用
        with self._lock:
            return len(self._items)


class RichSink:
    """默认 sink：一切透传给 Rich Console（行内路径，行为逐字节不变）。"""

    def __init__(self, console: RichConsole) -> None:
        self._console = console

    def __getattr__(self, name: str) -> Any:
        return getattr(self._console, name)

    def print(self, *objects: Any, **kwargs: Any) -> None:
        self._console.print(*objects, **kwargs)


class TuiSink:
    """TUI sink：``print`` → 队列 → transcript；量测面走内部 Console。"""

    def __init__(self, console: RichConsole, queue: SinkQueue) -> None:
        # 量测 Console：保留原 console 的样式/高亮配置，但 file 换成
        # StringIO——漏网写调用只会落进缓冲区，绝不至于打花 TUI 界面。
        self._measure = RichConsole(
            file=io.StringIO(),
            width=console.width or 80,
            height=console.height or 24,
            # rich 的 highlight/soft_wrap 是私有/半私有属性：取不到就退回
            # 默认值——量测只关心宽度与样式解析，不该因版本差异炸掉。
            highlight=getattr(console, "_highlight", True),
            soft_wrap=getattr(console, "soft_wrap", False),
            legacy_windows=False,
        )
        self.queue = queue
        # 兼容 ``console.raw.file = sys.stderr``（single_shot 路径的写法）：
        # 属性可赋值，但 TUI 模式下它不承载任何真实输出。
        self.file = self._measure.file

    # ── 量测面（调用方照旧用）────────────────────────────────────

    @property
    def width(self) -> int:
        return self._measure.width

    def update_size(self, width: int, height: int) -> None:
        """跟随 app 的实际尺寸（resize 时由 app 调用）。"""
        if width > 0:
            self._measure.width = width
        if height > 0:
            self._measure.height = height

    @property
    def height(self) -> int:
        return self._measure.height

    def render_lines(self, renderable: Any, pad: bool = False) -> list:
        return self._measure.render_lines(renderable, pad=pad)

    def capture(self):  # 返回 Rich 的上下文管理器
        return self._measure.capture()

    def __getattr__(self, name: str) -> Any:
        # 兜底委托：量测 Console 的 file 是 StringIO，故这条路径**不可能**
        # 写进终端（例如 menu_entries 之外某处误调 sink.rule()）。
        return getattr(self._measure, name)

    # ── 输出面 ──────────────────────────────────────────────────

    def print(self, *objects: Any, **kwargs: Any) -> None:
        """投递一个 renderable（布局类 kwargs 在 TUI 下无意义，忽略）。"""
        self.queue.push(_coalesce(objects))


def _coalesce(objects: tuple) -> Any:
    """``print()`` 的入参 → 单个 Rich renderable（空调用 = 一个空行）。"""
    if not objects:
        return ""
    if len(objects) == 1:
        return objects[0]
    return Group(*objects)


def make_measure_console(width: int = 80, height: int = 24) -> RichConsole:
    """独立量测 Console（无副作用）：测试与离线自检用。"""
    return RichConsole(
        file=io.StringIO(), width=width, height=height, legacy_windows=False
    )


__all__ = ["RichSink", "SinkQueue", "TuiSink", "make_measure_console"]
