"""选择弹窗（_raw_select）pty 级回归：两个用户报告 bug。

Bug A（按住 ↓ 选项重复打印）
    选项标签 = "label — description" 可超终端宽（choose_mode 含中文描述
    ~130 字符）。旧实现按**逻辑行**计上移行数（len(options)+1），终端
    物理换行使实际块体更高 → 每次重渲欠移、块体下移，按住方向键选项
    内容无限重复。修法：选项预折行（_wrap_by_cells，全角字符按 2 列），
    上移基数 = 物理行数 + 1。

Bug B（授权弹窗 Esc 无法打断）
    旧实现在 Esc 后**阻塞** read(1) 等后续字节：裸 Esc 挂死并吃掉下一
    个键；ask_permission 未启用 cancel 语义 → Esc 完全无效。修法：
    select 20ms 判别裸 Esc（同 InputCapture 手法），非取消语义弹窗裸
    Esc → KeyboardInterrupt（与流式期 Esc 打断同语义）。

pty 手法沿用 test_esc_interrupt.py：pty.openpty() 双工，sys.stdin/
stdout monkeypatch 到 slave 端文件对象，后台线程跑弹窗、主线程喂键。

运行：``python -m pytest tests/test_dialog_select.py -q``
"""

from __future__ import annotations

import io
import os
import pty
import select
import sys
import threading
import time
from types import SimpleNamespace

import pyte
import pyte.modes
import pytest
from rich.console import Console as RichConsole

from openx.config import OpenXConfig
from openx.ui.console import Console
from openx.ui._components import dialogs
from openx.ui._components.dialogs import _wrap_by_cells, _cell_len
from openx.ui._components.prompt import paste_aware_input


# ── 基建 ─────────────────────────────────────────────────────────


class _SelectBox:
    """后台线程运行 _raw_select 的结果容器。"""

    def __init__(self):
        self.result = None
        self.exc = None
        self.done = threading.Event()


class _StdinStub:
    """_raw_select 只需 fileno()/isatty()——读键全走 os.read(fd)。"""

    def __init__(self, fd):
        self._fd = fd

    def fileno(self):
        return self._fd

    def isatty(self):
        return True


def _drive(monkeypatch, tmp_path, options, keys, cancel=dialogs._NO_CANCEL,
           cancel_chars=""):
    """pty 驱动 _raw_select：喂入 keys（逐项间隔 60ms），返回 (box, 输出字节)。"""
    master, slave = pty.openpty()
    fout = os.fdopen(os.dup(slave), "w", buffering=1)
    monkeypatch.setattr(sys, "stdin", _StdinStub(slave))
    monkeypatch.setattr(sys, "stdout", fout)

    console = Console(config=OpenXConfig(workspace=str(tmp_path)))
    console._terminal_width = 80

    box = _SelectBox()

    def run():
        try:
            box.result = console._raw_select(
                options, 0, "Choose:",
                cancel=cancel, cancel_chars=cancel_chars,
            )
        except BaseException as e:  # noqa: BLE001 —— 捕获 KeyboardInterrupt
            box.exc = e
        finally:
            box.done.set()

    th = threading.Thread(target=run, daemon=True)
    th.start()

    out: list[bytes] = []

    def drain(wait: float = 0.05):
        while select.select([master], [], [], wait)[0]:
            try:
                data = os.read(master, 65536)
            except OSError:
                return
            if not data:
                return
            out.append(data)
            wait = 0.0

    try:
        time.sleep(0.05)  # 首渲
        drain()
        for key in keys:
            os.write(master, key)
            time.sleep(0.06)
            drain()
        assert box.done.wait(5), "弹窗挂死（未在规定时间返回）"
    finally:
        if not box.done.is_set():
            # 解阻塞在读的弹窗线程（Ctrl-C → KeyboardInterrupt）
            try:
                os.write(master, b"\x03")
            except OSError:
                pass
            box.done.wait(1)
        drain(0.02)
        fout.close()
        os.close(master)
        os.close(slave)
    return box, b"".join(out), console


def _screen_rows(raw: bytes, rows: int = 24, cols: int = 80) -> list[str]:
    screen = pyte.Screen(cols, rows)
    screen.set_mode(pyte.modes.LNM)
    pyte.Stream(screen).feed(raw.decode("utf-8", "replace"))
    return [
        "".join(c.data for c in screen.buffer[y].values()).rstrip()
        for y in range(rows)
    ]


LONG_OPTIONS = [
    ("Auto — Agent may write/run; normal permission prompts apply "
     "(stored rules & whitelist respected). 自动模式：按常规权限流程执行。",
     "auto"),
    ("Plan — Read-only exploration first, then approve a full plan. "
     "计划模式：先只读探索，提交计划供你审批。",
     "plan"),
    ("Stay in manual — Confirm every write/shell call individually. "
     "保持手动：每次写入/执行都逐项确认。",
     "manual"),
    ("Other (type your own)", "other"),
]


# ── Bug A：按住 ↓ 不得重复打印 ───────────────────────────────────


class TestHoldDownNoRepeat:
    def test_long_labels_wrapped_not_repeated(self, monkeypatch, tmp_path):
        """超长选项（含 CJK）折行渲染；连按 ↓ 三次屏上仍只有一份选项。"""
        box, raw, console = _drive(
            monkeypatch, tmp_path, LONG_OPTIONS,
            [b"\x1b[B", b"\x1b[B", b"\x1b[B", b"\r"],
        )
        assert box.exc is None, f"弹窗异常：{box.exc!r}"
        assert box.result == "other"  # 0 → 下三次 → 第 4 项

        # 折行确实发生（130 字符标签 > 75 列预算）
        assert len(console._wrapped_options[0]) >= 2

        rows = _screen_rows(raw)
        auto_rows = [r for r in rows if "Auto" in r]
        plan_rows = [r for r in rows if "Plan" in r]
        # 修复前：每次重渲欠移 → 块体下移 → 屏上多份副本（≥4 行）
        assert len(auto_rows) <= 2, f"选项重复打印：{auto_rows!r}"
        assert len(plan_rows) <= 2, f"选项重复打印：{plan_rows!r}"
        # 光标所在提示行唯一
        assert sum(1 for r in rows if "Choose:" in r) == 1

    def test_hint_shows_esc_semantics(self, monkeypatch, tmp_path):
        box, raw, _ = _drive(
            monkeypatch, tmp_path, LONG_OPTIONS[:2], [b"\r"],
        )
        assert box.result == "auto"
        assert "Esc to interrupt" in raw.decode("utf-8", "replace")


# ── Bug B：Esc 打断 / 取消 ───────────────────────────────────────


class TestEscInterrupt:
    def test_bare_esc_interrupts_authorization_dialog(
        self, monkeypatch, tmp_path
    ):
        """ask_permission 同款（cancel 未启用）：裸 Esc → KeyboardInterrupt。"""
        options = [("Yes, allow once", (True, False)),
                   ("No, don't run", (False, False))]
        box, raw, _ = _drive(monkeypatch, tmp_path, options, [b"\x1b"])
        assert isinstance(box.exc, KeyboardInterrupt), (
            f"裸 Esc 应打断授权弹窗，实际：result={box.result!r} exc={box.exc!r}"
        )

    def test_arrow_sequence_not_confused_with_esc(
        self, monkeypatch, tmp_path
    ):
        """方向键序列（ESC 开头）不得误判为裸 Esc。"""
        options = [("Yes", True), ("No", False)]
        box, raw, _ = _drive(
            monkeypatch, tmp_path, options, [b"\x1b[B", b"\r"],
        )
        assert box.exc is None
        assert box.result is False  # 下移一次 → 第 2 项

    def test_bare_esc_returns_cancel_when_enabled(
        self, monkeypatch, tmp_path
    ):
        """pick_session 语义（cancel=None）：裸 Esc → 取消值，不上抛。"""
        options = [("session A", "a"), ("session B", "b")]
        box, raw, _ = _drive(
            monkeypatch, tmp_path, options, [b"\x1b"],
            cancel=None, cancel_chars="q",
        )
        assert box.exc is None
        assert box.result is None  # cancel 值
        assert "Esc to cancel" in raw.decode("utf-8", "replace")

    def test_ctrl_c_still_interrupts(self, monkeypatch, tmp_path):
        options = [("Yes", True), ("No", False)]
        box, raw, _ = _drive(monkeypatch, tmp_path, options, [b"\x03"])
        assert isinstance(box.exc, KeyboardInterrupt)


# ── 折行助手单测 ─────────────────────────────────────────────────


class TestWrapByCells:
    def test_cjk_counts_double(self):
        assert _cell_len("ab") == 2
        assert _cell_len("内存") == 4
        assert _cell_len("a内") == 3

    def test_ascii_word_wrap(self):
        lines = _wrap_by_cells("one two three four", 10)
        assert all(_cell_len(ln) <= 10 for ln in lines)
        assert " ".join(lines) == "one two three four"

    def test_long_word_char_split(self):
        lines = _wrap_by_cells("abcdefghijklmnop", 5)
        assert lines == ["abcde", "fghij", "klmno", "p"]

    def test_cjk_wraps_at_cell_boundary(self):
        text = "自动模式按常规权限流程执行"  # 12 字 = 24 列
        lines = _wrap_by_cells(text, 10)
        assert all(_cell_len(ln) <= 10 for ln in lines)
        assert "".join(lines) == text

    def test_empty(self):
        assert _wrap_by_cells("", 10) == [""]


# ── 括号粘贴：多行文本支持 ───────────────────────────────────────


def _pty_pair(monkeypatch):
    """pty + sys.stdin/stdout monkeypatch + 后台持续 drain。

    **master 必须持续被读取**：被测代码收尾的 ``tcsetattr(TCSADRAIN)``
    会等待 pty 挂起输出被 master 读尽——主线程若在 wait() 上阻塞而不
    读 master 即死锁（真实 tty 由硬件排空，无此问题）。返回
    (master, slave, fout, stop_drain)；stop_drain() 停 drainer 并返回
    累积的全部输出字节。
    """
    master, slave = pty.openpty()
    fout = os.fdopen(os.dup(slave), "w", buffering=1)
    monkeypatch.setattr(sys, "stdin", _StdinStub(slave))
    monkeypatch.setattr(sys, "stdout", fout)
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

    th = threading.Thread(target=drainer, daemon=True)
    th.start()

    def stop_drain() -> bytes:
        stop.set()
        th.join(2)
        return b"".join(out)

    return master, slave, fout, stop_drain


class TestPasteAwareInput:
    def test_multiline_paste_returns_joined(self, monkeypatch, tmp_path):
        """对话框自由输入：多行粘贴以 \\n 连接整体返回（不再截首行）。"""
        master, slave, fout, stop_drain = _pty_pair(monkeypatch)
        rc = RichConsole(file=io.StringIO(), width=80)
        box = _SelectBox()

        def run():
            try:
                box.result = paste_aware_input(rc, "  ▸ your answer ")
            except BaseException as e:  # noqa: BLE001
                box.exc = e
            finally:
                box.done.set()

        th = threading.Thread(target=run, daemon=True)
        th.start()
        try:
            time.sleep(0.1)
            os.write(master, b"\x1b[200~line one\nline two\x1b[201~")
            time.sleep(0.1)
            os.write(master, b"\r")  # Enter 提交
            assert box.done.wait(5), "paste_aware_input 挂死"
        finally:
            stop_drain()
            fout.close()
            os.close(master)
            os.close(slave)
        assert box.exc is None, f"异常：{box.exc!r}"
        assert box.result == "line one\nline two"

    def test_typed_input_single_line(self, monkeypatch, tmp_path):
        """普通键入行为不变：单行、Enter 提交。"""
        master, slave, fout, stop_drain = _pty_pair(monkeypatch)
        rc = RichConsole(file=io.StringIO(), width=80)
        box = _SelectBox()

        def run():
            try:
                box.result = paste_aware_input(rc, "q ")
            except BaseException as e:  # noqa: BLE001
                box.exc = e
            finally:
                box.done.set()

        th = threading.Thread(target=run, daemon=True)
        th.start()
        try:
            time.sleep(0.1)
            os.write(master, b"hello\r")
            assert box.done.wait(5), "paste_aware_input 挂死"
        finally:
            stop_drain()
            fout.close()
            os.close(master)
            os.close(slave)
        assert box.exc is None
        assert box.result == "hello"

    def test_paste_content_echoed(self, monkeypatch, tmp_path):
        """/config 粘贴可见（用户报告 bug 3）：粘贴内容即时回显上屏。"""
        master, slave, fout, stop_drain = _pty_pair(monkeypatch)
        rc = RichConsole(file=io.StringIO(), width=80)
        box = _SelectBox()

        def run():
            try:
                box.result = paste_aware_input(rc, "q ")
            except BaseException as e:  # noqa: BLE001
                box.exc = e
            finally:
                box.done.set()

        th = threading.Thread(target=run, daemon=True)
        th.start()
        try:
            time.sleep(0.1)
            os.write(master, b"\x1b[200~api-base-value\ndef2\x1b[201~")
            time.sleep(0.1)
            os.write(master, b"\r")
            assert box.done.wait(5), "paste_aware_input 挂死"
        finally:
            raw = stop_drain()
            fout.close()
            os.close(master)
            os.close(slave)
        assert box.exc is None
        assert box.result == "api-base-value\ndef2"
        # 回显落屏：输出字节含粘贴内容（旧版只收不显）
        assert b"api-base-value" in raw, "粘贴内容未回显"
        assert b"def2" in raw


class TestEditorPaste:
    def test_multiline_paste_preview_and_full_submit(
        self, monkeypatch, tmp_path
    ):
        """主输入框：多行粘贴 → 框内首行+(+N)预览，Enter 提交全文。"""
        master, slave, fout, stop_drain = _pty_pair(monkeypatch)
        monkeypatch.setattr(
            "openx.ui._components.prompt.get_terminal_size",
            lambda: os.terminal_size((80, 24)),
        )
        console = Console(config=OpenXConfig(workspace=str(tmp_path)))
        console._terminal_width = 80
        box = _SelectBox()

        def run():
            try:
                box.result = console._read_line_interactive()
            except BaseException as e:  # noqa: BLE001
                box.exc = e
            finally:
                box.done.set()

        th = threading.Thread(target=run, daemon=True)
        th.start()
        try:
            time.sleep(0.1)
            os.write(master, b"\x1b[200~first line\nsecond line\x1b[201~")
            time.sleep(0.1)
            os.write(master, b"\r")
            assert box.done.wait(5), "_read_line_interactive 挂死"
        finally:
            raw = stop_drain()
            fout.close()
            os.close(master)
            os.close(slave)

        assert box.exc is None, f"异常：{box.exc!r}"
        assert box.result == "first line\nsecond line"
        # 多行粘贴全展开落屏（所输即所见）
        screen = pyte.Screen(80, 24)
        screen.set_mode(pyte.modes.LNM)
        pyte.Stream(screen).feed(raw.decode("utf-8", "replace"))
        text = "\n".join(
            "".join(c.data for c in screen.buffer[y].values())
            for y in range(24)
        )
        assert "first line" in text
        assert "second line" in text
        assert "+1 more lines" not in text


class TestEditorShiftEnter:
    def test_shift_enter_inserts_newline_enter_submits(
        self, monkeypatch, tmp_path
    ):
        """主输入框：Shift+Enter 插字面换行（预览首行+行数），Enter
        提交含 \\n 的全文。pty 真键路（kitty 序列字节）。"""
        master, slave, fout, stop_drain = _pty_pair(monkeypatch)
        monkeypatch.setattr(
            "openx.ui._components.prompt.get_terminal_size",
            lambda: os.terminal_size((80, 24)),
        )
        console = Console(config=OpenXConfig(workspace=str(tmp_path)))
        console._terminal_width = 80
        box = _SelectBox()

        def run():
            try:
                box.result = console._read_line_interactive()
            except BaseException as e:  # noqa: BLE001
                box.exc = e
            finally:
                box.done.set()

        th = threading.Thread(target=run, daemon=True)
        th.start()
        try:
            time.sleep(0.1)
            os.write(master, b"first line")
            time.sleep(0.05)
            os.write(master, b"\x1b[13;2u")  # Shift+Enter（kitty 形）
            time.sleep(0.1)
            os.write(master, b"second line")
            time.sleep(0.1)
            os.write(master, b"\r")          # Enter 提交
            assert box.done.wait(5), "_read_line_interactive 挂死"
        finally:
            raw = stop_drain()
            fout.close()
            os.close(master)
            os.close(slave)

        assert box.exc is None, f"异常：{box.exc!r}"
        assert box.result == "first line\nsecond line"
        # 换行后框内多行全展开（所输即所见），不再是单行预览
        screen = pyte.Screen(80, 24)
        screen.set_mode(pyte.modes.LNM)
        pyte.Stream(screen).feed(raw.decode("utf-8", "replace"))
        text = "\n".join(
            "".join(c.data for c in screen.buffer[y].values())
            for y in range(24)
        )
        assert "first line" in text
        assert "second line" in text
        assert "+1 more lines" not in text


class TestEditorCursor:
    """输入光标移动 + 删除无残影（用户报告 bug 1/2）。"""

    def _run_editor(self, monkeypatch, tmp_path, keys):
        """pty 驱动编辑器，依次喂 keys，返回 (box, 输出字节)。"""
        master, slave, fout, stop_drain = _pty_pair(monkeypatch)
        monkeypatch.setattr(
            "openx.ui._components.prompt.get_terminal_size",
            lambda: os.terminal_size((80, 24)),
        )
        console = Console(config=OpenXConfig(workspace=str(tmp_path)))
        console._terminal_width = 80
        box = _SelectBox()

        def run():
            try:
                box.result = console._read_line_interactive()
            except BaseException as e:  # noqa: BLE001
                box.exc = e
            finally:
                box.done.set()

        th = threading.Thread(target=run, daemon=True)
        th.start()
        try:
            time.sleep(0.1)
            for k in keys:
                os.write(master, k)
                time.sleep(0.05)
            assert box.done.wait(5), "_read_line_interactive 挂死"
        finally:
            raw = stop_drain()
            fout.close()
            os.close(master)
            os.close(slave)
        assert box.exc is None, f"异常：{box.exc!r}"
        return box, raw

    def test_arrow_left_inserts_in_middle(self, monkeypatch, tmp_path):
        """"ac" → ← → "b" → 光标移到 a|c 之间插入 → 提交 "abc"。"""
        box, _ = self._run_editor(
            monkeypatch, tmp_path,
            [b"a", b"c", b"\x1b[D", b"b", b"\r"],
        )
        assert box.result == "abc"

    def test_backspace_deletes_before_cursor(self, monkeypatch, tmp_path):
        """"abc" → ← → 退格删光标前的 b → 提交 "ac"。"""
        box, _ = self._run_editor(
            monkeypatch, tmp_path,
            [b"a", b"b", b"c", b"\x1b[D", b"\x7f", b"\r"],
        )
        assert box.result == "ac"

    def test_cjk_backspace_no_screen_residue(self, monkeypatch, tmp_path):
        """宽字符删尽后屏上零残影（旧增量写 bug：删干净了仍有残字）。"""
        box, raw = self._run_editor(
            monkeypatch, tmp_path,
            ["内存".encode(), b"\x7f", b"\x7f", b"\r"],
        )
        assert box.result == ""
        screen = pyte.Screen(80, 24)
        screen.set_mode(pyte.modes.LNM)
        pyte.Stream(screen).feed(raw.decode("utf-8", "replace"))
        rows = [
            "".join(c.data for c in screen.buffer[y].values())
            for y in range(24)
        ]
        prompt_rows = [r for r in rows if "❯" in r]
        assert prompt_rows, "提示符应在屏"
        for r in prompt_rows:
            assert r.strip() == "❯", f"❯ 行存在残影：{r!r}"
        assert not any("内" in r or "存" in r for r in rows)

    def test_multiline_edits_across_lines(self, monkeypatch, tmp_path):
        """跨行光标移动：第二行行首 ← 回第一行行尾插入/删除。"""
        # "ab" + 换行 + "cd"，←← 回到第二行行首，插入 X → "ab\nXcd"
        box, raw = self._run_editor(
            monkeypatch, tmp_path,
            [b"a", b"b", b"\x1b[13;2u", b"c", b"d",
             b"\x1b[D", b"\x1b[D", b"X", b"\r"],
        )
        assert box.result == "ab\nXcd"
        # 两行皆可见（多行全展开）
        screen = pyte.Screen(80, 24)
        screen.set_mode(pyte.modes.LNM)
        pyte.Stream(screen).feed(raw.decode("utf-8", "replace"))
        text = "\n".join(
            "".join(c.data for c in screen.buffer[y].values())
            for y in range(24)
        )
        assert "ab" in text and "Xcd" in text

    def test_backspace_across_line_boundary(self, monkeypatch, tmp_path):
        """跨行边界退格：删掉行首字符后两行仍完整渲染。"""
        # "ab\nXcd"，←← 至 X 后（c 前），退格删 X → "ab\ncd"
        box, raw = self._run_editor(
            monkeypatch, tmp_path,
            [b"a", b"b", b"\x1b[13;2u", b"X", b"c", b"d",
             b"\x1b[D", b"\x1b[D", b"\x7f", b"\r"],
        )
        assert box.result == "ab\ncd"
        screen = pyte.Screen(80, 24)
        screen.set_mode(pyte.modes.LNM)
        pyte.Stream(screen).feed(raw.decode("utf-8", "replace"))
        rows = [
            "".join(c.data for c in screen.buffer[y].values()).rstrip()
            for y in range(24)
        ]
        assert any("ab" in r for r in rows)
        assert any(r.endswith("cd") or "cd" in r for r in rows)
        assert not any("X" in r for r in rows), "X 应被删除且无残影"


class TestFramePreview:
    def test_frame_renderable_multiline_preview(self, tmp_path):
        """流式帧渲染 cap.current 含 \\n：首行+提示，框恒 4 行。"""
        console = Console(config=OpenXConfig(workspace=str(tmp_path)))
        buf = io.StringIO()
        console._console = RichConsole(
            file=buf, width=80, height=24, force_terminal=True
        )
        console._terminal_width = 80
        console._input_capture = SimpleNamespace(
            active=True, current="l1\nl2\nl3"
        )
        g = console._frame_renderable(0, 0)
        lines = console._console.render_lines(g, pad=False)
        assert len(lines) == 4, "框必须恒 4 行"
        joined = "\n".join("".join(s.text for s in ln) for ln in lines)
        assert "l1  (+2 more lines)" in joined
        assert "l2" not in joined
