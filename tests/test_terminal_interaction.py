"""终端交互层回归测试 —— 用 pyte 终端模拟器验证屏幕级行为。

覆盖两个用户报告（SDD sdd-terminal-interaction.md 的不变量必须保持）：

Bug 2（光标回归）
    用户输入处必须可见光标。Rich ``Live`` 在流式期间隐藏光标（?25l），
    若在 done/cancel/暂停-弹窗-恢复 的任何路径上没有配对地恢复（?25h），
    用户会在输入框/弹窗里失去光标。验证：流式中隐藏、done/cancel 后
    可见、暂停弹窗期间可见（恢复后重新隐藏）、Ctrl-C 打断也恢复。

Bug 3（超屏不跟随）
    响应超过一屏后，Rich Live 默认裁剪（vertical_overflow）使最新 token
    不可见。修复：StreamingService 只渲染**末尾视口窗口**（冠以 ``↑ …``
    标记）——验证：长流式途中屏幕底部是最新内容（而非开头）、标记存在、
    组永不超屏、框仍是末元素。

流式翻页闪烁（2026-07-25 修复）
    超一屏的流式回答翻页时整屏闪烁。四个根因对应四项修复：刷新率
    10Hz→5Hz、feed 强制刷新时间门控、响应渲染对象按 buffer 缓存、
    长响应模式区域高度恒定补齐（``_long_mode`` 闩）。屏幕级帧 diff
    回归见 :class:`TestFlickerRegression`。

pyte 细节（SDD §9）：真实 TTY 的输出驱动把 ``\\n`` 映射成 ``\\r\\n``
（ONLCR），Rich 只发裸 ``\\n``——故模拟器必须开 LNM 模式，否则逐行阶梯
式错位（那不是被测代码的 bug）。Live 的自动刷新线程会与 StringIO 并发
写（段错误）并劫持 stdout，统一换成 ``auto_refresh=False`` 的替身。

运行：``python -m pytest tests/test_terminal_interaction.py -q``
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
import pyte
import pyte.modes
from rich.console import Console as RichConsole
from rich.text import Text

from openx.services.streaming import (
    StreamingService,
    _SCROLL_MARKER,
    _SHIMMER_STEP_S,
    _SHIMMER_WIDTH,
)


# ── 测试基建 ──────────────────────────────────────────────────────


@pytest.fixture
def deterministic_live(monkeypatch):
    """关掉 Live 自动刷新线程与 stdout 劫持（确定性 + 不吞 pytest 输出）。

    拦截 ``_ResizeAwareLive``（start() 实际构造的类，``Live`` 子类）；
    测试 console 无 ``_resize`` 属性 → resize 通道为 None，行为与裸 Live
    一致（宽度漂移检测因定宽 console 恒不触发）。
    """
    import openx.services.streaming as streaming_mod
    from openx.services.streaming import _ResizeAwareLive

    class _Live(_ResizeAwareLive):
        def __init__(self, *args, **kwargs):
            kwargs.update(
                auto_refresh=False,
                redirect_stdout=False,
                redirect_stderr=False,
            )
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(streaming_mod, "_ResizeAwareLive", _Live)


class Harness:
    """StreamingService + pyte 屏幕：feed 后 flush 进模拟器，可读屏。"""

    def __init__(self, rows: int = 24, cols: int = 80):
        self.screen = pyte.Screen(cols, rows)
        self.screen.set_mode(pyte.modes.LNM)  # 模拟真实 TTY 的 ONLCR
        self.pyte = pyte.Stream(self.screen)
        self.buf = io.StringIO()
        rc = RichConsole(
            file=self.buf, width=cols, height=rows, force_terminal=True
        )
        console = SimpleNamespace(
            _console=rc,
            _input_queue=[],
            _frame_on_screen=False,
            _input_capture=None,
            _frame_renderable=lambda i, o: Text("FRAME"),
        )
        self.svc = StreamingService(console, input_tokens=0)

    def flush(self) -> None:
        """把 Rich 写出的 ANSI 喂进 pyte 屏幕。"""
        self.pyte.feed(self.buf.getvalue())
        self.buf.seek(0)
        self.buf.truncate()

    def rows(self) -> list[str]:
        return [
            "".join(c.data for c in self.screen.buffer[y].values())
            for y in range(self.screen.lines)
        ]

    def nonempty(self) -> list[tuple[int, str]]:
        return [(y, r.rstrip()) for y, r in enumerate(self.rows()) if r.strip()]


# ── Bug 2：光标可见性 ─────────────────────────────────────────────


class TestCursorVisibility:
    """光标：流式中隐藏，任何"轮到用户输入"的时刻必须可见。"""

    def test_hidden_during_stream_shown_after_done(self, deterministic_live):
        h = Harness()
        h.svc.start()
        h.svc.feed("hello world")
        h.svc._live.refresh()
        h.flush()
        assert h.screen.cursor.hidden is True, "流式期间光标应隐藏"

        h.svc.done()
        h.flush()
        assert h.screen.cursor.hidden is False, "done 后光标必须恢复"

    def test_shown_after_cancel(self, deterministic_live):
        h = Harness()
        h.svc.start()
        h.svc.feed("partial")
        h.svc._live.refresh()
        h.flush()
        h.svc.cancel()
        h.flush()
        assert h.screen.cursor.hidden is False, "cancel（错误路径）必须恢复光标"

    def test_shown_while_paused_for_dialog(self, deterministic_live):
        """暂停（弹窗期间）光标可见——弹窗要读键；恢复后重新隐藏。"""
        h = Harness()
        h.svc.start()
        h.svc.feed("streaming…")
        h.svc._live.refresh()
        h.flush()
        assert h.screen.cursor.hidden is True

        h.svc.pause()  # on_dialog_start → display.pause
        h.flush()
        assert h.screen.cursor.hidden is False, "弹窗期间光标必须可见"

        h.svc.resume()  # on_dialog_end → display.resume
        assert h.screen.cursor.hidden is False  # 尚未重渲，状态不变
        h.svc._live.refresh()
        h.flush()
        assert h.screen.cursor.hidden is True, "恢复流式后光标重新隐藏"

        h.svc.done()
        h.flush()
        assert h.screen.cursor.hidden is False

    def test_nested_pause_keeps_cursor_visible_until_fully_resumed(
        self, deterministic_live
    ):
        h = Harness()
        h.svc.start()
        h.svc._live.refresh()
        h.flush()
        h.svc.pause()
        h.svc.pause()   # 权限钩子 + 弹窗钩子叠加
        h.svc.resume()  # 内层结束
        assert not h.svc._live.is_started, "pause×2/resume×1 仍在暂停"
        h.svc.resume()
        assert h.svc._live.is_started
        h.svc.done()
        h.flush()
        assert h.screen.cursor.hidden is False

    def test_done_while_paused_still_shows_cursor(self, deterministic_live):
        """防御路径：流在暂停中被收尾，光标不得遗留隐藏。"""
        h = Harness()
        h.svc.start()
        h.svc._live.refresh()
        h.flush()
        h.svc.pause()
        h.flush()
        h.svc.done()  # 暂停中的收尾
        h.flush()
        assert h.screen.cursor.hidden is False

    @pytest.mark.asyncio
    async def test_ctrl_c_during_stream_restores_cursor(
        self, deterministic_live, tmp_path
    ):
        """KeyboardInterrupt 打断流式：cancel 恢复光标后再上抛。"""
        from openx.cli.interactive import _stream_response
        from openx.config import OpenXConfig
        from openx.ui.console import Console

        console = Console(config=OpenXConfig(workspace=str(tmp_path)))
        console._console = RichConsole(
            file=io.StringIO(), width=80, height=24, force_terminal=True
        )

        class FakeExecutor:
            on_prompt_start = None
            on_prompt_end = None

        class FakeAgent:
            config = SimpleNamespace(stream=True)
            total_input_tokens = 0
            total_output_tokens = 0
            tool_executor = FakeExecutor()
            todos: list = []    # 状态层 providers（v0.4.0）
            fleet = None

            async def stream_run(self, content):
                yield "partial…"
                raise KeyboardInterrupt  # 用户在流式中按 Ctrl-C

        with pytest.raises(KeyboardInterrupt):
            await _stream_response(FakeAgent(), console, "hi")

        # cancel() 已停掉 Live → 输出流里有配对的 ?25h（光标恢复）
        raw = console._console.file.getvalue()
        assert "\033[?25h" in raw, "Ctrl-C 退出路径必须恢复光标"
        assert console._input_capture is None  # 捕获线程已停（终端模式恢复）
        # 钩子清空，绝不泄漏
        assert console.on_dialog_start is None and console.on_dialog_end is None
        assert FakeAgent.tool_executor.on_prompt_start is None


# ── Bug 3：超屏响应自动跟随 ───────────────────────────────────────


class TestViewportFollow:
    """响应超屏时屏幕底部必须是最新内容（而非开头）。"""

    def test_long_stream_bottom_shows_latest_content(self, deterministic_live):
        h = Harness(rows=24, cols=80)
        h.svc.start()
        for i in range(1, 61):  # 60 段文本 ≫ 24 行视口
            h.svc.feed(f"response line {i}\n\n")
        h.svc._live.refresh()
        h.flush()

        rows = h.rows()
        # 最新内容在屏上……
        assert any("response line 60" in r for r in rows), "最新 token 必须可见"
        assert any("response line 59" in r for r in rows)
        # ……开头已被窗口滑出
        assert not any("response line 1 " in r for r in rows), "开头不应仍占屏"
        assert not any("response line 40 " in r for r in rows)
        # 末尾窗口标记存在
        assert any(_SCROLL_MARKER in r for r in rows), "↑ 标记缺失"
        # 框仍是末元素：FRAME 位于 spinner 之下
        visible = h.nonempty()
        frame_idx = next(y for y, t in visible if "FRAME" in t)
        spin_idx = next(y for y, t in visible if "Answering" in t)
        assert frame_idx > spin_idx
        # 光标在流式中保持隐藏
        assert h.screen.cursor.hidden is True

    def test_window_content_is_aligned(self, deterministic_live):
        """窗口内每行顶格——Rich 相对光标计算不得错位（历史抖动 bug）。"""
        h = Harness(rows=24, cols=80)
        h.svc.start()
        for i in range(1, 61):
            h.svc.feed(f"response line {i}\n\n")
        h.svc._live.refresh()
        h.flush()
        content = [r.rstrip() for r in h.rows() if "response line" in r]
        assert content, "no content rendered"
        assert all(r.startswith("response line") for r in content), (
            f"行未顶格（光标错位）: {content[:3]!r}"
        )

    def test_group_never_exceeds_viewport(self, deterministic_live):
        """整组永不超过视口：FRAME 下方不得再有内容行（不触底滚屏）。"""
        h = Harness(rows=24, cols=80)
        h.svc.start()
        for i in range(1, 200):  # 极长响应
            h.svc.feed(f"line {i}\n")
        h.svc._live.refresh()
        h.flush()
        visible = h.nonempty()
        frame_idx = max(y for y, t in visible if "FRAME" in t)
        assert frame_idx <= 23, "框不得被推出视口"
        below = [t for y, t in visible if y > frame_idx]
        assert not below, f"框下方出现内容: {below!r}"

    def test_short_stream_shows_full_content_without_marker(
        self, deterministic_live
    ):
        """未超屏：完整渲染、无 ↑ 标记（行为与修复前一致）。"""
        h = Harness(rows=24, cols=80)
        h.svc.start()
        h.svc.feed("# Title\n\nshort answer, one line.")
        h.svc._live.refresh()
        h.flush()
        rows = h.rows()
        assert any("Title" in r for r in rows)
        assert any("short answer" in r for r in rows)
        assert not any(_SCROLL_MARKER in r for r in rows)

    def test_done_keeps_tail_visible_and_cursor_shown(self, deterministic_live):
        """done 后：末尾窗口仍在屏上（用户看得见结尾），光标恢复。"""
        h = Harness(rows=24, cols=80)
        h.svc.start()
        for i in range(1, 61):
            h.svc.feed(f"response line {i}\n\n")
        h.svc._live.refresh()
        h.flush()
        h.svc.done()
        h.flush()
        rows = h.rows()
        assert any("response line 60" in r for r in rows), "结束后结尾必须留屏"
        assert h.screen.cursor.hidden is False

    def test_done_renders_full_transcript_for_scrollback(self, deterministic_live):
        """done 全文渲染（非尾窗）：超屏内容滚入 scrollback，上翻可见
        完整 transcript；``↑ …`` 只是流式期跟随标记，绝不入 transcript。

        pyte 无 scrollback——滚出顶部的行不在屏上但**必在输出字节里**
        （真实终端中它们已进 scrollback）：断言首行在 done 渲染字节中
        存在、在屏上不存在（已滚出），尾行在屏上，且屏上无 ↑ …。
        """
        h = Harness(rows=24, cols=80)
        h.svc.start()
        for i in range(1, 61):
            h.svc.feed(f"response line {i}\n\n")
            h.svc._live.refresh()
            h.flush()
        # 流式期：尾窗 + 标记（跟随最新 token 的设计保留）
        assert any(_SCROLL_MARKER in r for r in h.rows())

        h.svc.done()
        done_bytes = h.buf.getvalue()  # done 的渲染字节，先不喂 pyte
        h.flush()

        # 全文确被渲染：首行在字节里（真实终端 → scrollback）。
        # Rich 输出行补齐全宽（行尾填充空格），故不带 \n 匹配。
        assert "response line 1" in done_bytes, "done 未渲染全文（仍为尾窗）"
        assert "response line 60" in done_bytes
        rows = h.rows()
        # 首行已滚出视口（24 行屏装不下 60 段）——证明是真滚屏而非裁剪
        assert not any(r.strip() == "response line 1" for r in rows)
        # 尾行留屏 + transcript 无残留标记
        assert any("response line 60" in r for r in rows)
        assert not any(_SCROLL_MARKER in r for r in rows), "↑ … 不应入 transcript"


# ── 工具结果回显段落分隔（粘连乱序回归）──────────────────────────


class TestEchoParagraphSeparation:
    """工具/子代理结果回显与前后模型文字各自成段，绝不粘连。

    用户报告"有子代理时主输出乱序"根因：回显无前缀粘进上一段尾部、
    无后缀则下一轮正文（裸 token 流）粘进回显 → Markdown 重排成一段。
    """

    def test_echo_not_glued_to_surrounding_text(self, deterministic_live):
        from openx.agent import ToolResultEvent

        h = Harness(rows=24, cols=80)
        h.svc.start()
        h.svc.feed("I will dispatch two agents:")
        h.svc.feed(ToolResultEvent(
            name="task",
            output="Subagent report: found 3 files",
            is_error=False,
        ))
        h.svc.feed("Summary: all good")
        h.svc._live.refresh()
        h.flush()

        rows = [r.rstrip() for r in h.rows()]
        for r in rows:
            assert not ("dispatch" in r and "Subagent" in r), (
                f"回显粘进前文：{r!r}"
            )
            assert not ("Subagent" in r and "Summary" in r), (
                f"后续正文粘进回显：{r!r}"
            )
        ys = {}
        for y, r in enumerate(rows):
            for key in ("dispatch", "Subagent", "Summary"):
                if key in r and key not in ys:
                    ys[key] = y
        assert set(ys) == {"dispatch", "Subagent", "Summary"}
        assert ys["Subagent"] - ys["dispatch"] >= 2, "回显与前文须空行分隔"
        assert ys["Summary"] - ys["Subagent"] >= 2, "后续正文与回显须空行分隔"


# ── 流式滚动回看（↑/↓/PgUp/PgDn）─────────────────────────────────


class TestScrollback:
    """长回答流式期间可上翻回看：视窗偏移 + 冻结视图 + 回底恢复跟随。

    修复"回答边输出边滚走、来不及看"：offset>0 时视窗脱离末尾，新内容
    从 "↓ …" 标记下持续进入而不拽动视图；↓ 回底（offset=0）恢复跟随。
    """

    @staticmethod
    def _stream_long(h, n=60, prefix="response line"):
        for i in range(1, n + 1):
            h.svc.feed(f"{prefix} {i}\n\n")
        h.svc._live.refresh()
        h.flush()

    @staticmethod
    def _press(h, key, times=1):
        for _ in range(times):
            h.svc._capture._hotkeys.append(key)
        h.svc._live.refresh()
        h.flush()

    def test_scroll_up_freezes_view_with_down_marker(self, deterministic_live):
        h = Harness(rows=24, cols=80)
        h.svc.start()
        self._stream_long(h)
        assert any("response line 60" in r for r in h.rows())

        self._press(h, "\x1b[A", 3)  # ↑ ×3
        assert h.svc._scroll_offset == 3
        rows = h.rows()
        assert any("↓ …" in r for r in rows), "上翻后应显示'下文未显示'标记"
        assert not any("response line 60" in r for r in rows), "尾部应离开视窗"
        assert any(_SCROLL_MARKER in r for r in rows), "上文标记仍在"
        assert any("↑/↓ scroll" in r for r in rows), "spinner 应提示滚动态"

    def test_scroll_down_to_bottom_resumes_follow(self, deterministic_live):
        h = Harness(rows=24, cols=80)
        h.svc.start()
        self._stream_long(h)
        self._press(h, "\x1b[A", 2)
        assert h.svc._scroll_offset == 2
        self._press(h, "\x1b[B", 2)  # ↓ 回底
        assert h.svc._scroll_offset == 0
        rows = h.rows()
        assert any("response line 60" in r for r in rows)
        assert not any("↓ …" in r for r in rows)

    def test_new_content_keeps_frozen_view(self, deterministic_live):
        """上翻后新内容不拽动视图（冻结），从 ↓ … 之下进入。"""
        h = Harness(rows=24, cols=80)
        h.svc.start()
        self._stream_long(h)
        self._press(h, "\x1b[5~")  # PgUp → 半页偏移
        offset = h.svc._scroll_offset
        assert offset > 0

        for i in range(1, 6):
            h.svc.feed(f"extra line {i}\n\n")
        h.svc._live.refresh()
        h.flush()
        assert h.svc._scroll_offset == offset, "新内容不得改变滚动偏移"
        rows = h.rows()
        assert any("↓ …" in r for r in rows)
        assert not any("extra line 5" in r for r in rows), "新内容在视窗之下"

    def test_pageup_half_page_and_clamp(self, deterministic_live):
        h = Harness(rows=24, cols=80)
        h.svc.start()
        self._stream_long(h)
        self._press(h, "\x1b[5~")
        assert h.svc._scroll_offset == (24 - 7) // 2  # 半页步长
        # 连续 PgUp 不得超过可滚范围（_windowed 内夹取）
        self._press(h, "\x1b[5~", 20)
        assert h.svc._scroll_offset <= 120  # 60 段 ×2 行 − 17 窗口的量级上界
        assert h.svc._scroll_offset >= (24 - 7) // 2

    def test_frame_row_stable_while_scrolling(self, deterministic_live):
        """latched 组高恒定：滚动期间 FRAME 行不移动（光标锚点不变量）。"""
        h = Harness(rows=24, cols=80)
        h.svc.start()
        self._stream_long(h)
        frame_y = max(y for y, t in h.nonempty() if "FRAME" in t)
        self._press(h, "\x1b[A", 5)
        frame_y2 = max(y for y, t in h.nonempty() if "FRAME" in t)
        self._press(h, "\x1b[5~")
        frame_y3 = max(y for y, t in h.nonempty() if "FRAME" in t)
        assert frame_y == frame_y2 == frame_y3 <= 23

    def test_scroll_offset_reset_each_turn(self, deterministic_live):
        h = Harness(rows=24, cols=80)
        h.svc.start()
        self._stream_long(h)
        self._press(h, "\x1b[A", 4)
        assert h.svc._scroll_offset == 4
        h.svc.done()   # done 渲染全文并归零偏移
        assert h.svc._scroll_offset == 0
        h.svc.start()  # 新一轮亦归零
        assert h.svc._scroll_offset == 0


# ── 流式翻页闪烁修复：屏幕级帧 diff 回归 ─────────────────────────


class TestFlickerRegression:
    """超一屏流式回答翻页闪烁（2026-07-25 修复）的屏幕级回归。

    四项修复中可在屏幕层断言的三项：

    a. 响应渲染对象按 buffer 内容缓存 → buffer 不变时两帧内容区
       **0 格变化**（仅 spinner 计时行可变）；
    b. ``_windowed`` 长响应模式补齐到恒定区高 → 跨越视口边界后
       FRAME 行位置锁定、后续增长不再位移；reflow 致瞬时收缩也仍
       补齐恒定高度；
    c. 单字符追加（不新增行）变化行数有界 → 无整区滚动放大。

    刷新率 10Hz→5Hz 与 feed 时间门控属频域行为，屏幕层不可直接
    断言，由上述不变量间接保障（帧间差异收敛到最小）。

    pyte 细节：Rich 每次刷新以 ``\\x1b[2K`` 逐行擦除全区再重写，
    pyte 中空白格的内部表示会由"从未写入"（行长 0）变为"擦除过"
    （整行空格）——肉眼不可见，但 ``rows()`` 原始串不相等。故帧
    diff 一律在 ``rstrip`` 后比较，只计可见字符。
    """

    @staticmethod
    def _diff_rows(before: list[str], after: list[str]) -> list[int]:
        """两帧间可见字符有异的行号（rstrip 后比较，见类注 pyte 细节）。"""
        return [
            y for y, (b, a) in enumerate(zip(before, after))
            if b.rstrip() != a.rstrip()
        ]

    @staticmethod
    def _frame_y(h: "Harness") -> int:
        return max(y for y, t in h.nonempty() if "FRAME" in t)

    def test_unchanged_buffer_zero_content_change(self, deterministic_live):
        """buffer 不变：内容区帧间 0 格变化（缓存命中、无重排）。"""
        h = Harness(rows=24, cols=80)
        h.svc.start()
        for i in range(1, 61):
            h.svc.feed(f"response line {i}\n\n")
        h.svc._live.refresh()
        h.flush()
        before = h.rows()

        h.svc._live.refresh()  # 无 feed：模拟纯自动刷新节拍
        h.flush()
        after = h.rows()

        changed = self._diff_rows(before, after)
        content_changed = [
            y for y in changed
            if "Answering" not in before[y] and "Answering" not in after[y]
        ]
        assert content_changed == [], (
            "buffer 不变时内容区应 0 格变化，实际变化行 "
            f"{content_changed}: "
            f"{[(y, before[y].rstrip()[:30], after[y].rstrip()[:30]) for y in content_changed[:3]]!r}"
        )

    def test_single_char_append_changes_bounded_rows(self, deterministic_live):
        """末段追加一字（不新增行）：可见变化 ≤ 3 行。

        实测 2 行（末内容行 + spinner 计时行；计时同 0.1s 桶时为 1 行）：
        末尾窗口锚定屏底，追加不触发重排增行时窗口不整体滚动，仅末行
        内容变。上界放宽到 3 行容许计时进位。修复前逐 token 全量重解析
        + reflow 放大，变化行数不可控（整段重排 → 整窗滚动）。
        """
        h = Harness(rows=24, cols=80)
        h.svc.start()
        for i in range(1, 60):
            h.svc.feed(f"response line {i}\n\n")
        h.svc.feed("response line 60")  # 末段不带尾换行：追加落在末行
        h.svc._live.refresh()
        h.flush()
        before = h.rows()

        h.svc.feed("x")  # 不换行追加：不产生新行
        h.svc._live.refresh()
        h.flush()
        after = h.rows()

        assert any("response line 60x" in r for r in after), (
            "追加内容必须上屏（缓存随 buffer 变化失效重建）"
        )
        changed = self._diff_rows(before, after)
        assert len(changed) <= 3, (
            f"单字符追加变化 {len(changed)} 行超出上界: {changed!r}"
        )

    def test_frame_row_locked_after_viewport_boundary(self, deterministic_live):
        """跨越视口边界后 FRAME 行位置锁定（区域高度恒定，锚点不抖）。"""
        h = Harness(rows=24, cols=80)
        h.svc.start()
        positions = []
        for i in range(1, 40):  # 从短到长增长，穿过视口上限
            h.svc.feed(f"line {i}\n\n")
            h.svc._live.refresh()
            h.flush()
            positions.append(self._frame_y(h))

        # 进入长响应模式（FRAME 到达最高位）之后的所有位置
        locked = positions[positions.index(max(positions)):]
        assert len(locked) >= 20, "长模式后的采样不足，无法验证锁定"
        assert len(set(locked)) == 1, (
            f"进入长模式后 FRAME 行应锁定，实际抖动: {sorted(set(locked))!r} "
            f"(全程 {positions!r})"
        )
        assert locked[0] <= 23, "锁定位置不得超出视口"

    def test_windowed_pads_to_fixed_height_when_latched(self, deterministic_live):
        """长模式闩后：即使 reflow 致行数瞬时收缩，也补齐到恰好 max_lines。"""
        from rich.markdown import Markdown

        h = Harness(rows=24, cols=80)
        h.svc.start()
        h.svc._long_mode = True  # 模拟已越过上限
        view = h.svc._windowed(Markdown("short"))
        lines = h.svc._rich.render_lines(view, pad=False)
        max_lines = 24 - 7  # _VIEWPORT_RESERVE：框 4 + spinner 1 + 余量 2
        assert len(lines) == max_lines, (
            f"补齐高度应恒定 {max_lines}，实际 {len(lines)}"
        )

    def test_short_response_not_padded_to_bottom(self, deterministic_live):
        """短响应就地显示：绝不补齐到屏底（SDD 目标 1 不受修复牵连）。"""
        h = Harness(rows=24, cols=80)
        h.svc.start()
        h.svc.feed("short answer")
        h.svc._live.refresh()
        h.flush()
        frame_y = self._frame_y(h)
        assert frame_y < 12, (
            f"短响应框应紧跟内容就地显示，实际 FRAME 在第 {frame_y} 行"
        )


# ── spinner 标签扫光（shimmer，参考 OpenClaw tui-waiting.ts）─────


class TestSpinnerShimmer:
    """spinner 标签移动高亮窗：只改样式不改文字。

    不变量：文字内容逐帧恒定（pyte 帧 diff 比字符不比样式、plain
    断言、单行不变量全不受影响）；窗位是 elapsed 的纯函数 → 确定性。
    """

    def _svc(self) -> StreamingService:
        return Harness().svc  # 只构造 service，不 start Live

    def test_window_trajectory_sweeps_and_wraps(self):
        label = "Thinking…"
        n = len(label)
        cycle = n + _SHIMMER_WIDTH

        def bright_range(elapsed: float):
            spans = StreamingService._shimmer_spans(label, elapsed)
            bright = [(t, s) for t, s in spans if s.startswith("bold")]
            assert len(bright) == 1, spans
            text = bright[0][0]
            off = label.index(text)      # 窗内文本必为连续子串
            return off, off + len(text)

        def expected(elapsed: float):
            pos = int(elapsed / _SHIMMER_STEP_S) % cycle
            # 半开区间（与 bright_range 同口径）：end 不含
            return max(0, pos - _SHIMMER_WIDTH), min(n - 1, pos) + 1

        # 全程钉死：左缘半进 → 向右扫 → 右缘半出 → 回绕，逐帧与公式一致
        for k in range(3 * cycle):
            assert bright_range(k * _SHIMMER_STEP_S) == expected(
                k * _SHIMMER_STEP_S
            ), f"第 {k} 步窗位偏移"

    def test_spans_reassemble_label_single_bright_segment(self):
        label = "Answering…"
        for k in range(40):
            spans = StreamingService._shimmer_spans(
                label, k * _SHIMMER_STEP_S / 2
            )
            assert "".join(t for t, _ in spans) == label, "分段拼接须还原全标签"
            styles = [s for _, s in spans]
            assert sum(s.startswith("bold") for s in styles) == 1
            assert all(s == "dim" for s in styles if not s.startswith("bold"))

    def test_plain_text_constant_across_frames(self):
        svc = self._svc()
        # 扫光只动样式：去掉随帧变化的 braille 字形前缀（2 空格+字形+空格）
        # 与秒数尾巴后，标签部分逐帧恒定。
        labels = {
            svc._spinner_text(k * _SHIMMER_STEP_S).plain[4:].split("(")[0].rstrip()
            for k in range(20)
        }
        assert labels == {"Thinking…"}, labels

    def test_label_switch_and_hints_intact(self):
        svc = self._svc()
        assert "Thinking" in svc._spinner_text(0.3).plain
        svc._segments = [["text", "hi"]]
        t = svc._spinner_text(0.3)
        assert "Answering" in t.plain
        assert "esc to interrupt" in t.plain
        assert t.plain.count("\n") == 0, "spinner 必须单行"
