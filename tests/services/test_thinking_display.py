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
        assert "(ctrl+r to expand)" in text   # 常驻提示（done 后重印兑现）
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
        assert "(ctrl+r to collapse)" in text  # 展开定格 → 收起提示常驻

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

    def test_done_saves_replay_state_on_console(self, deterministic_live):
        """done() 落重印状态到 console._last_replay（回答结束后 idle
        Ctrl+R 就地展开的数据源）：thinking 对 + 指示行之外的全部 body 行。"""
        h = Harness()
        h.svc.start()
        h.svc.feed(StreamReasoning("deep thought"))
        h.svc.feed("answer body")
        h.svc.done()
        replay = h.svc._console._last_replay
        text, elapsed = replay["thinking"]
        assert text == "deep thought"
        assert elapsed > 0
        tail_plain = " ".join(t.plain for t in replay["tail"])
        assert "answer body" in tail_plain   # tail 可独立重印
        assert replay["gap"] == 1              # = _BODY_FRAME_GAP（对标 Claude Code）
        assert h.svc._console._replay_expanded is False
        # 折叠态块行数 = 指示行 + 块间空行 + 正文行（tail 计入标尺）
        assert h.svc._console._replay_block_rows == len(replay["tail"]) + 1

    def test_cancel_saves_replay_state(self, deterministic_live):
        """被打断的回合：已流入的部分思考同样可重印（无 done 尾距）。"""
        h = Harness()
        h.svc.start()
        h.svc.feed(StreamReasoning("partial cogitation"))
        h.svc.cancel()
        replay = h.svc._console._last_replay
        text, _ = replay["thinking"]
        assert text == "partial cogitation"
        assert replay["gap"] == 0

    def test_start_clears_replay_state(self, deterministic_live):
        """每轮 start() 清空：Ctrl+R 就地重印绝不作用于上上轮。"""
        h = Harness()
        h.svc._console._last_replay = {"bogus": True}
        h.svc._console._replay_expanded = True
        h.svc._console._replay_block_rows = 7
        h.svc.start()
        assert h.svc._console._last_replay is None
        assert h.svc._console._replay_expanded is False
        assert h.svc._console._replay_block_rows == 0

    def test_no_thinking_turn_leaves_replay_none(
        self, deterministic_live
    ):
        """无 reasoning 的回合：done 后 _last_replay 仍为 None（Ctrl+R
        静默忽略的数据前提）。"""
        h = Harness()
        h.svc.start()
        h.svc.feed("plain answer")
        h.svc.done()
        assert h.svc._console._last_replay is None

    def test_frozen_indicator_carries_permanent_hint(self, deterministic_live):
        """冻结指示行常驻 (ctrl+r to expand) 提示（用户界面需求）：
        折叠/done 后仍随行上屏，done 后热键经重印兑现。"""
        h = Harness()
        h.svc.start()
        h.svc.feed(StreamReasoning("some thinking"))
        h.svc.feed("answer")
        h.svc.done()
        h.flush()
        text = h.screen_text()
        assert "Thought for" in text
        assert "(ctrl+r to expand)" in text

    def test_stream_gap_between_body_and_spinner(self, deterministic_live):
        """流式期正文与框（经 spinner）恒隔 1 空行（_BODY_FRAME_GAP）。"""
        h = Harness()
        h.svc.start()
        h.svc.feed("body line here")
        h.refresh()
        rows = [r.rstrip() for r in h.rows()]
        body_y = next(y for y, r in enumerate(rows) if "body line here" in r)
        spin_y = next(
            y for y, r in enumerate(rows) if "esc to interrupt" in r)
        assert spin_y - body_y == 2          # 中间恰 1 空行
        assert rows[body_y + 1] == ""

    def test_done_gap_between_body_and_frame(self, deterministic_live):
        """done 后留屏形态（对标 Claude Code）：

        正文 › 1 空行 › ``✻ <动词> for <时长>`` › 1 空行（_BODY_FRAME_GAP）
        › FRAME。结束行**在 gap 之上**——它已固化进 scrollback，而 gap 是
        正文与框之间的固定间距。
        """
        h = Harness()
        h.svc.start()
        h.svc.feed("the answer body")
        h.svc.done()
        h.flush()
        rows = [r.rstrip() for r in h.rows()]
        body_y = next(y for y, r in enumerate(rows) if "the answer body" in r)
        frame_y = next(y for y, r in enumerate(rows) if "FRAME" in r)
        # 正文 → 空行 → 结束行（动词 + for + 时长）→ 空行 → FRAME
        assert rows[body_y + 1] == ""
        assert rows[body_y + 2].startswith("✻ "), rows[body_y + 2]
        assert " for " in rows[body_y + 2], rows[body_y + 2]
        assert rows[body_y + 3] == ""
        assert frame_y == body_y + 4

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


# ── 超长 thinking 的 Ctrl+R 窗口（上下文过多仍可展开/折叠）────────


class TestThinkingOversizedWindow:
    def test_expand_shows_window_when_thinking_exceeds_screen(
        self, deterministic_live
    ):
        """thinking 超一屏时 Ctrl+R 展开仍可见：指示行在屏 + 最近推理窗口
        （头部截断带 +N more thoughts 标记）——用户报告：上下文过多时
        展开打开不了（截窗保尾曾把指示行与全文全裁掉）。"""
        h = Harness()  # 24 行屏
        h.svc.start()
        h.svc.feed(StreamReasoning(
            "\n".join(f"thought line {i}" for i in range(1, 61))))
        h.press("\x12")  # 展开
        h.refresh()
        text = h.screen_text()
        assert "Thinking…" in text, "指示行必须在屏（展开反馈的锚点）"
        assert "ctrl+r to collapse" in text
        assert "thought line 60" in text, "最近推理必须可见（窗口保尾）"
        assert "thought line 1" not in text, "头部应被窗掉"
        assert "more thoughts" in text, "截断标记"
        # 框锚定不漂移
        frame_y = max(y for y, t in h.nonempty() if "FRAME" in t)
        assert frame_y <= 23

    def test_collapse_after_oversized_expand(self, deterministic_live):
        """超长展开后再折叠：回到单行指示、全文撤下、框仍在屏内。

        （流式 Live 区顶锚定：折叠后区高收缩、框随之上移是正常形态——
        断言的是内容正确性与框在屏内，而非两态框位相同。）
        """
        h = Harness()
        h.svc.start()
        h.svc.feed(StreamReasoning(
            "\n".join(f"thought line {i}" for i in range(1, 61))))
        h.press("\x12")
        h.refresh()
        h.press("\x12")  # 折叠
        h.refresh()
        text = h.screen_text()
        assert "ctrl+r to expand" in text
        assert "thought line 60" not in text
        assert "more thoughts" not in text
        frame_y2 = max(y for y, t in h.nonempty() if "FRAME" in t)
        assert frame_y2 <= 23

    def test_short_thinking_not_windowed(self, deterministic_live):
        """屏内放得下的 thinking 展开照旧全文显示（无截断标记）。"""
        h = Harness()
        h.svc.start()
        h.svc.feed(StreamReasoning("line a\nline b\nline c"))
        h.press("\x12")
        h.refresh()
        text = h.screen_text()
        assert "line a" in text and "line c" in text
        assert "more thoughts" not in text


class TestThinkingTranscriptComplete:
    def test_done_commits_full_oversized_thinking(self, deterministic_live):
        """done() 固化超长 thinking：transcript 全量落盘（窗化只作用于
        流式易变显示，固化渲染恒全文——历史完整性优先）。"""
        h = Harness()
        h.svc.start()
        lines = [f"deep thought {i}" for i in range(1, 61)]
        h.svc.feed(StreamReasoning("\n".join(lines)))
        h.press("\x12")  # 展开
        h.refresh()
        h.svc.feed("the answer")
        h.svc.done()
        raw = h.buf.getvalue()
        assert "deep thought 1" in raw, "固化必须含思考头部"
        assert "deep thought 60" in raw, "固化必须含思考尾部"
        assert "the answer" in raw
