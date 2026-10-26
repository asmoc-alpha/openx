"""斜杠命令补全回归测试（prompt_toolkit Completer 版）。

覆盖：
- 单元：menu_entries 数据源（排序/别名归组）；OpenXCompleter 过滤语义
  （/ 起头、前缀匹配主名与别名、命令名完整后关闭、meta 带描述）；
- pty 进程级：真实 prompt_toolkit 会话里键入 "/" 弹出补全菜单（pyte
  屏幕断言），↓ 导航、Tab 补全为 "name "、Enter 按选中提交、Esc 关菜单
  保留输入、写参数收菜单。

历史：v0.4.2 手绘菜单（_slash_menu/_menu_window/_format_menu_line）→
prompt_toolkit 迁移后由 OpenXCompleter + ptk COLUMN 补全承担，渲染与
导航键语义归 ptk（本文件随之改写）。

pty 须知：ptk 启动时发 CPR 查询（\\x1b[6n），无应答会等 CPR_TIMEOUT
（2s）才降级——pump 里检测到查询即按 pyte 光标位置应答，否则每个用例
平白付 2s。

运行：``python -m pytest tests/ui/test_slash_menu.py -q``
"""

from __future__ import annotations

import codecs
import io  # noqa: F401  （保持与其它 ui 测试一致的导入面）
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

import pyte
import pyte.modes
import pytest

from openx.config import OpenXConfig
from openx.ui.console import Console


# ── 单元：数据源 ─────────────────────────────────────────────────

class TestMenuEntries:
    def test_sorted_and_described(self):
        from openx.app.cli.commands import menu_entries
        entries = menu_entries()
        names = [n for n, _, _ in entries]
        assert names == sorted(names)
        assert "help" in names and "quit" in names
        descs = dict((n, d) for n, d, _ in entries)
        assert descs["help"]  # 有描述

    def test_aliases_grouped(self):
        from openx.app.cli.commands import menu_entries
        by_name = {n: a for n, _, a in menu_entries()}
        assert "exit" in by_name["quit"] and "q" in by_name["quit"]


# ── 单元：OpenXCompleter 过滤语义（原 _slash_menu 测试的迁移）─────


def _names(text: str) -> list[str]:
    from openx.ui._components.editor import OpenXCompleter
    from prompt_toolkit.document import Document
    c = OpenXCompleter()
    return [x.text for x in c.get_completions(Document(text), None)]


class TestCompleterFilter:
    def test_root_slash_lists_all(self):
        items = _names("/")
        assert items and len(items) > 5

    def test_prefix_matches_name(self):
        assert _names("/hel") == ["help"]

    def test_prefix_matches_alias(self):
        # "ex" 命中别名 exit → 提交文本是主名 quit（同旧版菜单语义）
        assert "quit" in _names("/ex")

    def test_closed_without_leading_slash(self):
        assert _names("he/") == []
        assert _names("") == []

    def test_closed_after_space(self):
        assert _names("/model g") == []

    def test_no_match_none(self):
        assert _names("/zzzzz") == []


# ── pty 进程级：真实 prompt_toolkit 会话交互 ─────────────────────

CHILD = r'''
import sys, json, asyncio
sys.path.insert(0, "{root}")
from openx.config import OpenXConfig
from openx.ui.console import Console
console = Console(OpenXConfig())
line = asyncio.run(console.print_user_prompt())
print(json.dumps({{"line": line}}), flush=True)
'''

CPR_QUERY = b"\x1b[6n"
# pyte 解析不了的终端控制序列（CSI 参数含 "=" 等），喂 pyte 前洗掉——
# 它们零视觉影响，残字（如 "1u"）会污染 pyte 镜像。raw 字节流保留原样。
_PYTE_UNPARSEABLE = re.compile(rb"\x1b\[=1u|\x1b\[<u|\x1b\[>4;2m|\x1b\[>4m")


class PtyPrompt:
    """在 pty 里跑真实 print_user_prompt 的驱动：写入按键、pyte 读屏。

    ptk 会向终端发 CPR 查询（\\x1b[6n）——pump 检测到即按 pyte 光标
    位置应答（\\x1b[{row};{col}R）；不应答则 ptk 等满 CPR_TIMEOUT
    （2s）才降级，每个用例平白付 2s。
    """

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

    def _pump_chunk(self, data: bytes) -> None:
        self.raw += data
        # CPR 应答要赶在渲染等待之前：查询单独成段（ptk 前后写别的内
        # 容），逐字节扫描不依赖块边界对齐。
        while CPR_QUERY in data:
            row = self.screen.cursor.y + 1
            col = self.screen.cursor.x + 1
            os.write(self.master, f"\x1b[{row};{col}R".encode())
            data = data.replace(CPR_QUERY, b"", 1)
        self.pyte.feed(self._decoder.decode(_PYTE_UNPARSEABLE.sub(b"", data)))

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
                self._pump_chunk(data)

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

    def wait_gone(self, needle: str, timeout: float = 5.0):
        end = time.time() + timeout
        while time.time() < end:
            if needle not in self.text():
                return
            self.pump(0.1)
        raise AssertionError(
            f"screen still shows {needle!r}:\n{self.text()}")

    def finish(self) -> dict:
        """读子进程最终的 JSON 行（print_user_prompt 的返回值）。

        结果行可能早已被 pump 读走（子进程退出很快）——先扫已累积的
        raw，未命中再继续读 master。
        """
        end = time.time() + 5.0
        while time.time() < end:
            # JSON 行可能前缀清框 ANSI → 正则找子串
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
                self._pump_chunk(data)
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
            p.wait_for("help")               # 补全浮层出现 help 候选
            assert "Show all available commands" in p.text()
            # Tab 补全 → 菜单关闭、输入成 "/help "
            p.type(b"\t")
            p.wait_gone("Show all available commands")
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

    def test_down_down_enter_selects_second(self):
        # 期望项由数据源派生（menu_entries 名单会随命令增删变化，不写死首项）。
        # ptk 原生语义：菜单打开时**不预选**首项（与旧版手绘菜单的差异）——
        # 第一次 ↓ 选中第 0 项，第二次 ↓ 才到第 1 项。
        from openx.app.cli.commands import menu_entries

        names = [name for name, _, _ in menu_entries()]
        p = PtyPrompt()
        try:
            p.wait_for("❯")
            p.type(b"/")                     # 全量菜单，按 menu_entries 顺序
            p.wait_for(names[0])
            p.type(b"\x1b[B")                # ↓ 选中第 0 项
            p.type(b"\x1b[B")                # ↓ 选中第 1 项
            p.type(b"\r")
            assert p.finish()["line"] == "/" + names[1]
        finally:
            p.close()

    def test_esc_closes_menu_keeps_input(self):
        p = PtyPrompt()
        try:
            p.wait_for("❯")
            p.type(b"/he")
            p.wait_for("help")
            # Esc 收菜单：真 pty 有 timeoutlen 定时器（~0.5s）把单独
            # Esc 冲出转义消歧——settle 给足，避免与后续 "l" 配成 meta-l。
            p.type(b"\x1b", settle=0.8)
            p.wait_gone("Show all available commands")
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
            p.type(b" g")                    # 写参数 → 命令名完整，无候选
            p.wait_gone("Switch LLM model")
            p.type(b"pt-4o\r")
            assert p.finish()["line"] == "/model gpt-4o"
        finally:
            p.close()
