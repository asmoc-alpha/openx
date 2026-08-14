"""终端 resize（SIGWINCH）支持回归测试 —— v0.3.1。

覆盖（SDD 终端交互 §4.5）：

- ResizeWatcher：事件读后清 / 安装守卫（非 TTY、非主线程）/ 链式前一
  处理器 / 幂等 / handler 绝不写屏；
- _ResizeAwareLive：事件或宽度漂移 → 擦区（``\\r[\\033[{h-1}A]\\r\\033[J``）
  + shape 复位 + 就地重渲；无 resize 不擦；h=1 跳过上移；自动刷新线程
  走覆写；
- pyte 屏幕级：加宽/缩窄后最新内容与 FRAME 完整、pause 期事件在 resume
  后消费、cancel/done 路径、无事件纯漂移兜底；
- 编辑器重绘：锚点 ``up = min(K_old, K_new)``（加宽/缩窄）、整除宽度
  补尾空格、Enter 发 ``\\r\\n``、清框公式修既有整除 off-by-one、宽字符
  按格归位；
- 留屏框复用：等宽复用 / 变宽擦旧框新绘。

风格：pytest-asyncio auto、手写 fake、禁 unittest.mock。

运行：``python -m pytest tests/test_resize.py -q``
"""

from __future__ import annotations

import io
import re
import signal
import threading
from types import SimpleNamespace

import pytest
import pyte
import pyte.modes
from rich.console import Console as RichConsole
from rich.text import Text

from openx.config import OpenXConfig
from openx.services.streaming import StreamingService, _ResizeAwareLive
from openx.ui.resize import ResizeWatcher


# ── 测试基建 ──────────────────────────────────────────────────────


@pytest.fixture
def deterministic_live(monkeypatch):
    """关掉 Live 自动刷新线程与 stdout 劫持（确定性 + 不吞 pytest 输出）。"""
    import openx.services.streaming as streaming_mod

    class _Live(_ResizeAwareLive):
        def __init__(self, *args, **kwargs):
            kwargs.update(
                auto_refresh=False,
                redirect_stdout=False,
                redirect_stderr=False,
            )
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(streaming_mod, "_ResizeAwareLive", _Live)


class StreamHarness:
    """StreamingService + pyte 屏幕；fake console 挂非活动 ResizeWatcher。"""

    def __init__(self, rows: int = 24, cols: int = 80):
        self.screen = pyte.Screen(cols, rows)
        self.screen.set_mode(pyte.modes.LNM)
        self.pyte = pyte.Stream(self.screen)
        self.buf = io.StringIO()
        self.rc = RichConsole(
            file=self.buf, width=cols, height=rows, force_terminal=True
        )
        ns = SimpleNamespace(
            _console=self.rc,
            _input_queue=[],
            _frame_on_screen=False,
            _input_capture=None,
            _frame_width=cols,
            _resize=ResizeWatcher(),  # 不 install：测试手工置事件
        )
        ns._frame_renderable = lambda i, o: self._frame(ns)
        self.ns = ns
        self.svc = StreamingService(ns, input_tokens=0)

    def _frame(self, ns):
        ns._frame_width = self.rc.width  # 模拟生产 _frame_renderable 的簿记
        return Text("FRAME")

    def flush(self):
        self.pyte.feed(self.buf.getvalue())
        self.buf.seek(0)
        self.buf.truncate()

    def rows(self) -> list[str]:
        return [
            "".join(c.data for c in self.screen.buffer[y].values())
            for y in range(self.screen.lines)
        ]

    def resize(self, cols: int, rows: int = 24):
        """模拟终端 resize：pyte 屏幕 + Rich console 尺寸（漂移源）。"""
        self.screen.resize(rows, cols)
        self.rc._width = cols
        self.rc._height = rows


# ── 1. ResizeWatcher ─────────────────────────────────────────────


class TestResizeWatcher:
    def test_event_set_check_clears(self):
        w = ResizeWatcher()
        assert w.check() is False
        w._handle(signal.SIGWINCH, None)
        assert w.check() is True
        assert w.check() is False  # 读后清

    def test_inactive_when_stdout_not_tty(self, monkeypatch):
        called = []
        monkeypatch.setattr(signal, "signal", lambda *a: called.append(a))
        monkeypatch.setattr(sys_stdout(), "isatty", lambda: False)
        w = ResizeWatcher()
        w.install()
        assert w.active is False and called == []

    def test_install_main_thread_tty(self, monkeypatch):
        recorded = []

        def fake_signal(sig, handler):
            recorded.append((sig, handler))
            return signal.SIG_DFL

        monkeypatch.setattr(signal, "signal", fake_signal)
        monkeypatch.setattr(sys_stdout(), "isatty", lambda: True)
        w = ResizeWatcher()
        w.install()
        assert w.active is True
        assert recorded[0][0] == signal.SIGWINCH and recorded[0][1] == w._handle

    def test_install_from_non_main_thread_is_noop(self, monkeypatch):
        monkeypatch.setattr(sys_stdout(), "isatty", lambda: True)
        results = []
        w = ResizeWatcher()

        def run():
            w.install()
            results.append(w.active)

        t = threading.Thread(target=run)
        t.start()
        t.join()
        assert results == [False]  # 非主线程：守卫拦下，不抛异常

    def test_chains_previous_handler(self, monkeypatch):
        calls = []
        prev = lambda signum, frame: calls.append(signum)  # noqa: E731
        monkeypatch.setattr(signal, "signal", lambda sig, h: prev)
        monkeypatch.setattr(sys_stdout(), "isatty", lambda: True)
        w = ResizeWatcher()
        w.install()
        w._handle(signal.SIGWINCH, None)
        assert calls == [signal.SIGWINCH]  # 链式调用前一处理器

    def test_install_idempotent(self, monkeypatch):
        count = []
        monkeypatch.setattr(
            signal, "signal", lambda sig, h: count.append(1) or signal.SIG_DFL
        )
        monkeypatch.setattr(sys_stdout(), "isatty", lambda: True)
        w = ResizeWatcher()
        w.install()
        w.install()
        assert len(count) == 1  # 不重复安装、不自链

    def test_console_owns_inactive_watcher_under_pytest(self, tmp_path):
        from openx.ui.console import Console

        c = Console(config=OpenXConfig(workspace=str(tmp_path)))
        assert isinstance(c._resize, ResizeWatcher)
        assert c._resize.active is False  # pytest 下 stdout 非 TTY
        assert c._resize.check() is False

    def test_handler_writes_nothing(self, monkeypatch):
        import sys

        written = []

        class Rec:
            def write(self, s):
                written.append(s)

            def flush(self):
                pass

        monkeypatch.setattr(sys, "stdout", Rec())
        w = ResizeWatcher()
        w._handle(signal.SIGWINCH, None)
        assert written == []  # 信号处理器绝不写屏


def sys_stdout():
    import sys

    return sys.stdout


# ── 2. _ResizeAwareLive ──────────────────────────────────────────


def _make_live(renderable=None, w=80, h=24, watcher=None):
    buf = io.StringIO()
    rc = RichConsole(file=buf, width=w, height=h, force_terminal=True)
    live = _ResizeAwareLive(
        renderable or Text("hello\nworld"),
        console=rc,
        auto_refresh=False,
        redirect_stdout=False,
        redirect_stderr=False,
        resize=watcher,
    )
    return live, rc, buf


class TestResizeAwareLive:
    def test_plain_refresh_renders_content_once(self):
        # rich 15 原生刷新自带"区域顶 → 擦到屏末 → 重渲"恢复序列；
        # 无 resize 时覆写不额外干预——内容恰好渲染一份
        live, rc, buf = _make_live()
        live.start()
        live.refresh()
        buf.seek(0)
        buf.truncate()
        live.refresh()  # 同尺寸第二次刷新
        assert buf.getvalue().count("hello") == 1
        assert live._live_render._shape is not None
        live.stop()

    def test_preset_event_consumed_and_renders_once(self):
        watcher = ResizeWatcher()
        live, rc, buf = _make_live(watcher=watcher)
        live.start()
        live.refresh()
        h = live._live_render._shape[1]
        buf.seek(0)
        buf.truncate()
        watcher._event.set()
        live.refresh()
        out = buf.getvalue()
        # 覆写即时擦区重渲：擦除序列在内容之前，且内容只渲染一份
        expected = "\r" + (f"\033[{h - 1}A" if h > 1 else "") + "\r\033[J"
        assert expected in out
        assert out.index("\033[J") < out.index("hello")
        assert out.count("hello") == 1
        assert live._live_render._shape is not None  # 重渲后形状重新记录
        assert watcher.check() is False  # 事件已被消费
        # 第三次刷新（无事件同宽）：内容仍恰好一份
        buf.seek(0)
        buf.truncate()
        live.refresh()
        assert buf.getvalue().count("hello") == 1
        live.stop()

    def test_event_with_no_shape_is_clean_first_render(self):
        watcher = ResizeWatcher()
        live, rc, buf = _make_live(watcher=watcher)
        watcher._event.set()  # 首渲前就置事件
        live.start()
        live.refresh()
        out = buf.getvalue()
        assert "\033[J" not in out  # 无旧形状 → 不擦，干净首渲
        assert "hello" in out
        live.stop()

    def test_width_drift_consumes_and_reanchors_without_event(self):
        live, rc, buf = _make_live(watcher=None)  # 无 watcher（Windows 场景）
        live.start()
        live.refresh()
        buf.seek(0)
        buf.truncate()
        rc._width = 100  # 仅改宽度：漂移兜底必须触发即时擦区重渲
        live.refresh()
        out = buf.getvalue()
        # 擦后重渲恰好一份内容，形状重新记录（rich 15 下与原生恢复
        # 字节等价——漂移分支的价值在即时性与跨版本稳健，见类 docstring）
        assert out.count("hello") == 1
        assert live._live_render._shape is not None
        live.stop()

    def test_h_equals_one_event_path_safe(self):
        watcher = ResizeWatcher()
        live, rc, buf = _make_live(renderable=Text("one line"), watcher=watcher)
        live.start()
        live.refresh()
        assert live._live_render._shape[1] == 1
        buf.seek(0)
        buf.truncate()
        watcher._event.set()
        live.refresh()
        out = buf.getvalue()
        # h=1：擦除不含上移序列（原生恢复的空 Control 也不含）
        assert out.count("one line") == 1
        assert watcher.check() is False
        live.stop()

    def test_auto_refresh_thread_uses_override(self):
        watcher = ResizeWatcher()
        buf = io.StringIO()
        rc = RichConsole(file=buf, width=80, height=24, force_terminal=True)
        live = _ResizeAwareLive(
            Text("tick"),
            console=rc,
            auto_refresh=True,
            refresh_per_second=20,
            redirect_stdout=False,
            redirect_stderr=False,
            resize=watcher,
        )
        live.start()
        import time

        time.sleep(0.05)  # 首渲完成、shape 就位
        watcher._event.set()
        time.sleep(0.2)  # 自动刷新线程消费事件
        live.stop()
        assert "\033[J" in buf.getvalue()  # 线程虚调 refresh → 覆写生效


# ── 3. pyte 屏幕级：流式 resize ──────────────────────────────────


class TestStreamResize:
    def _stream_long(self, h):
        h.svc.start()
        for i in range(1, 61):
            h.svc.feed(f"response line {i}\n\n")
        h.svc._live.refresh()
        h.flush()

    def test_grow_keeps_latest_content_and_frame(self, deterministic_live):
        h = StreamHarness(24, 80)
        self._stream_long(h)
        h.resize(100)
        h.ns._resize._event.set()
        h.svc._live.refresh()
        h.flush()
        rows = h.rows()
        assert any("response line 60" in r for r in rows), "最新内容必须可见"
        frame_rows = [y for y, r in enumerate(rows) if "FRAME" in r]
        assert len(frame_rows) == 1, "FRAME 恰好一份（无重影）"
        spin = next(y for y, r in enumerate(rows) if "Answering" in r)
        assert frame_rows[0] > spin and frame_rows[0] <= 23

    def test_shrink_keeps_latest_content_and_frame(self, deterministic_live):
        h = StreamHarness(24, 80)
        self._stream_long(h)
        h.resize(60)
        h.ns._resize._event.set()
        h.svc._live.refresh()
        h.flush()
        rows = h.rows()
        assert any("response line 60" in r for r in rows)
        frame_rows = [y for y, r in enumerate(rows) if "FRAME" in r]
        assert len(frame_rows) == 1 and frame_rows[0] <= 23

    def test_resize_during_pause_consumed_on_resume(self, deterministic_live):
        h = StreamHarness(24, 80)
        self._stream_long(h)
        h.svc.pause()
        h.buf.seek(0)
        h.buf.truncate()
        h.resize(100)
        h.ns._resize._event.set()
        assert h.buf.getvalue() == ""  # 暂停期间零输出
        h.svc.resume()
        h.buf.seek(0)
        h.buf.truncate()
        h.svc._live.refresh()  # resume 后首刷
        out = h.buf.getvalue()
        # pause 已置 shape=None → 首刷不擦（就地新宽渲染）
        assert "\033[J" not in out
        h.flush()
        assert any("FRAME" in r for r in h.rows())  # 框完整
        h.svc.done()

    def test_cancel_after_resize_clears_frame(
        self, deterministic_live, monkeypatch
    ):
        import sys

        h = StreamHarness(24, 80)
        self._stream_long(h)
        h.resize(100)
        h.ns._resize._event.set()
        h.svc._live.refresh()
        h.flush()
        # cancel() 的清框转义直写 sys.stdout（生产即真终端）→ 导入 harness
        monkeypatch.setattr(sys, "stdout", h.buf)
        h.svc.cancel()
        monkeypatch.undo()
        h.flush()
        assert not any("FRAME" in r for r in h.rows())

    def test_done_after_resize_leaves_frame_at_new_width(self, deterministic_live):
        h = StreamHarness(24, 80)
        self._stream_long(h)
        h.resize(100)
        h.ns._resize._event.set()
        h.svc.done()  # done 内最终刷新消费事件
        h.flush()
        rows = h.rows()
        assert any("response line 60" in r for r in rows)
        assert any("FRAME" in r for r in rows)
        assert h.ns._frame_width == 100  # 簿记随新宽刷新

    def test_drift_fallback_without_event(self, deterministic_live):
        h = StreamHarness(24, 80)
        self._stream_long(h)
        h.resize(100)  # 只改尺寸，不置事件（Windows / 事件丢失路径）
        h.svc._live.refresh()
        h.flush()
        rows = h.rows()
        assert any("response line 60" in r for r in rows)
        assert sum("FRAME" in r for r in rows) == 1


# ── 4. 编辑器 resize 重绘 ────────────────────────────────────────


class FakeStdout:
    """录制 write 的假 stdout（Rich force_terminal 下无需真 TTY）。"""

    def __init__(self):
        self.writes: list[str] = []

    def write(self, s: str) -> int:
        self.writes.append(s)
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return True

    def fileno(self):
        return 1

    @property
    def text(self) -> str:
        return "".join(self.writes)


class FakeStdin:
    def isatty(self):
        return True

    def fileno(self):
        return 7


class Script:
    """输入脚本：("char", ch) / ("resize", new_cols) / ("enter",)。"""

    def __init__(self, entries, cols=80):
        self.entries = list(entries)
        self.cols = cols


def _patch_editor(monkeypatch, tmp_path, script, fake_out):
    """接线假 stdin/stdout/termios/select/read/终端尺寸，返回 Console。"""
    import sys
    import openx.ui._components.prompt as prompt_mod
    from openx.ui.console import Console

    # Console 先于 sys.stdout 替换构造：其 __post_init__ 的 watcher.install()
    # 见的是 pytest 捕获 stdout（非 TTY）→ 不安装真实信号处理器
    c = Console(config=OpenXConfig(workspace=str(tmp_path)))
    c._console = RichConsole(
        file=fake_out, width=80, height=24, force_terminal=True
    )
    c._terminal_width = 80

    monkeypatch.setattr(sys, "stdin", FakeStdin())
    monkeypatch.setattr(sys, "stdout", fake_out)
    monkeypatch.setattr("termios.tcgetattr", lambda fd: None)
    monkeypatch.setattr("termios.tcsetattr", lambda fd, when, attrs: None)
    monkeypatch.setattr("tty.setcbreak", lambda fd, when=0: None)

    def fake_select(r, w, x, timeout):
        if not script.entries:
            return ([], [], [])  # 超时挂起（脚本必须以 enter 结束）
        if script.entries[0][0] == "resize":
            script.cols = script.entries.pop(0)[1]
            return ([], [], [])  # 超时 → 触发 resize 检查
        return ([r[0]], [], [])  # 可读

    def fake_read(fd):
        kind, *rest = script.entries.pop(0)
        if kind == "char":
            return rest[0]
        if kind == "enter":
            return "\r"
        raise AssertionError(f"unexpected script entry: {kind}")

    monkeypatch.setattr(prompt_mod.select, "select", fake_select)
    monkeypatch.setattr(prompt_mod, "read_unicode_char", fake_read)
    monkeypatch.setattr(
        prompt_mod,
        "get_terminal_size",
        lambda: SimpleNamespace(columns=script.cols, lines=24),
    )
    return c


class TestEditorRedraw:
    def test_grow_uses_min_anchor_and_new_rules(self, tmp_path, monkeypatch):
        # "中" + 83×a：c=87，K_old=ceil(87/80)=2；加宽 100 → K_new=1 → up=1
        typed = "中" + "a" * 83
        entries = [("char", ch) for ch in typed]
        entries.append(("resize", 100))
        entries.append(("enter",))
        out = FakeStdout()
        script = Script(entries)
        c = _patch_editor(monkeypatch, tmp_path, script, out)

        result = c.print_user_prompt(1, 2)
        assert result == typed  # 缓冲完整返回
        text = out.text
        # 重绘：min(2,1)=1 上移 + 擦除 + 100 宽框线 + 全量回显
        assert "\033[1A\033[J" in text
        assert "─" * 100 in text
        # 重绘的全量回显（最后一次连续出现）在擦除之后
        assert text.count(typed) == 2  # 逐字符拼接 + 重绘整串各一份
        assert text.index("\033[J") < text.rindex(typed)
        # 光标归位：c=87 → 87 % 100 = 87
        assert "\033[3A\r\033[87C" in text

    def test_shrink_uses_old_rows_anchor(self, tmp_path, monkeypatch):
        # 148×a：c=150，K_old=2；缩窄 60 → K_new=3 → up=min(2,3)=2
        typed = "a" * 148
        entries = [("char", ch) for ch in typed]
        entries.append(("resize", 60))
        entries.append(("enter",))
        out = FakeStdout()
        c = _patch_editor(monkeypatch, tmp_path, Script(entries), out)

        assert c.print_user_prompt() == typed
        text = out.text
        assert "\033[2A\033[J" in text
        assert "─" * 60 in text
        assert "\033[3A\r\033[30C" in text  # 150 % 60 = 30

    def test_timeout_without_resize_writes_nothing_extra(self, tmp_path, monkeypatch):
        # 超时但宽度未变：不触发重绘（框线只出现初始绘制的一次）
        entries = [("char", "x")]
        entries += [("__noop__",)] * 0  # 占位保持风格
        entries.append(("enter",))
        out = FakeStdout()
        script = Script(entries)

        # 注入两次纯超时：select 先返回空两次再供字符
        import openx.ui._components.prompt as prompt_mod

        real = {}

        def fake_select(r, w, x, timeout):
            if not hasattr(fake_select, "timeouts"):
                fake_select.timeouts = 0
            if fake_select.timeouts < 2:
                fake_select.timeouts += 1
                return ([], [], [])
            if not script.entries:
                return ([], [], [])
            return ([r[0]], [], [])

        c = _patch_editor(monkeypatch, tmp_path, script, out)
        monkeypatch.setattr(prompt_mod.select, "select", fake_select)

        assert c.print_user_prompt() == "x"
        # 初始新绘 2 条框线 + 键入 "x" 触发一次整框重绘（+2 条）；
        # 两次纯超时不触发任何重绘（本测试钉的就是这个）
        assert out.text.count("─" * 80) == 4

    def test_exact_multiple_appends_trailing_space(self, tmp_path, monkeypatch):
        # 78×a：c=80 恰整除 80 → 重绘补尾空格，c_render=81，k_new=2
        typed = "a" * 78
        entries = [("char", ch) for ch in typed]
        entries.append(("resize", 80))  # 同宽 resize（经事件通道触发）
        entries.append(("enter",))
        out = FakeStdout()
        script = Script(entries)
        c = _patch_editor(monkeypatch, tmp_path, script, out)
        # 走事件通道：resize 条目不改变宽度，手工置事件
        orig_select = None

        import openx.ui._components.prompt as prompt_mod

        def select_with_event(r, w, x, timeout):
            if script.entries and script.entries[0][0] == "resize":
                script.entries.pop(0)
                c._resize._event.set()  # 事件通道（宽度未变）
                return ([], [], [])
            if not script.entries:
                return ([], [], [])
            return ([r[0]], [], [])

        monkeypatch.setattr(prompt_mod.select, "select", select_with_event)

        assert c.print_user_prompt() == typed
        assert c._input_cells_on_screen == 81  # 80 + 尾空格
        assert c._input_rows_on_screen == 2
        # 光标归位：81 % 80 = 1（新行第 1 列，尾空格后）
        assert "\033[3A\r\033[1C" in out.text

    def test_enter_writes_crlf(self, tmp_path, monkeypatch):
        out = FakeStdout()
        c = _patch_editor(
            monkeypatch, tmp_path, Script([("char", "h"), ("enter",)]), out
        )
        assert c.print_user_prompt() == "h"
        # Enter 发 \r\n（取消 pending-wrap 二义性），且先于清框序列
        text = out.text
        assert "\r\n" in text
        assert text.index("\r\n") < text.rindex("\033[")

    def test_clear_uses_drawn_width_not_captured(self, tmp_path, monkeypatch):
        # 80 绘制 → resize 重绘到 100 → 清框按 100 计算（实例态），
        # 而非 print_user_prompt 捕获的旧 tw=80
        typed = "中" + "a" * 83  # c=87
        entries = [("char", ch) for ch in typed]
        entries.append(("resize", 100))
        entries.append(("enter",))
        out = FakeStdout()
        c = _patch_editor(monkeypatch, tmp_path, Script(entries), out)
        c.print_user_prompt()
        # 新宽清框：2 + (87−1)//100 = 2；旧宽会是 2 + 87//80 = 3
        assert "\033[2A\033[J" in out.text
        assert "\033[3A\033[J" not in out.text

    def test_clear_exact_multiple_no_overerase(self, tmp_path, monkeypatch):
        # 整除宽度回归：c=160 @ tw=80 → 重绘补尾空格 c_render=161、
        # k_new=3（输入占 3 物理行）；提交清框 = 2 + (161−1)//80 = 4A，
        # 恰落顶框线（不多一行吞对话、不少一行留残框）。
        typed = "a" * 158  # c=160
        entries = [("char", ch) for ch in typed]
        entries.append(("enter",))
        out = FakeStdout()
        c = _patch_editor(monkeypatch, tmp_path, Script(entries), out)
        c.print_user_prompt()
        assert "\033[4A\033[J" in out.text
        assert "\033[5A\033[J" not in out.text  # 再多一行即吞上方对话

    def test_wide_char_reposition_by_cells(self, tmp_path, monkeypatch):
        typed = "中文"  # 4 格，c=6
        entries = [("char", ch) for ch in typed]
        entries.append(("resize", 100))
        entries.append(("enter",))
        out = FakeStdout()
        c = _patch_editor(monkeypatch, tmp_path, Script(entries), out)
        assert c.print_user_prompt() == typed
        assert "\033[3A\r\033[6C" in out.text  # 按格（非字符数）归位


# ── 5. 留屏框复用 ────────────────────────────────────────────────


class TestFrameReuse:
    def test_reuse_same_width_reuses_frame(self, tmp_path, monkeypatch):
        out = FakeStdout()
        c = _patch_editor(
            monkeypatch, tmp_path, Script([("enter",)]), out
        )
        c._frame_on_screen = True
        c._frame_width = 80  # 与当前宽度一致 → 复用
        c.print_user_prompt()
        text = out.text
        assert text.startswith("\033[?25h\033[3A\033[2K") or "\033[3A\033[2K" in text
        assert "\033[4A\033[J" not in text  # 未擦旧框
        assert "─" * 80 not in text  # 未新绘框线

    def test_reuse_after_resize_erases_and_redraws(self, tmp_path, monkeypatch):
        script = Script([("enter",)], cols=100)
        out = FakeStdout()
        c = _patch_editor(monkeypatch, tmp_path, script, out)
        c._frame_on_screen = True
        c._frame_width = 80  # 旧宽 ≠ 新宽 100 → 擦旧框 + 新绘
        c._console._width = 100  # 生产 Rich console 实时测宽，此处镜像
        c.print_user_prompt()
        text = out.text
        assert "\033[4A\033[J" in text  # 尽力擦除旧框
        assert "─" * 100 in text  # 新宽重绘
        assert text.index("\033[4A\033[J") < text.index("─" * 100)
