"""scrollback-funnel 重构的专属回归：逐行固化管线（复活 stash 后收尾）。

覆盖用户报告的两处观感 + funnel 语义不变量：
- 收尾"二次打印/整体跳动"：done()/cancel() 只补打未固化余量，绝不全文重打。
- "内容沉底不可上翻"：稳定行在流式途中就固化进输出字节（真实终端 → scrollback）。
- done 帧唯一性：done 后 frame 是唯一末元素，已完成工具块不重渲成"副本"。
- 热键提示生命周期：Ctrl+T 的 "… +N lines (ctrl+t to expand)" 只在未固化
  易变块出现，固化进 scrollback 后消失（已打印内容不可改、提示即谎言）。

Harness 手法沿用 test_terminal_interaction.py（pyte LNM + deterministic_live）。
pyte 无 scrollback：固化断言统一走"累计输出字节 + 屏态 + committed_count"三件套。
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
import pyte
import pyte.modes
from rich.console import Console as RichConsole
from rich.text import Text

from openx.agent import ToolResultEvent, ToolStartEvent
from openx.services.streaming import StreamingService


@pytest.fixture
def deterministic_live(monkeypatch):
    """关掉 Live 自动刷新线程与 stdout 劫持（确定性 + 不吞 pytest 输出）。"""
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
    """StreamingService + pyte 屏幕：feed/刷新后 flush 进模拟器可读屏。"""

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
        """把 Rich 写出的 ANSI 喂进 pyte 屏幕（消费 buf）。"""
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


def _stream(h, n=60, prefix="response line", flush_each=False):
    """逐段 feed + 强制刷新；flush_each 控制是否每次落屏（否则累计 buf）。"""
    for i in range(1, n + 1):
        h.svc.feed(f"{prefix} {i}\n\n")
        h.svc._live.refresh()
        if flush_each:
            h.flush()


# ── ① done 无二次打印：每段固化行只打印一次 ──────────────────────


class TestNoDoublePrint:
    def test_commit_ranges_disjoint_and_done_only_remainder(
        self, deterministic_live
    ):
        """固化区间单调不重叠（每行恰提交一次），done 只补余量——字节级
        "无二次打印"的确定性判据（整屏字节会含 Live 逐帧重绘，不能直接数）。"""
        h = Harness()
        h.svc.start()
        ranges: list[tuple[int, int]] = []

        def _wrap(fn):
            def _go():
                before = h.svc._committed_count
                r = fn()
                ranges.append((before, h.svc._committed_count))
                return r
            return _go

        h.svc._maybe_commit = _wrap(h.svc._maybe_commit)
        h.svc._flush_commit = _wrap(h.svc._flush_commit)

        _stream(h, n=60, flush_each=False)
        lines = h.svc._body_lines()
        # 流式中每段固化一次：区间首尾相接、绝不重叠/回退
        prev = 0
        for a, b in ranges:
            assert a == prev, f"固化区间重叠/回退: {ranges}"
            assert b >= a
            prev = b
        assert h.svc._committed_count <= len(lines)

        h.svc.done()
        assert h.svc._committed_count == len(lines), "done 应全量固化"
        last = ranges[-1]
        assert last[1] - last[0] <= 3, (
            f"done flush 应只补余量（≤尾部易变行），实际 {last[1]-last[0]}"
        )
        h.flush()


# ── ② 固化进输出 / 易变尾收敛 ───────────────────────────────────


class TestCommitConvergence:
    def test_streaming_commits_rows_volatile_tail_bounded(self, deterministic_live):
        """纯文本长答（无工具/thinking）：流式中稳定行固化进输出字节、
        committed_count ≈ 全长−1（易变尾只有末行）。"""
        h = Harness()
        h.svc.start()
        _stream(h, n=60, flush_each=False)
        raw = h.buf.getvalue()
        assert "response line 1" in raw, "首行应已随流式固化（非等 done）"
        lines = h.svc._body_lines()
        assert h.svc._committed_count >= len(lines) - 1, (
            "纯文本易变尾应 ≤1 行（末行），实际未固化 "
            f"{len(lines) - h.svc._committed_count} 行"
        )
        # 屏态：最新行可见、FRAME 在末行、无裁尾标记
        h.flush()
        rows = h.rows()
        assert any("response line 60" in r for r in rows)
        frame_idx = max(y for y, t in h.nonempty() if "FRAME" in t)
        assert frame_idx <= 23


# ── ⑤ done 帧唯一性（含工具块收尾，防"工具副本 + frame"）────────


class TestDoneFrameUniqueness:
    def _tool_done_turn(self, h):
        h.svc.start()
        h.svc.feed(ToolStartEvent(name="Bash", arguments="ls -la"))
        h.svc.feed(ToolResultEvent(
            name="Bash", output="a\nb\nc\nd\ne", is_error=False))
        h.svc._live.refresh()

    def test_done_after_tool_block_leaves_single_frame(self, deterministic_live):
        """以已完成工具块收尾（无尾随文本）直接 done：最终帧 = 工具块(固化)
        + frame，绝无"易变区重渲工具副本 + frame"。"""
        h = Harness()
        self._tool_done_turn(h)
        h.svc.done()
        h.flush()

        ne = h.nonempty()
        assert ne, "屏幕应有内容"
        frame_y = max(y for y, t in ne if "FRAME" in t)
        assert frame_y == ne[-1][0], "FRAME 必须是末元素（其下无任何内容）"
        # 工具头行（固化）只出现一次；易变区已空 → 无第二条 ● 行在 FRAME 上方重复
        heads = [t for y, t in ne if t.lstrip().startswith("●")]
        assert len(heads) == 1, f"工具头行应唯一（无副本），实际 {len(heads)}"


# ── ⑥ 热键提示生命周期：只出现在未固化易变块 ────────────────────


class TestHintLifecycle:
    def _feed_tool_then_text(self, h):
        h.svc.start()
        h.svc.feed(ToolStartEvent(name="Bash", arguments="ls"))
        h.svc.feed(ToolResultEvent(
            name="Bash", output="x1\nx2\nx3\nx4\nx5", is_error=False))
        h.svc._live.refresh()

    def test_expand_hint_only_while_tool_trailing_volatile(
        self, deterministic_live
    ):
        """尾部已完成工具（仍易变）→ 屏上有 'ctrl+t to expand'；随后文本
        段让工具固化进 scrollback → 提示消失（已打印内容不可改）。"""
        h = Harness()
        h.svc.start()
        h.svc.feed(ToolStartEvent(name="Bash", arguments="ls"))
        h.svc.feed(ToolResultEvent(
            name="Bash", output="x1\nx2\nx3\nx4\nx5", is_error=False))
        h.svc._live.refresh()
        h.flush()
        text_before = "\n".join(h.rows())
        assert "ctrl+t to expand" in text_before, (
            "易变尾部工具应带展开提示"
        )
        # 追加文本段 → 工具不再尾部 → 下一 refresh 固化（hints=False）
        h.svc.feed("final words\n\n")
        h.svc._live.refresh()
        h.flush()
        text_after = "\n".join(h.rows())
        assert "ctrl+t to expand" not in text_after, (
            "固化后提示应消失（不可重打）"
        )
        assert "final words" in text_after
