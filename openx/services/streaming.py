"""Live streaming display with Markdown, a trailing frame, and queued input.

The accumulated response is re-rendered as Markdown via a Rich ``Live``
region whose group is ``[response] + [animated status] + [input frame]``.
The frame sits at the bottom and is pushed downward as the response grows,
so it stays visible and continuously moves down while the model answers.

While streaming, :class:`~openx.ui.input_capture.InputCapture` reads
keystrokes concurrently (cbreak mode, no echo) and the frame renders the
in-progress line — so the user can type follow-up messages that are queued
and sent after the current answer finishes.  The capture is always stopped
and the terminal restored (``stop`` is idempotent and guarded).
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

import asyncio
import json
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from rich.console import Group
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.text import Text

from ..ui.input_capture import InputCapture
from ..ui._style import (
    ACCENT, ACCENT_BOLD, DIM, ERROR_STYLE, MARK_BULLET, MARK_FAIL, MARK_INFO,
    MARK_OK, MARK_PENDING, SUCCESS_STYLE,
)

# Braille dots cycle every 80 ms for a smooth spin.
_SPIN = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

# 扫光（shimmer）：spinner 标签上的移动高亮窗。参考 OpenClaw
# src/tui/tui-waiting.ts 的 shimmerText——亮窗逐字扫过状态文本
# （窗内 bold+强调色，窗外 dim），静止色相 + 动态字重传达"进行中"，
# 与 v0.5.0 克制设计语言一致（层次靠字重不靠色相）。窗口位置是
# elapsed 的纯函数 → 测试确定；只改样式不改文字 → pyte 帧 diff、
# plain 断言与单行不变量全不受影响。
_SHIMMER_WIDTH = 4        # 高亮窗宽（字符数）
_SHIMMER_STEP_S = 0.15    # 窗口每 150ms 前进一字（9 字标签扫一轮 ≈2s）

# Live 自动刷新频率。原 10Hz 每 100ms 全区重写一次（光标上移 N 行 +
# 重写 N 行，Rich Live 无逐行 diff）——响应超一屏时重写区≈整屏，是
# 翻页闪烁的首要来源。降到 5Hz：spinner（80ms 源帧）在 200ms 采样下
# 仍连续动画、0.1s 精度的计时显示仍逐秒跳动，而重写频率减半。
# Live auto-refresh rate: 10 Hz rewrote the whole (near full-screen)
# region every 100 ms — the main flicker source once an answer scrolls
# past one screen. 5 Hz still animates the spinner and ticks the timer,
# at half the rewrite traffic.
_REFRESH_PER_SECOND = 5

# feed() 强制刷新的最小间隔。token 爆发（每几十毫秒一个）曾每 3 个
# token 立即重绘一次，在自动刷新节拍之上叠加整区重写、放大闪烁。
# 改为时间门控：仅当距上次强制刷新 ≥ 本间隔才立即重绘，其余交给 5Hz
# 自动节拍（更新延迟 ≤200ms，不可感知）。首个 token 必立即刷新
# （初值 0.0）。
# Minimum gap between feed()-triggered refreshes; burst tokens fall
# back to the 5 Hz tick (≤200 ms latency, imperceptible).
_MIN_FORCE_REFRESH = 0.2

# 渲染响应窗口时为组内"非响应"部分预留的行数：4 行输入框 + 1 行 spinner
# + 2 行余量。余量让整组始终低于视口底边两行：Rich Live 的相对光标计算
# 对"顶满视口"的渲染区极其敏感（SDD §8 记录的 Rich 已知抖动局限），
# 留出余量后重渲永不触底、永不滚屏。
# Rows reserved for frame (4) + spinner (1) + slack (2): keep the whole
# group below the viewport bottom so Rich's relative cursor math never
# has to cope with a region touching the screen edge (known jitter).
_VIEWPORT_RESERVE = 7

# running 工具长输出裁尾时易变区首行的"上文在易变区外"标记
# （固化行在 scrollback，终端原生上翻可见，无需标记）。
_SCROLL_MARKER = "↑ …"

# 状态层（deck，frame 之上）的行数上限：计划面板最多 _DECK_PLAN_ROWS
# 条 todos、队列面板最多 _DECK_QUEUE_ROWS 条待发提示、舰队最多
# _DECK_FLEET_ROWS 个子代理，超出折叠成 "+N more"。
# 实际渲染还会被视口预算（height - _VIEWPORT_RESERVE - 5）二次裁剪，
# 保证 max_lines ≥ 5——deck 永不把响应窗口挤没、永不撑爆视口。
_DECK_PLAN_ROWS = 6
_DECK_QUEUE_ROWS = 4
_DECK_FLEET_ROWS = 4

# Rich markup tags the agent yields (e.g. "[dim]● tool[/dim]"); stripped
# before Markdown() which would otherwise render them literally. 只作用于
# 模型文本段——工具块走独立渲染（Text.from_markup），颜色不经此剥离。
_RICH_TAG = re.compile(
    r"\[/(?:dim|red|green|yellow|blue|cyan|magenta|white|bold|italic|underline)\]"
    r"|\[(?:dim|red|green|yellow|blue|cyan|magenta|white|bold|italic|underline)"
    r"(?:\s+[^\]]*)?\]"
)

# ── 工具块结构化渲染（Claude Code 风格）──────────────────────────
# 结果截断：折叠态 3 行 + "… +N lines (ctrl+t to expand)"；错误 10 行。
# 展开态（Ctrl+T 全局开关）硬上限 200 行防巨块撑爆。
_RESULT_MAX_LINES = 3
_ERROR_MAX_LINES = 10
_EXPAND_HARD_CAP = 200
_GUTTER = "⎿"    # Claude Code 结果槽线符号（U+23BF）


@dataclass
class _ToolRecord:
    """一次工具调用的结构化记录（主转录工具段的载荷）。

    结构化渲染的核心：工具块**绕过 Markdown** 独立渲染（Text.from_markup），
    状态点红/绿、⎿ 槽线、diff 着色等颜色语义因此可行——单个字符串缓冲
    整体过 Markdown 时一切标签都被 _RICH_TAG 剥净，颜色不可能显示。
    """

    name: str
    arguments: str = ""
    status: str = "running"      # running / done / error
    output: str = ""
    is_error: bool = False


def _extract_task_desc(arguments: str) -> str:
    """task 工具 arguments 里的 description（结果回显头行用）。"""
    try:
        args = json.loads(arguments) if arguments else {}
    except (ValueError, TypeError):
        return ""
    if isinstance(args, dict):
        return str(args.get("description") or "")
    return ""


class _LiveView:
    """每次刷新都重建流式视图的 Rich 渲染对象。

    Rich ``Live`` 的自动刷新线程约每秒重绘 5 次（`_REFRESH_PER_SECOND`），每次都会调用这里的
    ``__rich_console__``——因此即便模型还在思考、尚未吐出任何 token，
    耗时 spinner 也会持续走动（这正是"实时计时"的关键）。

    若某次构建视图抛异常（如终端状态切换的瞬间），退回上一次成功的视图，
    保证刷新线程不会悄悄退出、导致计时重新卡死。
    """

    def __init__(self, service: "StreamingService") -> None:
        self._svc = service

    def __rich_console__(self, console, options):
        try:
            view = self._svc._build_renderable()
        except Exception:
            view = self._svc._last_view
        self._svc._last_view = view
        yield view


class _ResizeAwareLive(Live):
    """终端 resize 后立即擦区重锚、按新尺寸就地重渲的 Live。

    rich 14/15 的原生刷新是"移到区域顶（按记忆的形状上移 h-1）→ 逐行
    ``\\033[2K`` 擦除旧区 → 重渲"（position_cursor = CR + 2K + (UP,2K)×h−1，
    无 ``\\033[J``）——宽度**加宽**时区域各行以真 ``\\n`` 结尾
    （硬换行，reflow 终端不合并不拆分）→ 旧 h 精确，原生刷新即正确；
    **缩窄** reflow 终端拆分超宽硬行使物理区高 h′ > h → 原生上移欠
    (h′−h) 行，在新帧之上留有界装饰残带（随滚动消失）——任何不查询
    光标绝对位置（DSR，已与捕获线程 select 竞争而否决）的方案都无法
    消除，属已知局限（SDD §8）。**任何终端类下上移绝不越界吞对话**。

    本覆写的增量价值：(1) 信号事件/宽度漂移触发**即时**擦区重渲，
    不必等下一个自动刷新节拍；(2) 擦区 + 重渲包在 ``console._lock``
    内与其他 console 写入原子化；(3) 不依赖 rich 内部恢复序列的具体
    实现（旧版 rich 逐行 ``\\033[2K``，行为更差）。

    线程安全：``Live._lock`` 是 RLock——自动刷新线程持锁虚调
    ``refresh``（覆写生效），feed()/done() 的强制刷新同样串行。
    锁序 ``Live._lock → console._lock`` 与 Rich 内部一致（反向死锁）。
    写 ``console.file``（Rich 经 ``rich_proxied_file`` 解包 FileProxy，
    redirect_stdout 期间仍是真 stdout——**不**写 sys.stdout，那会经
    AnsiDecoder 重入渲染钩子）。
    """

    def __init__(self, *args, resize=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._resize = resize  # ResizeWatcher | None（测试 SimpleNamespace console）
        # 终端宽度漂移兜底的自跟踪基线：上次渲染所见终端列宽。
        # （不能再用 shape[0]：易变区宽 < 终端宽，逐帧误判为 resize。）
        self._last_term_width: Optional[int] = None

    def refresh(self) -> None:
        svc = getattr(self._renderable, "_svc", None)
        with self._lock:  # RLock：刷新线程已持锁（可重入），feed/done 串行
            shape = self._live_render._shape
            resized = self._resize.check() if self._resize is not None else False
            # 宽度漂移兜底：以"当前终端列宽 vs 上次渲染所见"判定——易变区
            # 可能比终端窄（shape[0] 是区宽不是终端宽），原 shape 比对在
            # funnel 下会逐帧误判为 resize、吞掉 _maybe_commit。
            try:
                tw = self.console.width or shutil.get_terminal_size(
                    (80, 24)).columns
            except Exception:
                tw = shutil.get_terminal_size((80, 24)).columns
            if self._last_term_width is None:
                self._last_term_width = tw
            drift = tw != self._last_term_width
            if shape is not None and (resized or drift):
                if drift:
                    self._last_term_width = tw
                # resize：新宽重渲染前按"保 volatile 行数"重锚水印——
                # 旧宽已固化行不重打（无二次打印）、旧易变行在新宽下
                # 重新导出（无内容丢失），仅边界留宽差缝（与现状同类）
                if svc is not None and self.is_started:
                    svc._on_width_change()
                _, h = shape
                with self.console._lock:  # 与其他 console 写入原子化
                    f = self.console.file
                    f.write("\r")  # 退出末行 pending-wrap/任意列 → 列 0
                    if h > 1:
                        f.write(f"\033[{h - 1}A")  # 尽力到区域顶
                    f.write("\r\033[J")  # 区域顶 → 屏末
                    f.flush()
                    self._live_render._shape = None  # 下次渲染视为首渲（不移光标）
                    super().refresh()  # RLock 重入；就地按新尺寸重渲
                return
            # 固化漏斗：5Hz 节拍与 feed 强制刷新都经此——新稳定行
            # 提交进 scrollback（print-during-Live 嵌到区域上方），
            # 随后 super().refresh() 只重绘更小的易变区。
            # is_started 守卫：pause 的 transient stop 后 Live 已停、渲染
            # 钩子已弹，此路径绝不能再 print（否则弹窗光标处落盘弄脏画面）。
            if svc is not None and self.is_started:
                svc._maybe_commit()
        super().refresh()


class StreamingService:
    """Live response + animated status + trailing frame, with queued input."""

    def __init__(
        self,
        console,
        input_tokens: int = 0,
        todos_provider: Optional[Any] = None,
        fleet: Optional[Any] = None,
        panels: Optional[Any] = None,
    ) -> None:
        self._console = console
        self._rich = console._console
        self._input_tokens = input_tokens
        # 状态层（deck，v0.4.0）：todos_provider() → agent.todos 快照
        # （TodoWriteTool 以 store[:]= 整体替换全新 dict，切片即原子，
        # 无需加锁）；fleet → FleetMonitor，子代理运行态与 Ctrl-O 详情。
        # 两者皆 None（旧调用方 / 测试 Harness）→ 整组与旧版逐字节一致。
        self._todos_provider = todos_provider
        self._fleet = fleet
        # 插件 UI 面板（ui/v1，P-D）：panels → UiPanelCollector（每帧征集
        # + 崩溃跳过 + 熔断，见 services/assembly.py）。None → 零变化。
        self._panels = panels
        # 状态层焦点：0 = 主视图；1..n = 第 n 个子代理详情（Ctrl-O 循环）
        self._focus = 0
        # 权限选择桥接（流式期 ask_permission 委托至此）：面板渲染在
        # 输入框之下（见 _permission_renderable），↑/↓/Enter 经捕获热键
        # 消费（本函数 drain 循环的 perm 分支）——Live 不暂停、内容照
        # 常展示（用户报告：全屏权限弹窗占满屏影响体验）。None = 无待
        # 决权限请求。
        self._permission = None
        # 舰队列表选择（↓ 键）：-1 = 未选择；0 = 主代理条目；1..n =
        # 第 n 个子代理。列表含主代理条目（编号 0）——答案末尾（scroll
        # offset=0）且有子代理时，↓ 选中首个运行中的子代理，再按在
        # 0..n 间循环（含主条目），Enter 切换主视图到选中条目；进入
        # 详情后 ↑/↓ 直接循环切换代理（含回主视图）。
        self._fleet_selected = -1
        # 最近一次成功构建的 deck 高度：cancel() 擦除行数的保险
        # （正常路径会被 stop 内最终构建自清零，仅 _LiveView 回落
        # _last_view 的兜底渲染路径需要这个存量值）。
        self._last_deck_h = 0
        # ── Esc 打断（v0.4.1）────────────────────────────────────
        # _loop/_cancel_target：捕获线程经 call_soon_threadsafe 取消
        # **流消费子任务**（交互层经 set_cancel_target 登记）——绝不
        # 取消主任务：子任务完成后 cancel() 即 no-op，从根上消除
        # "回合结束后的流弹 Esc 误取消下一回合"的竞态。
        self._loop = None
        self._cancel_target = None
        self._interrupt_fired = False   # 单回合闩：连按 Esc 只触发一次
        self.esc_interrupted = False    # 交互层据此吞掉 CancelledError
        # Enter 排队反馈（v0.4.1）：捕获线程写、Live 线程读 → 独立锁
        self._queued: list = []
        self._queued_lock = threading.Lock()
        self._t0: float = 0.0
        self._token_count = 0
        # 主转录段序列：["text", str] 文本段（过 Markdown）与
        # ["tool", _ToolRecord] 工具段（独立渲染，颜色可行）交替。
        # 取代旧单字符串缓冲——结构化渲染（Claude Code 风格）的基础。
        self._segments: list[list] = []
        # task 工具起始事件不落工具段（deck 已覆盖），但暂存其描述供
        # 结果回显头行使用（deck 随 done 消失，回显是永久记录）
        self._pending_task_desc: list[str] = []
        # Ctrl+T 全局展开/折叠全部工具块（start() 归零）
        self._tools_expanded = False
        # ── thinking（模型推理内容）──────────────────────────────
        # reasoning 走独立缓冲，与正文 Markdown 缓冲隔离：纯文本 dim
        # 呈现，**绝不进 Markdown**（thinking 内的围栏/反引号会被重排
        # 乱）。默认折叠（指示行一行），Ctrl+R 切换展开（热键在
        # _build_renderable 的 drain 循环消费，同 Ctrl-O）。
        # _reasoning_done：首个非 reasoning 内容到达即冻结推理阶段；
        # _thinking_elapsed：冻结时刻耗时 → 静态 "Thought for Ns"
        # 指示（指示行不带滴答计时——进缓存体，逐秒变会每 5Hz 失效
        # 缓存复活闪烁病；计时只在 spinner 行）。done() 对纯 thinking
        # 回合兜底冻结。
        self._reasoning_buffer = ""
        self._reasoning_expanded = False
        self._reasoning_done = False
        self._thinking_elapsed = 0.0
        self._done = False
        self._last_view = Group()
        self._live: Optional[Live] = None
        self._capture: Optional[InputCapture] = None
        # 引用计数式完全暂停：pause×N 需要 resume×N 才真正恢复，因此
        # executor 权限钩子与 console 弹窗钩子可以安全叠加触发。
        # Ref-counted pause depth: nested pause/resume pairs stay balanced.
        self._pause_count = 0
        # ── 逐行固化 scrollback 管线（取代尾窗 _windowed）──────────
        # 已提交进终端 scrollback 的 body 行数。贪心换行前缀稳定——
        # token 追加只改渲染结果的最后一行——故 [0, watermark) 的行
        # 固化后永不重渲/重打（"二次打印"的根因修法）。watermark 另
        # 夹取：首个 running 工具记录首行（状态点 dim→绿 会变）、尾
        # 部未闭合表格首行（表格非前缀稳定）、末 text 段的最后一行。
        # Committed body lines in terminal scrollback; never re-rendered.
        self._committed_count = 0
        # body render_lines 缓存：键含宽度（resize 失效）；bounds 记每
        # chunk（thinking/段）的行区间供水印计算。5Hz 重建整组但内容
        # 未变时复用——不重解析 Markdown，帧间差异收敛到 spinner 行。
        self._body_cache_key: Optional[tuple] = None
        self._body_cache_lines: list = []
        self._body_bounds: list = []
        # 上次强制刷新时刻（monotonic）；0.0 = 从未，首个 token 必刷新。
        self._last_force_refresh = 0.0

    # ── public API ──────────────────────────────────────────────

    def start(self) -> None:
        """Begin streaming: start input capture, then the live display."""
        self._t0 = time.monotonic()
        self._done = False
        # 每轮干净起点：焦点回主视图、清空上一轮的子代理视图。fleet 挂
        # 在 agent 上跨轮存活，start() 是唯一保证每轮必跑的钩子（done()
        # 在 cancel/异常路径可能不执行）。
        self._focus = 0
        self._fleet_selected = -1
        self._permission = None
        self._segments = []
        self._pending_task_desc = []
        self._tools_expanded = False
        self._last_deck_h = 0
        # 流式服务注册到 console：期间 ask_permission 委托权限选择到
        # 本服务的桥接面板（输入框下方，不占满屏、不暂停 Live）
        self._console._streaming_service = self
        self._interrupt_fired = False
        self.esc_interrupted = False
        # thinking 状态每轮归零（跨轮不延续折叠态）
        self._reasoning_buffer = ""
        self._reasoning_expanded = False
        self._reasoning_done = False
        self._thinking_elapsed = 0.0
        # 固化管线每轮归零（实例本就每轮新建，双保险）
        self._committed_count = 0
        self._body_cache_key = None
        self._body_cache_lines = []
        self._body_bounds = []
        if self._fleet is not None:
            self._fleet.reset()
        # 事件循环引用：Esc 打断经 call_soon_threadsafe 取消消费任务。
        # 无事件循环的同步调用（测试 Harness）→ None，打断降级 no-op。
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        # InputCapture 总是创建（含非 TTY）：回调接线不依赖终端，测试
        # 可直接驱动 on_interrupt；线程/termios 层才需要 TTY（capture
        # .start() 自行 no-op 判断）。
        self._new_capture()
        self._live = _ResizeAwareLive(
            _LiveView(self),
            console=self._rich,
            refresh_per_second=_REFRESH_PER_SECOND,
            transient=False,
            auto_refresh=True,
            resize=getattr(self._console, "_resize", None),
        )
        self._live.start()

    def _new_capture(self) -> None:
        """创建并接线一个 InputCapture（start 与 resume 共用）。

        **回调必须随每次重建重新接线**：弹窗（choose_mode / 权限询问）
        经 pause→resume 换掉整个 capture 对象，旧接线随之失效——漏接
        会让 Esc 打断与 Enter 排队在第一次弹窗后静默失灵（v0.4.1 修复
        的用户报告根因）。非 TTY 下线程/termios 层 no-op，但对象与
        接线照常就位（测试可直接驱动回调）。
        """
        self._capture = InputCapture()
        self._capture.on_interrupt = self._interrupt
        self._capture.on_line_queued = self._on_line_queued
        self._console._input_capture = self._capture
        self._capture.start()

    def set_cancel_target(self, task) -> None:
        """登记 Esc 打断要取消的流消费任务（交互层在 await 前调用）。"""
        self._cancel_target = task

    def _interrupt(self) -> None:
        """Esc 热键（捕获线程）：取消流消费任务 → 打断当前回合。

        只取消 set_cancel_target 登记的**子任务**，主任务不受影响：
        子任务完成（回合正常结束）后 cancel() 即 no-op，回合结束后的
        流弹 Esc 绝不误伤下一回合；_interrupt_fired 闩防单回合连按重复
        触发。取消经 call_soon_threadsafe 投递——屏幕写操作仍只在事件
        循环线程发生。交互层在 CancelledError 处读 esc_interrupted 决定
        吞掉（回 REPL）还是上抛（真实外部取消）。
        """
        if self._interrupt_fired:
            return
        self._interrupt_fired = True
        self.esc_interrupted = True
        task = self._cancel_target
        loop = self._loop
        if task is not None and loop is not None and not task.done():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # loop 已关闭 → 降级 no-op

    def _on_line_queued(self, line: str) -> None:
        """流式期间 Enter（捕获线程）：记录排队行，即时刷新队列面板。"""
        with self._queued_lock:
            self._queued.append(line)
        if self._live is None:
            return
        # 时间门控同 feed()：反馈行最迟 200ms 内可见（5Hz 自动节拍兜底）
        now = time.monotonic()
        if now - self._last_force_refresh >= _MIN_FORCE_REFRESH:
            self._last_force_refresh = now
            self._live.refresh()

    def feed(self, chunk: Any) -> None:
        """Feed one incoming chunk; re-render (time-gated).

        chunk 类型：文本 token（``str``）→ 追加到末文本段；``ToolStartEvent``
        → 新建 running 工具记录入段；``ToolResultEvent`` → 匹配最后一个
        同名 running 记录落结果（并行结果按 gather 原序回传，与 start 序
        一致 → 按序匹配正确）；``StreamReasoning`` → 独立 thinking 缓冲
        （Ctrl+R 折叠块），首个非 reasoning chunk 冻结推理阶段。

        ``task`` 工具的**起始**不落工具段：输入框上方的状态层（deck）
        已实时展示子代理运行态，再打一行 ``● task`` 纯属冗余；**结果**
        回显保留（建一条 done 记录）——deck 随 done() 消失，回显是子
        代理报告在 transcript 里的唯一永久记录。

        工具段渲染绕过 Markdown（见 _tool_renderables）→ 状态点红/绿、
        ⎿ 槽线、diff 着色可行（Claude Code 风格）。
        """
        from ..agent import ToolStartEvent, ToolResultEvent
        from ..llm import StreamReasoning
        if isinstance(chunk, StreamReasoning):
            self._reasoning_buffer += chunk.text
            if self._live is None:
                return
            now = time.monotonic()
            if now - self._last_force_refresh >= _MIN_FORCE_REFRESH:
                self._last_force_refresh = now
                self._live.refresh()
            return
        # 首个非推理内容 → 推理阶段结束：冻结耗时供静态指示行使用
        if self._reasoning_buffer and not self._reasoning_done:
            self._reasoning_done = True
            self._thinking_elapsed = time.monotonic() - self._t0
        if isinstance(chunk, ToolStartEvent):
            if chunk.name == "task":
                # 不落工具段，但暂存描述供结果回显头行（FIFO 配对）
                desc = _extract_task_desc(chunk.arguments)
                self._pending_task_desc.append(desc)
            else:
                self._segments.append(
                    ["tool", _ToolRecord(name=chunk.name,
                                         arguments=chunk.arguments)])
                self._token_count += 1
        elif isinstance(chunk, ToolResultEvent):
            if chunk.name == "task":
                # task 结果回显：用暂存描述建 done 记录（永久记录）
                desc = (self._pending_task_desc.pop(0)
                        if self._pending_task_desc else "")
                self._segments.append(["tool", _ToolRecord(
                    name="task", arguments=desc,
                    status="error" if chunk.is_error else "done",
                    output=chunk.output or "", is_error=chunk.is_error)])
                self._token_count += 1
            else:
                record = self._match_running_record(chunk.name)
                if record is not None:
                    record.output = chunk.output or ""
                    record.is_error = chunk.is_error
                    record.status = "error" if chunk.is_error else "done"
                else:
                    # 无匹配（理论上不发生）：兜底独立记录，不丢结果
                    self._segments.append(["tool", _ToolRecord(
                        name=chunk.name,
                        status="error" if chunk.is_error else "done",
                        output=chunk.output or "",
                        is_error=chunk.is_error)])
                self._token_count += 1
        elif isinstance(chunk, str):
            if self._segments and self._segments[-1][0] == "text":
                self._segments[-1][1] += chunk
            else:
                self._segments.append(["text", chunk])
            self._token_count += 1
        else:
            return
        if self._live is None:
            return
        # 时间门控强制刷新：token 爆发曾每 3 个 token 立即重绘一次，在
        # 自动刷新节拍之上叠加整区重写 → 闪烁。现仅当距上次强制刷新
        # ≥ _MIN_FORCE_REFRESH 才立即重绘，其余交给 5Hz 自动节拍（文字
        # 延迟 ≤200ms，不可感知）。首个 token 必立即刷新（初值 0.0）。
        now = time.monotonic()
        if now - self._last_force_refresh >= _MIN_FORCE_REFRESH:
            self._last_force_refresh = now
            self._live.refresh()

    def _match_running_record(self, name: str) -> Optional[_ToolRecord]:
        """第一个同名 running 工具记录（并行结果按 gather 原序回传，
        先到先配，配完即 done → 下一个结果自然落到下一个 running 记录）。"""
        for kind, payload in self._segments:
            if kind == "tool" and payload.name == name \
                    and payload.status == "running":
                return payload
        return None

    def _has_body(self) -> bool:
        """转录是否有可渲染内容（body 门）。"""
        for kind, payload in self._segments:
            if kind == "text":
                if payload.strip():
                    return True
            else:
                return True  # 任何工具段都渲染
        return False

    def done(self) -> float:
        """Finalise: commit remainder, stop capture, leave frame."""
        self._done = True
        elapsed = time.monotonic() - self._t0
        # 纯 thinking 回合（无正文/工具事件收尾）兜底冻结推理阶段
        if self._reasoning_buffer and not self._reasoning_done:
            self._reasoning_done = True
            self._thinking_elapsed = elapsed
        # 未固化余量一次性提交进 scrollback——**只打余量、不全文重
        # 渲染**（旧行为在此整体重打全文 = 用户报告的"二次打印"）。
        # 固化行早已在 scrollback，余量 = 易变尾（≤ 末行 + 未冻结
        # thinking + 尾部工具块），通常只有几行。
        self._flush_commit()
        if self._live:
            if not self._live.is_started:
                # done-while-paused（弹窗未结束/异常路径）：Live 已被
                # pause 的 transient stop 停掉、渲染钩子已弹——直接
                # refresh 是空操作。先重启再重绘，最终帧真正落屏。
                sys.stdout.write("\r")  # 兜底回列 0（同 resume，防弹窗未回列）
                sys.stdout.flush()
                self._live.start()
            # 以 done 状态再重绘一次：易变区已空 → 组 = 仅 frame
            # （spinner/deck 门控跳过），stop 后 frame 留屏供下轮复用。
            self._live.refresh()
            self._live.stop()
        self._stop_capture(keep_queue=True)
        # 只允许在最终帧确已渲染时声称为真——否则下一轮 prompt 的复用
        # 分支（\033[3A）会踩进答案区。_live 非 None ≡ 上面走过了重绘。
        self._console._frame_on_screen = self._live is not None
        self._permission = None                # 兜底清待决权限面板
        self._console._streaming_service = None  # 注销桥接
        return elapsed

    def pause(self) -> None:
        """Fully pause the stream for an interactive dialog (Bug 10, extended).

        交互式弹窗（ask_user / exit_plan_mode 审批 / 权限询问）在工具执行
        期间发生。此时若 Live 仍以 10Hz 重绘，弹窗的 ANSI 光标控制会被
        周期性重画踩乱 → 屏幕疯狂打印；若 InputCapture 线程仍在 cbreak
        下读键，会与弹窗 ``_raw_select`` 的 raw-mode 直读互抢 termios 与
        按键 → 问题迟迟无法作答。因此弹窗期间必须**同时**停掉 Live 刷新
        与输入捕获。

        引用计数：pause×N 需 resume×N 才真正恢复——executor 权限钩子
        （``pause_capture``）与 console 弹窗钩子（``on_dialog_start``）可能
        嵌套触发同一次暂停，叠加必须安全。
        Ref-counted full pause (Live redraw + input capture) for dialogs.
        """
        self._pause_count += 1
        if self._pause_count > 1:
            return  # 已暂停，仅加深计数
        self._stop_capture(keep_queue=True)
        if self._live is None:
            return
        # 以 transient 方式停止：Rich 自行擦除冻结帧（含末行换行），
        # 弹窗即可原地显示，恢复后也不会与冻结帧重复。
        # Stop transiently so Rich erases its frozen frame in place.
        # **try/finally 必保 transient 回置**：stop() 若抛异常而 transient
        # 滞留为真，后续 done() 的 stop() 会走 restore_cursor 擦掉整个
        # 答案区——"回答完成瞬间答案消失"（用户报告根因之一）。
        self._live.transient = True
        try:
            self._live.stop()
        finally:
            self._live.transient = False
        # 复位 LiveRender 的光标形状记忆：否则恢复后首次刷新会按旧高度
        # 上移光标、踩进弹窗内容。内部属性，失败无碍（最坏仅轻微错位）。
        # Forget the stale cursor shape so resume renders fresh in place.
        try:
            self._live._live_render._shape = None
        except Exception:
            pass

    def resume(self) -> None:
        """Resume after :meth:`pause`; no-op until the count drops to zero.

        计数归零才真正恢复：重启 Live 刷新线程与 InputCapture（新的 cbreak
        会话）。流已结束（``done``/``cancel``）时绝不重启。
        """
        if self._pause_count == 0:
            return  # 幂等：无配对的 resume 为空操作
        self._pause_count -= 1
        if self._pause_count or self._done:
            return
        if self._live is not None and not self._live.is_started:
            # 兜底回列 0：弹窗收尾若未回列（raw 模式裸 LF 等路径），
            # Live 首渲不带 CR（shape 已在 pause 复位为 None）会从弹窗
            # 末行光标列直接开写 → 首行缩进错位、后续擦除行数失准、
            # 残帧粘连。现有弹窗均在返回前发过 LF → 光标已在新行，
            # CR 只回列不挪行、零副作用；未来新弹窗漏 LF 时此处兜底。
            sys.stdout.write("\r")
            sys.stdout.flush()
            self._live.start()
        if self._capture is None:
            self._new_capture()  # 回调重新接线（见 _new_capture 说明）

    def pause_capture(self) -> None:
        """Back-compat alias for :meth:`pause` (executor prompt hooks).

        权限弹窗钩子的旧入口——如今委托给引用计数式完全暂停，弹窗期间
        Live 重绘也一并停掉（Bug 10 最初只停了捕获）。
        """
        self.pause()

    def resume_capture(self) -> None:
        """Back-compat alias for :meth:`resume`."""
        self.resume()

    def cancel(self) -> None:
        """Stop on error: stop capture (keep queued lines), clear the frame.

        擦除 frame 的 4 行 + ``_last_deck_h`` 行状态层。正常路径下
        ``_done=True`` 先于 ``stop()`` → stop 内的最终 refresh 重建时
        deck 已被门控跳过、rich 原生 ``\\033[J`` 顺手擦净旧 deck，
        ``_last_deck_h`` 也已自清零 → 实际擦 4 行与旧版一致。存量值只
        兜底 ``_LiveView`` 异常回落 ``_last_view``（带 deck 的流式期
        视图）这一条路径。

        **pause 态（Live 未运行）绝不擦**：frame 不在屏（已被 pause 的
        transient stop 擦除），光标停在弹窗处——此时 ``\\033[4A`` 会从
        弹窗位置越界上移，吞掉框上方的既有对话。
        """
        self._done = True
        # stop() 会停掉 Live——须在停之前读 is_started 判别 frame 在屏否
        frame_on_screen = self._live is not None and self._live.is_started
        if self._live:
            # 部分回答保真：未固化余量提交进 scrollback（旧行为经 stop
            # 内全文重渲染保留部分回答——新管线只打余量，等价无重打）
            self._flush_commit()
            self._live.stop()
            self._live = None
        self._stop_capture(keep_queue=True)
        if frame_on_screen:
            sys.stdout.write(f"\033[{4 + self._last_deck_h}A\033[J")
            sys.stdout.flush()
        self._console._frame_on_screen = False
        self._permission = None                # 兜底清待决权限面板
        self._console._streaming_service = None  # 注销桥接

    def _permission_renderable(self) -> tuple:
        """权限选择面板 → ``(Group | None, 行数)``（框之下、舰队之上）。

        紧凑布局：问题行（Allow 工具? 原因）+ 变更摘要行（diff 路径
        或截断参数）+ 选项（❯ 标记当前项）+ 按键提示。取代旧版占满
        全屏的权限弹窗——上方内容照常展示。
        """
        perm = self._permission
        if perm is None:
            return None, 0
        rows: list = [self._deck_markup(
            f" [bold yellow]Allow[/] [bold]{escape(perm['tool_name'])}[/]"
            f"[dim]? {escape(perm['reason'])[:60]}[/]")]
        diff = perm["diff"]
        if diff:
            try:
                path, _old, new = diff
                n_lines = len((new or "").splitlines())
                rows.append(self._deck_line(
                    f"  ~ {path} · {n_lines} lines", "dim"))
            except Exception:
                pass
        elif perm["details"]:
            summary = " ".join(perm["details"].split())[:100]
            rows.append(self._deck_line(f"  {summary}", "dim"))
        for i, (label, _value) in enumerate(perm["options"]):
            if i == perm["selected"]:
                rows.append(self._deck_markup(
                    f"[{ACCENT_BOLD}]❯ {escape(label)}[/]"))
            else:
                rows.append(self._deck_line(f"  {label}", "dim"))
        rows.append(self._deck_line(
            "  ↑/↓ select · ↵ confirm · esc interrupt", "dim"))
        return Group(*rows), len(rows)

    # ── 权限选择桥接（流式期 ask_permission 委托）─────────────────

    def is_live_active(self) -> bool:
        """Live 区是否在运行（ask_permission 据此选择桥接/传统弹窗）。"""
        return self._live is not None and self._live.is_started

    async def ask_permission_bridge(
        self,
        tool_name: str,
        reason: str,
        details: str = "",
        args_summary: str = "",
        can_remember: bool = True,
        diff: tuple | None = None,
    ) -> tuple[bool, bool]:
        """流式期权限选择：面板嵌在输入框下方，Live 不暂停、上方内容
        照常展示（用户报告：全屏弹窗占满屏影响体验）。

        用户经捕获热键选择：↑/↓ 移选项、Enter 确认（drain 循环的
        perm 分支消费）；Esc 走既有中断路径（取消消费任务 → 本协程
        的 await 被取消 → finally 清面板）。返回 ``(approved, remember)``。
        """
        options: list[tuple[str, tuple[bool, bool]]] = [
            ("Yes, allow once", (True, False)),
        ]
        if args_summary and can_remember:
            options.insert(1, ("Yes, and don't ask again for this", (True, True)))
        options.append(("No, don't run", (False, False)))
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._permission = {
            "tool_name": tool_name,
            "reason": reason or "",
            "details": details,
            "diff": diff,
            "options": options,
            "selected": 0,
            "future": fut,
        }
        try:
            return await fut
        finally:
            self._permission = None

    def _stop_capture(self, *, keep_queue: bool) -> None:
        if self._capture is not None:
            self._capture.stop()
            if keep_queue:
                self._console._input_queue.extend(self._capture.drain())
            else:
                self._capture.drain()
        self._console._input_capture = None
        self._capture = None

    # ── internals ───────────────────────────────────────────────

    def _build_renderable(self):
        """Build the Live group: [body] + [spinner] + [frame] + [deck?].

        每次 Rich ``Live`` 刷新都会经由 :class:`_LiveView` 调用到这里，因此
        ``elapsed`` 每帧都重新计算——计时器持续走动，哪怕模型还在思考、
        尚未吐出任何 token。

        v0.4.0 状态层（deck）渲染在**输入框之上**：计划面板（todos）+
        排队队列 + 子代理舰队。body 默认是主响应；Ctrl-O 热键（InputCapture
        队列，本函数内消费——写屏仍只在 Live 线程）循环切换到子代理详情
        视图。deck 门与 spinner 门同为 ``not self._done``：done()/cancel()
        后 frame 复为末元素，``_frame_on_screen`` 复用链不受影响。
        """
        elapsed = time.monotonic() - self._t0
        tok_s = (
            f"{self._token_count / 1000:.1f}k"
            if self._token_count >= 1000
            else str(self._token_count)
        )

        parts = []
        # ── 状态层快照 + 热键（Ctrl-O 循环切换主视图 ⇄ 子代理详情）──
        # 快照一帧一份：渲染只读拷贝，绝不跨线程迭代活对象（fleet 锁序
        # monitor→view，本路径是唯一消费者）。todos 无需锁：TodoWriteTool
        # 以 store[:]= 整体替换全新 dict，切片复制在 GIL 下原子。
        snap = self._fleet.snapshot() if self._fleet is not None else []
        if self._capture is not None:
            for key in self._capture.drain_hotkeys():
                if key == "\x14":
                    # Ctrl+T：全局展开/折叠全部工具块（对标 Claude Code
                    # ctrl+o 展开转录；openx 的 ctrl+o 已被舰队切换占用）
                    self._tools_expanded = not self._tools_expanded
                    continue
                if self._permission is not None:
                    # 权限面板期间：↑/↓/Enter 专属选择（Enter 经
                    # call_soon_threadsafe 唤醒 ask_permission_bridge 的
                    # await）；其他热键忽略，Esc 走中断路径。
                    perm = self._permission
                    n_opts = len(perm["options"])
                    if key == "\x1b[A":
                        perm["selected"] = (perm["selected"] - 1) % n_opts
                    elif key == "\x1b[B":
                        perm["selected"] = (perm["selected"] + 1) % n_opts
                    elif key in ("\r", "\n"):
                        value = perm["options"][perm["selected"]][1]
                        fut = perm["future"]
                        if not fut.done() and self._loop is not None:
                            self._loop.call_soon_threadsafe(
                                fut.set_result, value)
                    continue
                if key == "\x0f" and snap:
                    # Ctrl-O：主视图 ⇄ 各子代理直接循环切换
                    self._focus = (self._focus + 1) % (1 + len(snap))
                    self._fleet_selected = -1  # 直接切换 → 清待确认选择
                elif key == "\x12" and self._reasoning_buffer \
                        and not self._reasoning_done:
                    # Ctrl+R：thinking 展开/折叠（对标 Claude Code）。
                    # 无推理内容的回合静默忽略。下一个 5Hz 节拍内生效。
                    # **冻结后 no-op**：冻结即随下 tick 固化进 scrollback，
                    # 已固化行不可重渲（同 Ctrl+T 纪律）。
                    self._reasoning_expanded = not self._reasoning_expanded
                elif (
                    len(key) == 2 and key[0] == "\x1b" and key[1].isdigit()
                    and snap
                ):
                    # Alt+0..9：舰队直选（对标 Claude Code 窗格导航）。
                    # 0 = 回主视图；1..N = 第 N 个子代理详情（越界钳到
                    # 最后一个——按键必须总有响应，静默失败最困惑）。
                    n = int(key[1])
                    self._focus = 0 if n == 0 else min(n, len(snap))
                    self._fleet_selected = -1
                elif key == "\x1b[B" and self._focus > 0 and snap:
                    # 详情视图内：↓ 循环切换到下一项（含回到主视图 0）
                    self._focus = (self._focus + 1) % (len(snap) + 1)
                elif key == "\x1b[A" and self._focus > 0 and snap:
                    # 详情视图内：↑ 循环切换到上一项（主视图回卷末代理）
                    self._focus = (self._focus - 1) % (len(snap) + 1)
                elif key == "\x1b[A" and self._fleet_selected >= 0:
                    # ↑ 反向循环选择（含主条目 0）。窗内上翻回看已删——
                    # 固化进 scrollback 后终端原生滚动接管（↑/PgUp 不再
                    # 入热键，终端自行处理）。
                    self._fleet_selected = (
                        (self._fleet_selected - 1) % (len(snap) + 1)
                    )
                elif key == "\x1b[B" and snap:
                    # ↓ 选中首个运行中的子代理（再按在 0..N 间循环，
                    # 0 = 主条目），Enter 确认把主视图切换到选中条目。
                    if self._fleet_selected < 0:
                        self._fleet_selected = next(
                            (i + 1 for i, v in enumerate(snap)
                             if v["status"] == "running"),
                            1,
                        )
                    else:
                        self._fleet_selected = (
                            (self._fleet_selected + 1) % (len(snap) + 1)
                        )
                elif key in ("\r", "\n") and self._fleet_selected >= 0 and snap:
                    # Enter 确认：主视图切换到选中条目（0 = 回主视图，
                    # 1..N = 子代理详情，内容展示在输入框上方）。空 Enter
                    # 才入热键；有键入内容时 Enter 仍走排队消息路径。
                    self._focus = min(self._fleet_selected, len(snap))
        self._focus = min(self._focus, len(snap))  # reset 后钳位防越界
        if self._fleet_selected > len(snap):  # 子代理增减后选择失效 → 清零
            self._fleet_selected = -1

        # 状态层拆为上下两区：Plan + Queue 在输入框**之上**（排队待发
        # 提示常驻至发完），子代理列表（含主代理条目 0）在输入框**之下**
        # （用户需求：列表在对话框下方，↓ 选中 + Enter 切换主视图到输入
        # 框上方的详情/主回答）。门同 spinner 为 not _done —— done()/
        # cancel() 的最终构建必跳过两区，frame 复为末元素（_frame_on_screen
        # 复用链前提）；_last_deck_h 只记框下（舰队）行数供 cancel 擦除。
        if not self._done:
            deck, deck_h = self._deck_renderable(snap)
            perm_deck, perm_h = self._permission_renderable()
            fleet_deck, fleet_deck_h = self._fleet_deck_renderable(snap)
            plugin_deck, plugin_h = self._plugin_deck_renderable()
        else:
            deck, deck_h = None, 0
            perm_deck, perm_h = None, 0
            fleet_deck, fleet_deck_h = None, 0
            plugin_deck, plugin_h = None, 0
        extra = deck_h + perm_h + fleet_deck_h + plugin_h  # 额外行 → 视口预算

        if self._focus > 0 and snap:
            parts.append(self._detail_view(snap[self._focus - 1], extra))
        else:
            # body = 未固化行（易变尾）；已固化行在 scrollback，不进组
            vol = self._volatile_view(extra)
            if vol is not None:
                parts.append(vol)

        if not self._done:
            parts.append(self._spinner_text(elapsed))
        if deck is not None:
            parts.append(deck)
        parts.append(self._console._frame_renderable(
            self._input_tokens, self._token_count
        ))
        if perm_deck is not None:
            parts.append(perm_deck)
        if fleet_deck is not None:
            parts.append(fleet_deck)
        if plugin_deck is not None:
            parts.append(plugin_deck)
        self._last_deck_h = perm_h + fleet_deck_h + plugin_h  # 框下总行数
        return Group(*parts)

    def _thinking_block(self, hints: bool = False) -> list:
        """thinking 块部件列表（置于正文 Markdown 之前）。

        折叠态 = 单行静态指示行；展开态 = 指示行 + 全文（纯文本 dim，
        **绝不走 Markdown**——thinking 内的围栏/反引号会被重排乱）+
        空行分隔。指示行遵循 deck 行不变量（no_wrap + ellipsis ≡
        1 终端行）；指示文本**不含滴答计时**（缓存体逐秒变化会每 5Hz
        失效缓存、复活闪烁病——耗时只由 spinner 行呈现）。

        ``hints``：易变显示渲染传 True、固化渲染传 False。**冻结
        （_reasoning_done）即随下一 tick 固化进 scrollback**——热键
        提示只存在于冻结前的易变期（同 Ctrl+T 提示纪律）。
        """
        if not self._reasoning_buffer:
            return []
        if not self._reasoning_done:
            label = f"{MARK_PENDING} Thinking…"        # ○ 推理进行中
        else:
            label = f"{MARK_INFO} Thought for {self._thinking_elapsed:.1f}s"  # ●
        if hints and not self._reasoning_done and not self._done:
            hint = (" — ctrl+r to collapse" if self._reasoning_expanded
                    else " — ctrl+r to expand")
        else:
            hint = ""  # 冻结/done 后热键 no-op——提示即谎言
        indicator = self._deck_line(f"  {label}{hint}", style=DIM)
        if not self._reasoning_expanded:
            return [indicator]
        return [indicator, Text(self._reasoning_buffer, style=DIM), Text("")]

    # ── 逐行固化管线（取代尾窗 _windowed）────────────────────────

    def _body_chunks(self, hints: bool) -> list:
        """body 有序 chunk 列表 → ``[(kind, meta, renderable)]``。

        kind："thinking" / "text" / "tool"；meta：tool 段携带记录对象
        （水印/易变视图需要 running 位置与记录本身），其余为状态串。
        段间空行分隔（防粘连）并入后续 chunk 头部。``hints`` 控制交互
        提示（ctrl+r / ctrl+t 字样）：**固化渲染恒 False**（提示在固化
        后是谎言），易变显示渲染传 True。
        """
        chunks: list = []
        if self._reasoning_buffer:
            chunks.append(("thinking", "thinking",
                           Group(*self._thinking_block(hints))))
        for kind, payload in self._segments:
            if kind == "text":
                clean = _RICH_TAG.sub("", payload)
                if not clean.strip():
                    continue
                rend: Any = Markdown(clean, code_theme="monokai")
                meta: Any = "text"
            else:
                rows = self._tool_renderables(payload, hints=hints)
                if not rows:
                    continue
                rend = Group(*rows) if len(rows) > 1 else rows[0]
                meta = payload
            if chunks:
                rend = Group(Text(""), rend)
            chunks.append((kind, meta, rend))
        return chunks

    def _body_lines(self) -> list:
        """body 渲染行（无 hints 版）+ 每 chunk 行区间，按内容缓存。

        键只含宽度（render_lines 只需宽；窗化已删，高/deck 不再入键）。
        bounds 元素 ``(kind, meta, start, end)`` 供水印与易变视图使用。
        """
        if self._rich is None:
            return []
        try:
            width = self._rich.width
        except Exception:
            width = None
        key = (
            self._segments_fingerprint(),
            self._tools_expanded,
            self._reasoning_buffer, self._reasoning_expanded,
            self._reasoning_done, self._done,
            width,
        )
        if key != self._body_cache_key:
            self._body_cache_key = key
            lines: list = []
            bounds: list = []
            for kind, meta, rend in self._body_chunks(hints=False):
                s = len(lines)
                try:
                    lines.extend(self._rich.render_lines(rend, pad=False))
                except Exception:
                    pass  # fail-open：坏渲染对象不拖垮管线
                bounds.append((kind, meta, s, len(lines)))
            self._body_cache_lines = lines
            self._body_bounds = bounds
        return self._body_cache_lines

    def _commit_watermark(self, lines: list) -> int:
        """可固化行上限：前缀稳定部分（贪心换行实测验证）。

        夹取三处（实测的非前缀稳定源）：① 首个 running 工具记录首行
        （并行工具：状态点 dim→绿、Running…→输出 会变，固化留 stale
        scrollback）；② 尾部已完成工具连跑保持 volatile（Ctrl+T 作用
        窗，直到后续段到达并入稳定前缀）；③ 末 text 段仅最后一行
        volatile，且尾部未闭合表格夹到表格首行（表格列宽随后到的行
        重排）。done 态 = 全部可固化（flush）。
        """
        if self._done:
            return len(lines)
        if self._reasoning_buffer and not self._reasoning_done:
            return 0  # thinking 未冻结：整体 volatile（保 Ctrl+R 窗口）
        wm = len(lines)
        bounds = self._body_bounds
        for kind, meta, s, _e in bounds:
            if kind == "tool" and getattr(meta, "status", "") == "running":
                wm = min(wm, s)
                break  # running 起往后皆 volatile
        # ② 尾部已完成工具连跑
        i = len(bounds)
        while (i and bounds[i - 1][0] == "tool"
               and getattr(bounds[i - 1][1], "status", "") != "running"):
            i -= 1
        if i < len(bounds):
            wm = min(wm, bounds[i][2])
        # ③ 末 text 段：末行 volatile + 未闭合表格夹取
        if bounds and bounds[-1][0] == "text":
            s = bounds[-1][2]
            wm = min(wm, max(s, len(lines) - 1))
            clamp = self._open_table_clamp()
            if clamp is not None:
                wm = min(wm, s + clamp)
        return wm

    def _open_table_clamp(self) -> Optional[int]:
        """尾部未闭合表格的行夹取（相对末 text 块首行）；无需夹取返 None。

        启发式：末 text 段尾部是顶格的连续 ``|`` 行（尾无空行 = 表格可能
        还在生长）且不在围栏内 → 渲染表格之前的源前缀定边界行数（仅在
        开表期间每 tick 多一次前缀渲染）。
        """
        if not self._segments or self._segments[-1][0] != "text":
            return None
        text = _RICH_TAG.sub("", self._segments[-1][1])
        src = text.split("\n")
        i = len(src)
        while i and src[i - 1].strip().startswith("|"):
            i -= 1
        if i == len(src):
            return None  # 尾部无表格行
        if sum(1 for ln in src[:i]
               if ln.lstrip().startswith("```")) % 2:
            return None  # "| 行"在围栏内 = 代码不是表格
        try:
            return len(self._rich.render_lines(
                Markdown("\n".join(src[:i]), code_theme="monokai"), pad=False))
        except Exception:
            return None

    @staticmethod
    def _copy_line(line) -> Text:
        """render_lines 行 → Text：只取可见文本段（控制段不得混入重渲）。"""
        text = Text()
        for seg in line:
            if seg.text and not seg.is_control:
                text.append(seg.text, seg.style)
        return text

    def _volatile_view(self, extra: int = 0):
        """Live 区 body = 未固化行（易变尾）。已固化行在 scrollback 不
        进组——擦重写区从"整尾窗"缩到"易变尾"，闪烁结构性消失。尾部
        已完成工具块以 hints=True 重渲染（Ctrl+T 窗口）；无易变内容返
        None（done 态组 = 仅 frame，复用链前提）。

        ``extra``：frame 之上/之下的状态层（plan/queue + perm/fleet/plugin）
        总行数——易变区可视预算相应收紧（整组高度恒 ≤ 视口，同 HEAD 的
        ``height - _VIEWPORT_RESERVE - extra`` 公式，funnel WIP 没吃 extra
        会在浮层在场时撑爆视口，此处补齐）。
        """
        lines = self._body_lines()
        if self._rich is None:
            return None
        C = min(self._committed_count, len(lines))
        bounds = self._body_bounds
        pre_think = bool(self._reasoning_buffer) and not self._reasoning_done
        # done 且已全固化 → 无易变体：组退化为仅 frame（复用链前提）。
        # 已固化行（含已完成工具块）在 done()/_flush_commit() 已落盘，
        # 绝不能在此重渲成"工具副本 + frame"。
        if self._done and not pre_think and C >= len(lines):
            return None
        i = len(bounds)
        while (i and bounds[i - 1][0] == "tool"
               and getattr(bounds[i - 1][1], "status", "") != "running"):
            i -= 1
        tail_tools = bounds[i:]
        copy_end = tail_tools[0][2] if tail_tools else len(lines)
        # 去重：pre_think（推理未冻结）时首 chunk 是 thinking，其 body 行
        # 未固化（水印=0）且由下方 _thinking_block(hints=True) 单独渲染——
        # 跳过该 chunk 的 body 行，否则屏上出现两条指示行（funnel WIP 缺陷）。
        copy_start = C
        if pre_think and bounds and bounds[0][0] == "thinking":
            copy_start = max(C, bounds[0][3])
        if not pre_think and not tail_tools and C >= copy_end:
            return None
        parts: list = []
        if pre_think:
            parts.extend(self._thinking_block(hints=True))
        for line in lines[copy_start:copy_end]:
            parts.append(self._copy_line(line))
        for _kind, rec, _s, _e in tail_tools:
            parts.extend(self._tool_renderables(rec, hints=True))
        if not parts:
            return None
        # 易变区超视口预算 → 裁尾 + ↑ … 标记。**按渲染行数测量**，不能按
        # renderable 个数（展开 thinking 是一个单对象多行 Text，按对象数
        # 判不超、整组会撑爆视口把 FRAME 顶掉——funnel WIP 缺陷）。
        try:
            height = self._rich.height
        except Exception:
            height = 24
        cap = max(5, height - _VIEWPORT_RESERVE - extra)
        group = Group(*parts)
        try:
            rendered = self._rich.render_lines(group, pad=False)
        except Exception:
            rendered = None
        if rendered is not None and len(rendered) > cap:
            tail = [Text(_SCROLL_MARKER, style="dim")]
            tail += [self._copy_line(l) for l in rendered[-(cap - 1):]]
            return Group(*tail)
        return group

    def _maybe_commit(self) -> None:
        """固化漏斗：refresh 覆写（Live 锁内）调用。新稳定行经
        print-during-Live 提交——Rich 渲染钩子把内容嵌到 Live 区上方
        （scrollback）并在下方重绘易变区，一步完成"固化 + 重锚"。
        锁序 Live→console 与 resize 路径一致。
        """
        if self._rich is None:
            return
        lines = self._body_lines()
        wm = self._commit_watermark(lines)
        if wm <= self._committed_count:
            return
        new = lines[self._committed_count:wm]
        # 先记账再 print：print 钩子触发的组重建应看到新水印。
        # print 内部自取 rich console 锁（锁序 Live→console 与 resize 同）。
        self._committed_count = wm
        self._rich.print(Group(*(self._copy_line(l) for l in new)))

    def _flush_commit(self) -> None:
        """done/cancel：未固化余量全提交（_done 已置 → 水印 = 全部）。

        只打余量、绝不全文重打——"二次打印"的根因修法。Live 在跑则
        走渲染钩子嵌上方；暂停/无 Live 则普通 print 落 scrollback。
        """
        if self._rich is None:
            return
        lines = self._body_lines()
        wm = self._commit_watermark(lines)
        if wm <= self._committed_count:
            return
        new = lines[self._committed_count:wm]
        self._committed_count = wm
        self._rich.print(Group(*(self._copy_line(l) for l in new)))

    def _on_width_change(self) -> None:
        """resize 重锚水印（保 volatile 行数）：旧宽已固化行不重打
        （无二次打印）；旧易变行在新宽下重新导出（无内容丢失）；边界
        留宽差缝（与现状 resize 同类可接受）。
        """
        k = max(0, len(self._body_cache_lines) - self._committed_count)
        self._body_cache_key = None  # 宽度入键，强制按新宽重渲染
        lines = self._body_lines()
        self._committed_count = max(0, min(len(lines), len(lines) - k))

    def _segments_fingerprint(self) -> tuple:
        """段序列指纹（缓存键用）：文本取文本，工具取 (名/参/态/出)。

        running 记录不含时变分量（Running… 为静态行）→ 缓存逐帧有效。
        """
        fp = []
        for kind, payload in self._segments:
            if kind == "text":
                fp.append(("t", payload))
            else:
                fp.append(("tool", payload.name, payload.arguments,
                           payload.status, payload.output,
                           payload.is_error))
        return tuple(fp)

    def _tool_renderables(self, record: "_ToolRecord",
                          hints: bool = False) -> list:
        """工具段 → Text.from_markup 行列表（Claude Code 风格）。

        头行 ``[{态色}●] name(args)``：running dim、done green、error
        red。结果 ⎿ 槽线块：折叠 3 行（错误 10 行）+ "… +N lines"；空
        输出 ``(No output)``。edit_file 结果行级着色（-红 +绿 @@dim）。
        Ctrl+T 展开（硬上限 200 行）**仅作用于未固化块** → 热键字样
        只在 ``hints=True``（易变显示渲染）出现；固化渲染（进
        scrollback）不带字样——提示随内容生命周期（同 thinking 纪律）。
        """
        from ..orchestration.fleet import _tool_call_summary
        from ..ui._style import MARK_INFO, SUCCESS_STYLE, ERROR_STYLE, DIM
        dot_style = {
            "running": DIM,
            "done": SUCCESS_STYLE,
            "error": ERROR_STYLE,
        }.get(record.status, DIM)
        # task 记录的 arguments 存的是暂存描述串（非 JSON）→ 直接展示
        if record.name == "task" and record.arguments:
            summary = record.arguments[:60]
        else:
            summary = _tool_call_summary(record.name, record.arguments)
        head = f"[{dot_style}]{MARK_INFO}[/] [bold]{record.name}[/]"
        if summary:
            head += f"[dim]({summary})[/]"
        rows: list = [Text.from_markup(head)]
        if record.status == "running":
            rows.append(Text.from_markup(f"[dim]  {_GUTTER}  Running…[/]"))
            return rows
        output = record.output.rstrip("\n")
        if not output:
            rows.append(Text.from_markup(f"[dim]  {_GUTTER}  (No output)[/]"))
            return rows
        lines = output.splitlines()
        is_edit = record.name == "edit_file"
        cap = _ERROR_MAX_LINES if record.is_error else _RESULT_MAX_LINES
        if self._tools_expanded:
            shown, rest = lines[:_EXPAND_HARD_CAP], 0
            if len(lines) > _EXPAND_HARD_CAP:
                rest = len(lines) - _EXPAND_HARD_CAP
                shown = shown[:]
        else:
            shown, rest = lines[:cap], max(0, len(lines) - cap)
        for i, ln in enumerate(shown):
            prefix = f"  {_GUTTER}  " if i == 0 else "     "
            rows.append(Text.from_markup(
                f"[dim]{prefix}[/]{self._result_line_markup(ln, is_edit)}"))
        if not self._tools_expanded and rest:
            tail = " (ctrl+t to expand)" if hints else ""
            rows.append(Text.from_markup(
                f"[dim]     … +{rest} lines{tail}[/]"))
        elif self._tools_expanded:
            if rest:
                rows.append(Text.from_markup(
                    f"[dim]     … +{rest} more (output too large)[/]"))
            tail = " · ctrl+t to collapse" if hints else ""
            rows.append(Text.from_markup(
                f"[dim]     ({len(lines)} lines{tail})[/]"))
        return rows

    @staticmethod
    def _result_line_markup(line: str, is_edit: bool) -> str:
        """结果行 markup：edit_file 的 diff 行级着色（-红 +绿 @@dim），
        其余工具原样（markup 转义防输出含方括号）。"""
        from rich.markup import escape
        if is_edit:
            # diff 头行（---/+++/@@）dim——须先于 -/+ 判断（--- 也以 - 开头）
            if line.startswith(("@@", "---", "+++")):
                return f"[dim]{escape(line)}[/]"
            if line.startswith("-"):
                return f"[red]{escape(line)}[/]"
            if line.startswith("+"):
                return f"[green]{escape(line)}[/]"
        return escape(line)

    @staticmethod
    def _shimmer_spans(label: str, elapsed: float) -> list:
        """把标签按扫光窗切成 ≤3 段 → ``[(文本, 样式), …]``（dim / 亮 / dim）。

        参考 OpenClaw ``shimmerText``：窗位 ``pos = ⌊elapsed/步长⌋ mod
        (n+窗宽)``，从左缘进入、右缘没出后回绕。``start = max(0,
        pos−窗宽)``、``end = min(n−1, pos)``——窗在两端半进半出，扫过
        全程无跳变。纯函数（只依赖 elapsed）→ 测试确定。
        """
        n = len(label)
        if n == 0:
            return []
        pos = int(elapsed / _SHIMMER_STEP_S) % (n + _SHIMMER_WIDTH)
        start = max(0, pos - _SHIMMER_WIDTH)
        end = min(n - 1, pos)
        bright = f"bold {ACCENT}"
        spans: list = []
        if start > 0:
            spans.append((label[:start], DIM))
        spans.append((label[start:end + 1], bright))
        if end + 1 < n:
            spans.append((label[end + 1:], DIM))
        return spans

    def _spinner_text(self, elapsed: float) -> Text:
        """Animated braille spinner + 标签扫光 — 单一强调色（v0.5.0 去掉
        彩虹循环：色彩循环是游乐场气质，静止色相 + 动态字形/字重同样
        传达"进行中"）。标签（Thinking…/Answering…）带移动高亮窗
        （:meth:`_shimmer_spans`，参考 OpenClaw tui-waiting.ts）——
        文字内容恒定、只有样式逐帧移动，缓存/帧 diff 不变量不破。"""
        glyph = _SPIN[int(elapsed * 1000 / 80) % len(_SPIN)]
        label = "Thinking…" if not self._has_body() else "Answering…"
        # "esc to interrupt" 常驻提示（v0.4.1）：思考与输出阶段都可打断，
        # 能力必须可见——用户报告"不知道能打断"即缺此提示。
        # （窗内滚动提示已随尾窗删除：scrollback 原生上翻无需热键。）
        text = Text()
        text.append(f"  {glyph} ", style=ACCENT)
        for seg_text, seg_style in self._shimmer_spans(label, elapsed):
            text.append(seg_text, style=seg_style)
        text.append(
            f"  ({elapsed:.1f}s)  ·  esc to interrupt",
            style="dim",
        )
        return text

    # ── 状态层（deck）：frame 之上的计划面板 + 队列 + 子代理舰队 ──

    @staticmethod
    def _deck_line(text: str, style: str = "") -> Text:
        """单行 deck 行。**硬不变量：1 deck 行 ≡ 1 终端行**——no_wrap +
        ellipsis 杜绝自动换行撑高区域、破坏视口预算与擦除行数计算。"""
        return Text(text, style=style, no_wrap=True, overflow="ellipsis")

    @staticmethod
    def _deck_markup(markup: str) -> Text:
        """带 markup 的单行 deck 行（同 _deck_line 的单行不变量）。"""
        t = Text.from_markup(markup)
        t.no_wrap = True
        t.overflow = "ellipsis"
        return t

    def _deck_spin(self) -> str:
        return _SPIN[int(time.monotonic() * 1000 / 80) % len(_SPIN)]

    def _deck_renderable(self, snap: list, extra_reserve: int = 0) -> tuple:
        """构建**上状态层** → ``(Group | None, 行数)``（渲染在输入框之上）。

        自上而下两块：**Plan**（todos 计划面板）→ **Queue**（流式期间
        排队待发的跟进消息，FIFO 全列、跨轮常驻直至全部发出）。子代理
        列表在输入框之下（见 :meth:`_fleet_deck_renderable`）。

        ``extra_reserve``：deck 之外已占用的额外行——从预算中扣除，
        保证叠加后仍恒有 max_lines ≥ 5。

        fail-open：任何异常（坏 todos 数据、console 取高失败……）落回
        无 deck——与 _LiveView 的回落哲学一致，状态层绝不拖垮主渲染。
        """
        try:
            todos = (
                list(self._todos_provider()) if self._todos_provider else []
            )
        except Exception:
            todos = []
        plan_items = [t for t in todos if isinstance(t, dict)]
        # 排队待发全列（FIFO）：跨轮留存（console._input_queue，REPL
        # 逐条 pop）+ 本轮流式中新排（self._queued，stop 时才并入前者）。
        # 顺序拼接即全貌，无重复——并入发生在 stop，而 done 态不渲染
        # deck，两源从不同帧出现。
        with self._queued_lock:
            queued_now = list(self._queued)
        console_q = getattr(self._console, "_input_queue", None) or []
        queue_items = [str(q) for q in list(console_q)]
        queue_items += [str(q) for q in queued_now]
        if not plan_items and not queue_items:
            return None, 0
        try:
            height = self._rich.height
        except Exception:
            height = 24
        # 视口预算：恒保 max_lines ≥ 5（_windowed 的 <5 兜底是退化终端
        # 的最后防线，正常路径由本预算钉死）
        budget = max(0, height - _VIEWPORT_RESERVE - 5 - extra_reserve)
        if budget == 0:
            return None, 0

        plan_allow = min(len(plan_items), _DECK_PLAN_ROWS)
        queue_allow = min(len(queue_items), _DECK_QUEUE_ROWS)
        headers = (1 if plan_items else 0) + (1 if queue_items else 0)
        # 折叠行（"+N more"）计数：某块被裁过就占一行
        def _overflow() -> int:
            return (
                (1 if len(plan_items) > plan_allow else 0)
                + (1 if len(queue_items) > queue_allow else 0)
            )
        # 超预算时先裁较大的块，直到放下
        while headers + plan_allow + queue_allow + _overflow() > budget:
            if plan_allow <= 0 and queue_allow <= 0:
                break
            if plan_allow >= queue_allow and plan_allow > 0:
                plan_allow -= 1
            elif queue_allow > 0:
                queue_allow -= 1

        rows: list = []
        if plan_items:
            done = sum(
                1 for t in plan_items if t.get("status") == "completed"
            )
            rows.append(self._deck_markup(
                f" [bold]Plan[/bold] [dim]{done}/{len(plan_items)}[/dim]"))
            for t in plan_items[:plan_allow]:
                status = t.get("status")
                if status == "completed":
                    rows.append(self._deck_line(
                        f"  {MARK_OK} {t.get('content') or ''}", "green"))
                elif status == "in_progress":
                    # activeForm 的存在意义：进行中一项显示"正在做什么"
                    label = t.get("activeForm") or t.get("content") or ""
                    rows.append(self._deck_line(
                        f"  {self._deck_spin()} {label}", ACCENT))
                else:
                    rows.append(self._deck_line(
                        f"  {MARK_PENDING} {t.get('content') or ''}", "dim"))
            if len(plan_items) > plan_allow:
                rows.append(self._deck_line(
                    f"  +{len(plan_items) - plan_allow} more", "dim"))
        if queue_items:
            # 队列面板：按序全列（超 _DECK_QUEUE_ROWS 折叠 +N more），
            # 位置在 Plan 之下、输入框之上——发出后 REPL 从头部 pop，
            # 面板随下一轮流式区重建，用户视角常驻直至清空。
            rows.append(self._deck_markup(
                f" [bold]Queue[/bold] [dim]({len(queue_items)})[/dim]"))
            for q in queue_items[:queue_allow]:
                rows.append(self._deck_line(f"  {MARK_BULLET} {q}", "dim"))
            if len(queue_items) > queue_allow:
                rows.append(self._deck_line(
                    f"  +{len(queue_items) - queue_allow} more", "dim"))
        if not rows:
            return None, 0
        return Group(*rows), len(rows)

    def _fleet_deck_renderable(self, snap: list) -> tuple:
        """构建**下状态层** → ``(Group | None, 行数)``（渲染在输入框之下）。

        子代理列表，含**主代理条目（编号 0）**：↓ 选中 + Enter 把输入
        框上方的主视图切换到选中条目（0 = 主回答，1..N = 子代理详情）。
        ❯ 标记待确认选择（优先）或当前视图条目。无子代理时不渲染。
        """
        if not snap:
            return None, 0
        try:
            height = self._rich.height
        except Exception:
            height = 24
        budget = max(0, height - _VIEWPORT_RESERVE - 5)
        if budget == 0:
            return None, 0
        fleet_allow = min(len(snap), _DECK_FLEET_ROWS)
        # 头部 1 + 主条目 1 + 子代理行 + 折叠行，超预算裁子代理行
        while 2 + fleet_allow + (1 if len(snap) > fleet_allow else 0) > budget:
            if fleet_allow <= 0:
                break
            fleet_allow -= 1
        # ❯ 标记：待确认选择优先，其次当前视图条目
        marked = (
            self._fleet_selected
            if self._fleet_selected >= 0 else self._focus
        )
        rows: list = [self._deck_markup(
            f" [bold]Agents[/bold] [dim]({len(snap)})"
            f" · ↓ select · ↵ view[/dim]")]
        # 主代理条目（0）：Enter 回到主回答视图
        main_head = (
            f"[{ACCENT_BOLD}]❯0[/]" if marked == 0 else "[dim] 0[/dim]"
        )
        main_style = "bold" if marked == 0 else "dim"
        rows.append(self._deck_markup(
            f"{main_head} [{main_style}]main[/][dim] · main answer[/dim]"))
        for v in snap[:fleet_allow]:
            secs = f"{v['elapsed']:.0f}s"
            tools = f"{v['tools_count']} tools"
            # 编号 + 标记：恒 2 格宽前缀（"❯N" / " N"）。
            # 标签经 markup 转义：子代理描述可含方括号。
            head = (
                f"[{ACCENT_BOLD}]❯{v['id']}[/]"
                if marked == v["id"] else f"[dim] {v['id']}[/dim]"
            )
            label = escape(v["label"])
            if v["status"] == "running":
                body = (
                    f"[{ACCENT}] {self._deck_spin()} {label}"
                    f" · {tools} · {secs}[/]"
                )
            elif v["status"] == "error":
                body = f"[red] {MARK_FAIL} {label} · {secs}[/]"
            else:
                body = f"[green] {MARK_OK} {label} · {tools} · {secs}[/]"
            rows.append(self._deck_markup(head + body))
        if len(snap) > fleet_allow:
            rows.append(self._deck_line(
                f"  +{len(snap) - fleet_allow} more", "dim"))
        return Group(*rows), len(rows)

    def _plugin_deck_renderable(self) -> tuple:
        """插件 UI 面板（ui/v1，P-D）→ ``(Group | None, 行数)``。

        渲染在输入框之下、舰队列表之后。征集与故障隔离（崩溃跳过 /
        熔断摘除 / 行数限额 / refresh_hz 节流）全在 UiPanelCollector
        （services/assembly.py）——渲染帧绝不能被插件拖死；这里只做
        markup 行化（deck 行不变量：no_wrap + ellipsis ≡ 1 终端行）与
        单面板兜底（坏 markup → 该面板本帧缺席）。视口预算内超出行
        折叠成 "+N more"。None（无面板 / 无收集器）→ 零行为变化。
        """
        if self._panels is None:
            return None, 0
        try:
            panel_list = self._panels.panels()
        except Exception:
            return None, 0
        if not panel_list:
            return None, 0
        try:
            height = self._rich.height
        except Exception:
            height = 24
        budget = max(0, height - _VIEWPORT_RESERVE - 5)
        if budget == 0:
            return None, 0
        rows: list = []
        dropped = 0
        for _name, lines in panel_list:
            try:
                panel_rows = [self._deck_markup(ln) for ln in lines]
            except Exception:
                continue  # 坏 markup（未闭合标签等）→ 该面板本帧缺席
            for row in panel_rows:
                if len(rows) >= budget:
                    dropped += 1
                else:
                    rows.append(row)
        if not rows:
            return None, 0
        if dropped:
            rows.append(self._deck_line(f"  +{dropped} more", "dim"))
        return Group(*rows), len(rows)

    def _detail_view(self, view: dict, deck_h: int):
        """Ctrl-O 切换到的子代理详情视图：头行 + 捕获事件流的末尾窗口。

        纯 Text.from_markup 行（非 Markdown）：子代理流以工具指示行为主，
        每 tick 重建 ≤200 行成本可忽略，无需缓存、无闪烁面。
        """
        try:
            height = self._rich.height
        except Exception:
            height = 24
        max_lines = max(3, height - _VIEWPORT_RESERVE - deck_h - 1)
        status_label = {
            "running": f"[{ACCENT}]{self._deck_spin()} running[/{ACCENT}]",
            "done": f"[{SUCCESS_STYLE}]{MARK_OK} done[/{SUCCESS_STYLE}]",
            "error": f"[{ERROR_STYLE}]{MARK_FAIL} error[/{ERROR_STYLE}]",
        }.get(view["status"], view["status"])
        header = self._deck_markup(
            f" [bold]Agent {view['id']}: {escape(view['label'])}[/bold]"
            f" [dim]·[/dim] {status_label}"
            f" [dim]· {view['tools_count']} tools"
            f" · {view['elapsed']:.0f}s"
            f" · ↑/↓ switch · alt+0 back[/dim]",
        )
        lines = list(view["lines"])
        if view["pending"]:
            lines.append(view["pending"])
        parts: list = [header]
        window = lines[-max_lines:]
        if window:
            parts.extend(Text.from_markup(ln) for ln in window)
        else:
            parts.append(Text("  (no output yet)", style="dim"))
        return Group(*parts)


if __name__ == "__main__":
    from types import SimpleNamespace
    clean = _RICH_TAG.sub("", "[dim]● tool[/dim] ran [bold]ls -la[/bold]")
    assert clean == "● tool ran ls -la", clean

    console = SimpleNamespace(
        _console=None, _input_queue=[], _frame_on_screen=False,
        _frame_renderable=lambda i, o: Text(""),  # 桩，避免真实终端 I/O
    )
    svc = StreamingService(console, input_tokens=5)  # Live 未启动 → 纯缓冲
    for tok in ("Hel", "lo", " world"):
        svc.feed(tok)
    assert svc._segments == [["text", "Hello world"]]
    assert svc._token_count == 3

    # 结构化段：工具事件落成记录；task 起始跳过（deck 展示）、结果回显
    from ..agent import ToolStartEvent, ToolResultEvent
    svc_e = StreamingService(console, input_tokens=0)
    svc_e.feed(ToolStartEvent(name="read_file"))
    svc_e.feed(ToolResultEvent(name="read_file", output="boom", is_error=True))
    svc_e.feed(ToolResultEvent(name="write_file", output="ok", is_error=False))
    svc_e.feed(ToolResultEvent(name="write_file", output="", is_error=False))
    svc_e.feed(ToolStartEvent(name="task"))  # 子代理起始 → 跳过（deck 展示）
    svc_e.feed(ToolResultEvent(
        name="task", output="Subagent 'x' finished: d\n\nreport", is_error=False,
    ))
    segs = svc_e._segments
    assert [k for k, _ in segs] == ["tool"] * 4, segs
    r0 = segs[0][1]
    assert (r0.name, r0.status, r0.output, r0.is_error) == (
        "read_file", "error", "boom", True)
    r1 = segs[1][1]
    assert (r1.name, r1.status, r1.output) == ("write_file", "done", "ok")
    assert segs[2][1].output == ""  # 空输出也落记录（渲染 (No output)）
    assert segs[3][1].name == "task" and "report" in segs[3][1].output
    assert svc_e._token_count == 5  # task 起始不计，其余事件各 1

    # spinner 标签随内容切换：空 → Thinking，有内容 → Answering
    svc._segments = []
    assert "Thinking" in svc._spinner_text(0.3).plain
    svc._segments = [["text", "hi"]]
    assert "Answering" in svc._spinner_text(0.3).plain

    # 动态渲染对象每次刷新都重建视图（从而重算耗时）；done 时隐藏 spinner。
    # —— 这是"实时计时"的核心。
    svc._t0 = time.monotonic() - 1.23
    svc._done = False
    calls = {"n": 0}
    _orig = svc._build_renderable
    def _counting():
        calls["n"] += 1
        return _orig()
    svc._build_renderable = _counting
    assert len(list(_LiveView(svc).__rich_console__(None, None))) == 1 and calls["n"] == 1
    svc._done = True
    done_group = svc._build_renderable()
    assert all("Thinking" not in getattr(p, "plain", "") for p in done_group.renderables)

    # 端到端：完全不 feed（模拟思考阶段），自动刷新线程也会周期性重建视图
    # → 耗时持续前进，而不是卡死到 done 才跳变。
    import io as _io
    from rich.console import Console as _RichConsole
    _rc = _RichConsole(file=_io.StringIO(), force_terminal=True, width=100)
    console._console = _rc
    svc2 = StreamingService(console, input_tokens=0)
    svc2._t0 = time.monotonic()
    _n = {"c": 0}
    _ob = svc2._build_renderable
    def _cb():
        _n["c"] += 1
        return _ob()
    svc2._build_renderable = _cb
    _live = Live(_LiveView(svc2), console=_rc, refresh_per_second=20, auto_refresh=True)
    _live.start()
    time.sleep(0.3)  # 期间从不调用 feed()
    _live.stop()
    assert _n["c"] >= 3, f"思考阶段也应多次重建视图，实际 {_n['c']} 次"

    print(f"rich-tag strip ✓, spinner[0]={_SPIN[0]}, "
          f"segments={len(svc._segments)}, "
          f"dynamic-timer ✓ (no-token rebuilds={_n['c']})")
    print("openx/services/streaming.py OK ✓")
