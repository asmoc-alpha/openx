"""工具调用聚合展示（对标 Claude Code 折叠摘要）的专属回归。

覆盖：
- 连续只读探查 / shell 调用折成一行摘要（"Read 2 files, ran 1 command"）；
- 单条不聚合（保留逐条头行 + ⎿ 预览——"只有一条却折起来"是净损失）；
- 错误记录与写操作打断组，且逐条可见（红色块 = 审计语义）；
- 组内 running 记录不入组（状态点会变、结果未到）；
- Ctrl+T 在固化前可展开明细，固化进 scrollback 后定格为摘要行
  （已打印行永不重写——与 thinking/"… +N lines" 同一提示生命周期纪律）；
- 尾部聚合组保持易变（_committed_count 不越过组首行），后续段到达才固化。

Harness 手法沿用 test_funnel_commit.py（pyte LNM + deterministic_live）。
"""

from __future__ import annotations

import io
import re
import time
from types import SimpleNamespace

import pytest
import pyte
import pyte.modes
from rich.console import Console as RichConsole
from rich.text import Text

from openx.agent import ToolResultEvent, ToolStartEvent
from openx.services.streaming import _GUTTER, _SPIN, StreamingService


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
        self.pyte.feed(self.buf.getvalue())
        self.buf.seek(0)
        self.buf.truncate()

    def rows(self) -> list[str]:
        return [
            "".join(c.data for c in self.screen.buffer[y].values())
            for y in range(self.screen.lines)
        ]

    def text(self) -> str:
        return "\n".join(self.rows())

    def refresh(self) -> None:
        self.svc._live.refresh()
        self.flush()

    def key(self, key: str) -> None:
        """注入热键（下一帧经 _capture.drain_hotkeys 消费）。"""
        self.svc._capture._hotkeys.append(key)


def _tool(h: Harness, name: str, arg: str = "x", output: str = "",
          error: bool = False, start_only: bool = False) -> None:
    """一条完整工具调用（起始 → 结果 → 刷屏）。"""
    h.svc.feed(ToolStartEvent(name=name, arguments=arg))
    if not start_only:
        h.svc.feed(ToolResultEvent(name=name, output=output,
                                   is_error=error))
    h.refresh()


def _read(h: Harness, path: str, output: str = "", **kw) -> None:
    _tool(h, "read_file", arg=path, output=output, **kw)


# ── ① 折叠成摘要行 ───────────────────────────────────────────────


class TestGroupSummary:
    def test_two_reads_collapse_to_one_summary_line(self, deterministic_live):
        """两次 read 折成一行 "Read 2 files"，逐条头行不再出现。"""
        h = Harness()
        h.svc.start()
        _read(h, "a.py", "l1\nl2")
        _read(h, "b.py", "l3\nl4")
        screen = h.text()
        assert "Read 2 files" in screen, screen
        assert "read_file" not in screen, (
            "已聚合的调用不应再有逐条头行\n" + screen
        )

    def test_summary_uses_singular_for_one_of_a_kind(
        self, deterministic_live
    ):
        """单复数按族计数：2 读 + 1 shell → "Read 2 files, ran 1 command"。"""
        h = Harness()
        h.svc.start()
        _read(h, "a.py")
        _read(h, "b.py")
        _tool(h, "shell", arg='{"command": "ls -la"}', output="x")
        assert "Read 2 files, ran 1 command" in h.text()

    def test_phrase_order_follows_first_appearance(self, deterministic_live):
        """短语按族**首次出现**排序（先搜后读 ≠ 先读后搜）。"""
        h = Harness()
        h.svc.start()
        _tool(h, "shell", arg='{"command": "ls"}', output="x")
        _read(h, "a.py")
        _read(h, "b.py")
        assert "Ran 1 command, read 2 files" in h.text()

    def test_single_call_stays_expanded(self, deterministic_live):
        """单条不聚合：保留头行 + ⎿ 结果预览（现状不回退）。"""
        h = Harness()
        h.svc.start()
        _read(h, "a.py", "content line")
        screen = h.text()
        assert "read_file" in screen and "Read 1 file" not in screen, screen
        assert "content line" in screen, "单条的结果预览应照常展示"

    def test_git_queries_share_one_bucket(self, deterministic_live):
        """同族归并计数：git_status + git_log → "Ran 2 git commands"。"""
        h = Harness()
        h.svc.start()
        _tool(h, "git_status", output="clean")
        _tool(h, "git_log", output="abc")
        assert "Ran 2 git commands" in h.text()


# ── ② 什么打断组（错误 / 写操作 / running）───────────────────────


class TestGroupBreakers:
    def test_error_record_never_joins_group(self, deterministic_live):
        """出错调用逐条可见（红色块），前后记录因此各自逐条展示。"""
        h = Harness()
        h.svc.start()
        _read(h, "a.py")
        _read(h, "b.py", "boom: no such file", error=True)
        _read(h, "c.py")
        screen = h.text()
        assert "Read 2 files" not in screen, "错误不该被折进摘要\n" + screen
        assert "boom: no such file" in screen, "错误输出必须逐条可见"
        assert screen.count("read_file") == 3, screen

    def test_write_tool_splits_group(self, deterministic_live):
        """写操作打断组：两段各聚合一次，写操作本身逐条展示。"""
        h = Harness()
        h.svc.start()
        _read(h, "a.py")
        _read(h, "b.py")
        _tool(h, "edit_file", arg="c.py", output="-old\n+new")
        _read(h, "d.py")
        _read(h, "e.py")
        screen = h.text()
        assert screen.count("Read 2 files") == 2, screen
        assert "edit_file" in screen, screen

    def test_running_call_not_grouped(self, deterministic_live):
        """running 记录不入组（结果未到、状态点仍会变）——前缀已完成记录
        照常聚合，running 单条在线。"""
        h = Harness()
        h.svc.start()
        _read(h, "a.py", "l1")
        _read(h, "b.py", "l2")
        _read(h, "c.py", start_only=True)
        screen = h.text()
        assert "Read 2 files" in screen, screen
        assert "read_file" in screen and "Running…" in screen, (
            "running 调用必须单独可见\n" + screen
        )

    def test_text_segment_breaks_group(self, deterministic_live):
        """正文打断组：中间隔一段正文的两批 read 各自成组。"""
        h = Harness()
        h.svc.start()
        _read(h, "a.py")
        _read(h, "b.py")
        h.svc.feed("说明一段\n\n")
        h.refresh()
        _read(h, "d.py")
        _read(h, "e.py")
        assert h.text().count("Read 2 files") == 2, h.text()


# ── ③ Ctrl+T 展开窗口与固化定格 ──────────────────────────────────


class TestExpandWindow:
    def test_ctrl_t_expands_before_commit(self, deterministic_live):
        """尾部聚合组：Ctrl+T 展开为逐条明细（含输出），再按收回摘要。"""
        h = Harness()
        h.svc.start()
        _read(h, "a.py", "alpha line")
        _read(h, "b.py", "beta line")
        assert "Read 2 files" in h.text()
        h.key("\x14")
        h.refresh()
        expanded = h.text()
        assert expanded.count("read_file") == 2, (
            "展开态应回退逐条明细块\n" + expanded
        )
        assert "alpha line" in expanded and "beta line" in expanded
        h.key("\x14")
        h.refresh()
        assert "Read 2 files" in h.text()

    def test_hint_only_while_group_trailing_volatile(
        self, deterministic_live
    ):
        """展开提示只在未固化期出现；固化后提示消失（已打印行不可改）。"""
        h = Harness()
        h.svc.start()
        _read(h, "a.py")
        _read(h, "b.py")
        assert "ctrl+t to expand" in h.text()
        h.svc.feed("后续正文\n\n")
        h.refresh()
        after = h.text()
        assert "ctrl+t to expand" not in after, (
            "固化进 scrollback 的摘要行不该带热键字样\n" + after
        )
        assert "Read 2 files" in after, "摘要行本身应随固化保留"

    def test_ctrl_t_after_commit_keeps_summary(self, deterministic_live):
        """固化后的组对 Ctrl+T no-op：定格为摘要行（已打印行不可重写）。

        这同时避开了"重渲已固化块 → 与 scrollback 内容重复打印"的坑：
        组的渲染形态不再随 _tools_expanded 变，缓存行与固化行恒一致。
        """
        h = Harness()
        h.svc.start()
        _read(h, "a.py", "alpha line")
        _read(h, "b.py", "beta line")
        h.svc.feed("后续正文\n\n")
        h.refresh()
        assert h.svc._committed_count >= 1, "组应已固化"
        h.key("\x14")
        h.refresh()
        screen = h.text()
        assert "Read 2 files" in screen, screen
        assert "read_file" not in screen, (
            "固化后的组不该再被展开（明细只存在于 Ctrl+T 时的易变区）\n"
            + screen
        )

    def test_group_stays_volatile_until_broken(self, deterministic_live):
        """尾部聚合组保持易变（_committed_count 停在组首）；后续正文到达
        才整组固化——这是"固化前可展开"的实现基础。"""
        h = Harness()
        h.svc.start()
        _read(h, "a.py")
        _read(h, "b.py")
        lines = h.svc._body_lines()
        assert len(lines) == 1, f"整组应只有摘要一行，实际 {len(lines)}"
        assert h.svc._committed_count == 0, (
            "尾部聚合组必须留在易变区（否则 Ctrl+T 无从展开）"
        )
        h.svc.feed("后续正文\n\n")
        h.refresh()
        assert h.svc._committed_count >= 1, "组被打断后应固化进 scrollback"

    def test_done_prints_summary_exactly_once(self, deterministic_live):
        """收尾固化不产生摘要行副本（done 只补余量）。"""
        h = Harness()
        h.svc.start()
        _read(h, "a.py")
        _read(h, "b.py")
        h.svc.done()
        h.flush()
        screen = h.text()
        assert screen.count("Read 2 files") == 1, (
            "摘要行应恰出现一次\n" + screen
        )


# ── ⑤ running 行的转帧动画 ───────────────────────────────────────
#
# 用户报告：命令跑久了看不出是在执行还是已经卡死/出错。修复 = running 的
# 结果槽行走逐帧重渲（转帧 + 耗时读秒），静态形态只留给固化进 scrollback
# 的那份。见 streaming._running_row / _volatile_view。


def _glyph_on(line: str) -> str:
    """屏上该行 ``Running…`` 正前方的转帧字形（无帧返回 ""）。

    取"正前方那个字符"而非"行内是否含帧集里的字"：帧集里的 ``·`` 与
    耗时分隔符 ``·`` 是同一个字符，按包含判定会把静态行判成带动画。
    """
    m = re.search(r"(\S)\s+Running…", line)
    return m.group(1) if m and m.group(1) in _SPIN else ""


def _plain(line) -> str:
    """render_lines 行（Segment 列表）→ 可见文本。"""
    return "".join(seg.text for seg in line if not seg.is_control)


def _running_line(h: Harness) -> str:
    return next(r for r in h.rows() if "Running…" in r)


class TestRunningAnimation:
    def test_running_row_shows_spinner_and_elapsed(self, deterministic_live):
        h = Harness()
        h.svc.start()
        _tool(h, "shell", arg="npm test", start_only=True)

        line = _running_line(h)
        assert _glyph_on(line), f"running 行应带转帧：{line!r}"
        assert re.search(r"Running… · \d+s", line), line

    def test_spinner_advances_across_frames(self, deterministic_live, monkeypatch):
        """逐帧重渲：转针真的在动（不是每帧同一个字形）。"""
        import openx.services.streaming as sm

        monkeypatch.setattr(sm, "_SPIN_MS", 1)   # 1ms/帧 → 无需等 120ms
        h = Harness()
        h.svc.start()
        _tool(h, "shell", arg="npm test", start_only=True)

        seen = set()
        for _ in range(5):
            h.refresh()
            seen.add(_glyph_on(_running_line(h)))
            time.sleep(0.005)
        assert len(seen) >= 2, f"转帧应随时间推进，实际只见到 {seen}"

    def test_elapsed_ticks_with_the_clock(self, deterministic_live):
        h = Harness()
        h.svc.start()
        h.svc.feed(ToolStartEvent(name="shell", arguments="sleep 42"))
        record = h.svc._segments[-1][1]
        record.started_at = time.monotonic() - 42   # 假装已经跑了 42 秒
        h.refresh()
        assert "Running… · 42s" in _running_line(h)

    def test_running_row_never_wraps(self, deterministic_live):
        """读秒进位（9s→10s→1h 0m）不许把贴边那行折成两行。

        高度漂移会带偏易变区的擦除标尺（\033[{n}A\033[J 的 n 是行数）。
        窄终端下逼出贴边：头行 + running 行应恒为 2 行。
        """
        h = Harness(cols=20)
        h.svc.start()
        h.svc.feed(ToolStartEvent(name="sh", arguments=""))
        record = h.svc._segments[-1][1]
        record.started_at = time.monotonic() - 3600   # 最长的耗时形态 1h 0m

        view = h.svc._volatile_view()
        rendered = h.svc._rich.render_lines(view, pad=False)
        assert len(rendered) == 2, list(map(_plain, rendered))

    def test_cached_lines_stay_static_and_do_not_churn(
        self, deterministic_live, monkeypatch
    ):
        """缓存形态不含时变分量：转帧不许逐帧打掉 body 渲染缓存。

        打掉的代价是每帧重渲整段 Markdown（``_body_lines`` 的键），
        滚动长回答时这就是卡顿与闪烁的来源。
        """
        import openx.services.streaming as sm

        h = Harness()
        h.svc.start()
        _tool(h, "shell", arg="npm test", start_only=True)
        key_before = h.svc._body_cache_key

        monkeypatch.setattr(sm, "_SPIN_MS", 1)
        h.refresh()
        time.sleep(0.005)
        h.refresh()

        assert h.svc._body_cache_key == key_before, "转帧不应使 body 缓存失效"
        cached = [l for l in map(_plain, h.svc._body_lines()) if "Running…" in l]
        assert cached and cached[0].strip() == f"{_GUTTER}  Running…", cached
        # 而屏上那份是动的
        assert _glyph_on(_running_line(h))

    def test_interrupted_tool_lands_static_in_scrollback(self, deterministic_live):
        """跑到一半被打断：固化进 scrollback 的是静态形态（转帧冻住像坏字形）。"""
        h = Harness()
        h.svc.start()
        _tool(h, "shell", arg="npm test", start_only=True)
        h.svc.cancel()
        h.flush()
        line = _running_line(h)
        assert not _glyph_on(line), f"残留行不该带转帧：{line!r}"
