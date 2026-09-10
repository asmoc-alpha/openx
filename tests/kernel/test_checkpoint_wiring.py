"""容灾接线：agent 回合里的 checkpoint 提交、恢复与生命周期钩子。

**头号用例**（``test_mid_turn_crash_then_resume_does_not_replay``）：一个
工具轮已提交后进程"崩溃"，重建 agent 恢复 -> 那个工具**调用次数仍是 1**。

覆盖：轮次提交时机 / 在途轮不重跑 / 陈旧与撕裂 / 写盘失败不打断回合 /
回合收口删旁挂 / 资源闸触顶可续跑 / on_checkpoint 与 on_resume 钩子 /
零参钩子向后兼容 / 钩子异常隔离。

SESSIONS_DIR 与 SETTINGS_PATH 均 monkeypatch 到 tmp_path，绝不触碰真实
``~/.openx``。运行：``python -m pytest tests/kernel/test_checkpoint_wiring.py -q``
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import openx.orchestration.sessions as sessions_mod
from openx.agent import OpenXAgent
from openx.config import OpenXConfig
from openx.kernel import get_kernel, reset_kernel
from openx.kernel.recovery import (
    CheckpointStore,
    ResumeVerdict,
    REASON_IO,
)
from openx.orchestration.sessions import SessionStore
from openx.permissions import Permission, PermissionLevel, PermissionRules
from openx.tools.base import Tool, ToolResult

from ..test_bugfixes import FakeConsole, FakeLLM


# ── 工具替身：记录调用次数（"不重放"的观测点）──────────────────


class CountingTool(Tool):
    """每被真正执行一次就 +1--断言"已完成的调用没有被重跑"。"""

    name = "echo"
    description = "echo back"
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self) -> None:
        self.calls: list[dict] = []

    @property
    def permission(self) -> Permission:
        return Permission(level=PermissionLevel.ALLOW)

    async def execute(self, **kwargs) -> ToolResult:
        self.calls.append(dict(kwargs))
        return ToolResult(output=f"echo#{len(self.calls)}")


def _tool_call(call_id: str, name: str = "echo") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


# ── fixtures ────────────────────────────────────────────────────


@pytest.fixture
def recovery_env(tmp_path, monkeypatch):
    """隔离会话目录 / settings / 内核，返回 (workspace, sessions_root)。"""
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


def _make_agent(workspace: str, responses, store=None, session_id=None):
    """构造挂 FakeLLM + 计数工具的 agent（绕过真实 API 与 settings）。"""
    config = OpenXConfig()
    config.workspace = workspace
    config.model = "test-model"
    agent = OpenXAgent(
        config,
        session_store=store,
        session_id=session_id,
        console=FakeConsole(),
    )
    tool = CountingTool()
    agent.tools[tool.name] = tool
    agent.tool_schemas = agent._compute_tool_schemas()
    agent.llm = FakeLLM(responses)
    agent.tool_executor._rules = PermissionRules()
    return agent, tool


async def _drain(agent, message, **kw) -> list[str]:
    """消费一轮流，只留文本 chunk（工具事件是结构化对象，另走展示层）。"""
    return [
        chunk async for chunk in agent.stream_run(message, **kw)
        if isinstance(chunk, str)
    ]


def _sidecar(store: SessionStore) -> CheckpointStore:
    return CheckpointStore(store.path)


# ── 提交时机 ────────────────────────────────────────────────────


class TestCommitTiming:
    """每个工具轮收口提交一次；回合收口删除。"""

    async def test_commit_after_each_tool_round(self, recovery_env):
        ws, _ = recovery_env
        store = SessionStore.create(ws, "m", session_id="s1")
        agent, tool = _make_agent(ws, [
            (None, [_tool_call("t1")]),      # 第 1 轮：调工具
            ("all done", None),              # 第 2 轮：最终回复
        ], store=store, session_id="s1")

        await _drain(agent, "go")

        assert len(tool.calls) == 1
        # 回合正常收口 -> 旁挂文件被删（"无陈旧 checkpoint"是默认状态）
        assert not _sidecar(store).path.exists()
        # 账本留下完整叙事：round 事件 + turn_end 终局事实
        types = _ledger_types(store)
        assert types.count("checkpoint") >= 2
        assert "turn_started" in types
        assert _sidecar(store).read() is None

    async def test_gate_trip_emits_event_and_keeps_checkpoint(self, recovery_env):
        """收尾请求也失败时（触顶且拿不到总结），保留旁挂文件供恢复。"""
        ws, _ = recovery_env
        store = SessionStore.create(ws, "m", session_id="s1")
        agent, _ = _make_agent(ws, [
            (None, [_tool_call("t1")]),
        ], store=store, session_id="s1")
        agent.config.max_tool_rounds = 1

        await _drain(agent, "go")

        assert "resource_gate_tripped" in _ledger_types(store)
        record = _sidecar(store).read()
        assert record is not None
        assert record.reason == "gate_tripped"


# ── 头号用例：崩后恢复不重放 ────────────────────────────────────


class TestResumeDoesNotReplay:
    """已完成的工具调用绝不重跑--这是整个容灾模块的硬承诺。"""

    async def test_mid_turn_crash_then_resume_does_not_replay(self, recovery_env):
        ws, _ = recovery_env
        store = SessionStore.create(ws, "m", session_id="s1")

        # ── 第一阶段：跑完 1 轮工具后"崩溃"（第 2 次请求抛异常）──
        class CrashingLLM(FakeLLM):
            async def stream_chat(self, messages, tools=None):
                if self.call_count >= 1:
                    raise KeyboardInterrupt("simulated crash")
                async for chunk in super().stream_chat(messages, tools):
                    yield chunk

        agent, tool = _make_agent(ws, [
            (None, [_tool_call("t1")]),
        ], store=store, session_id="s1")
        agent.llm = CrashingLLM([(None, [_tool_call("t1")])])

        with pytest.raises(KeyboardInterrupt):
            await _drain(agent, "go")

        assert len(tool.calls) == 1, "第 1 轮工具应已执行"
        record = _sidecar(store).read()
        assert record is not None, "崩溃后应留下可续跑的 checkpoint"
        assert record.ledger_seq > 0

        # ── 第二阶段：重建 agent（模拟重启），恢复 ──
        store2 = SessionStore.open(store.meta)
        agent2, tool2 = _make_agent(
            ws, [("all done", None)], store=store2, session_id="s1"
        )
        plan = agent2.recover_session()

        assert plan.verdict is ResumeVerdict.OK, plan.detail
        assert plan.tool_rounds == 1
        # 已完成那一轮的 assistant + tool 结果都在，序列 provider 合法
        roles = [m["role"] for m in plan.new_turn]
        assert roles == ["user", "assistant", "tool"]

        # ── 关键断言：续跑**不重跑**已完成的工具调用 ──
        chunks = await _drain(agent2, "", resume=plan)
        assert "".join(chunks).strip()
        assert len(tool2.calls) == 0, (
            "恢复后不得重放已完成的工具调用（计数工具应零新增执行）"
        )
        assert agent2.last_tool_rounds == 1

    async def test_inflight_round_is_not_replayed_either(self, recovery_env):
        """崩在工具执行**中途**：那些调用结果未知 -> 补合成结果、不重跑。"""
        ws, _ = recovery_env
        store = SessionStore.create(ws, "m", session_id="s1")
        agent, tool = _make_agent(ws, [
            (None, [_tool_call("t1")]),
        ], store=store, session_id="s1")
        # 模拟"在途"：直接落一份带在途标记的 checkpoint（等价于崩在 gather 里）
        agent._checkpoint.begin_turn(
            SimpleNamespace(tool_rounds=0),
            [
                {"role": "user", "content": "go"},
                {"role": "assistant", "content": "", "tool_calls": [_tool_call("t1")]},
            ],
            history_len=0, engine="stream_run", modal=False, resumed=True,
        )
        agent._checkpoint.mark_inflight()
        assert _sidecar(store).read() is not None

        agent2, tool2 = _make_agent(
            ws, [("recovered", None)], store=SessionStore.open(store.meta),
            session_id="s1",
        )
        plan = agent2.recover_session()
        assert plan.verdict is ResumeVerdict.OK, plan.detail
        assert plan.repaired_calls == ["t1"]
        assert plan.tool_rounds == 1        # 在途轮算已消耗

        await _drain(agent2, "", resume=plan)
        assert len(tool2.calls) == 0, "在途调用结果未知，绝不重跑"

    async def test_stale_checkpoint_is_not_resumed(self, recovery_env):
        """回合其实已落盘（崩在持久化与删文件之间）-> 不重复补齐。"""
        ws, _ = recovery_env
        store = SessionStore.create(ws, "m", session_id="s1")
        agent, _ = _make_agent(ws, [
            (None, [_tool_call("t1")]),
            ("done", None),
        ], store=store, session_id="s1")
        await _drain(agent, "go")

        # 手工放回一份"陈旧"的旁挂文件（模拟崩溃时序）
        snap = agent._checkpoint.snapshot(
            SimpleNamespace(tool_rounds=1),
            [
                {"role": "user", "content": "go"},
                {"role": "assistant", "content": "", "tool_calls": [_tool_call("t1")]},
                {"role": "tool", "tool_call_id": "t1", "content": "echo#1"},
            ],
        )
        from openx.kernel.recovery import CheckpointRecord, snapshot_digest

        _sidecar(store).write(CheckpointRecord(
            session_id="s1", workspace=ws, ledger_seq=1,
            snapshot_digest=snapshot_digest(snap), snapshot=snap,
        ))

        agent2, _ = _make_agent(ws, [("x", None)], store=SessionStore.open(store.meta),
                                session_id="s1")
        from openx.orchestration.sessions import SessionStore as SS

        _, messages = SS.load(store.path)
        agent2.load_session(store.meta, messages)
        plan = agent2.recover_session()
        assert plan.verdict is ResumeVerdict.ALREADY_COMPLETE
        assert not plan.usable


# ── 失败不打断回合 ──────────────────────────────────────────────


class TestFailureIsolation:
    """checkpoint 写盘失败绝不能打断对话（持久化是优化）。"""

    async def test_write_failure_never_breaks_turn(self, recovery_env, monkeypatch):
        ws, _ = recovery_env
        store = SessionStore.create(ws, "m", session_id="s1")
        agent, tool = _make_agent(ws, [
            (None, [_tool_call("t1")]),
            ("all done", None),
        ], store=store, session_id="s1")

        monkeypatch.setattr(CheckpointStore, "write", lambda self, rec: REASON_IO)

        chunks = await _drain(agent, "go")
        assert "all done" in "".join(chunks)
        assert len(tool.calls) == 1        # 工具照常执行
        # 失败被如实记成 skipped（下次裁决判 ABSENT 而不是撕裂）
        assert "checkpoint" in _ledger_types(store)

    async def test_commit_exception_is_swallowed(self, recovery_env, monkeypatch):
        ws, _ = recovery_env
        store = SessionStore.create(ws, "m", session_id="s1")
        agent, _ = _make_agent(ws, [
            (None, [_tool_call("t1")]),
            ("all done", None),
        ], store=store, session_id="s1")

        def boom(*a, **k):
            raise RuntimeError("checkpoint exploded")

        monkeypatch.setattr(agent._checkpoint, "commit", boom)
        chunks = await _drain(agent, "go")
        assert "all done" in "".join(chunks)


# ── 生命周期钩子接线 ────────────────────────────────────────────


class TestLifecycleHooks:
    """on_checkpoint / on_resume：契约零参 + 可选 payload，异常隔离。"""

    async def test_checkpoint_and_resume_hooks_fire(self, recovery_env):
        ws, _ = recovery_env
        store = SessionStore.create(ws, "m", session_id="s1")
        seen: list[dict] = []

        from openx.kernel.assembly.context import LifecycleHooks

        hooks = LifecycleHooks(
            on_checkpoint=lambda payload: seen.append(("checkpoint", payload)),
            on_resume=lambda payload: seen.append(("resume", payload)),
        )

        agent, _ = _make_agent(ws, [
            (None, [_tool_call("t1")]),
            ("done", None),
        ], store=store, session_id="s1")
        _register_hooks(hooks)   # 必须在 agent 构造之后（它会重载注册表）

        await _drain(agent, "go")
        events = [name for name, _ in seen]
        assert "checkpoint" in events
        # 一个工具轮**落两次盘**：执行前的在途标记（phase=inflight），
        # 与结果收口后的提交（phase=committed）。钩子对两者都触发--它们
        # 都是真实的持久化边界，插件该知道。
        payloads = [p for name, p in seen if name == "checkpoint"]
        assert {p["phase"] for p in payloads} == {"inflight", "committed"}
        committed = next(p for p in payloads if p["phase"] == "committed")
        assert committed["reason"] == "tool_round"
        assert committed["ledger_seq"] > 0
        assert committed["tool_rounds"] == 1
        assert committed["session_id"] == "s1"
        # 在途标记记的是"该轮尚未收口"
        inflight = next(p for p in payloads if p["phase"] == "inflight")
        assert inflight["tool_rounds"] == 0

        # 恢复侧：账本里造一份 checkpoint 后恢复，on_resume 应触发
        agent._checkpoint.begin_turn(
            SimpleNamespace(tool_rounds=1),   # 一轮已收口
            [{"role": "user", "content": "go"},
             {"role": "assistant", "content": "", "tool_calls": [_tool_call("t1")]},
             {"role": "tool", "tool_call_id": "t1", "content": "echo#1"}],
            history_len=0, engine="stream_run", modal=False, resumed=True,
        )
        agent._checkpoint.commit()
        seen.clear()      # 只关心恢复触发的钩子（上面那次 commit 也算 checkpoint）

        agent2, _ = _make_agent(ws, [("x", None)],
                                store=SessionStore.open(store.meta), session_id="s1")
        assert agent2.recover_session().verdict is ResumeVerdict.OK
        assert [name for name, _ in seen] == ["resume"]
        assert seen[0][1]["tool_rounds"] == 1

    async def test_zero_arg_hook_still_works(self, recovery_env):
        """向后兼容：历史上零参的钩子不该因为 payload 而失效。"""
        ws, _ = recovery_env
        store = SessionStore.create(ws, "m", session_id="s1")
        fired: list[str] = []

        from openx.kernel.assembly.context import LifecycleHooks

        agent, _ = _make_agent(ws, [
            (None, [_tool_call("t1")]),
            ("done", None),
        ], store=store, session_id="s1")
        _register_hooks(LifecycleHooks(on_checkpoint=lambda: fired.append("hit")))
        await _drain(agent, "go")
        assert fired, "零参 on_checkpoint 必须照常被调用"

    async def test_hook_exception_is_isolated(self, recovery_env):
        ws, _ = recovery_env
        store = SessionStore.create(ws, "m", session_id="s1")

        def boom(payload=None):
            raise RuntimeError("hook exploded")

        from openx.kernel.assembly.context import LifecycleHooks

        agent, tool = _make_agent(ws, [
            (None, [_tool_call("t1")]),
            ("all done", None),
        ], store=store, session_id="s1")
        _register_hooks(LifecycleHooks(on_checkpoint=boom))

        chunks = await _drain(agent, "go")
        assert "all done" in "".join(chunks)
        assert len(tool.calls) == 1
        # 插件异常记成 plugin_error（observation），不炸主流程
        assert "plugin_error" in _ledger_types(store)


# ── 助手 ────────────────────────────────────────────────────────


def _register_hooks(hooks) -> None:
    """把一份 LifecycleHooks 装进内核（用后由 fixture 的 reset_kernel 清掉）。

    **必须在构造 agent 之后调用**：agent 初始化会触发 ``ensure_loaded``，
    而 ``_reload`` 会重建全部注册表--先注册的会被清掉。这也正是"插件注册
    只在内核装配期与动态插入期发生"的结构性后果。
    """
    kernel = get_kernel()
    registry = kernel.registry("lifecycle")
    registry.register("test-hooks", hooks, "test-plugin")


def _ledger_types(store: SessionStore) -> list[str]:
    """会话文件里所有账本行的 type（读盘不依赖内核挂接）。"""
    try:
        lines = store.path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    types: list[str] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            line = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(line, dict) and "seq" in line and "digest" in line:
            types.append(str(line.get("type", "")))
    return types
