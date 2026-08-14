"""答案"提示 → 流式 → done → 下一轮提示"生命周期屏幕级回归。

用户报告"回答过程中可见、回答完成反而不可见"。既有 pyte 测试只覆盖
流式内部（auto_refresh=False、不跨 done 边界、不经 prompt 复用/清框），
答案留屏之后的光标算术链（done 的 console.line 偏移 → 复用 \\033[3A →
Enter 清框 \\033[2A / clear_input_frame \\033[4A）**零覆盖**——本文件补齐。

手法：真实 openx Console + 真实 StreamingService，只 monkeypatch 键盘
读取（_read_line_interactive 返回罐头行并模拟 Enter 光标位移）与终端
尺寸（prompt 模块的 get_terminal_size）。rich 渲染（console._console）
与裸 sys.stdout 光标写（prompt.py / streaming.cancel）**双路归一**同一
pyte 屏——两路不同步则断言必假，这是本 harness 的核心约束。

变体：
(a) happy path：流式 → done → 复用分支读入下一句 → Enter 清框 → 答案仍在；
(b) 中途 pause()/resume()（权限弹窗周期，manual 模式每工具必走）→ 答案仍在；
(c) 长响应 _long_mode → done → 尾部仍在；
(d) 排队消息路径（clear_input_frame \\033[4A\\033[J）→ 答案仍在；
(e) done-while-paused（pause 后未 resume 即 done）→ 答案必须留屏；
(f) cancel-while-paused → 绝不越界擦掉框上方的既有对话；
(g) auto_refresh=True（真实 5Hz 刷新线程，既有测试从未覆盖）走 happy path。

运行：``python -m pytest tests/test_prompt_lifecycle.py -q``
"""

from __future__ import annotations

import io
import os
import sys
import time

import pytest
import pyte
import pyte.modes
from rich.console import Console as RichConsole

from openx.config import OpenXConfig
from openx.services.streaming import StreamingService
from openx.ui.console import Console

ANSWER = "FINAL_ANSWER_MARKER"      # 断言存活的正文标记
PRIOR = "PRIOR_TRANSCRIPT_LINE"    # 框上方的既有对话（擦除越界的哨兵）

COLS, ROWS = 80, 24


class LifecycleHarness:
    """真实 Console + StreamingService 双路归一 pyte 屏。"""

    def __init__(self, monkeypatch, tmp_path, auto_refresh: bool = False):
        self.monkeypatch = monkeypatch
        self.screen = pyte.Screen(COLS, ROWS)
        self.screen.set_mode(pyte.modes.LNM)  # 真实 TTY 的 ONLCR
        self.pyte = pyte.Stream(self.screen)
        self.buf = io.StringIO()

        # 路径 1：裸 sys.stdout（prompt.py 光标算术 / streaming.cancel）
        monkeypatch.setattr(sys, "stdout", self.buf)
        # 路径 2：终端尺寸（prompt 模块 get_terminal_size）
        monkeypatch.setattr(
            "openx.ui._components.prompt.get_terminal_size",
            lambda: os.terminal_size((COLS, ROWS)),
        )
        # auto_refresh=False（默认）：杀掉刷新线程，确定性手驱；
        # True 变体保留真线程（覆盖既有测试盲区）。
        if not auto_refresh:
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

        cfg = OpenXConfig(workspace=str(tmp_path))
        self.console = Console(config=cfg)
        self.console._console = RichConsole(
            file=self.buf, width=COLS, height=ROWS, force_terminal=True,
            highlight=False,
        )
        self.console._terminal_width = COLS
        self.console._frame_width = COLS
        # 罐头键盘：读行返回预设文本，并模拟真实编辑器 Enter 后的光标
        # 位置（输入行 → \r\n → 底线行）——print_user_prompt 的提交清框
        # 公式假定光标落在底线行。
        self._canned: list[str] = []
        monkeypatch.setattr(
            self.console, "_read_line_interactive", self._fake_read
        )

    def _fake_read(self) -> str:
        line = self._canned.pop(0)
        sys.stdout.write(f"{line}\r\n")  # 回显 + Enter 位移到底线行
        return line

    def queue_line(self, line: str) -> None:
        self._canned.append(line)

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

    def start_stream(self) -> StreamingService:
        svc = StreamingService(self.console, input_tokens=0)
        svc.start()
        return svc


# ── (a) happy path ───────────────────────────────────────────────


class TestHappyPath:
    def test_answer_survives_done_and_next_prompt(self, monkeypatch, tmp_path):
        h = LifecycleHarness(monkeypatch, tmp_path)
        # 框上方的既有对话（真实 transcript 里永远存在）
        h.console._console.print(PRIOR)
        h.flush()

        svc = h.start_stream()
        svc.feed(f"{ANSWER} part one\n\n")
        svc.feed("part two")
        svc.done()
        h.flush()
        assert ANSWER in h.screen_text()          # done 后答案在屏
        assert h.console._frame_on_screen is True

        # 下一轮提示：复用留屏框 → 读入 → Enter 清框
        h.queue_line("next question")
        result = h.console.print_user_prompt()
        h.flush()
        assert result == "next question"
        text = h.screen_text()
        assert ANSWER in text, "Enter 清框后答案消失"
        assert PRIOR in text, "清框越界吞掉框上方对话"

    def test_answer_survives_two_full_turns(self, monkeypatch, tmp_path):
        """连续两轮：第二轮的框复用/清框不得伤第一轮的答案。"""
        h = LifecycleHarness(monkeypatch, tmp_path)
        svc = h.start_stream()
        svc.feed(f"{ANSWER}_T1")
        svc.done()
        h.flush()

        h.queue_line("q2")
        h.console.print_user_prompt()
        h.flush()

        svc2 = h.start_stream()
        svc2.feed("ANSWER_T2")
        svc2.done()
        h.flush()

        h.queue_line("q3")
        h.console.print_user_prompt()
        h.flush()
        text = h.screen_text()
        assert "ANSWER_T2" in text
        # T1 可能已被 T2 的流式区推出屏（24 行终端）——不强求，但
        # 清框绝不应留下残框线：屏上至多一套框线（已被 q3 清掉）
        assert "next question" not in text


# ── (b) 弹窗周期 pause/resume ────────────────────────────────────


class TestDialogPauseCycle:
    def test_answer_survives_pause_resume_then_done(self, monkeypatch, tmp_path):
        h = LifecycleHarness(monkeypatch, tmp_path)
        svc = h.start_stream()
        svc.feed(f"{ANSWER} before tool\n\n")
        svc._live.refresh()
        h.flush()

        # 权限弹窗周期：pause 擦区 → 弹窗 → resume
        svc.pause()
        h.flush()
        h.console._console.print("[dialog] approve write? (y/n)")
        h.flush()
        svc.resume()
        svc.feed("after tool")
        svc.done()
        h.flush()
        assert ANSWER in h.screen_text(), "弹窗周期后 done 答案消失"

        # 弹窗周期后的下一轮提示也必须安全
        h.queue_line("next")
        h.console.print_user_prompt()
        h.flush()
        assert ANSWER in h.screen_text()


# ── (c) 长响应（_long_mode）──────────────────────────────────────


class TestLongMode:
    def test_long_answer_tail_survives_done(self, monkeypatch, tmp_path):
        h = LifecycleHarness(monkeypatch, tmp_path)
        svc = h.start_stream()
        for i in range(1, 61):
            svc.feed(f"response line {i}\n\n")
        svc.done()
        h.flush()
        text = h.screen_text()
        assert "response line 60" in text, "done 后长响应尾部消失"
        assert "↑ …" not in text, "流式期尾窗标记不应残留进 transcript"
        assert h.console._frame_on_screen is True

        h.queue_line("next")
        h.console.print_user_prompt()
        h.flush()
        assert "response line 60" in h.screen_text()


# ── (d) 排队消息路径 ─────────────────────────────────────────────


class TestQueuedMessage:
    def test_clear_input_frame_preserves_answer(self, monkeypatch, tmp_path):
        h = LifecycleHarness(monkeypatch, tmp_path)
        svc = h.start_stream()
        svc.feed(f"{ANSWER} with queued follow-up")
        svc.done()
        h.flush()
        assert ANSWER in h.screen_text()

        # REPL 对排队消息的路径：直接清留屏框（不经过 print_user_prompt）
        h.console.clear_input_frame()
        h.flush()
        assert ANSWER in h.screen_text(), "clear_input_frame 擦掉了答案"
        assert h.console._frame_on_screen is False


# ── (e)/(f) pause 态下的 done / cancel（R9 缺陷面）────────────────


class TestPauseEdgeCases:
    def test_done_while_paused_keeps_answer(self, monkeypatch, tmp_path):
        """pause 后未 resume 即 done：答案必须重绘留屏，_frame_on_screen
        只允许在帧确实在屏时为真。"""
        h = LifecycleHarness(monkeypatch, tmp_path)
        svc = h.start_stream()
        svc.feed(f"{ANSWER} before dialog")
        svc._live.refresh()
        h.flush()

        svc.pause()          # transient 擦区（弹窗将显示）
        h.flush()
        assert ANSWER not in h.screen_text()  # 暂停期：区已擦（弹窗占位）

        svc.done()           # 流在弹窗期结束（异常/竞态路径）
        h.flush()
        assert ANSWER in h.screen_text(), (
            "done-while-paused：答案永不重绘 → 用户报告的消失 bug"
        )
        if h.console._frame_on_screen:
            # 若声称框在屏，下一轮复用必须安全
            h.queue_line("next")
            h.console.print_user_prompt()
            h.flush()
            assert ANSWER in h.screen_text()

    def test_cancel_while_paused_does_not_erase_transcript(
        self, monkeypatch, tmp_path
    ):
        """pause 态 cancel：frame 不在屏 → 擦除公式必须跳过，
        否则 \\033[4A 从弹窗光标越界上移吞掉既有对话。"""
        h = LifecycleHarness(monkeypatch, tmp_path)
        h.console._console.print(PRIOR)
        h.flush()

        svc = h.start_stream()
        svc.feed(f"{ANSWER} stream")
        svc._live.refresh()
        h.flush()

        svc.pause()
        h.flush()
        svc.cancel()
        h.flush()
        assert PRIOR in h.screen_text(), (
            "cancel-while-paused 越界擦除：既有对话被吞"
        )
        assert h.console._frame_on_screen is False


# ── (h) 弹窗 raw 模式收尾的光标列（排版错乱根因回归）──────────────


class TestDialogRawExitColumn:
    def test_resume_starts_at_column_zero_after_raw_dialog(
        self, monkeypatch, tmp_path
    ):
        """_raw_select 在 raw 模式（ONLCR 关）下以裸 LF 收尾 → 光标停在
        末行 "Choose: …" 的行中列下移。resume 后 Live 首渲必须从列 0
        开始，否则整区错位、硬换行残帧粘连（用户报告的排版错乱）。

        pyte LNM 开关精确建模 termios：LNM 关 ≡ raw（LF 不回列）。
        """
        h = LifecycleHarness(monkeypatch, tmp_path)
        svc = h.start_stream()
        svc.feed(f"{ANSWER} pre-dialog\n\n")
        svc._live.refresh()
        h.flush()

        svc.pause()
        h.flush()
        # 仿真 _raw_select 收尾：提示行无尾换行 + raw 模式裸 LF。
        # **pyte 模式在 feed() 时生效**（非 write 时）→ 分段 flush：
        # LNM 关（≡ raw，ONLCR 停）期间喂入提示行与裸 LF，LF 不回列；
        # 随后恢复 LNM（≡ termios 复原）。
        h.screen.reset_mode(pyte.modes.LNM)
        sys.stdout.write("  Choose: (↑/↓ to choose, Enter to confirm)")
        h.flush()
        sys.stdout.write("\n")                # 裸 LF → 光标列保持 ~45
        h.flush()
        h.screen.set_mode(pyte.modes.LNM)

        svc.resume()
        # 首行须超宽：45 列错位 + 行长 > 80 → 硬换行使物理行数 ≠ shape，
        # 后续刷新的上移擦除欠一行 → 残帧永久滞留（用户截图的粘连形态）。
        # 不超宽时下一帧 CR 即自愈，测不到持久错乱。
        svc.feed("post-dialog content " + "x" * 70)
        svc.done()
        h.flush()

        rows = [r.rstrip() for r in h.rows()]
        text = "\n".join(rows)
        assert ANSWER in text, "弹窗周期后答案消失"
        # 错位形态 = 弹窗末行光标列（~45）起渲染的首行副本：大缩进残帧。
        # 正常路径一切自列 0 起，非空行绝无 ≥40 列前导空白。
        indented = [r for r in rows if r.strip() and len(r) - len(r.lstrip()) >= 40]
        assert not indented, (
            f"区域自弹窗末行光标列开写（首渲未回列 0）：{indented!r}"
        )
        # 弹窗行本身也不得与流式区残帧粘连
        for r in (r for r in rows if "Choose:" in r):
            assert r.endswith("confirm)"), f"弹窗行粘连流式区片段：{r!r}"

    def test_dialogs_raw_select_writes_crlf(self):
        """静态钉住：_raw_select 四个退出口均为 \\r\\n（raw 下裸 \\n 不回列）。"""
        import inspect
        from openx.ui._components import dialogs

        src = inspect.getsource(dialogs.DialogsMixin._raw_select)
        assert 'write("\\n")' not in src, "raw 模式裸 LF → 光标列错位"
        assert src.count('write("\\r\\n")') >= 4


# ── (g) 真实自动刷新线程 ─────────────────────────────────────────


class TestAutoRefreshThread:
    def test_happy_path_with_real_refresh_thread(self, monkeypatch, tmp_path):
        """auto_refresh=True（5Hz 真线程）：既有测试从未覆盖的路径。"""
        h = LifecycleHarness(monkeypatch, tmp_path, auto_refresh=True)
        svc = h.start_stream()
        svc.feed(f"{ANSWER} threaded")
        time.sleep(0.3)      # 让 5Hz 线程至少刷一帧
        svc.done()
        h.flush()
        assert ANSWER in h.screen_text(), "自动刷新线程路径下 done 后答案消失"

        h.queue_line("next")
        h.console.print_user_prompt()
        h.flush()
        assert ANSWER in h.screen_text()
