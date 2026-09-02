"""Fleet monitor — 并行子代理的运行时可见性（v0.4.0 状态层）。

每个经 ``task`` 工具 / 工作流派生的子代理在此登记一个
:class:`SubagentView`：子代理的流事件（文本 token + ToolStart/
ToolResultEvent）喂进它的内存行缓冲，``StreamingService`` 的 5Hz
刷新线程经 :meth:`FleetMonitor.snapshot` 取一次性快照渲染状态层
（输入框上方的 deck）与 Ctrl-O 切换的详情视图。

线程模型（不变量，改动前必读）
================================
- **生产者**全部在事件循环线程：并行子代理是多个 asyncio 任务，但
  只在 ``await`` 处交错，``feed`` 本身同步 → 单 view 内无并发写；
- **消费者**是 Live 自动刷新线程（5Hz）：经 ``snapshot()`` 取深拷贝，
  之后渲染只读快照——**绝不跨线程迭代活对象**；
- **锁序**：monitor 锁 → view 锁（仅 snapshot 嵌套）；生产者只用
  view 锁，从不持 monitor 锁做嵌套操作。与 ``Live._lock → console._lock``
  无交集（本模块锁只在 _build_renderable 内、Live._lock 之下被获取，
  生产者永不触碰 Live）；
- 本模块**零终端 I/O**：只存数据，渲染归 StreamingService。
"""

from __future__ import annotations

# ── 独立调试支持：允许直接运行本文件（python openx/.../xxx.py）──────
if __name__ == "__main__" and not __package__:
    import sys as _sys
    from pathlib import Path as _Path
    _file = _Path(__file__).resolve()
    _root = _file.parent
    while _root != _root.parent and not (_root / "pyproject.toml").exists():
        _root = _root.parent
    _sys.path.insert(0, str(_root))
    __package__ = ".".join(_file.relative_to(_root).parts[:-1])

import json
import threading
import time
from collections import deque
from typing import Any, Optional

# 单个子代理视图保留的最大行数：环形缓冲，超长自动丢最旧。
MAX_VIEW_LINES = 200

# 子代理详情视图的工具结果展示上限（format_stream_event 用）：超出 →
# 头部截断 + 剩余行数提示。详情视图本就是"展开看更多"之处，比主转录
# 的 3 行折叠宽。
_RESULT_MAX_LINES = 10
_RESULT_MAX_CHARS = 1200


def _tool_call_summary(name: str, arguments: str) -> str:
    """工具头部一行命令摘要（Claude Code ``● Bash(cmd)`` 风格）。

    shell 展示命令本身；其他工具展示紧凑 key=value（值超长按字符截
    断）。解析失败 → 空串（头部回落纯工具名，绝不抛错）。
    """
    try:
        args = json.loads(arguments) if arguments else {}
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(args, dict) or not args:
        return ""
    if name == "shell":
        cmd = args.get("command")
        if isinstance(cmd, str) and cmd.strip():
            return " ".join(cmd.split())[:120]
    parts = []
    for k, v in list(args.items())[:3]:
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        s = " ".join(s.split())
        if len(s) > 60:
            s = s[:50] + "…"
        parts.append(f"{k}={s}")
    return " ".join(parts)[:160]


def _format_tool_result(output: str, is_error: bool) -> str:
    """工具结果 → ⎿ 槽线块字符串（子代理详情视图用，逐行
    Text.from_markup 渲染，markup 颜色保留）。

    Claude Code 风格：首行 ``⎿`` 槽线 + 续行 5 空格对齐；超限头部截
    断 + ``... (N more lines)`` 提示；错误首行冠红色 ``✕``。输出行经
    markup 转义（防输出含方括号触发 MarkupError）。
    """
    from rich.markup import escape
    lines = output.rstrip("\n").splitlines() or [""]
    body: list[str] = []
    chars = 0
    for i, ln in enumerate(lines):
        if (len(body) >= _RESULT_MAX_LINES
                or chars + len(ln) > _RESULT_MAX_CHARS):
            body.append(f"[dim]... ({len(lines) - len(body)} more lines)[/]")
            break
        if is_error and i == 0:
            body.append(f"[red]✕[/] {escape(ln)}")
        else:
            body.append(escape(ln))
        chars += len(ln) + 1
    parts = []
    for i, ln in enumerate(body):
        prefix = f"[dim]  {chr(0x23bf)}  [/]" if i == 0 else "[dim]     [/]"
        parts.append(prefix + ln)
    return "\n\n" + "\n".join(parts) + "\n\n"


def format_stream_event(event: Any) -> Optional[str]:
    """stream_run 事件 → 展示串（**子代理详情视图**的格式源）。

    主转录已改结构化段序列（StreamingService._tool_renderables 独立
    渲染，不经本函数）；本函数仍为子代理详情视图（_detail_view 逐行
    Text.from_markup）供格式：

    - ``str`` token 原样返回；
    - ``ToolStartEvent`` → ``"\\n\\n[dim]● {name}[/dim][dim]({摘要})[/dim]\\n"``
      （Claude Code 风格头行 ``● shell(ls -la)``）；
    - ``ToolResultEvent`` → ⎿ 槽线块（见 :func:`_format_tool_result`：
      10 行/1200 字符截断 + 提示；错误首行红色 ``✕``）；空输出 → ``None``。

    事件类**延迟导入**：agent 模块反过来导入 core 包，顶层导入成环
    （与 StreamingService.feed 同款手法）。
    """
    from ..agent import ToolStartEvent, ToolResultEvent

    if isinstance(event, str):
        return event
    if isinstance(event, ToolStartEvent):
        summary = _tool_call_summary(event.name, event.arguments)
        header = f"\n\n[dim]● {event.name}[/dim]"
        if summary:
            header += f"[dim]({summary})[/dim]"
        return header + "\n"
    if isinstance(event, ToolResultEvent):
        if not event.output:
            return None  # 空输出不占 transcript
        return _format_tool_result(event.output, event.is_error)
    # 未知事件类型（如未来新增）：宽容忽略，绝不抛出拖垮刷新线程
    return None


class SubagentView:
    """一个子代理运行实例的可见状态 + 事件行缓冲。

    ``feed(event)`` 由事件循环线程调用（同步）；``snapshot`` 数据由
    Live 刷新线程经 :meth:`FleetMonitor.snapshot` 在 view 锁内拷贝。
    """

    def __init__(self, view_id: int, label: str, subagent_type: str) -> None:
        self.id = view_id
        self.label = label
        self.subagent_type = subagent_type
        self.started_at = time.monotonic()
        self.status = "running"     # running | done | error
        self.tools_count = 0        # ToolStartEvent 计数（活跃度指标）
        self.finished = False       # complete 幂等闩
        self.lines: deque = deque(maxlen=MAX_VIEW_LINES)
        self._pending = ""          # 未遇 \n 的尾片段（详情视图一并展示）
        self._lock = threading.Lock()

    def feed(self, event: Any) -> None:
        """喂入一个流事件（str token 或工具事件）。同步、线程安全。"""
        from ..agent import ToolStartEvent

        if isinstance(event, ToolStartEvent):
            with self._lock:
                self.tools_count += 1
        formatted = format_stream_event(event)
        if not formatted:
            return
        # 按 \n 切成完整行进缓冲；尾巴留在 _pending 等下一片段
        with self._lock:
            self._pending += formatted
            while "\n" in self._pending:
                line, self._pending = self._pending.split("\n", 1)
                self.lines.append(line)

    def snapshot(self, now: float) -> dict:
        """view 锁内取深拷贝（调用方已可持有 monitor 锁——锁序允许）。"""
        with self._lock:
            return {
                "id": self.id,
                "label": self.label,
                "subagent_type": self.subagent_type,
                "status": self.status,
                "tools_count": self.tools_count,
                "elapsed": now - self.started_at,
                "lines": list(self.lines),
                "pending": self._pending,
            }


class FleetMonitor:
    """一轮对话内全部子代理视图的登记簿（挂在父 agent 上）。

    ``register`` / ``complete`` / ``reset`` 由事件循环线程调用；
    ``snapshot`` 由 Live 刷新线程每帧调用一次。
    """

    def __init__(self) -> None:
        self._views: list[SubagentView] = []
        self._lock = threading.Lock()
        self._next_id = 0

    def register(self, label: str, subagent_type: str = "general-purpose") -> SubagentView:
        """登记一个新子代理视图并返回（调用方负责 feed 与 complete）。"""
        with self._lock:
            self._next_id += 1
            view = SubagentView(
                self._next_id, label or subagent_type, subagent_type
            )
            self._views.append(view)
            return view

    def complete(self, view: SubagentView, is_error: bool = False) -> None:
        """标记视图结束。**幂等**：重复调用（finally 兜底）无效。"""
        with view._lock:
            if view.finished:
                return
            view.finished = True
            view.status = "error" if is_error else "done"

    def reset(self) -> None:
        """清空全部视图——每轮对话开始时调用（StreamingService.start）。"""
        with self._lock:
            self._views.clear()

    def running_count(self) -> int:
        with self._lock:
            return sum(1 for v in self._views if not v.finished)

    def snapshot(self) -> list[dict]:
        """全部视图的一致性快照（注册顺序）。锁序：monitor → view。"""
        with self._lock:
            views = list(self._views)
        now = time.monotonic()
        return [v.snapshot(now) for v in views]


if __name__ == "__main__":
    from ..agent import ToolStartEvent, ToolResultEvent

    # ── format_stream_event：⎿ 风格（子代理详情视图格式源）──
    assert format_stream_event("tok ") == "tok "
    assert format_stream_event(ToolStartEvent(name="read_file")) == \
        "\n\n[dim]● read_file[/dim]\n"
    assert format_stream_event(
        ToolStartEvent(name="shell", arguments='{"command": "ls"}')
    ) == "\n\n[dim]● shell[/dim][dim](ls)[/dim]\n"
    assert format_stream_event(
        ToolResultEvent(name="x", output="boom", is_error=True)
    ) == "\n\n[dim]  ⎿  [/][red]✕[/] boom\n\n"
    long_out = "y" * 600
    assert format_stream_event(
        ToolResultEvent(name="x", output=long_out, is_error=False)
    ) == f"\n\n[dim]  ⎿  [/]{'y' * 600}\n\n"
    assert format_stream_event(
        ToolResultEvent(name="x", output="", is_error=False)
    ) is None

    # ── register / feed / complete / reset ──
    mon = FleetMonitor()
    v1 = mon.register("find auth code", "explore")
    v2 = mon.register("review tests")
    assert v1.id != v2.id and v2.subagent_type == "general-purpose"
    assert mon.running_count() == 2

    v1.feed("partial ")          # 无 \n → 留在 pending
    v1.feed(ToolStartEvent(name="grep"))
    v1.feed("tail\nsecond line\n")
    snap = mon.snapshot()
    s1 = snap[0]
    # 工具事件自带首尾 \n → "partial " 独立成行、空行、dim 指示行
    assert s1["lines"] == [
        "partial ", "", "[dim]● grep[/dim]", "tail", "second line",
    ]
    assert s1["pending"] == ""
    assert s1["tools_count"] == 1
    assert s1["label"] == "find auth code" and s1["status"] == "running"

    # 未闭合尾片段留在 pending（详情视图会一并展示）
    v1.feed("no-newline-yet")
    assert mon.snapshot()[0]["pending"] == "no-newline-yet"

    # 幂等 complete
    mon.complete(v1)
    mon.complete(v1, is_error=True)  # 第二次无效
    assert v1.status == "done"
    assert mon.running_count() == 1

    # snapshot 隔离性：快照后再喂不影响已取快照
    before = mon.snapshot()
    v2.feed("more\n")
    assert before[1]["lines"] == []

    # 200 行封顶（环形丢最旧）
    v3 = mon.register("flood")
    for i in range(250):
        v3.feed(f"line{i}\n")
    s3 = [s for s in mon.snapshot() if s["id"] == v3.id][0]
    assert len(s3["lines"]) == MAX_VIEW_LINES
    assert s3["lines"][0] == "line50" and s3["lines"][-1] == "line249"

    mon.reset()
    assert mon.snapshot() == [] and mon.running_count() == 0
    print("openx/orchestration/fleet.py OK ✓")
