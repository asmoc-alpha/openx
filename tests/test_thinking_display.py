"""thinking（模型推理内容）展示层测试 —— pyte 屏幕级验证。

覆盖：
- reasoning 默认折叠：推理文本不上屏，仅一行静态指示行（``○ Thinking…``）；
- Ctrl+R 热键（InputCapture 队列，_build_renderable 内消费）展开/折叠；
- 推理阶段结束（首个正文 chunk）→ 指示行变 ``● Thought for Ns``；
- done() 后按当时状态定格进 transcript（折叠留指示行、展开留全文）；
- 无 reasoning 的回合零指示行（旧行为逐字节保持）；
- 指示行**静态性**：推理中两次无 feed 刷新，帧 diff 仅 spinner 行变化
  （指示行若带滴答计时会每 5Hz 失效 _response_view 缓存、复活闪烁病）。

Harness 手法沿用 test_terminal_interaction.py（pyte LNM + deterministic_live）。

运行：``python -m pytest tests/test_thinking_display.py -q``
"""

from __future__ import annotations

import io
import time
from types import SimpleNamespace

import pytest
import pyte
import pyte.modes
from rich.console import Console as RichConsole
from rich.text import Text

from openx.llm import StreamReasoning
from openx.services.streaming import StreamingService


# ── 测试基建 ──────────────────────────────────────────────────────


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

    def refresh(self) -> None:
        """手动驱动一次 Live 刷新并落屏（替代自动刷新线程）。"""
        self.svc._live.refresh()
        self.flush()

    def rows(self) -> list[str]:
        return [
            "".join(c.data for c in self.screen.buffer[y].values())
            for y in range(self.screen.lines)
        ]

    def nonempty(self) -> list[tuple[int, str]]:
        return [(y, r.rstrip()) for y, r in enumerate(self.rows()) if r.strip()]

    def screen_text(self) -> str:
        return "\n".join(self.rows())

    def press(self, key: str) -> None:
        """模拟流式热键（Ctrl-O/Ctrl-R 同款注入路径）。"""
        self.svc._capture._hotkeys.append(key)

    @staticmethod
    def diff_rows(before: list[str], after: list[str]) -> list[int]:
        """变化行号（rstrip 后比较：\x1b[2K 把 pyte"未写"变"擦过"的空格差异无视）。"""
        return [
            i for i, (b, a) in enumerate(zip(before, after))
            if b.rstrip() != a.rstrip()
        ]


# ── 折叠 / 展开 / 定格 ────────────────────────────────────────────


class TestThinkingDisplay:
    def test_reasoning_collapsed_by_default(self, deterministic_live):
        h = Harness()
        h.svc.start()
        h.svc.feed(StreamReasoning("let me think carefully "))
        h.svc.feed(StreamReasoning("about this problem"))
        h.refresh()

        text = h.screen_text()
        # 指示行在屏、热键提示在屏
        assert "Thinking…" in text
        assert "ctrl+r to expand" in text
        # 推理正文**不**上屏
        assert "carefully" not in text
        assert "problem" not in text

    def test_ctrl_r_expands_and_collapses(self, deterministic_live):
        h = Harness()
        h.svc.start()
        h.svc.feed(StreamReasoning("hidden reasoning body"))
        h.refresh()
        assert "hidden reasoning body" not in h.screen_text()

        h.press("\x12")  # Ctrl-R → 展开
        h.refresh()
        assert "hidden reasoning body" in h.screen_text()
        assert "ctrl+r to collapse" in h.screen_text()

        h.press("\x12")  # 再按 → 折叠
        h.refresh()
        assert "hidden reasoning body" not in h.screen_text()
        assert "ctrl+r to expand" in h.screen_text()

    def test_ctrl_r_noop_without_reasoning(self, deterministic_live):
        h = Harness()
        h.svc.start()
        h.svc.feed("plain answer")
        h.press("\x12")  # 无推理内容 → 静默忽略
        h.refresh()
        text = h.screen_text()
        assert "plain answer" in text
        assert "ctrl+r" not in text  # 无指示行

    def test_indicator_switches_after_content_arrives(self, deterministic_live):
        h = Harness()
        h.svc.start()
        h.svc.feed(StreamReasoning("thinking…"))
        h.refresh()
        assert "Thinking…" in h.screen_text()

        h.svc.feed("the answer")  # 首个正文 chunk → 冻结推理阶段
        h.refresh()
        text = h.screen_text()
        assert "Thought for" in text          # ○ → ● 冻结指示
        assert "Thinking…" not in text
        assert "the answer" in text

    def test_done_freezes_collapsed_state(self, deterministic_live):
        h = Harness()
        h.svc.start()
        h.svc.feed(StreamReasoning("secret plans"))
        h.svc.feed("final answer")
        h.svc.done()
        h.flush()

        text = h.screen_text()
        assert "final answer" in text
        assert "Thought for" in text          # 折叠指示留屏
        assert "ctrl+r" not in text           # done 后无热键提示
        assert "secret plans" not in text     # 折叠态：正文不上屏

    def test_done_freezes_expanded_state(self, deterministic_live):
        h = Harness()
        h.svc.start()
        h.svc.feed(StreamReasoning("secret plans"))
        h.press("\x12")                       # 展开
        h.refresh()
        h.svc.feed("final answer")
        h.svc.done()
        h.flush()

        text = h.screen_text()
        assert "final answer" in text
        assert "secret plans" in text         # 展开态：全文留屏（transcript）
        assert "ctrl+r" not in text

    def test_reasoning_only_turn_done_latches(self, deterministic_live):
        """纯 thinking 回合（无正文）：done() 兜底冻结 → Thought for。"""
        h = Harness()
        h.svc.start()
        h.svc.feed(StreamReasoning("only thinking, no answer"))
        h.svc.done()
        h.flush()
        assert "Thought for" in h.screen_text()

    def test_indicator_above_answer(self, deterministic_live):
        """指示行渲染在正文之上（thinking 先于 answer 的阅读序）。"""
        h = Harness()
        h.svc.start()
        h.svc.feed(StreamReasoning("cogitation"))
        h.svc.feed("the reply")
        h.refresh()
        ne = h.nonempty()
        y_indicator = next(y for y, t in ne if "Thought for" in t)
        y_answer = next(y for y, t in ne if "the reply" in t)
        assert y_indicator < y_answer

    def test_start_resets_thinking_state(self, deterministic_live):
        """每轮 start() 归零 thinking 状态（不跨轮延续）。"""
        h = Harness()
        h.svc.start()
        h.svc.feed(StreamReasoning("round one thoughts"))
        h.svc.done()
        h.flush()
        h.screen.reset()  # 清屏模拟 REPL 翻页——本测只关心新一轮渲染

        h.svc.start()  # 新一轮
        h.svc.feed("round two answer")
        h.refresh()
        text = h.screen_text()
        assert "round one thoughts" not in text
        assert "Thought for" not in text      # 新一轮无推理 → 零指示行
        assert "round two answer" in text
        assert h.svc._reasoning_expanded is False
        assert h.svc._reasoning_buffer == ""


# ── 静态指示行与闪烁回归 ──────────────────────────────────────────


class TestThinkingFlicker:
    def test_indicator_static_across_ticks(self, deterministic_live):
        """推理进行中两次无 feed 刷新：仅 spinner 行变化（指示行无滴答计时）。"""
        h = Harness()
        h.svc.start()
        h.svc.feed(StreamReasoning("long running reasoning"))
        h.refresh()
        before = h.rows()

        time.sleep(0.1)  # 跨过 spinner 的 80ms 字形帧 + 0.1s 计时刻度
        h.refresh()  # 5Hz 下一拍，无新内容
        after = h.rows()

        changed = h.diff_rows(before, after)
        # 至多 spinner 行变化（计时/字形）；指示行与其余全屏必须恒定。
        # 以 "esc to interrupt"（spinner 行专属后缀）识别 spinner——
        # 推理中 spinner 标签同为 "Thinking…"，不能按该词判别。
        assert changed, "跨过 spinner 帧后应恰有 spinner 行变化"
        for y in changed:
            assert "esc to interrupt" in after[y], (
                f"行 {y} 帧间变化却不是 spinner → 缓存失效源：{after[y]!r}"
            )

    def test_expanded_thinking_group_height_constant_when_latched(
        self, deterministic_live
    ):
        """展开超长 thinking 触发 _long_mode 后：组高恒定（锚点不变量）。"""
        h = Harness(rows=24, cols=80)
        h.svc.start()
        h.svc.feed(StreamReasoning("line\n" * 60))  # 远超一屏
        h.press("\x12")  # 展开
        h.refresh()
        frame_y_1 = max(y for y, t in h.nonempty() if "FRAME" in t)

        h.svc.feed(StreamReasoning("more\n" * 5))   # 继续增长
        h.refresh()
        frame_y_2 = max(y for y, t in h.nonempty() if "FRAME" in t)

        assert frame_y_1 == frame_y_2 <= 23  # 锚定且永不超屏


# ── 客户端事件直通（agent 透传无需改，此处钉住契约）────────────────


class TestEventContract:
    def test_stream_reasoning_is_dataclass_with_text(self):
        e = StreamReasoning("abc")
        assert e.text == "abc"

    def test_feed_accumulates_reasoning_buffer_not_main(self, deterministic_live):
        h = Harness()
        h.svc.start()
        h.svc.feed(StreamReasoning("part1 "))
        h.svc.feed(StreamReasoning("part2"))
        assert h.svc._reasoning_buffer == "part1 part2"
        assert h.svc._segments == []          # 绝不混入主转录段

    def test_feed_latches_reasoning_done_on_first_content(self, deterministic_live):
        h = Harness()
        h.svc.start()
        h.svc.feed(StreamReasoning("x"))
        assert h.svc._reasoning_done is False
        h.svc.feed("answer")
        assert h.svc._reasoning_done is True
        assert h.svc._thinking_elapsed > 0
