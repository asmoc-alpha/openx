"""斜杠命令补全菜单回归测试 —— v0.4.2。

覆盖：
- 单元：menu_entries 数据源（排序/别名归组）；_slash_menu 过滤语义
  （/ 起头、前缀匹配主名与别名、命令名完整后关闭）；_menu_window
  窗口切片；_format_menu_line 选中反白与单行截断；
- pty 进程级：真实 cbreak 编辑器里键入 "/" 弹出菜单（pyte 屏幕断言），
  ↑↓ 导航、Tab 补全为 "name "、Enter 按选中提交、Esc 关菜单保留输入。

风格：pytest-asyncio auto（本文件多为同步）、手写替身、禁 unittest.mock。

运行：``python -m pytest tests/test_slash_menu.py -q``
"""

from __future__ import annotations

import codecs
import io
import json
import os
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import time
import tty

import pyte
import pyte.modes
import pytest

from openx.config import OpenXConfig
from openx.ui.console import Console


# ── 单元：数据源与纯函数 ─────────────────────────────────────────

class TestMenuEntries:
    def test_sorted_and_described(self):
        from openx.cli.commands import menu_entries
        entries = menu_entries()
        names = [n for n, _, _ in entries]
        assert names == sorted(names)
        assert "help" in names and "quit" in names
        descs = dict((n, d) for n, d, _ in entries)
        assert descs["help"]  # 有描述

    def test_aliases_grouped(self):
        from openx.cli.commands import menu_entries
        by_name = {n: a for n, _, a in menu_entries()}
        assert "exit" in by_name["quit"] and "q" in by_name["quit"]


def _console() -> Console:
    c = Console(OpenXConfig())
    c._terminal_width = 80
    return c


class TestSlashMenuFilter:
    def test_root_slash_lists_all(self):
        c = _console()
        items = c._slash_menu(list("/"))
        assert items and len(items) > 5

    def test_prefix_matches_name(self):
        c = _console()
        names = [n for n, _, _ in c._slash_menu(list("/hel"))]
        assert names == ["help"]

    def test_prefix_matches_alias(self):
        c = _console()
        # "ex" 命中别名 exit → 归到主名 quit
        names = [n for n, _, _ in c._slash_menu(list("/ex"))]
        assert "quit" in names

    def test_closed_without_leading_slash(self):
        c = _console()
        assert c._slash_menu(list("he/")) is None
        assert c._slash_menu(list("")) is None

    def test_closed_after_space(self):
        c = _console()
        assert c._slash_menu(list("/model g")) is None

    def test_no_match_none(self):
        c = _console()
        assert c._slash_menu(list("/zzzzz")) is None


class TestMenuWindow:
    def test_small_list_passthrough(self):
        c = _console()
        items = [("a", "", []), ("b", "", [])]
        rows, above, below = c._menu_window(items, 0)
        assert rows == items and above == 0 and below == 0

    def test_large_list_windowed(self):
        c = _console()
        items = [(f"c{i:02d}", "", []) for i in range(30)]
        rows, above, below = c._menu_window(items, 15)
        assert len(rows) == c._MENU_MAX_ROWS
        assert above + len(rows) + below == 30
        # 选中项在切片内
        assert items[15] in rows

    def test_window_clamps_at_edges(self):
        c = _console()
        items = [(f"c{i:02d}", "", []) for i in range(30)]
        rows, above, below = c._menu_window(items, 0)
        assert above == 0 and rows[0] == items[0]
        rows, above, below = c._menu_window(items, 29)
        assert below == 0 and rows[-1] == items[-1]


class TestFormatMenuLine:
    def test_selected_reverse_video(self):
        c = _console()
        sel = c._format_menu_line("help", "Show all", [], True, 80)
        plain = c._format_menu_line("help", "Show all", [], False, 80)
        assert "\033[7m" in sel and "\033[7m" not in plain
        assert "help" in sel and "Show all" in sel

    def test_alias_hint(self):
        c = _console()
        line = c._format_menu_line("quit", "Exit", ["exit", "q"], False, 80)
        assert "(exit,q)" in line

    def test_truncated_to_width(self):
        c = _console()
        line = c._format_menu_line(
            "x", "d" * 500, [], False, 40)
        visible = re.sub(r"\033\[[0-9;]*m", "", line)
        assert len(visible) <= 40


# ── pty 进程级：真实编辑器交互 ───────────────────────────────────

CHILD = r'''
import sys, json
sys.path.insert(0, "{root}")
from openx.config import OpenXConfig
from openx.ui.console import Console
console = Console(OpenXConfig())
line = console.print_user_prompt()
print(json.dumps({{"line": line}}), flush=True)
'''


class PtyPrompt:
    """在 pty 里跑真实 print_user_prompt 的驱动：写入按键、pyte 读屏。"""

    def __init__(self):
        root = "/Users/asmoc/Documents/code/openx"
        self.master, slave = pty.openpty()
        # 固定窗口尺寸 80x24（否则 pty 默认 0 → 宽度探测不稳定）
        import fcntl
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
                    struct.pack("HHHH", 24, 80, 0, 0))
        self.proc = subprocess.Popen(
            [sys.executable, "-c", CHILD.format(root=root)],
            stdin=slave, stdout=slave, stderr=slave, close_fds=True,
        )
        os.close(slave)
        self.screen = pyte.Screen(80, 24)
        self.screen.set_mode(pyte.modes.LNM)
        self.pyte = pyte.Stream(self.screen)
        # 增量 UTF-8 解码：read() 块边界可能切开多字节字符（框线 "─"
        # = e2 94 80），逐块 decode(replace) 会造出 U+FFFD 并让 pyte 错
        # 位——真实终端按字节流连续解码，绝无此问题。
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.raw = b""  # 全量原始字节：子进程结果 JSON 可能被 pump 先吃到

    def pump(self, seconds: float = 0.3):
        """把 pty 输出喂进 pyte（最多 seconds 秒），同时留存原始字节。"""
        end = time.time() + seconds
        while time.time() < end:
            r, _, _ = select.select([self.master], [], [], 0.05)
            if r:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    return
                if not data:
                    return
                self.raw += data
                self.pyte.feed(self._decoder.decode(data))

    def rows(self) -> list[str]:
        return [
            "".join(c.data for c in self.screen.buffer[y].values()).rstrip()
            for y in range(24)
        ]

    def text(self) -> str:
        return "\n".join(self.rows())

    def type(self, data: bytes, settle: float = 0.25):
        os.write(self.master, data)
        self.pump(settle)

    def wait_for(self, needle: str, timeout: float = 5.0):
        end = time.time() + timeout
        while time.time() < end:
            if needle in self.text():
                return
            self.pump(0.1)
        raise AssertionError(f"screen never showed {needle!r}:\n{self.text()}")

    def finish(self) -> dict:
        """读子进程最终的 JSON 行（print_user_prompt 的返回值）。

        结果行可能早已被 pump 读走（子进程退出很快）——先扫已累积的
        raw，未命中再继续读 master。
        """
        end = time.time() + 5.0
        while time.time() < end:
            # JSON 行可能前缀清框 ANSI（\033[2A\033[J）→ 正则找子串
            m = re.search(rb'\{"line":[^\n]*\}', self.raw)
            if m:
                if self.proc.poll() is None:
                    self.proc.wait(timeout=5)
                return json.loads(m.group(0))
            r, _, _ = select.select([self.master], [], [], 0.1)
            if r:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    break
                if not data:
                    break
                self.raw += data
                self.pyte.feed(self._decoder.decode(data))
        raise AssertionError(f"no result JSON; raw tail={self.raw[-300:]!r}")

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        os.close(self.master)


class TestMenuInRealEditor:
    def test_slash_opens_menu_and_tab_completes(self):
        p = PtyPrompt()
        try:
            p.wait_for("❯")                  # 输入框就绪
            p.type(b"/he")
            p.wait_for("help")               # 菜单出现 help 候选
            assert "Show all available commands" in p.text()
            # Tab 补全 → 菜单关闭、输入成 "/help "
            p.type(b"\t")
            time.sleep(0.2)
            p.pump(0.2)
            assert "Show all available commands" not in p.text()  # 菜单已收
            p.type(b"\r")
            assert p.finish()["line"] == "/help "
        finally:
            p.close()

    def test_enter_submits_selected(self):
        p = PtyPrompt()
        try:
            p.wait_for("❯")
            p.type(b"/hel")
            p.wait_for("help")
            p.type(b"\r")                    # 按选中项提交（/hel → /help）
            assert p.finish()["line"] == "/help"
        finally:
            p.close()

    def test_down_then_enter_selects_second(self):
        p = PtyPrompt()
        try:
            p.wait_for("❯")
            p.type(b"/")                     # 全量菜单，首项按字母序
            p.wait_for("auto-approve")
            p.type(b"\x1b[B")                # ↓ 选中第二项
            p.type(b"\r")
            line = p.finish()["line"]
            assert line.startswith("/") and line != "/auto-approve"
        finally:
            p.close()

    def test_esc_closes_menu_keeps_input(self):
        p = PtyPrompt()
        try:
            p.wait_for("❯")
            p.type(b"/he")
            p.wait_for("help")
            p.type(b"\x1b")                  # 单独 Esc → 关菜单，不清输入
            time.sleep(0.2)
            p.pump(0.2)
            assert "Show all available commands" not in p.text()
            p.type(b"lp\r")                  # 输入仍在：/he + lp → /help
            assert p.finish()["line"] == "/help"
        finally:
            p.close()

    def test_space_after_name_closes_menu(self):
        p = PtyPrompt()
        try:
            p.wait_for("❯")
            p.type(b"/model")
            p.wait_for("model")
            p.type(b" g")                    # 写参数 → 菜单关闭
            time.sleep(0.2)
            p.pump(0.2)
            assert "Switch LLM model" not in p.text()
            p.type(b"pt-4o\r")
            assert p.finish()["line"] == "/model gpt-4o"
        finally:
            p.close()
