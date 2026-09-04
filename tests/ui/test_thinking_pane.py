"""回答结束后 Ctrl+R 就地重印 thinking 的屏幕级测试（prompt._replay_toggle）。

语义（用户需求）：展开内容必须**紧随 ● Thought for Ns 指示行**、不在对话
末尾；指示行带常驻 (ctrl+r to expand) 提示。实现 = 从指示行到屏末整段擦除
+ 重印 [指示行 (+ 思考全文 + 空行) + tail + gap 空行] + fresh 画框。

覆盖：
- 展开：思考全文落在指示行与正文之间（顺序断言），tail/框完好；
- 打字重绘不破坏已展开区（框重绘锚点在顶框线）；
- 收起：回到折叠形态、哨兵对话行不越界被吞；
- 簿记：_replay_block_rows/_replay_expanded 随 toggle 更新；
- 编辑器真按键（_read_line_interactive 的 \x12 分支）接线；
- 无 replay 数据静默忽略；gap=0（cancel 回合）算术正确。

手法沿用 test_prompt_lifecycle.py（真实 Console + pyte 双路归一）。
_setup_turn 直接构造"done 后留屏"形态：哨兵行 → 折叠指示行 → 正文两行
→ 2 空行 → 框 4 行，光标回输入行——_last_replay 手工装配（指示行与
prompt._replay_indicator 折叠版逐字一致）。

运行：``python -m pytest tests/ui/test_thinking_pane.py -q``
"""

from __future__ import annotations

import io
import os
import re
import sys

import pytest
import pyte
import pyte.modes
from rich.console import Console as RichConsole
from rich.text import Text

from openx.config import OpenXConfig
from openx.ui._components.prompt import PromptMixin
from openx.ui._style import DIM, MARK_INFO
from openx.ui.console import Console

THINKING = "the hidden reasoning body"
TAIL1 = "answer line one"
TAIL2 = "answer line two"
PRIOR = "PRIOR_TRANSCRIPT_LINE"

COLS, ROWS = 80, 24


def _collapsed_indicator(elapsed: float = 2.5) -> Text:
    t = Text(
        f"  {MARK_INFO} Thought for {elapsed:.1f}s (ctrl+r to expand)",
        style=DIM,
    )
    t.no_wrap = True
    t.overflow = "ellipsis"
    return t


class ReplayHarness:
    """真实 Console + pyte 屏，手搭"done 后留屏 + 折叠指示行"回合形态。"""

    def __init__(self, monkeypatch, tmp_path, gap: int = 2):
        self.screen = pyte.Screen(COLS, ROWS)
        self.screen.set_mode(pyte.modes.LNM)
        self.pyte = pyte.Stream(self.screen)
        self.buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", self.buf)
        monkeypatch.setattr(
            "openx.ui._components.prompt.get_terminal_size",
            lambda: os.terminal_size((COLS, ROWS)),
        )
        cfg = OpenXConfig(workspace=str(tmp_path))
        self.console = Console(config=cfg)
        self.console._console = RichConsole(
            file=self.buf, width=COLS, height=ROWS, force_terminal=True,
            highlight=False,
        )
        self.console._terminal_width = COLS
        self.console._frame_width = COLS
        self.gap = gap

    def setup_turn(self) -> None:
        """哨兵 → 指示行 → tail → gap 空行 → 框；光标停在输入行。"""
        c = self.console
        c._console.print(PRIOR)
        c._console.print(_collapsed_indicator())
        c._console.print(TAIL1)
        c._console.print(Text(TAIL2))
        for _ in range(self.gap):
            c._console.print(Text(""))
        c._console.print(c._frame_renderable(0, 0))
        c._frame_on_screen = True
        c._input_rows_on_screen = 1
        c._input_cells_on_screen = 2
        sys.stdout.write("\033[3A\033[2K❯ ")  # 复用分支同款到输入行
        c._last_replay = {
            "thinking": (THINKING, 2.5),
            "tail": [Text(TAIL1), Text(TAIL2)],
            "gap": self.gap,
        }
        c._replay_expanded = False
        c._replay_block_rows = 3  # 指示行 + tail 两行（重印标尺含 tail）
        self.flush()

    def flush(self) -> None:
        self.pyte.feed(self.buf.getvalue())
        self.buf.seek(0)
        self.buf.truncate()

    def rows(self) -> list[str]:
        return [
            "".join(c.data for c in self.screen.buffer[y].values())
            for y in range(self.screen.lines)
        ]

    def screen_text(self) -> str:
        return "\n".join(self.rows())

    def y_of(self, needle: str) -> int:
        return next(
            y for y, r in enumerate(self.rows()) if needle in r.rstrip()
        )

    def count_reprinted(self, needle: str) -> int:
        """reprint_bytes 里 needle 的出现次数（重打量 = 重复打印量）。
        (?!\d) 防 "line 1" 误配 "line 10"。"""
        return len(re.findall(re.escape(needle) + r"(?!\d)",
                              getattr(self, "reprint_bytes", "")))


# ── 展开 / 收起 / 簿记 ────────────────────────────────────────────


class TestReplayToggle:
    def test_expand_lands_directly_under_indicator(self, monkeypatch, tmp_path):
        """展开：思考全文紧随指示行、在正文之前（不在对话末尾）。"""
        h = ReplayHarness(monkeypatch, tmp_path)
        h.setup_turn()
        c = h.console
        c._replay_toggle([], None, 0, 0)
        h.flush()
        assert h.y_of("Thought for") == h.y_of(THINKING) - 1
        assert h.y_of(THINKING) < h.y_of(TAIL1) < h.y_of("❯")
        assert "PRIOR" in h.screen_text()
        assert h.y_of(PRIOR) < h.y_of("Thought for")
        assert c._replay_expanded is True
        assert c._replay_block_rows == 5   # 指示 + 全文 + 空行 + tail 两行
        # 指示行提示翻转
        assert "(ctrl+r to collapse)" in h.screen_text()

    def test_collapse_restores_folded_form(self, monkeypatch, tmp_path):
        h = ReplayHarness(monkeypatch, tmp_path)
        h.setup_turn()
        c = h.console
        c._replay_toggle([], None, 0, 0)     # 展开
        c._replay_toggle([], None, 0, 0)     # 收起
        h.flush()
        text = h.screen_text()
        assert THINKING not in text          # 收起：全文撤下
        assert "(ctrl+r to expand)" in text  # 提示复原
        # 折叠形态与初始逐行等价：指示行 → 正文 → 框
        assert h.y_of("Thought for") == h.y_of(PRIOR) + 1
        assert h.y_of(TAIL1) == h.y_of("Thought for") + 1
        assert c._replay_expanded is False
        assert c._replay_block_rows == 3

    def test_collapse_never_eats_above_transcript(self, monkeypatch, tmp_path):
        """收起擦除从指示行起算——框上方既有对话（哨兵行）永不越界被吞。"""
        h = ReplayHarness(monkeypatch, tmp_path)
        h.setup_turn()
        c = h.console
        c._replay_toggle([], None, 0, 0)
        h.flush()
        assert PRIOR in h.screen_text()
        c._replay_toggle([], None, 0, 0)
        h.flush()
        assert PRIOR in h.screen_text()
        assert TAIL2 in h.screen_text()

    def test_toggle_with_typed_buffer_keeps_input(self, monkeypatch, tmp_path):
        """框内已有输入 + 光标中途：toggle 后输入原样恢复。"""
        h = ReplayHarness(monkeypatch, tmp_path)
        h.setup_turn()
        c = h.console
        buf = list("hello world")
        c._replay_toggle(buf, None, 0, 3)    # 光标在 "hel|lo"
        h.flush()
        assert THINKING in h.screen_text()
        assert "❯ hello world" in h.screen_text()

    def test_typing_after_expand_keeps_block(self, monkeypatch, tmp_path):
        """展开后打字：普通框重绘（锚点顶框线）不碰上方已展开思考块。"""
        h = ReplayHarness(monkeypatch, tmp_path)
        h.setup_turn()
        c = h.console
        c._replay_toggle([], None, 0, 0)
        c._redraw_frame(list("hi"), None, 0, 2)
        h.flush()
        text = h.screen_text()
        assert THINKING in text
        assert "❯ hi" in text
        assert h.y_of(THINKING) < h.y_of("❯ hi")

    def test_gap0_turn_cancel_paths_arithmetic(self, monkeypatch, tmp_path):
        """cancel 回合（gap=0、正文紧贴框）：展开/收起算术同样正确。"""
        h = ReplayHarness(monkeypatch, tmp_path, gap=0)
        h.setup_turn()
        c = h.console
        c._replay_toggle([], None, 0, 0)
        h.flush()
        assert h.y_of("Thought for") == h.y_of(THINKING) - 1
        c._replay_toggle([], None, 0, 0)
        h.flush()
        assert THINKING not in h.screen_text()
        assert PRIOR in h.screen_text()
        assert h.y_of("Thought for") == h.y_of(TAIL1) - 1


# ── 编辑器真按键接线（_read_line_interactive 的 Ctrl+R 分支）──────


def _drive_editor(monkeypatch, keys: list[str]) -> None:
    """脚本化喂键跑一遍 _read_line_interactive（termios/fd 全桩）。"""
    import termios as _termios
    import tty as _tty

    import openx.ui._components.prompt as prompt_mod
    monkeypatch.setattr(_termios, "tcgetattr", lambda fd: None)
    monkeypatch.setattr(_termios, "tcsetattr", lambda *a: None)
    monkeypatch.setattr(_tty, "setcbreak", lambda *a: None)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    it = iter(keys)
    monkeypatch.setattr(prompt_mod, "read_unicode_char", lambda fd: next(it))


class TestCtrlRKeyWiring:
    def test_ctrl_r_expands_via_editor_loop(self, monkeypatch, tmp_path):
        h = ReplayHarness(monkeypatch, tmp_path)
        h.setup_turn()
        c = h.console
        _drive_editor(monkeypatch, ["\x12", "\x04"])  # Ctrl+R → Ctrl-D(空)
        assert c._read_line_interactive() == ""
        h.flush()
        assert THINKING in h.screen_text()
        assert c._replay_expanded is True

    def test_ctrl_r_twice_collapses_back(self, monkeypatch, tmp_path):
        h = ReplayHarness(monkeypatch, tmp_path)
        h.setup_turn()
        c = h.console
        _drive_editor(monkeypatch, ["\x12", "\x12", "\x04"])
        assert c._read_line_interactive() == ""
        h.flush()
        text = h.screen_text()
        assert THINKING not in text            # 收起回到折叠态
        assert PRIOR in text                   # 越界未吞对话
        assert c._replay_expanded is False
        assert c._replay_block_rows == 3


# ── 无数据静默纪律 ────────────────────────────────────────────────


class TestNoReplayData:
    def test_ctrl_r_silent_without_thinking(self, monkeypatch, tmp_path):
        """_last_replay=None → Ctrl+R 静默忽略（同流式期无推理纪律）。"""
        h = ReplayHarness(monkeypatch, tmp_path)
        h.setup_turn()
        c = h.console
        c._last_replay = None
        _drive_editor(monkeypatch, ["\x12", "\x04"])
        assert c._read_line_interactive() == ""
        h.flush()
        assert THINKING not in h.screen_text()
        assert c._replay_expanded is False
        assert "(ctrl+r" in h.screen_text()  # 折叠指示行仍在原位


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call(
        [sys.executable, "-m", "pytest", __file__, "-q"]))


# ── 块超一屏（指示行滚出屏顶）：窗口重印纪律 ─────────────────────
# 用户报告：上下文过多时 Ctrl+R 打开/关闭不了；且输出前半部分重复出现、
# 多轮之前的对话看不到。根因 = 整块重印的上移被终端钳在行 0：擦除区吞上方
# 对话、整块重印把已滚入 scrollback 的行二次追加。窗口路径只擦本回合可见
# 区、绝不重打 scrollback 行；窗口闩锁定本回合此后恒走窗口路径。


N_LONG_TAIL = 40  # ≫ 24 行屏 → 指示行必滚出屏顶


class ReplayLongHarness(ReplayHarness):
    """40 行正文的"done 后留屏"形态：头部（哨兵+指示行+前段正文）已在
    scrollback，屏上只有正文尾部 + 间距 + 框。"""

    LONG_PREFIX = "long answer line "

    def setup_turn(self) -> None:
        c = self.console
        c._console.print(PRIOR)
        c._console.print(_collapsed_indicator())
        self.long_tail = [
            Text(f"{self.LONG_PREFIX}{i}") for i in range(1, N_LONG_TAIL + 1)
        ]
        for t in self.long_tail:
            c._console.print(t)
        for _ in range(self.gap):
            c._console.print(Text(""))
        c._console.print(c._frame_renderable(0, 0))
        c._frame_on_screen = True
        c._input_rows_on_screen = 1
        c._input_cells_on_screen = 2
        sys.stdout.write("\033[3A\033[2K❯ ")
        c._last_replay = {
            "thinking": (THINKING, 2.5),
            "tail": list(self.long_tail),
            "gap": self.gap,
        }
        c._replay_expanded = False
        c._replay_block_rows = N_LONG_TAIL + 1
        self.flush()


class TestReplayToggleOversized:
    def _expand_collapse_cycle(self, h) -> None:
        c = h.console
        c._replay_toggle([], None, 0, 0)     # 展开
        h.flush()
        c._replay_toggle([], None, 0, 0)     # 收起
        h.flush()

    def test_expand_shows_thinking_window(self, monkeypatch, tmp_path):
        """超屏展开：指示行回屏 + 思考全文可见（短思考全量），正文尾部
        保留窗口行——切换有可见效果。"""
        h = ReplayLongHarness(monkeypatch, tmp_path)
        h.setup_turn()
        h.reprint_bytes = ""
        h.console._replay_toggle([], None, 0, 0)
        h.reprint_bytes = h.buf.getvalue()
        h.flush()
        text = h.screen_text()
        assert "Thought for" in text, "指示行必须回到屏上"
        assert "(ctrl+r to collapse)" in text
        assert THINKING in text, "思考内容必须可见（展开的焦点）"
        assert f"{h.LONG_PREFIX}{N_LONG_TAIL}" in text, "正文尾行保留"
        assert h.console._replay_windowed is True

    def test_collapse_restores_tail_window(self, monkeypatch, tmp_path):
        """超屏收起：思考撤下、正文尾窗还原、指示行提示复原。"""
        h = ReplayLongHarness(monkeypatch, tmp_path)
        h.setup_turn()
        c = h.console
        c._replay_toggle([], None, 0, 0)
        h.flush()
        c._replay_toggle([], None, 0, 0)
        h.flush()
        text = h.screen_text()
        assert THINKING not in text
        assert "(ctrl+r to expand)" in text
        assert f"{h.LONG_PREFIX}{N_LONG_TAIL}" in text
        assert f"{h.LONG_PREFIX}1 " not in text  # 头部不在窗口内

    def test_no_scrollback_reprint_in_cycle(self, monkeypatch, tmp_path):
        """3 次 toggle 全程：已滚入 scrollback 的头部行与上方哨兵对话
        零重打（"前半部分重复 + 历史对话丢失"的判据）。"""
        h = ReplayLongHarness(monkeypatch, tmp_path)
        h.setup_turn()
        c = h.console
        total = ""
        for _ in range(3):
            c._replay_toggle([], None, 0, 0)
            total += h.buf.getvalue()
            h.flush()
        h.reprint_bytes = total
        # 头部行（1..20 已在 scrollback）与哨兵绝不重打
        for i in (1, 5, 10, 20):
            assert h.count_reprinted(f"{h.LONG_PREFIX}{i}") == 0, (
                f"头部行 {i} 被二次打印进 scrollback")
        assert h.count_reprinted(PRIOR) == 0, "上方对话被重打/吞没"
        # 可见尾行允许经窗口重印，但必须有界（≤ 每次窗口一份）
        assert h.count_reprinted(
            f"{h.LONG_PREFIX}{N_LONG_TAIL}") <= 3

    def test_window_latch_persists_within_turn(self, monkeypatch, tmp_path):
        """窗口闩：一旦走过窗口路径，后续即使簿记行数变小也绝不回到
        整块重印（整块重印会二次打印 scrollback 行）。"""
        h = ReplayLongHarness(monkeypatch, tmp_path)
        h.setup_turn()
        c = h.console
        c._replay_toggle([], None, 0, 0)     # → 窗口路径、闩置位
        h.flush()
        assert c._replay_windowed is True
        assert c._replay_block_rows <= 24, "簿记应为窗口行数（非全量）"
        # 闩在 → 即便 up 算得 ≤ R 也必须走窗口：再 toggle 不重打头部
        total = ""
        for _ in range(2):
            c._replay_toggle([], None, 0, 0)
            total += h.buf.getvalue()
            h.flush()
        h.reprint_bytes = total
        assert h.count_reprinted(f"{h.LONG_PREFIX}1") == 0


class TestReplayToggleMediumBlockLongThinking:
    """中等正文（折叠块放得下）+ 长思考：展开走整块路径（块全在屏，
    滚动首次入 scrollback 无重复），收起时块已超屏 → 自动落窗口路径，
    全程头部零重打。"""

    def test_expand_scrolls_then_collapse_windows(self, monkeypatch, tmp_path):
        h = ReplayHarness(monkeypatch, tmp_path)
        c = h.console
        # 10 行正文（折叠块 11 行 ≤ 预算 → 整块路径可用）
        mid_tail = [Text(f"mid answer line {i}") for i in range(1, 11)]
        c._console.print(PRIOR)
        c._console.print(_collapsed_indicator())
        for t in mid_tail:
            c._console.print(t)
        for _ in range(h.gap):
            c._console.print(Text(""))
        c._console.print(c._frame_renderable(0, 0))
        c._frame_on_screen = True
        sys.stdout.write("\033[3A\033[2K❯ ")
        c._last_replay = {
            "thinking": (("long thought\n" * 30).strip(), 2.5),
            "tail": list(mid_tail),
            "gap": h.gap,
        }
        c._replay_expanded = False
        c._replay_block_rows = len(mid_tail) + 1
        h.flush()

        c._replay_toggle([], None, 0, 0)     # 展开（整块，滚动过屏）
        h.flush()
        assert c._replay_expanded is True
        c._replay_toggle([], None, 0, 0)     # 收起 → 窗口路径
        h.flush()
        text = h.screen_text()
        assert "(ctrl+r to expand)" in text
        assert "mid answer line 10" in text  # 尾行可见
        # 整轮 toggle 里，首行正文只应被整块展开重打过一次（它当时还
        # 在屏上、不在 scrollback）——绝无第二次。
        h.reprint_bytes = ""
        c._replay_toggle([], None, 0, 0)     # 再展开（窗口）
        total = h.buf.getvalue()
        h.flush()
        h.reprint_bytes = total
        assert h.count_reprinted("mid answer line 1") == 0
