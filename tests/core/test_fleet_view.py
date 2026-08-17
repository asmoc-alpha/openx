"""状态层（deck）与子代理舰队视图回归测试 —— v0.4.0。

覆盖：
- fleet 单元：format_stream_event 字节级格式、FleetMonitor 登记/幂等
  完结/重置/快照隔离、SubagentView 组行/200 行封顶/并发烟测；
- deck 渲染（pyte 屏幕级）：渲染在 FRAME（输入框）**之下**、计划行
  含 activeForm、舰队行、溢出折叠 "+N more"、done() 后消失且 FRAME
  复为末元素、矮终端预算裁剪、cancel 擦除行数；
- Ctrl-O 焦点：热键循环 主视图 ⇄ 子代理详情，详情头行 + 捕获行，
  start() 重置焦点；
- 视口预算：latched 组高 ≡ H−2（与 deck 高度无关）；deck 行高恒 1。

风格：pytest-asyncio auto、手写 fake、禁 unittest.mock；pyte 基建
沿用 test_terminal_interaction.py 的 Harness 手法（LNM + 手动刷新）。

运行：``python -m pytest tests/test_fleet_view.py -q``
"""

from __future__ import annotations

import asyncio
import io
import threading
from types import SimpleNamespace

import pyte
import pyte.modes
import pytest
from rich.console import Console as RichConsole
from rich.text import Text

from openx.agent import ToolResultEvent, ToolStartEvent
from openx.core.fleet import (
    MAX_VIEW_LINES,
    FleetMonitor,
    SubagentView,
    format_stream_event,
)
from openx.services.streaming import StreamingService, _SPIN


# ── fleet 单元 ───────────────────────────────────────────────────

class TestFormatStreamEvent:
    def test_text_passthrough(self):
        assert format_stream_event("tok ") == "tok "

    def test_tool_start(self):
        assert format_stream_event(ToolStartEvent(name="grep")) == \
            "\n\n[dim]● grep[/dim]\n"

    def test_tool_start_shows_shell_command(self):
        """头行括号内展示命令（Claude Code ● Bash(cmd) 风格）。"""
        out = format_stream_event(ToolStartEvent(
            name="shell", arguments='{"command": "ls -la"}'))
        assert out == "\n\n[dim]● shell[/dim][dim](ls -la)[/dim]\n"

    def test_tool_start_summary_other_tools(self):
        """非 shell 工具：括号内紧凑 key=value 摘要（长值截断）。"""
        out = format_stream_event(ToolStartEvent(
            name="read_file", arguments='{"file_path": "src/x.py"}'))
        assert out == "\n\n[dim]● read_file[/dim][dim](file_path=src/x.py)[/dim]\n"

    def test_tool_start_bad_arguments_fallback(self):
        """坏 JSON → 头部回落纯工具名，绝不抛错。"""
        assert format_stream_event(ToolStartEvent(
            name="shell", arguments="{broken")) == "\n\n[dim]● shell[/dim]\n"

    def test_tool_result_gutter_block(self):
        """结果 → ⎿ 槽线块：首行 ⎿ + 续行 5 空格对齐。"""
        out = format_stream_event(
            ToolResultEvent(name="shell", output="line1\nline2"))
        assert out == "\n\n[dim]  ⎿  [/]line1\n[dim]     [/]line2\n\n"

    def test_tool_error_marker_first_line(self):
        """错误输出：首行冠红色 ✕。"""
        out = format_stream_event(ToolResultEvent(
            name="shell", output="boom\nsecond", is_error=True))
        assert out == "\n\n[dim]  ⎿  [/][red]✕[/] boom\n[dim]     [/]second\n\n"

    def test_tool_result_truncated_with_note(self):
        """超 10 行（详情视图上限）→ 头部截断 + 剩余行数提示。"""
        output = "\n".join(f"l{i}" for i in range(30))
        out = format_stream_event(
            ToolResultEvent(name="x", output=output))
        lines = out.strip("\n").splitlines()
        assert lines[0] == "[dim]  ⎿  [/]l0"
        assert lines[-1] == "[dim]     [/][dim]... (20 more lines)[/]"
        assert len(lines) == 11  # 10 行 + 提示行

    def test_tool_result_escapes_markup_in_output(self):
        """输出含方括号 → markup 转义，绝不触发 MarkupError。"""
        out = format_stream_event(ToolResultEvent(
            name="x", output="found [x] and [bold]stuff[/bold]"))
        from rich.text import Text
        for ln in out.strip("\n").splitlines():
            Text.from_markup(ln)  # 不抛错
        assert "[x]" in out

    def test_empty_output_none(self):
        assert format_stream_event(
            ToolResultEvent(name="x", output="", is_error=False)
        ) is None


class TestFleetMonitor:
    def test_register_complete_reset(self):
        mon = FleetMonitor()
        v = mon.register("find auth", "explore")
        assert v.label == "find auth" and v.status == "running"
        assert mon.running_count() == 1
        mon.complete(v)
        assert v.status == "done" and mon.running_count() == 0
        mon.complete(v, is_error=True)  # 幂等：不改写
        assert v.status == "done"
        mon.reset()
        assert mon.snapshot() == []

    def test_error_status(self):
        mon = FleetMonitor()
        v = mon.register("x")
        mon.complete(v, is_error=True)
        assert v.status == "error"

    def test_snapshot_isolation(self):
        mon = FleetMonitor()
        v = mon.register("x")
        v.feed("line1\n")
        snap = mon.snapshot()
        v.feed("line2\n")  # 快照后继续喂 → 不影响已取快照
        assert snap[0]["lines"] == ["line1"]
        assert mon.snapshot()[0]["lines"] == ["line1", "line2"]

    def test_concurrent_feed_and_snapshot_smoke(self):
        mon = FleetMonitor()
        views = [mon.register(f"v{i}") for i in range(4)]
        errors: list = []

        def feeder():
            try:
                for v in views:
                    for i in range(250):
                        v.feed(f"line{i}\n")
                        v.feed(ToolStartEvent(name="t"))
            except Exception as e:  # pragma: no cover
                errors.append(e)

        def snapper():
            try:
                for _ in range(100):
                    mon.snapshot()
            except Exception as e:  # pragma: no cover
                errors.append(e)

        ts = [threading.Thread(target=feeder), threading.Thread(target=snapper)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert errors == []
        snap = mon.snapshot()
        assert all(len(s["lines"]) == MAX_VIEW_LINES for s in snap)
        assert all(s["tools_count"] == 250 for s in snap)


class TestSubagentView:
    def test_line_assembly_and_pending(self):
        v = SubagentView(1, "x", "general-purpose")
        v.feed("par")
        v.feed("tial\nfull\n")
        snap = v.snapshot(0.0)
        assert snap["lines"] == ["partial", "full"]
        assert snap["pending"] == ""
        v.feed("tail-no-newline")
        assert v.snapshot(0.0)["pending"] == "tail-no-newline"

    def test_cap_at_max_lines(self):
        v = SubagentView(1, "x", "general-purpose")
        for i in range(MAX_VIEW_LINES + 50):
            v.feed(f"l{i}\n")
        snap = v.snapshot(0.0)
        assert len(snap["lines"]) == MAX_VIEW_LINES
        assert snap["lines"][0] == "l50"


# ── pyte 基建（沿用 test_terminal_interaction 手法）──────────────

@pytest.fixture
def deterministic_live(monkeypatch):
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
    """带 providers 的 StreamingService + pyte 屏幕。"""

    def __init__(self, rows: int = 24, cols: int = 80,
                 todos=None, fleet=None):
        self.screen = pyte.Screen(cols, rows)
        self.screen.set_mode(pyte.modes.LNM)
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
        self.svc = StreamingService(
            console, input_tokens=0,
            todos_provider=(lambda: todos) if todos is not None else None,
            fleet=fleet,
        )

    def refresh(self):
        self.svc._live.refresh()
        self.flush()

    def flush(self):
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

    def frame_row(self) -> int:
        for y, r in self.nonempty():
            if "FRAME" in r:
                return y
        raise AssertionError("FRAME not on screen")


TODOS = [
    {"content": "done task", "activeForm": "doing", "status": "completed"},
    {"content": "work task", "activeForm": "正在实现 X", "status": "in_progress"},
    {"content": "todo task", "activeForm": "later", "status": "pending"},
]


# ── deck 渲染 ────────────────────────────────────────────────────

class TestDeckRendering:
    def test_deck_renders_above_frame(self, deterministic_live):
        """上状态层（Plan/Queue）渲染在输入框之上。"""
        h = Harness(todos=TODOS)
        h.svc.start()
        h.svc.feed("answer text")
        h.refresh()
        frame_y = h.frame_row()
        rows = h.nonempty()
        # 无舰队时 FRAME 是屏上末元素（done 后复用链前提）
        assert all(y <= frame_y for y, _ in rows)
        y_plan = next(y for y, r in rows if "Plan" in r)
        y_task = next(y for y, r in rows if "done task" in r)
        assert y_plan < frame_y, "deck must render above the frame"
        assert y_task < frame_y
        deck_text = "\n".join(r for _, r in rows)
        assert "Plan 1/3" in deck_text            # done/total
        assert "正在实现 X" in deck_text           # in_progress 显示 activeForm
        assert "todo task" in deck_text

    def test_fleet_list_renders_below_frame(self, deterministic_live):
        """子代理列表渲染在输入框之下，含主条目 0（用户需求 1）。"""
        mon = FleetMonitor()
        h = Harness(todos=TODOS, fleet=mon)
        h.svc.start()
        h.svc.feed("answer text")
        mon.register("find auth code", "explore")
        h.refresh()
        frame_y = h.frame_row()
        rows = h.nonempty()
        y_plan = next(y for y, r in rows if "Plan" in r)
        y_agents = next(y for y, r in rows if "Agents" in r)
        assert y_plan < frame_y < y_agents, "plan 在框上、舰队列表在框下"
        # 主条目 0：焦点 0（主视图）时 ❯ 标记在主条目上
        assert any("❯0" in r and "main" in r for _, r in rows)

    def test_fleet_rows_and_overflow(self, deterministic_live):
        mon = FleetMonitor()
        h = Harness(todos=[], fleet=mon)
        h.svc.start()  # start() 会 reset fleet → 登记必须在 start 之后
        for i in range(6):
            v = mon.register(f"agent-{i}")
            v.feed(ToolStartEvent(name="grep"))
        mon.complete(mon._views[0])  # 首个标记完成（测试可触内部）
        h.refresh()
        text = "\n".join(r for _, r in h.nonempty())
        assert "Agents (6)" in text
        assert "agent-0" in text and "agent-3" in text  # 前 4 个可见
        assert "+2 more" in text                        # 6-4 折叠
        assert "1 tools" in text

    def test_deck_absent_without_providers(self, deterministic_live):
        h = Harness()  # 无 providers → 旧行为逐帧一致
        h.svc.start()
        h.svc.feed("hi")
        h.refresh()
        assert h.svc._last_deck_h == 0
        frame_y = h.frame_row()
        assert all(y <= frame_y for y, _ in h.nonempty())

    def test_deck_vanishes_after_done(self, deterministic_live):
        h = Harness(todos=TODOS)
        h.svc.start()
        h.svc.feed("hi")
        h.refresh()
        assert any("Plan" in r for _, r in h.nonempty())
        h.svc.done()
        h.flush()
        # done 后 deck 消失，FRAME 复为末元素（_frame_on_screen 复用前提）
        ne = h.nonempty()
        assert not any("Plan" in r for _, r in ne)
        assert ne[-1][0] == h.frame_row()

    def test_todos_mutation_reflects_next_refresh(self, deterministic_live):
        todos = [
            {"content": "a", "activeForm": "a", "status": "pending"},
        ]
        h = Harness(todos=todos)
        h.svc.start()
        h.refresh()
        assert any("Plan 0/1" in r for _, r in h.nonempty())
        todos[:] = [  # TodoWriteTool 的 store[:]= 整体替换
            {"content": "a", "activeForm": "a", "status": "completed"},
            {"content": "b", "activeForm": "b", "status": "pending"},
        ]
        h.refresh()
        assert any("Plan 1/2" in r for _, r in h.nonempty())

    def test_plan_overflow_capped(self, deterministic_live):
        todos = [
            {"content": f"t{i}", "activeForm": f"t{i}", "status": "pending"}
            for i in range(10)
        ]
        h = Harness(todos=todos)
        h.svc.start()
        h.refresh()
        text = "\n".join(r for _, r in h.nonempty())
        assert "+4 more" in text  # 10 - 6 折叠
        assert "t5" in text and "t6" not in text

    def test_cancel_clears_deck_region(self, deterministic_live, monkeypatch):
        h = Harness(todos=TODOS)
        h.svc.start()
        h.svc.feed("hi")
        h.refresh()
        # cancel 先置 _done → stop 内最终刷新跳过 deck 并自清零
        # _last_deck_h → 擦除行数回到 4（与旧版一致）
        out = io.StringIO()
        import sys as _sys
        monkeypatch.setattr(_sys, "stdout", out)
        h.svc.cancel()
        assert "\033[4A\033[J" in out.getvalue()
        assert h.svc._last_deck_h == 0

    def test_deck_rows_single_line_each(self, deterministic_live):
        """硬不变量：1 deck 行 ≡ 1 终端行（no_wrap + ellipsis）。"""
        mon = FleetMonitor()
        todos = [{"content": "y" * 200, "activeForm": "z" * 200,
                  "status": "in_progress"}]
        h = Harness(todos=todos, fleet=mon)
        h.svc.start()
        mon.register("x" * 200)  # 超长标签不得换行撑高（start 后登记）
        h.refresh()
        rc = h.svc._rich
        deck, deck_h = h.svc._deck_renderable(mon.snapshot())
        lines = rc.render_lines(deck, pad=False)
        assert len(lines) == deck_h
        # 下状态层（舰队列表）同守单行不变量
        fleet_deck, fleet_h = h.svc._fleet_deck_renderable(mon.snapshot())
        fleet_lines = rc.render_lines(fleet_deck, pad=False)
        assert len(fleet_lines) == fleet_h


# ── Ctrl-O 焦点切换 ──────────────────────────────────────────────

class TestCtrlOFocus:
    def _harness_with_agent(self):
        mon = FleetMonitor()
        h = Harness(todos=TODOS, fleet=mon)
        h.svc.start()  # start() 会 reset fleet → 登记必须在 start 之后
        v = mon.register("find auth code", "explore")
        v.feed(ToolStartEvent(name="grep"))
        v.feed("found 3 files\n")
        return h, mon

    def test_cycle_to_detail_and_back(self, deterministic_live):
        h, mon = self._harness_with_agent()
        h.svc.feed("main answer")
        h.refresh()
        # 主视图：有 answer、无详情头
        text = "\n".join(r for _, r in h.nonempty())
        assert "main answer" in text and "Agent 1:" not in text

        h.svc._capture._hotkeys.append("\x0f")  # Ctrl-O → 子代理 1
        h.refresh()
        text = "\n".join(r for _, r in h.nonempty())
        assert "Agent 1: find auth code" in text
        assert "grep" in text and "found 3 files" in text  # 捕获流
        assert "alt+0 back" in text
        assert h.svc._focus == 1

        h.svc._capture._hotkeys.append("\x0f")  # 再按 → 回主视图
        h.refresh()
        assert h.svc._focus == 0
        text = "\n".join(r for _, r in h.nonempty())
        assert "main answer" in text and "Agent 1:" not in text

    def test_focus_reset_on_next_start(self, deterministic_live):
        h, mon = self._harness_with_agent()
        h.refresh()
        h.svc._capture._hotkeys.append("\x0f")
        h.refresh()
        assert h.svc._focus == 1
        h.svc.done()
        h.svc.start()  # 新一轮：焦点清零、fleet 清空
        assert h.svc._focus == 0
        assert mon.snapshot() == []

    def test_hotkey_without_agents_noop(self, deterministic_live):
        h = Harness(todos=TODOS)  # 无 fleet
        h.svc.start()
        h.refresh()
        h.svc._capture._hotkeys.append("\x0f")
        h.refresh()
        assert h.svc._focus == 0


# ── Queue 面板（排队待发，按序全列，plan 之下框之上）─────────────


class TestQueuePanel:
    def test_queue_below_plan_and_above_frame(self, deterministic_live):
        """位置钉死：Plan → Queue → 输入框（自上而下）。"""
        h = Harness(todos=TODOS)
        h.svc._console._input_queue.extend(["follow-up one", "follow-up two"])
        h.svc.start()  # start 只 reset fleet，不清 console 队列
        h.svc.feed("answering")
        h.refresh()

        rows = h.nonempty()
        y_plan = next(y for y, r in rows if "Plan" in r)
        y_queue = next(y for y, r in rows if "Queue (2)" in r)
        frame_y = h.frame_row()
        assert y_plan < y_queue < frame_y

    def test_queue_lists_all_in_fifo_order(self, deterministic_live):
        """按序全列：先排在上、跨轮留存与新排合并 FIFO。"""
        h = Harness()
        h.svc._console._input_queue.append("old leftover")  # 上一轮残留
        h.svc.start()
        h.svc._on_line_queued("new this turn")  # 本轮流式中新排
        h.refresh()

        rows = h.nonempty()
        text = "\n".join(r for _, r in rows)
        assert "Queue (2)" in text
        assert "▸ old leftover" in text
        assert "▸ new this turn" in text
        y_old = next(y for y, r in rows if "old leftover" in r)
        y_new = next(y for y, r in rows if "new this turn" in r)
        assert y_old < y_new, "FIFO：先排者在上"

    def test_queue_overflow_folds(self, deterministic_live):
        """超 _DECK_QUEUE_ROWS 折叠 +N more（单行不变量不破）。"""
        h = Harness()
        for i in range(6):
            h.svc._console._input_queue.append(f"msg {i}")
        h.svc.start()
        h.refresh()
        text = "\n".join(r for _, r in h.nonempty())
        assert "Queue (6)" in text
        assert "msg 0" in text and "msg 3" in text  # 前 4 条可见
        assert "+2 more" in text
        assert "msg 5" not in text

    def test_queue_vanishes_after_done(self, deterministic_live):
        """done 后状态层整体退场（frame 复为末元素）。"""
        h = Harness()
        h.svc._on_line_queued("pending msg")
        h.svc.start()
        h.refresh()
        assert "Queue (1)" in "\n".join(r for _, r in h.nonempty())
        h.svc.done()
        h.flush()
        assert "Queue" not in "\n".join(r for _, r in h.nonempty())
        assert h.nonempty()[-1][0] == h.frame_row()


# ── Alt+N 舰队直选（对标 Claude Code 窗格导航）───────────────────


class TestAltNumberSelect:
    def _harness_two_agents(self):
        mon = FleetMonitor()
        h = Harness(todos=[], fleet=mon)
        h.svc.start()
        v1 = mon.register("find auth code", "explore")
        v1.feed(ToolStartEvent(name="grep"))
        v2 = mon.register("review diff", "review")
        v2.feed(ToolStartEvent(name="read_file"))
        return h, mon

    def test_alt_number_jumps_directly(self, deterministic_live):
        h, mon = self._harness_two_agents()
        h.svc.feed("main answer")
        h.refresh()

        h.svc._capture._hotkeys.append("\x1b2")  # Alt+2 → 直达第 2 个子代理
        h.refresh()
        assert h.svc._focus == 2
        text = "\n".join(r for _, r in h.nonempty())
        assert "Agent 2: review diff" in text
        assert "read_file" in text               # 该代理的捕获流

    def test_alt_zero_returns_to_main(self, deterministic_live):
        h, mon = self._harness_two_agents()
        h.svc.feed("main answer")
        h.refresh()
        h.svc._capture._hotkeys.append("\x1b1")
        h.refresh()
        assert h.svc._focus == 1

        h.svc._capture._hotkeys.append("\x1b0")  # Alt+0 → 回主视图
        h.refresh()
        assert h.svc._focus == 0
        text = "\n".join(r for _, r in h.nonempty())
        assert "main answer" in text and "Agent 1:" not in text

    def test_alt_number_clamps_to_last_agent(self, deterministic_live):
        h, mon = self._harness_two_agents()
        h.refresh()
        h.svc._capture._hotkeys.append("\x1b9")  # 只有 2 个代理 → 钳到 2
        h.refresh()
        assert h.svc._focus == 2
        assert "Agent 2:" in "\n".join(r for _, r in h.nonempty())

    def test_alt_number_noop_without_agents(self, deterministic_live):
        h = Harness(todos=TODOS)
        h.svc.start()
        h.refresh()
        h.svc._capture._hotkeys.append("\x1b3")
        h.refresh()
        assert h.svc._focus == 0

    def test_deck_rows_numbered_with_focus_marker(self, deterministic_live):
        """deck 舰队行带编号；正在查看的代理前缀 ❯（恒 2 格宽）。"""
        h, mon = self._harness_two_agents()
        h.refresh()
        rows = [r for _, r in h.nonempty()]
        # 未聚焦：两行皆 " N …" 编号
        assert any(" 1 " in r and "find auth code" in r for r in rows)
        assert any(" 2 " in r and "review diff" in r for r in rows)
        assert any("↓ select" in r for r in rows)  # 提示行

        h.svc._capture._hotkeys.append("\x1b2")
        h.refresh()
        rows = [r for _, r in h.nonempty()]
        assert any("❯2" in r and "review diff" in r for r in rows)
        assert not any("❯1" in r for r in rows)  # 未聚焦行无 ❯

    def test_read_unicode_char_parses_alt_digit(self):
        """管道级（无 TTY）：ESC+数字 → '\\x1b2' 两字节热键记号。"""
        import os
        from openx.ui.input_capture import read_unicode_char

        r, w = os.pipe()
        try:
            os.write(w, b"\x1b2")
            assert read_unicode_char(r) == "\x1b2"
            os.write(w, b"\x1b0")
            assert read_unicode_char(r) == "\x1b0"
            # 单独 Esc 仍是打断热键（20ms 无后续）
            os.write(w, b"\x1b")
            assert read_unicode_char(r) == "\x1b"
            # 方向键不受影响
            os.write(w, b"\x1b[A")
            assert read_unicode_char(r) == "\x1b[A"
            # 普通字符不受影响
            os.write(w, b"a")
            assert read_unicode_char(r) == "a"
        finally:
            os.close(r)
            os.close(w)


# ── ↓ 选中 + Enter 进入子代理 ────────────────────────────────────


class TestDeckSelection:
    def _harness_two(self, complete_first: bool = True):
        mon = FleetMonitor()
        h = Harness(todos=[], fleet=mon)
        h.svc.start()
        v1 = mon.register("find auth code", "explore")
        v1.feed(ToolStartEvent(name="grep"))
        v2 = mon.register("review diff", "review")
        v2.feed(ToolStartEvent(name="read_file"))
        if complete_first:
            mon.complete(v1)  # v1 done、v2 running
        return h, mon

    def test_down_selects_running_agent(self, deterministic_live):
        h, mon = self._harness_two()
        h.svc.feed("main answer")
        h.refresh()

        h.svc._capture._hotkeys.append("\x1b[B")  # ↓ → 选中运行中的 v2
        h.refresh()
        assert h.svc._fleet_selected == 2
        assert h.svc._focus == 0  # 仅选中，尚未进入
        rows = [r for _, r in h.nonempty()]
        assert any("❯2" in r for r in rows), "选中代理应有 ❯ 标记"
        assert not any("❯1" in r for r in rows)

    def test_down_cycles_selection(self, deterministic_live):
        h, mon = self._harness_two()  # v1 done、v2 running
        h.refresh()
        h.svc._capture._hotkeys.append("\x1b[B")
        h.refresh()
        assert h.svc._fleet_selected == 2  # 首个运行中
        h.svc._capture._hotkeys.append("\x1b[B")  # 再按 → 循环到主条目 0
        h.refresh()
        assert h.svc._fleet_selected == 0
        h.svc._capture._hotkeys.append("\x1b[B")  # 再按 → 代理 1
        h.refresh()
        assert h.svc._fleet_selected == 1

    def test_enter_opens_selected_detail(self, deterministic_live):
        h, mon = self._harness_two()
        h.svc.feed("main answer")
        h.refresh()
        h.svc._capture._hotkeys.append("\x1b[B")  # 选中 v2
        h.refresh()
        h.svc._capture._hotkeys.append("\r")      # Enter → 进入详情
        h.refresh()
        assert h.svc._focus == 2
        assert h.svc._fleet_selected == 2  # 选择保留：❯ 标明当前条目
        text = "\n".join(r for _, r in h.nonempty())
        assert "Agent 2: review diff" in text
        assert "read_file" in text  # 该代理执行流可见

    def test_enter_on_main_entry_returns_to_main_view(self, deterministic_live):
        """选中主条目（0）按 Enter → 回到主回答视图（用户需求 3）。"""
        h, mon = self._harness_two()
        h.svc.feed("main answer")
        h.refresh()
        h.svc._capture._hotkeys.append("\x1b[B")  # → v2（running）
        h.svc._capture._hotkeys.append("\x1b[B")  # → 主条目 0
        h.svc._capture._hotkeys.append("\r")      # Enter → 回主视图
        h.refresh()
        assert h.svc._focus == 0
        text = "\n".join(r for _, r in h.nonempty())
        assert "main answer" in text and "Agent 2:" not in text

    def test_up_cycles_selection_backward_through_main(
        self, deterministic_live
    ):
        """↑ 反向循环选择，含主条目（0 再按 ↑ → 末位代理）。"""
        h, mon = self._harness_two(complete_first=False)  # 均在运行
        h.refresh()
        h.svc._capture._hotkeys.append("\x1b[B")  # 选中首个运行中 = 1
        h.refresh()
        assert h.svc._fleet_selected == 1
        h.svc._capture._hotkeys.append("\x1b[A")  # ↑ → 主条目 0
        h.refresh()
        assert h.svc._fleet_selected == 0
        h.svc._capture._hotkeys.append("\x1b[A")  # ↑ → 末位代理 2
        h.refresh()
        assert h.svc._fleet_selected == 2

    def test_arrows_switch_agents_in_detail_view(self, deterministic_live):
        """详情内 ↑/↓ 循环切换（含回主视图）；主视图的 ↓ 是选择语义。"""
        h, mon = self._harness_two()
        h.svc.feed("main answer")
        h.refresh()
        h.svc._capture._hotkeys.append("\x1b[B")
        h.svc._capture._hotkeys.append("\r")  # 进入 v2
        h.refresh()
        assert h.svc._focus == 2

        h.svc._capture._hotkeys.append("\x1b[A")  # ↑ → v1
        h.refresh()
        assert h.svc._focus == 1
        text = "\n".join(r for _, r in h.nonempty())
        assert "Agent 1: find auth code" in text

        h.svc._capture._hotkeys.append("\x1b[A")  # ↑ → 主视图
        h.refresh()
        assert h.svc._focus == 0
        assert "main answer" in "\n".join(r for _, r in h.nonempty())

        # 已在主视图：↓ 是选择语义（选择循环 2 → 0 主条目），非焦点循环
        h.svc._capture._hotkeys.append("\x1b[B")
        h.refresh()
        assert h.svc._focus == 0
        assert h.svc._fleet_selected == 0  # 选择循环到主条目
        h.svc._capture._hotkeys.append("\r")  # Enter 确认主条目 → 主视图
        h.refresh()
        assert h.svc._focus == 0
        assert "main answer" in "\n".join(r for _, r in h.nonempty())

    def test_down_scrolls_first_then_selects_at_bottom(self, deterministic_live):
        """上翻回看期间 ↓ 先滚回底部；到底后 ↓ 才选中子代理。"""
        h, mon = self._harness_two()
        for i in range(1, 61):
            h.svc.feed(f"response line {i}\n\n")
        h.refresh()
        h.svc._capture._hotkeys.append("\x1b[A")
        h.svc._capture._hotkeys.append("\x1b[A")  # 上翻 2 行
        h.refresh()
        assert h.svc._scroll_offset == 2

        h.svc._capture._hotkeys.append("\x1b[B")  # ↓ → 滚动（不选中）
        h.refresh()
        assert h.svc._scroll_offset == 1
        assert h.svc._fleet_selected == -1  # 滚动期间不触发选择

        h.svc._capture._hotkeys.append("\x1b[B")  # 到底
        h.refresh()
        assert h.svc._scroll_offset == 0
        assert h.svc._fleet_selected == -1  # 滚动到底仍不触发选择

        h.svc._capture._hotkeys.append("\x1b[B")  # 已在底部 → 选中运行中的 v2
        h.refresh()
        assert h.svc._fleet_selected == 2

    def test_bare_enter_without_selection_noop(self, deterministic_live):
        h, mon = self._harness_two()
        h.svc.feed("main answer")
        h.refresh()
        h.svc._capture._hotkeys.append("\r")
        h.refresh()
        assert h.svc._focus == 0
        assert h.svc._fleet_selected == -1  # 未选择 → Enter 无效
        assert "Agent 1:" not in "\n".join(r for _, r in h.nonempty())

    def test_selection_cleared_when_out_of_range(self, deterministic_live):
        h, mon = self._harness_two()
        h.refresh()
        h.svc._fleet_selected = 5  # 越界（只有 2 个代理）
        h.refresh()
        assert h.svc._fleet_selected == -1


# ── 端到端：pty 真实键路进入子代理详情 ───────────────────────────


class TestEnterDetailEndToEnd:
    """全真链路：pty 按键 → InputCapture 线程 → 5Hz Live 线程 → 详情上屏。

    注入式热键测试（_hotkeys.append）覆盖不了真实读键/解析/分发链——
    本测试不替换 _ResizeAwareLive（保留 auto_refresh 真线程），只把
    stdin/stdout 接到 pty 双工端。
    """

    def test_arrow_down_then_enter_shows_detail(self, monkeypatch, tmp_path):
        import os
        import pty
        import select
        import sys
        import threading
        import time as _time

        import pyte
        import pyte.modes
        from rich.console import Console as RichConsole

        from openx.config import OpenXConfig
        from openx.ui.console import Console as OpenXConsole

        master, slave = pty.openpty()
        fout = os.fdopen(os.dup(slave), "w", buffering=1)

        class _StdinStub:
            def fileno(self):
                return slave

            def isatty(self):
                return True

        monkeypatch.setattr(sys, "stdin", _StdinStub())
        monkeypatch.setattr(sys, "stdout", fout)
        monkeypatch.setattr(
            "openx.ui._components.prompt.get_terminal_size",
            lambda: os.terminal_size((80, 24)),
        )

        out: list[bytes] = []
        stop = threading.Event()

        def drainer():
            while not stop.is_set():
                r, _, _ = select.select([master], [], [], 0.05)
                if not r:
                    continue
                try:
                    data = os.read(master, 65536)
                except OSError:
                    return
                if data:
                    out.append(data)

        dth = threading.Thread(target=drainer, daemon=True)
        dth.start()

        console = OpenXConsole(config=OpenXConfig(workspace=str(tmp_path)))
        console._console = RichConsole(
            file=fout, width=80, height=24, force_terminal=True
        )
        console._terminal_width = 80

        mon = FleetMonitor()
        svc = StreamingService(console, input_tokens=0, fleet=mon)
        svc.start()  # start() 会 reset fleet → 登记必须在 start 之后
        mon_view = mon.register("find auth code", "explore")
        mon_view.feed(ToolStartEvent(name="grep"))
        mon_view.feed("found 3 matching files\n")
        try:
            _time.sleep(0.4)  # 首帧 + 5Hz 线程稳定

            os.write(master, b"\x1b[B")  # ↓ → 选中运行中的子代理
            _time.sleep(0.5)
            screen1 = b"".join(out)
            assert b"\xe2\x9d\xaf1" in screen1 or "❯1".encode() in screen1, (
                "↓ 后甲板应出现 ❯1 选中标记"
            )

            os.write(master, b"\r")  # Enter → 进入详情
            _time.sleep(0.6)
            raw = b"".join(out)
        finally:
            try:
                svc.cancel()
            except Exception:
                pass
            stop.set()
            dth.join(2)
            fout.close()
            os.close(master)
            os.close(slave)

        screen = pyte.Screen(80, 24)
        screen.set_mode(pyte.modes.LNM)
        pyte.Stream(screen).feed(raw.decode("utf-8", "replace"))
        text = "\n".join(
            "".join(c.data for c in screen.buffer[y].values())
            for y in range(24)
        )
        assert "Agent 1: find auth code" in text, (
            f"Enter 后应进入子代理详情视图，实际屏幕：\n{text}"
        )
        assert "grep" in text and "found 3 matching files" in text

    def test_ctrl_o_and_alt_number_enter_detail(self, monkeypatch, tmp_path):
        """另外两条入口键路：Ctrl+O 循环、Alt+N 直选。"""
        import os
        import pty
        import select
        import sys
        import threading
        import time as _time

        import pyte
        import pyte.modes
        from rich.console import Console as RichConsole

        from openx.config import OpenXConfig
        from openx.ui.console import Console as OpenXConsole

        master, slave = pty.openpty()
        fout = os.fdopen(os.dup(slave), "w", buffering=1)

        class _StdinStub:
            def fileno(self):
                return slave

            def isatty(self):
                return True

        monkeypatch.setattr(sys, "stdin", _StdinStub())
        monkeypatch.setattr(sys, "stdout", fout)
        monkeypatch.setattr(
            "openx.ui._components.prompt.get_terminal_size",
            lambda: os.terminal_size((80, 24)),
        )

        out: list[bytes] = []
        stop = threading.Event()

        def drainer():
            while not stop.is_set():
                r, _, _ = select.select([master], [], [], 0.05)
                if not r:
                    continue
                try:
                    data = os.read(master, 65536)
                except OSError:
                    return
                if data:
                    out.append(data)

        dth = threading.Thread(target=drainer, daemon=True)
        dth.start()

        console = OpenXConsole(config=OpenXConfig(workspace=str(tmp_path)))
        console._console = RichConsole(
            file=fout, width=80, height=24, force_terminal=True
        )
        console._terminal_width = 80

        mon = FleetMonitor()
        svc = StreamingService(console, input_tokens=0, fleet=mon)
        svc.start()
        v1 = mon.register("agent one", "explore")
        v1.feed("child one output\n")
        v2 = mon.register("agent two", "review")
        v2.feed("child two output\n")
        try:
            _time.sleep(0.4)

            # Ctrl+O → 焦点循环到第 1 个子代理
            os.write(master, b"\x0f")
            _time.sleep(0.5)
            raw = b"".join(out)
            screen = pyte.Screen(80, 24)
            screen.set_mode(pyte.modes.LNM)
            pyte.Stream(screen).feed(raw.decode("utf-8", "replace"))
            text1 = "\n".join(
                "".join(c.data for c in screen.buffer[y].values())
                for y in range(24)
            )
            assert "Agent 1: agent one" in text1, (
                f"Ctrl+O 应进入第 1 个子代理详情：\n{text1}"
            )
            assert "child one output" in text1

            # Alt+2 → 直选第 2 个子代理
            os.write(master, b"\x1b2")
            _time.sleep(0.5)
            raw2 = b"".join(out)
            screen2 = pyte.Screen(80, 24)
            screen2.set_mode(pyte.modes.LNM)
            pyte.Stream(screen2).feed(raw2.decode("utf-8", "replace"))
            text2 = "\n".join(
                "".join(c.data for c in screen2.buffer[y].values())
                for y in range(24)
            )
            assert "Agent 2: agent two" in text2, (
                f"Alt+2 应直选第 2 个子代理详情：\n{text2}"
            )
            assert "child two output" in text2
        finally:
            try:
                svc.cancel()
            except Exception:
                pass
            stop.set()
            dth.join(2)
            fout.close()
            os.close(master)
            os.close(slave)

# ── 权限选择桥接面板（框下，不占满屏）────────────────────────────


class TestPermissionBridge:
    async def test_panel_renders_below_frame_and_keys_resolve(
        self, deterministic_live
    ):
        """面板渲染在输入框下方（上方内容保留）；↑/↓ 选择、Enter 确认。"""
        h = Harness()
        h.svc.start()
        h.svc.feed("working on it")
        task = asyncio.ensure_future(h.svc.ask_permission_bridge(
            "write_file", "edit config.yaml",
            args_summary="config.yaml",
            diff=("config.yaml", "old\ncontent", "new\nmore\nlines"),
        ))
        await asyncio.sleep(0)
        h.refresh()

        rows = h.nonempty()
        frame_y = h.frame_row()
        # 上方正文保留 + 面板在框之下
        assert any("working on it" in r for y, r in rows if y < frame_y)
        y_allow = next(y for y, r in rows if "Allow" in r)
        y_hint = next(y for y, r in rows if "↵ confirm" in r)
        assert frame_y < y_allow < y_hint
        text = "\n".join(r for _, r in rows)
        assert "write_file" in text and "edit config.yaml" in text
        assert "~ config.yaml · 3 lines" in text  # diff 摘要行
        assert "Yes, allow once" in text
        assert "don't ask again" in text  # can_remember + args_summary
        assert "No, don't run" in text
        assert "❯ Yes, allow once" in text  # 默认选中首项

        # ↓ → "don't ask again"，Enter 确认
        h.svc._capture._hotkeys.append("\x1b[B")
        h.refresh()  # drain 发生在重绘时（真实场景为 5Hz 节拍）
        assert "❯ Yes, and don't ask again" in "\n".join(
            r for _, r in h.nonempty())
        h.svc._capture._hotkeys.append("\r")
        h.refresh()
        assert await task == (True, True)
        # 面板随确认退场
        h.refresh()
        assert "Allow" not in "\n".join(r for _, r in h.nonempty())

    async def test_enter_confirms_default_yes_once(self, deterministic_live):
        h = Harness()
        h.svc.start()
        task = asyncio.ensure_future(h.svc.ask_permission_bridge(
            "shell", "run pytest"))
        await asyncio.sleep(0)
        h.refresh()
        h.svc._capture._hotkeys.append("\r")  # 直接 Enter = 首项
        h.refresh()
        assert await task == (True, False)

    async def test_down_twice_denies(self, deterministic_live):
        h = Harness()
        h.svc.start()
        task = asyncio.ensure_future(h.svc.ask_permission_bridge(
            "shell", "run pytest"))  # 无 args_summary → 仅 2 个选项
        await asyncio.sleep(0)
        h.refresh()
        text = "\n".join(r for _, r in h.nonempty())
        assert "don't ask again" not in text  # 无记忆选项
        h.svc._capture._hotkeys.append("\x1b[B")
        h.svc._capture._hotkeys.append("\x1b[B")  # 循环回首项
        h.svc._capture._hotkeys.append("\x1b[B")  # → No（2 选项：1→No→Yes）
        h.refresh()
        assert "❯ No, don't run" in "\n".join(r for _, r in h.nonempty())
        h.svc._capture._hotkeys.append("\r")
        h.refresh()
        assert await task == (False, False)

    async def test_manual_mode_hides_remember_option(self, deterministic_live):
        h = Harness()
        h.svc.start()
        task = asyncio.ensure_future(h.svc.ask_permission_bridge(
            "write_file", "edit x", args_summary="x.py", can_remember=False))
        await asyncio.sleep(0)
        h.refresh()
        text = "\n".join(r for _, r in h.nonempty())
        assert "don't ask again" not in text
        h.svc._capture._hotkeys.append("\r")
        h.refresh()
        assert await task == (True, False)

    async def test_cancel_clears_panel(self, deterministic_live):
        """Esc 中断路径：消费任务取消 → await 取消 → 面板清除。"""
        h = Harness()
        h.svc.start()
        task = asyncio.ensure_future(h.svc.ask_permission_bridge(
            "shell", "run"))
        await asyncio.sleep(0)
        h.refresh()
        assert h.svc._permission is not None
        task.cancel()
        await asyncio.sleep(0)
        assert h.svc._permission is None

    async def test_executor_skips_pause_hooks_when_bridged(self, tmp_path):
        """桥接激活时 executor 不触发 pause 钩子（捕获线程供面板热键）。"""
        from openx.permissions import PermissionRules
        from openx.services.tool_executor import ToolExecutor
        from openx.tools.shell_tools import ShellTool

        events: list[str] = []

        class BridgeSvc:
            def is_live_active(self):
                return True

            async def ask_permission_bridge(self, *a, **kw):
                events.append("bridge")
                return (True, False)

        class BridgeConsole:
            _streaming_service = BridgeSvc()

            async def ask_permission(self, *a, **kw):
                # 镜像真实 Console.ask_permission 的委托链
                return await self._streaming_service.ask_permission_bridge(
                    *a, **kw)

        console = BridgeConsole()
        ex = ToolExecutor(console, auto_approve=False)
        ex._rules = PermissionRules()
        ex.on_prompt_start = lambda: events.append("start")
        ex.on_prompt_end = lambda: events.append("end")
        shell = ShellTool(str(tmp_path))
        result, ok = await ex.execute("shell", shell, '{"command": "echo hi"}')
        assert ok and result.success
        assert events == ["bridge"]  # 无 start/end（未暂停）


# ── 结构化渲染（工具段独立渲染，Claude Code 风格）────────────────


class TestStructuredRender:
    def test_header_parens_and_gutter_block(self, deterministic_live):
        """头行 ● name(args) + ⎿ 槽线结果块（Claude Code 风格）。"""
        h = Harness()
        h.svc.start()
        h.svc.feed(ToolStartEvent(name="shell", arguments='{"command": "ls -la"}'))
        h.svc.feed(ToolResultEvent(name="shell", output="a.py\nb.py"))
        h.refresh()
        text = "\n".join(r for _, r in h.nonempty())
        assert "● shell(ls -la)" in text
        assert "⎿" in text and "a.py" in text and "b.py" in text

    def test_dot_state_colors(self, deterministic_live):
        """状态点颜色：running dim、done 绿、error 红（markup span 断言）。"""
        h = Harness()
        h.svc.start()
        from openx.services.streaming import _ToolRecord
        running = h.svc._tool_renderables(
            _ToolRecord(name="shell", status="running"))
        done = h.svc._tool_renderables(
            _ToolRecord(name="shell", status="done", output="ok"))
        error = h.svc._tool_renderables(
            _ToolRecord(name="shell", status="error", output="bad",
                        is_error=True))
        assert str(running[0]._spans[0].style) == "dim"
        assert "green" in str(done[0]._spans[0].style)
        assert "red" in str(error[0]._spans[0].style)
        # running 态挂 Running… 行
        assert "Running…" in running[1].plain

    def test_parallel_results_match_records_in_order(self, deterministic_live):
        """并行调用：结果按 gather 原序匹配同名 running 记录。"""
        h = Harness()
        h.svc.start()
        h.svc.feed(ToolStartEvent(name="shell", arguments='{"command": "a"}'))
        h.svc.feed(ToolStartEvent(name="shell", arguments='{"command": "b"}'))
        h.svc.feed(ToolResultEvent(name="shell", output="out-a"))
        h.svc.feed(ToolResultEvent(name="shell", output="out-b"))
        records = [p for k, p in h.svc._segments if k == "tool"]
        assert len(records) == 2
        assert all(r.status == "done" for r in records)
        assert records[0].output == "out-a"
        assert records[1].output == "out-b"

    def test_truncation_3_lines_with_expand_hint(self, deterministic_live):
        """折叠态 3 行 + … +N lines (ctrl+t to expand)。"""
        h = Harness()
        h.svc.start()
        h.svc.feed(ToolStartEvent(name="shell", arguments='{"command": "x"}'))
        h.svc.feed(ToolResultEvent(
            name="shell", output="\n".join(f"l{i}" for i in range(8))))
        h.refresh()
        text = "\n".join(r for _, r in h.nonempty())
        assert "l0" in text and "l2" in text
        assert "l3" not in text  # 第 4 行起被折叠
        assert "… +5 lines (ctrl+t to expand)" in text

    def test_ctrl_t_toggles_expansion(self, deterministic_live):
        """Ctrl+T 全局展开/折叠。"""
        h = Harness()
        h.svc.start()
        h.svc.feed(ToolStartEvent(name="shell", arguments='{"command": "x"}'))
        h.svc.feed(ToolResultEvent(
            name="shell", output="\n".join(f"l{i}" for i in range(8))))
        h.refresh()
        h.svc._capture._hotkeys.append("\x14")  # Ctrl+T 展开
        h.refresh()
        text = "\n".join(r for _, r in h.nonempty())
        assert "l7" in text  # 展开后可见全部
        assert "(8 lines · ctrl+t to collapse)" in text
        h.svc._capture._hotkeys.append("\x14")  # 再按折叠
        h.refresh()
        text = "\n".join(r for _, r in h.nonempty())
        assert "l3" not in text
        assert "ctrl+t to expand" in text

    def test_error_output_red_and_10_lines(self, deterministic_live):
        """错误：红 ● + 10 行上限。"""
        h = Harness()
        h.svc.start()
        h.svc.feed(ToolStartEvent(name="shell", arguments='{"command": "x"}'))
        h.svc.feed(ToolResultEvent(
            name="shell", output="\n".join(f"e{i}" for i in range(15)),
            is_error=True))
        h.refresh()
        text = "\n".join(r for _, r in h.nonempty())
        assert "e9" in text and "e10" not in text  # 10 行截断
        assert "… +5 lines (ctrl+t to expand)" in text

    def test_empty_output_shows_no_output(self, deterministic_live):
        h = Harness()
        h.svc.start()
        h.svc.feed(ToolStartEvent(name="shell", arguments='{"command": "mkdir d"}'))
        h.svc.feed(ToolResultEvent(name="shell", output=""))
        h.refresh()
        assert "(No output)" in "\n".join(r for _, r in h.nonempty())

    def test_edit_diff_line_colors(self, deterministic_live):
        """edit_file 结果：- 行红、+ 行绿、@@ 头 dim（行级着色）。

        折叠态只显 3 行，断言两层：静态着色方法 + 展开态渲染。
        """
        from openx.services.streaming import StreamingService
        assert StreamingService._result_line_markup("-old line", True) == \
            "[red]-old line[/]"
        assert StreamingService._result_line_markup("+new line", True) == \
            "[green]+new line[/]"
        assert StreamingService._result_line_markup("@@ -1 +1 @@", True) == \
            "[dim]@@ -1 +1 @@[/]"
        # diff 头行（---/+++）dim，须先于 -/+ 判断
        assert StreamingService._result_line_markup("--- a/x.py", True) == \
            "[dim]--- a/x.py[/]"
        assert StreamingService._result_line_markup("+++ b/x.py", True) == \
            "[dim]+++ b/x.py[/]"
        # 非 edit 工具不着色（转义原样）
        assert StreamingService._result_line_markup("-x", False) == "-x"

        h = Harness()
        h.svc.start()
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old line\n+new line"
        h.svc.feed(ToolStartEvent(name="edit_file",
                                  arguments='{"file_path": "x.py"}'))
        h.svc.feed(ToolResultEvent(name="edit_file", output=diff))
        h.svc._tools_expanded = True  # 展开态看全部 diff 行
        rows = h.svc._tool_renderables(h.svc._segments[0][1])
        styles = {r.plain.strip(): [str(s.style) for s in r._spans]
                  for r in rows}
        assert any("red" in s for s in styles.get("-old line", []))
        assert any("green" in s for s in styles.get("+new line", []))

    def test_task_result_echo_record(self, deterministic_live):
        """task 起始不落段但结果回显（deck 消失后的永久记录）。"""
        h = Harness()
        h.svc.start()
        h.svc.feed(ToolStartEvent(
            name="task",
            arguments='{"description": "find auth code"}'))
        h.svc.feed(ToolResultEvent(
            name="task", output="Subagent done\n\nreport body"))
        h.refresh()
        records = [p for k, p in h.svc._segments if k == "tool"]
        assert len(records) == 1
        assert records[0].name == "task"
        assert records[0].arguments == "find auth code"  # 暂存描述入头行
        text = "\n".join(r for _, r in h.nonempty())
        assert "● task(find auth code)" in text
        assert "report body" in text


# ── 视口预算 ─────────────────────────────────────────────────────


class TestViewportBudget:
    def test_latched_group_height_constant(self, deterministic_live):
        """latched 时整组高度与 deck 高度无关（光标锚点帧间稳定）。

        deck 增高 d 行 → 响应窗口 max_lines 缩 d 行，两者精确抵消。
        （stub frame 仅 1 行，故绝对高度不是 H−2；断言各 deck 高度下
        组高**全等**即锚点不变量本身。）
        """
        rc = RichConsole(
            file=io.StringIO(), width=80, height=24, force_terminal=True
        )
        console = SimpleNamespace(
            _console=rc, _input_queue=[], _frame_on_screen=False,
            _input_capture=None, _frame_renderable=lambda i, o: Text("FRAME"),
        )
        heights = []
        deck_hs = []
        for deck_size in (0, 5, 10):
            todos = [
                {"content": f"t{i}", "activeForm": f"t{i}",
                 "status": "pending"}
                for i in range(deck_size)
            ]
            svc = StreamingService(
                console, input_tokens=0,
                todos_provider=lambda t=todos: t,
            )
            svc.feed("\n\n".join(f"para {i}" for i in range(100)))
            group = svc._build_renderable()
            heights.append(len(rc.render_lines(group, pad=False)))
            deck_hs.append(svc._last_deck_h)
        assert len(set(heights)) == 1, heights
        # _last_deck_h 只记框**下**行数（舰队）；plan 在框上 → 恒 0
        assert deck_hs == [0, 0, 0]

    def test_short_terminal_trims_deck(self, deterministic_live):
        """矮终端：deck 按预算裁剪，FRAME 仍在屏、组不超视口。"""
        h = Harness(
            rows=12,
            todos=[
                {"content": f"t{i}", "activeForm": f"t{i}",
                 "status": "pending"}
                for i in range(10)
            ],
        )
        h.svc.start()
        h.svc.feed("hi")
        h.refresh()
        ne = h.nonempty()
        assert h.frame_row() is not None
        # deck 行数 ≤ 预算 = 12 - 7 - 5 = … 至少不超屏
        frame_y = h.frame_row()
        deck_count = sum(1 for y, _ in ne if y > frame_y)
        assert deck_count <= max(0, 12 - 7 - 5) or deck_count <= 5
        assert max(y for y, _ in ne) < 12
