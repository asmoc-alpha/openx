"""中断与恢复：控制器语义 + 中断落盘接线。

覆盖：信号登记并**链式交回**原处理器（既有退出路径零改动）/ 非主线程安装
是 no-op / note 只登记不取消 / request 取消登记的任务 / flush_current 的
幂等与次序（checkpoint 先落盘，interrupt 事件后记）/ clear 复位 /
serve 的重复打断**不**致命。

不真的发信号（那会干扰 pytest 主线程）：直接调用 ``_handle_signal`` 并
注入一个记录用的"原处理器"，把链式行为测成可断言的。

运行：``python -m pytest tests/services/test_interrupt_recovery.py -q``
"""

from __future__ import annotations

import asyncio
import json
import signal
import threading
from types import SimpleNamespace

import pytest

import openx.orchestration.sessions as sessions_mod
from openx.kernel import reset_kernel
from openx.kernel.recovery import CheckpointStore
from openx.orchestration.sessions import SessionStore
from openx.services.interrupt import InterruptController, InterruptKind


@pytest.fixture
def irq_env(tmp_path, monkeypatch):
    """隔离会话目录 + 新鲜内核，返回 (workspace, sessions_root)。"""
    sessions_root = tmp_path / "sessions"
    monkeypatch.setattr(sessions_mod, "SESSIONS_DIR", sessions_root)
    monkeypatch.setattr(
        "openx.kernel.audit.hooks.SETTINGS_PATH", tmp_path / "no-settings.json"
    )
    reset_kernel()
    ws = tmp_path / "ws"
    ws.mkdir()
    yield str(ws), sessions_root
    reset_kernel()


def _agent(workspace: str, store=None, session_id: str = "s1"):
    from openx.agent import OpenXAgent
    from openx.config import OpenXConfig
    from openx.permissions import PermissionRules

    from ..test_bugfixes import FakeConsole, FakeLLM

    config = OpenXConfig()
    config.workspace = workspace
    config.model = "test-model"
    agent = OpenXAgent(
        config, session_store=store, session_id=session_id, console=FakeConsole()
    )
    agent.llm = FakeLLM([("done", None)])
    agent.tool_executor._rules = PermissionRules()
    return agent


def _ledger_lines(store: SessionStore) -> list[dict]:
    try:
        raw = store.path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "seq" in obj and "digest" in obj:
            out.append(obj)
    return out


# ── 控制器语义 ──────────────────────────────────────────────────


class TestControllerSemantics:
    """登记、取消、复位--且绝不自己发起退出。"""

    def test_note_records_without_cancelling(self):
        ctl = InterruptController()
        cancelled = []
        ctl._turn_task = SimpleNamespace(
            done=lambda: False, cancel=lambda: cancelled.append(True)
        )
        ctl.note("esc")
        assert ctl.pending is InterruptKind.ESC
        assert ctl.kind_name() == "esc"
        assert ctl.requested
        assert cancelled == [], "note 只登记来源，取消由调用方自己做"

    def test_request_records_and_cancels(self):
        ctl = InterruptController()
        cancelled = []
        ctl._turn_task = SimpleNamespace(
            done=lambda: False, cancel=lambda: cancelled.append(True)
        )
        ctl.request("client")
        assert ctl.pending is InterruptKind.CLIENT
        assert cancelled == [True]

    def test_kind_name_falls_back_when_nothing_pending(self):
        assert InterruptController().kind_name() == "signal"
        assert InterruptController().kind_name("esc") == "esc"

    def test_clear_resets_state(self):
        ctl = InterruptController()
        ctl.request("sigint")
        assert ctl.requested
        ctl.clear()
        assert ctl.pending is None
        assert not ctl.requested
        assert ctl.kind_name() == "signal"

    def test_cancel_is_noop_without_task(self):
        InterruptController().request("sigint")   # 不抛

    def test_cancel_skips_finished_task(self):
        ctl = InterruptController()
        cancelled = []
        ctl._turn_task = SimpleNamespace(
            done=lambda: True, cancel=lambda: cancelled.append(True)
        )
        ctl.request("esc")
        assert cancelled == []

    def test_repeated_interrupt_never_exits_process(self):
        """重复打断只累加计数--**绝不** os._exit（服务端绝不能被几次点击杀死）。"""
        ctl = InterruptController()
        ctl._turn_task = SimpleNamespace(done=lambda: False, cancel=lambda: None)
        for _ in range(5):
            ctl.request("client")
        assert ctl.pending is InterruptKind.CLIENT   # 首次来源保留


class TestSignalChaining:
    """信号只做加法：登记来源后把控制权交回原处理器。"""

    def test_previous_handler_is_called(self):
        ctl = InterruptController()
        seen = []
        ctl._previous[signal.SIGINT] = lambda signum, frame: seen.append(signum)
        ctl._handle_signal(signal.SIGINT, None)
        assert seen == [signal.SIGINT], "必须链式交回原处理器"
        assert ctl.pending is InterruptKind.SIGINT

    def test_sigterm_maps_to_sigterm_kind(self):
        ctl = InterruptController()
        ctl._previous[signal.SIGTERM] = lambda signum, frame: None
        ctl._handle_signal(signal.SIGTERM, None)
        assert ctl.pending is InterruptKind.SIGTERM

    def test_no_previous_handler_falls_back_to_default(self, monkeypatch):
        """没有原处理器（SIG_DFL）：还原默认并**重发**，绝不把信号吞掉。

        ``os.kill`` 必须打桩--真的重发会把跑测试的进程杀掉（这正是默认
        终止语义的证明，但不该由单元测试来演示）。
        """
        killed: list[int] = []
        monkeypatch.setattr(
            "openx.services.interrupt.os.kill",
            lambda pid, sig: killed.append(sig),
        )
        monkeypatch.setattr(
            "openx.services.interrupt.signal.signal", lambda sig, handler: None
        )
        ctl = InterruptController()
        ctl._handle_signal(signal.SIGINT, None)   # _previous 为空 = SIG_DFL
        assert killed == [signal.SIGINT]
        assert ctl.pending is InterruptKind.SIGINT

    def test_handler_that_raises_falls_back_to_default(self, monkeypatch):
        """原处理器抛异常：同样降级为默认重发，不把异常喷回信号处理链。"""
        killed: list[int] = []
        monkeypatch.setattr(
            "openx.services.interrupt.os.kill",
            lambda pid, sig: killed.append(sig),
        )
        monkeypatch.setattr(
            "openx.services.interrupt.signal.signal", lambda sig, handler: None
        )

        def boom(signum, frame):
            raise RuntimeError("previous handler down")

        ctl = InterruptController()
        ctl._previous[signal.SIGINT] = boom
        ctl._handle_signal(signal.SIGINT, None)
        assert killed == [signal.SIGINT]
        assert ctl.pending is InterruptKind.SIGINT

    def test_handler_that_returns_normally_does_not_reraise(self, monkeypatch):
        """原处理器正常返回（如 serve 的 add_signal_handler 风格）：不重发。"""
        killed: list[int] = []
        monkeypatch.setattr(
            "openx.services.interrupt.os.kill",
            lambda pid, sig: killed.append(sig),
        )
        ctl = InterruptController()
        ctl._previous[signal.SIGINT] = lambda signum, frame: None
        ctl._handle_signal(signal.SIGINT, None)
        assert killed == []

    def test_install_off_main_thread_is_noop(self):
        """signal.signal 只在主线程可用：非主线程安静返回 False。"""
        ctl = InterruptController()
        result = {}

        def run():
            result["ok"] = ctl.install_signals()

        thread = threading.Thread(target=run)
        thread.start()
        thread.join()
        assert result["ok"] is False
        assert ctl._installed == []

    def test_install_then_disarm_restores_previous(self):
        """安装/还原必须成对，别把处理器泄漏给调用方。"""
        ctl = InterruptController()
        original = signal.getsignal(signal.SIGINT)
        try:
            if not ctl.install_signals():
                pytest.skip("signal handlers unavailable in this environment")
            assert signal.getsignal(signal.SIGINT) == ctl._handle_signal
        finally:
            ctl.disarm()
        assert signal.getsignal(signal.SIGINT) == original


# ── 中断落盘接线 ────────────────────────────────────────────────


class TestInterruptCheckpoint:
    """flush_current：在途进展落盘 + interrupt 事件留痕。"""

    async def test_flush_writes_checkpoint_and_records_interrupt(self, irq_env):
        ws, _ = irq_env
        store = SessionStore.create(ws, "m", session_id="s1")
        agent = _agent(ws, store=store)

        # 造一个"正在跑工具"的回合
        agent._checkpoint.begin_turn(
            SimpleNamespace(tool_rounds=1),
            [{"role": "user", "content": "go"},
             {"role": "assistant", "content": "", "tool_calls": [
                 {"id": "t1", "type": "function",
                  "function": {"name": "echo", "arguments": "{}"}}]}],
            history_len=0, engine="stream_run", modal=False, resumed=True,
        )
        agent._checkpoint.mark_inflight()

        seq = agent.flush_checkpoint("sigint")
        assert seq > 0

        record = CheckpointStore(store.path).read()
        assert record is not None
        assert record.phase == "inflight"
        assert record.reason == "signal"

        interrupts = [ln for ln in _ledger_lines(store)
                      if ln.get("type") == "interrupt"]
        assert len(interrupts) == 1
        payload = interrupts[0]["payload"]
        assert payload["kind"] == "sigint"
        assert payload["checkpoint_seq"] == seq
        assert interrupts[0]["origin"] == "user"

    async def test_flush_is_idempotent_per_turn(self, irq_env):
        """一条退出路径可能多个分支兜底调用：同一次中断只记一次。"""
        ws, _ = irq_env
        store = SessionStore.create(ws, "m", session_id="s1")
        agent = _agent(ws, store=store)
        agent._checkpoint.begin_turn(
            SimpleNamespace(tool_rounds=1),
            [{"role": "user", "content": "go"}],
            history_len=0, engine="stream_run", modal=False, resumed=True,
        )

        first = agent.flush_checkpoint("esc")
        second = agent.flush_checkpoint("esc")
        assert first == second

        interrupts = [ln for ln in _ledger_lines(store)
                      if ln.get("type") == "interrupt"]
        assert len(interrupts) == 1, "重复兜底不该把一次中断记成好几次"

    async def test_flush_without_open_turn_is_noop(self, irq_env):
        ws, _ = irq_env
        store = SessionStore.create(ws, "m", session_id="s1")
        agent = _agent(ws, store=store)
        assert agent.flush_checkpoint("sigint") == 0
        assert CheckpointStore(store.path).read() is None

    async def test_flush_never_raises_even_if_store_is_broken(
        self, irq_env, monkeypatch
    ):
        ws, _ = irq_env
        store = SessionStore.create(ws, "m", session_id="s1")
        agent = _agent(ws, store=store)
        agent._checkpoint.begin_turn(
            SimpleNamespace(tool_rounds=0),
            [{"role": "user", "content": "go"}],
            history_len=0, engine="stream_run", modal=False, resumed=True,
        )

        def boom(*a, **k):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(agent._checkpoint, "_commit_raw", boom)
        assert agent.flush_checkpoint("sigint") == 0   # 不抛

    async def test_esc_path_marks_reason_esc(self, irq_env):
        """Esc 打断的 reason 应记成 esc（经 note 登记来源）。"""
        ws, _ = irq_env
        store = SessionStore.create(ws, "m", session_id="s1")
        agent = _agent(ws, store=store)
        agent.enable_interrupts()
        agent.note_interrupt("esc")
        agent._checkpoint.begin_turn(
            SimpleNamespace(tool_rounds=0),
            [{"role": "user", "content": "go"}],
            history_len=0, engine="stream_run", modal=False, resumed=True,
        )

        agent.flush_checkpoint(agent.interrupt.kind_name())
        record = CheckpointStore(store.path).read()
        assert record is not None and record.reason == "esc"
        agent.clear_interrupt()


class TestSignalInstallLifecycle:
    """enable_interrupts / set_interrupt_target / clear_interrupt 接线。"""

    async def test_agent_helpers_are_safe_without_controller(self, irq_env):
        """未安装控制器时，登记/清理/通知都应是安静的 no-op。"""
        ws, _ = irq_env
        agent = _agent(ws, store=SessionStore.create(ws, "m", session_id="s1"))
        assert agent.interrupt is None
        agent.set_interrupt_target(None)     # 不抛
        agent.note_interrupt("esc")          # 不抛
        agent.clear_interrupt()              # 不抛

    async def test_target_registration_and_clearing(self, irq_env):
        ws, _ = irq_env
        agent = _agent(ws, store=SessionStore.create(ws, "m", session_id="s1"))
        ctl = agent.enable_interrupts()
        assert agent.interrupt is ctl

        cancelled = []

        async def body():
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        task = asyncio.ensure_future(body())
        await asyncio.sleep(0)
        agent.set_interrupt_target(task)

        agent.interrupt.request("client")
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled == [True]

        agent.clear_interrupt()
        assert agent.interrupt.pending is None
        ctl.disarm()
