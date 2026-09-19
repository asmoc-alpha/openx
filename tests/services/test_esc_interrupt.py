"""Esc 打断与排队反馈回归测试 —— v0.4.1。

覆盖两个用户报告的修复：

（1）think / 输出阶段 Esc 打断
    - read_unicode_char：单独 Esc（20ms 无后续字节）→ "\\x1b"；完整
      转义序列（方向键）→ ""（pty 实测，钉死识别窗口语义）；
    - InputCapture：Esc → on_interrupt 回调；
    - StreamingService._interrupt：取消登记的消费任务（非主任务）、
      单回合闩防连按、任务完成后 cancel 为 no-op（流弹 Esc 不误伤）；
    - 端到端：Esc 打断 _stream_response → 正常返回（吞掉取消）、
      光标恢复、捕获清理——绝不泄漏异常出 REPL。

（2）输出期间输入反馈
    - Enter 排队 → "» queued: … (esc to interrupt & send)" 反馈行
      渲染在输入框之下；done() 后消失；
    - spinner 行常驻 "esc to interrupt" 提示（能力可见性）。

风格：pytest-asyncio auto、手写 fake、禁 unittest.mock；pyte 基建
沿用 test_terminal_interaction.py 手法。

运行：``python -m pytest tests/test_esc_interrupt.py -q``
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import pty
import termios
import time
import tty
from types import SimpleNamespace

import pyte
import pyte.modes
import pytest
from rich.console import Console as RichConsole
from rich.text import Text

import openx.orchestration.sessions as sessions_mod
from openx.config import OpenXConfig
from openx.kernel import reset_kernel
from openx.kernel.recovery import (
    INTERRUPTED_TOOL_RESULT,
    CheckpointStore,
    ResumeVerdict,
)
from openx.llm import StreamDone
from openx.orchestration.sessions import SessionStore
from openx.permissions import PermissionRules
from openx.services.streaming import StreamingService
from openx.tools.base import Tool, ToolResult
from openx.ui.console import Console
from openx.ui.input_capture import InputCapture, read_unicode_char


# ── (1a) 单独 Esc 识别（pty 实测）────────────────────────────────

class TestBareEscDetection:
    def test_sequence_vs_bare_esc(self):
        master, slave = pty.openpty()
        try:
            tty.setcbreak(slave, termios.TCSANOW)
            # 无参数方向键 → 规范序列串（v0.4.2 菜单导航）
            for seq, expect in [
                (b"\x1b[A", "\x1b[A"), (b"\x1b[B", "\x1b[B"),
                (b"\x1b[C", "\x1b[C"), (b"\x1b[D", "\x1b[D"),
                (b"\x1bOA", "\x1b[A"),  # SS3（应用模式方向键）同样归一
            ]:
                os.write(master, seq)
                time.sleep(0.05)
                assert read_unicode_char(slave) == expect, seq
            # 带参数的 CSI（Ctrl-Right 等）→ "" 已消费忽略
            os.write(master, b"\x1b[1;5C")
            time.sleep(0.05)
            assert read_unicode_char(slave) == ""
            # 单独 Esc（20ms 窗口内无后续字节）→ "\x1b" 热键
            os.write(master, b"\x1b")
            time.sleep(0.05)  # 越过 20ms 后续窗口，确保"无后续"成立
            assert read_unicode_char(slave) == "\x1b"
            # PgUp/PgDn（CSI 5/6 ~）→ 规范记号（流式滚动回看）
            for seq, expect in [(b"\x1b[5~", "\x1b[5~"), (b"\x1b[6~", "\x1b[6~")]:
                os.write(master, seq)
                time.sleep(0.05)
                assert read_unicode_char(slave) == expect, seq
            # 普通字符不受影响
            os.write(master, b"a")
            time.sleep(0.05)
            assert read_unicode_char(slave) == "a"
        finally:
            os.close(master)
            os.close(slave)


# ── (1b) InputCapture Esc 分发 ───────────────────────────────────

class TestCaptureEscDispatch:
    def test_esc_fires_on_interrupt(self):
        cap = InputCapture()
        fired = []
        cap.on_interrupt = lambda: fired.append("esc")
        cap._handle("\x1b")
        cap._handle("a")       # 普通键不触发
        cap._handle("")        # 完整序列不触发
        assert fired == ["esc"]

    def test_interrupt_callback_error_swallowed(self):
        cap = InputCapture()

        def bad():
            raise RuntimeError("boom")

        cap.on_interrupt = bad
        cap._handle("\x1b")  # 不得抛出

    def test_enter_fires_on_line_queued(self):
        cap = InputCapture()
        queued = []
        cap.on_line_queued = lambda line: queued.append(line)
        cap._current = "hello"
        cap._handle("\r")
        assert queued == ["hello"]
        assert cap.drain() == ["hello"]

    def test_empty_enter_enqueues_hotkey(self):
        """空 Enter → 热键（舰队选择确认）；不触发排队回调。"""
        fired = []
        cap = InputCapture()
        cap.on_line_queued = lambda line: fired.append(line)
        cap._handle("\r")
        assert cap.drain_hotkeys() == ["\r"]
        assert fired == []
        assert cap.drain() == []

    def test_nonempty_enter_does_not_enqueue_hotkey(self):
        """有键入内容时 Enter 仍只走排队路径（与选择确认互斥）。"""
        cap = InputCapture()
        cap._current = "hello"
        cap._handle("\r")
        assert cap.drain_hotkeys() == []
        assert cap.drain() == ["hello"]


# ── (1d) 括号粘贴（多行粘贴修复）─────────────────────────────────

class TestBracketedPaste:
    def test_paste_marker_tokens_parsed(self):
        """pty 实测：ESC[200~ / ESC[201~ → 粘贴起止记号。"""
        master, slave = pty.openpty()
        try:
            tty.setcbreak(slave, termios.TCSANOW)
            for seq, expect in [
                (b"\x1b[200~", "\x1b[200~"),
                (b"\x1b[201~", "\x1b[201~"),
            ]:
                os.write(master, seq)
                time.sleep(0.05)
                assert read_unicode_char(slave) == expect, seq
        finally:
            os.close(master)
            os.close(slave)

    def test_handle_paste_state_literals(self):
        """粘贴体内：换行字面保留、控制符不触发热键/提交。"""
        from openx.ui.input_capture import PASTE_END, PASTE_START

        queued: list = []
        cap = InputCapture()
        cap.on_line_queued = lambda line: queued.append(line)
        cap._handle(PASTE_START)
        cap._handle("l1")
        cap._handle("\n")
        cap._handle("\r")      # 粘贴体内的 \r → 字面换行，不提交
        cap._handle("l2")
        cap._handle("\x0f")    # 粘贴体内的 Ctrl-O → 字面，非热键
        cap._handle(PASTE_END)
        assert cap.current == "l1\n\nl2\x0f"
        assert cap.drain_hotkeys() == []
        assert queued == []

        cap._handle("\r")      # 粘贴结束后 Enter → 整条多行消息排队
        assert queued == ["l1\n\nl2\x0f"]


# ── (1e) Shift+Enter 换行（三种键形归一）─────────────────────────

class TestShiftEnter:
    def test_shift_enter_forms_parsed(self):
        """pty 实测：kitty / modifyOtherKeys / Alt+Enter 三形 → 同一记号。"""
        from openx.ui.input_capture import SHIFT_ENTER

        master, slave = pty.openpty()
        try:
            tty.setcbreak(slave, termios.TCSANOW)
            for seq in (
                b"\x1b[13;2u",      # kitty 键盘协议：Shift+Enter
                b"\x1b[27;2;13~",   # modifyOtherKeys：Shift+Enter
                b"\x1b\r",          # Alt+Enter（通用回退）
            ):
                os.write(master, seq)
                time.sleep(0.05)
                assert read_unicode_char(slave) == SHIFT_ENTER, seq
            # 裸 Enter 不受影响（仍是提交；ICRNL 把 CR 映射成 NL）
            os.write(master, b"\r")
            time.sleep(0.05)
            assert read_unicode_char(slave) in ("\r", "\n")
            # 其他带参 CSI 仍被消费忽略（';' 保留不致误判）
            os.write(master, b"\x1b[1;5C")
            time.sleep(0.05)
            assert read_unicode_char(slave) == ""
        finally:
            os.close(master)
            os.close(slave)

    def test_handle_shift_enter_appends_newline(self):
        """Shift+Enter 插字面换行、不提交；Enter 仍提交多行全文。"""
        from openx.ui.input_capture import SHIFT_ENTER

        queued: list = []
        cap = InputCapture()
        cap.on_line_queued = lambda line: queued.append(line)
        cap._handle("line1")
        cap._handle(SHIFT_ENTER)
        cap._handle("line2")
        assert cap.current == "line1\nline2"
        assert cap.drain_hotkeys() == []
        assert queued == []
        cap._handle("\r")  # Enter 提交整条
        assert queued == ["line1\nline2"]


# ── (1c) StreamingService._interrupt 机制 ────────────────────────

def _svc():
    """无 TTY 的 StreamingService（console 桩 + 未启动 Live）。"""
    rc = RichConsole(file=io.StringIO(), width=80, height=24, force_terminal=True)
    console = SimpleNamespace(
        _console=rc, _input_queue=[], _frame_on_screen=False,
        _input_capture=None, _frame_renderable=lambda i, o: Text("FRAME"),
    )
    return StreamingService(console, input_tokens=0)


class TestInterruptMechanism:
    async def test_interrupt_cancels_target_task(self):
        svc = _svc()
        svc.start()  # 非 TTY：capture 对象与回调就位，线程 no-op
        assert svc._loop is asyncio.get_running_loop()

        task = asyncio.ensure_future(asyncio.sleep(30))
        svc.set_cancel_target(task)
        svc._interrupt()  # 模拟捕获线程的 Esc
        assert svc.esc_interrupted is True
        with pytest.raises(asyncio.CancelledError):
            await task  # call_soon_threadsafe 投递的取消生效

    async def test_latch_blocks_repeat_fires(self):
        svc = _svc()
        svc.start()
        runs = []

        async def work():
            runs.append(1)
            await asyncio.sleep(30)

        task = asyncio.ensure_future(work())
        svc.set_cancel_target(task)
        svc._interrupt()
        svc._interrupt()  # 连按：闩住，不重复取消
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_stray_esc_after_completion_is_noop(self):
        """回合结束后（任务完成）的流弹 Esc 绝不取消任何东西。"""
        svc = _svc()
        svc.start()
        done_task = asyncio.ensure_future(asyncio.sleep(0))
        await done_task
        svc.set_cancel_target(done_task)
        svc._interrupt()  # 任务已完成 → cancel 为 no-op
        assert done_task.done() and not done_task.cancelled()
        assert svc.esc_interrupted is True  # 标志置位但无任务受影响

    def test_interrupt_without_loop_is_noop(self):
        svc = _svc()
        svc._interrupt()  # start() 未调用 → _loop/_cancel_target None
        assert svc.esc_interrupted is True  # 只置标志，无副作用


class TestEscSurvivesDialogPause:
    """回归：弹窗 pause→resume 换掉 capture 后回调必须重新接线。

    用户报告根因：resume() 重建 InputCapture 时漏接 on_interrupt，
    manual 模式首个弹窗（choose_mode）后 Esc 静默失效。
    """

    async def test_callbacks_rewired_after_resume(self, deterministic_live):
        svc = _svc()
        svc.start()
        assert svc._capture.on_interrupt is not None

        svc.pause()   # 弹窗开始 → 捕获整体停掉
        assert svc._capture is None
        svc.resume()  # 弹窗结束 → 全新 capture
        assert svc._capture is not None
        assert svc._capture.on_interrupt == svc._interrupt
        assert svc._capture.on_line_queued == svc._on_line_queued

        # 新 capture 上的 Esc 真的能打断
        task = asyncio.ensure_future(asyncio.sleep(30))
        svc.set_cancel_target(task)
        svc._capture._handle("\x1b")  # 模拟捕获线程收到单独 Esc
        with pytest.raises(asyncio.CancelledError):
            await task
        assert svc.esc_interrupted is True

    async def test_nested_pause_resumes_keep_wiring(self, deterministic_live):
        """权限钩子与弹窗钩子叠加（引用计数 2）后仍恢复接线。"""
        svc = _svc()
        svc.start()
        svc.pause()
        svc.pause_capture()   # 第二层（executor 钩子路径）
        svc.resume_capture()
        assert svc._capture is None  # 计数未归零 → 仍停
        svc.resume()
        assert svc._capture.on_interrupt == svc._interrupt


# ── (1d) 端到端：Esc 打断 _stream_response ───────────────────────

class FakeAgent:
    """流式假 agent：吐一个 token 后长时间等待（模拟 think/生成）。"""

    def __init__(self):
        self.config = SimpleNamespace(stream=True)
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.tool_executor = SimpleNamespace(
            on_prompt_start=None, on_prompt_end=None)
        self.todos: list = []
        self.fleet = None
        self.interrupted = False

    async def stream_run(self, content):
        yield "partial…"
        try:
            await asyncio.sleep(30)  # 等待被打断
        except asyncio.CancelledError:
            self.interrupted = True
            raise


def _real_console() -> Console:
    console = Console(OpenXConfig())
    console._console = RichConsole(
        file=io.StringIO(), width=100, height=30, force_terminal=True
    )
    return console


class TestEndToEndEsc:
    async def test_esc_interrupts_and_returns_to_repl(self, deterministic_live):
        from openx.app.cli.interactive import _stream_response

        agent = FakeAgent()
        console = _real_console()

        async def press_esc_soon():
            # 等流式启动 + 首个 token 后，模拟捕获线程收到 Esc
            for _ in range(200):
                if console._input_capture is not None:
                    break
                await asyncio.sleep(0.005)
            cap = console._input_capture
            assert cap is not None, "capture 应已接线"
            await asyncio.sleep(0.02)
            cap.on_interrupt()  # 捕获线程在单独 Esc 上的实际调用

        # _stream_response 必须**正常返回**（吞掉取消），绝不上抛
        await asyncio.wait_for(
            asyncio.gather(
                _stream_response(agent, console, "hi"),
                press_esc_soon(),
            ),
            timeout=5,
        )
        assert agent.interrupted            # 子任务确实被取消
        assert console._input_capture is None   # 捕获已清理
        raw = console._console.file.getvalue()
        assert "\033[?25h" in raw           # 光标已恢复

    async def test_ctrl_c_still_raises_keyboardinterrupt(self, deterministic_live):
        """Ctrl-C 语义保持：清理后以 KeyboardInterrupt 上抛（退出 REPL）。"""
        from openx.app.cli.interactive import _stream_response

        class CtrlCAgent(FakeAgent):
            async def stream_run(self, content):
                yield "partial…"
                raise KeyboardInterrupt

        console = _real_console()
        with pytest.raises(KeyboardInterrupt):
            await _stream_response(CtrlCAgent(), console, "hi")
        assert console._input_capture is None
        assert "\033[?25h" in console._console.file.getvalue()


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


# ── (2) 排队反馈行（pyte 屏幕级）─────────────────────────────────

class Harness:
    def __init__(self, rows: int = 24, cols: int = 80):
        self.screen = pyte.Screen(cols, rows)
        self.screen.set_mode(pyte.modes.LNM)
        self.pyte = pyte.Stream(self.screen)
        self.buf = io.StringIO()
        rc = RichConsole(
            file=self.buf, width=cols, height=rows, force_terminal=True
        )
        console = SimpleNamespace(
            _console=rc, _input_queue=[], _frame_on_screen=False,
            _input_capture=None, _frame_renderable=lambda i, o: Text("FRAME"),
        )
        self.svc = StreamingService(console, input_tokens=0)

    def refresh(self):
        self.svc._live.refresh()
        self.flush()

    def flush(self):
        self.pyte.feed(self.buf.getvalue())
        self.buf.seek(0)
        self.buf.truncate()

    def text(self) -> str:
        return "\n".join(
            "".join(c.data for c in self.screen.buffer[y].values()).rstrip()
            for y in range(self.screen.lines)
        )

    def frame_row(self) -> int:
        for y in range(self.screen.lines):
            row = "".join(
                c.data for c in self.screen.buffer[y].values()
            )
            if "FRAME" in row:
                return y
        raise AssertionError("FRAME not on screen")


class TestQueuedFeedback:
    def test_queued_panel_renders_above_frame(self, deterministic_live):
        """流式期间 Enter 排队 → Queue 面板即时可见，位于输入框之上。"""
        h = Harness()
        h.svc.start()
        h.svc.feed("answering…")
        # 模拟流式期间 Enter 排队（捕获线程路径）
        h.svc._capture._current = "next question"
        h.svc._capture._handle("\r")
        h.refresh()
        text = h.text()
        assert "Queue (1)" in text
        assert "▸ next question" in text
        # 队列面板在 FRAME 之上（状态层在输入框上方）
        for y in range(h.screen.lines):
            row = "".join(c.data for c in h.screen.buffer[y].values())
            if "Queue" in row:
                assert y < h.frame_row()
                break
        else:
            pytest.fail("queue panel not on screen")

    def test_queued_panel_vanishes_after_done(self, deterministic_live):
        h = Harness()
        h.svc.start()
        h.svc.feed("hi")
        h.svc._on_line_queued("queued msg")
        h.refresh()
        assert "Queue (1)" in h.text()
        h.svc.done()
        h.flush()
        assert "Queue" not in h.text()

    def test_multi_queue_lists_all_in_order(self, deterministic_live):
        """多条排队按序全列（不再只显示最新一条）。"""
        h = Harness()
        h.svc.start()
        h.svc.feed("hi")
        h.svc._on_line_queued("first")
        h.svc._on_line_queued("second")
        h.refresh()
        rows = [
            "".join(c.data for c in h.screen.buffer[y].values())
            for y in range(h.screen.lines)
        ]
        text = "\n".join(rows)
        assert "Queue (2)" in text
        assert "▸ first" in text and "▸ second" in text
        y_first = next(i for i, r in enumerate(rows) if "first" in r)
        y_second = next(i for i, r in enumerate(rows) if "second" in r)
        assert y_first < y_second

    def test_spinner_carries_esc_hint(self, deterministic_live):
        h = Harness()
        h.svc.start()
        h.refresh()
        assert "esc to interrupt" in h.text()


# ── (3) 打断后的上下文保真：被打断的那一轮并入历史 ──────────────
#
# 用户报告：Esc 打断之后接着对话，模型像换了个人--连"你刚才问过我什么"
# 都答不上来。根因：``history.add`` 只在回合末尾，取消把整轮 ``new_turn``
# （含用户自己那条消息）一起带走了。修复见 ``agent.stream_run`` 的取消
# 边界 + ``OpenXAgent._harvest_interrupted_turn``。

class _ScriptedLLM:
    """脚本化流式假 LLM，取消点是**确定**的（不靠 sleep 抢时序）。

    每个剧本项是 ``(text, tool_calls, gate)``：``gate=True`` 表示吐完文本
    后卡在永不置位的 Event 上--测试据此拿到"正在生成"的稳定瞬间再取消。
    """

    def __init__(self, script):
        self.script = list(script)
        self.call_count = 0
        self.seen: list[list[dict]] = []
        self.waiting = False
        self.gate = asyncio.Event()

    async def stream_chat(self, messages, tools=None):
        self.seen.append([dict(m) for m in messages])
        text, tool_calls, gate = self.script[self.call_count]
        self.call_count += 1
        if text:
            yield text
        if tool_calls:
            yield StreamDone(
                response={"role": "assistant", "content": None,
                          "tool_calls": tool_calls},
                token_count=1, input_tokens=1,
            )
            return
        if gate:
            self.waiting = True
            await self.gate.wait()   # 永不置位：等测试取消
            return
        yield StreamDone(
            response={"role": "assistant", "content": text},
            token_count=1, input_tokens=1,
        )


class _BlockingTool(Tool):
    """永不返回的工具：模拟"正在执行时被打断"。"""

    name = "blocking"

    def __init__(self):
        self.started = False

    async def execute(self, **kw):
        self.started = True
        await asyncio.Event().wait()
        return ToolResult(output="never")


def _tool_call(call_id: str, name: str = "blocking") -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


@pytest.fixture
def esc_env(tmp_path, monkeypatch):
    """隔离会话目录 + 新鲜内核：返回 (workspace, session_store)。"""
    monkeypatch.setattr(sessions_mod, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(
        "openx.kernel.audit.hooks.SETTINGS_PATH", tmp_path / "no-settings.json"
    )
    reset_kernel()
    store = SessionStore.create(str(tmp_path), "test-model")
    yield str(tmp_path), store
    reset_kernel()


def _esc_agent(env):
    from openx.agent import OpenXAgent
    from ..test_bugfixes import FakeConsole

    workspace, store = env
    config = OpenXConfig()
    config.workspace = workspace
    config.model = "test-model"
    agent = OpenXAgent(
        config, session_store=store, session_id=store.meta.session_id,
        console=FakeConsole(),
    )
    agent.tool_executor._rules = PermissionRules()
    return agent


async def _drain(gen) -> None:
    async for _ in gen:
        pass


async def _interrupt(agent, message: str, *, ready) -> None:
    """跑一轮，等 ``ready()`` 成立后取消回合任务（等价 REPL 的 Esc）。"""
    task = asyncio.ensure_future(_drain(agent.stream_run(message)))
    for _ in range(1000):
        if ready():
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("turn never reached the interruption point")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class TestInterruptedTurnMemory:
    """被打断的那一轮留在历史里：用户消息 + 部分回答 + 在途工具的合成结果。"""

    async def test_question_and_partial_answer_survive(self, esc_env):
        agent = _esc_agent(esc_env)
        agent.llm = _ScriptedLLM([("partial ", None, True)])

        await _interrupt(agent, "帮我看看 X", ready=lambda: agent.llm.waiting)

        roles = [m["role"] for m in agent.history.messages]
        assert roles == ["user", "assistant"], roles
        assert agent.history.messages[0]["content"] == "帮我看看 X"
        assert "partial" in agent.history.messages[1]["content"]
        assert "interrupted" in agent.history.messages[1]["content"].lower()

    async def test_next_turn_still_sees_the_question(self, esc_env):
        """核心回归：Esc 之后接着问，模型仍看得到上一句问的是什么。"""
        agent = _esc_agent(esc_env)
        agent.llm = _ScriptedLLM([("half ", None, True)])
        await _interrupt(agent, "第一个问题", ready=lambda: agent.llm.waiting)

        agent.llm = _ScriptedLLM([("answer", None, False)])
        await _drain(agent.stream_run("继续"))

        sent = agent.llm.seen[0]
        assert any("第一个问题" in str(m.get("content")) for m in sent)
        assert any("half" in str(m.get("content")) for m in sent)

    async def test_inflight_tool_call_gets_synthetic_result(self, esc_env):
        """工具执行中被打断：序列仍合法，且绝不重放（与 --recover 同一句话）。"""
        agent = _esc_agent(esc_env)
        tool = _BlockingTool()
        agent.tools["blocking"] = tool
        agent.llm = _ScriptedLLM([(None, [_tool_call("c1")], False)])

        await _interrupt(agent, "跑个工具", ready=lambda: tool.started)

        msgs = agent.history.messages
        assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant"]
        assert msgs[2]["tool_call_id"] == "c1"
        assert msgs[2]["content"] == INTERRUPTED_TOOL_RESULT
        assert agent.history.validate()

    async def test_interrupted_turn_reaches_the_session_file(self, esc_env):
        """不只是内存：会话文件里也有，--continue 之后仍在。"""
        agent = _esc_agent(esc_env)
        agent.llm = _ScriptedLLM([("partial ", None, True)])
        await _interrupt(agent, "帮我看看 X", ready=lambda: agent.llm.waiting)

        _, messages = SessionStore.load(agent.session_store.path)
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[0]["content"] == "帮我看看 X"

    async def test_recover_after_interrupt_does_not_replay(self, esc_env):
        """重启 --recover：这轮已落进历史 → 判陈旧（ALREADY_COMPLETE），不重放。

        落盘两处必须对得上：历史里有这一轮，旁挂快照里也有它--裁决据此
        判定"没东西可续"，模型不会把同一轮看两遍。
        """
        agent = _esc_agent(esc_env)
        agent.llm = _ScriptedLLM([("partial ", None, True)])
        await _interrupt(agent, "帮我看看 X", ready=lambda: agent.llm.waiting)
        agent.flush_checkpoint("esc")     # 端层（REPL / serve）紧随其后的兜底落盘

        plan = agent.recover_session()
        assert plan.verdict is ResumeVerdict.ALREADY_COMPLETE, plan.detail
        # 合并进历史不影响容灾侧：旁挂文件照旧写出、reason 记成 esc
        record = CheckpointStore(agent.session_store.path).read()
        assert record is not None and record.reason == "esc"

        events = [
            obj for obj in (
                json.loads(line) for line in
                agent.session_store.path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ) if obj.get("type") == "interrupt"
        ]
        assert events and events[-1]["payload"]["kind"] == "esc"

    async def test_completed_turn_is_not_duplicated(
        self, esc_env, monkeypatch
    ):
        """已并入历史的回合再被取消：绝不重复并入（历史里出现两遍同一轮）。"""
        agent = _esc_agent(esc_env)
        agent.llm = _ScriptedLLM([("done", None, False)])
        entered = asyncio.Event()

        async def _slow_compact() -> bool:
            entered.set()
            await asyncio.Event().wait()   # 卡在"已经并入历史之后"
            return False

        monkeypatch.setattr(agent, "_maybe_auto_compact", _slow_compact)
        await _interrupt(agent, "一次正常回合", ready=entered.is_set)

        roles = [m["role"] for m in agent.history.messages]
        assert roles == ["user", "assistant"], roles
        assert agent.history.messages[1]["content"] == "done"
        assert "interrupted" not in agent.history.messages[1]["content"].lower()


class TestHarvestShapes:
    """``_harvest_interrupted_turn`` 的纯函数契约（不发模型、不落盘）。"""

    @staticmethod
    def _harvest(new_turn, text=""):
        from openx.agent import OpenXAgent

        return OpenXAgent._harvest_interrupted_turn(new_turn, text)

    def test_synthesizes_only_the_missing_results(self):
        harvested = self._harvest([
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": None,
             "tool_calls": [_tool_call("c1"), _tool_call("c2")]},
            {"role": "tool", "tool_call_id": "c1", "content": "real output"},
        ])
        assert [m["role"] for m in harvested] == [
            "user", "assistant", "tool", "tool", "assistant",
        ]
        assert harvested[2]["content"] == "real output"        # 已有结果原样保留
        assert harvested[3]["tool_call_id"] == "c2"            # 缺的那条才补
        assert harvested[3]["content"] == INTERRUPTED_TOOL_RESULT

    def test_marker_used_when_no_text_was_produced(self):
        harvested = self._harvest([{"role": "user", "content": "q"}])
        assert [m["role"] for m in harvested] == ["user", "assistant"]
        assert harvested[1]["content"].strip()           # 绝不落空 content
        assert "interrupted" in harvested[1]["content"].lower()

    def test_empty_turn_harvests_to_nothing(self):
        assert self._harvest([]) == []

    def test_partial_text_is_appended_once(self):
        harvested = self._harvest(
            [{"role": "user", "content": "q"}], "半句"
        )
        assert harvested[1]["content"].startswith("半句")
        assert harvested[1]["content"].count("半句") == 1
