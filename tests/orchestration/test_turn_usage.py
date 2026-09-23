"""P-E 轨迹升级 · ``turn_usage`` 成本字段（agent → 会话账本）。

覆盖：
- ``run()``（非流式）与 ``stream_run()``（流式、REPL 主路径）**每回合一条**
  ``turn_usage``，字段为回合**增量** token + 整轮时长；
- ``turn_index`` 跨回合递增；
- **子代理守卫**：无 ``session_store`` 的 agent 不 emit（共享同一内核，否则
  会串写进父会话账本——与 ``attach_ledger`` / CheckpointManager 同一条纪律）；
- 回合内**抛异常**时 ``try/finally`` 仍补记（P-E 的健壮性承诺）。

SESSIONS_DIR 与 hooks SETTINGS_PATH 均 monkeypatch 到 tmp_path，绝不触碰真实
``~/.openx``。运行：``python -m pytest tests/orchestration/test_turn_usage.py -q``
"""

from __future__ import annotations

import json

import pytest

from openx.config import OpenXConfig
from openx.llm import StreamDone
from openx.orchestration.sessions import SessionStore
from openx.permissions import Permission, PermissionLevel, PermissionRules
from openx.tools.base import Tool, ToolResult

from ..test_bugfixes import FakeLLM


@pytest.fixture
def sessions_tmp(tmp_path, monkeypatch):
    """隔离会话目录与全局 settings.json（agent 构造会读后者）。"""
    monkeypatch.setattr(
        "openx.orchestration.sessions.SESSIONS_DIR", tmp_path / "sessions"
    )
    monkeypatch.setattr(
        "openx.kernel.audit.hooks.SETTINGS_PATH", tmp_path / "no-such-settings.json"
    )
    return tmp_path / "sessions"


def _make_agent(tmp_path, responses, store=None, sid=None):
    """构造挂载 FakeLLM 的 OpenXAgent（绕过真实 API 与 settings.json）。"""
    from openx.agent import OpenXAgent

    config = OpenXConfig()
    config.workspace = str(tmp_path)
    config.model = "test-model"
    agent = OpenXAgent(config, session_store=store, session_id=sid)
    agent.llm = FakeLLM(responses)
    agent.tool_executor._rules = PermissionRules()  # 忽略真实 settings.json
    return agent


def _turn_usages(store: SessionStore) -> list[dict]:
    """从会话文件读出全部 turn_usage 事件（账本行投影）。"""
    return [e for e in SessionStore.iter_events(store.path)
            if e.get("type") == "turn_usage"]


class _UsageLLM(FakeLLM):
    """非流式替身：chat() 带服务端 usage（prompt/completion/cached）。"""

    async def chat(self, messages, tools=None, stream=True):
        resp = await super().chat(messages, tools, stream)
        resp["usage"] = {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "cached_tokens": 3,
        }
        return resp


class _StreamUsageLLM(FakeLLM):
    """流式替身：StreamDone 携带 input/cached token。"""

    async def stream_chat(self, messages, tools=None):
        content, tool_calls = self.responses[self.call_count]
        self.call_count += 1
        if content:
            for tok in content.split():
                yield tok + " "
        resp = {"role": "assistant", "content": content or None}
        if tool_calls:
            resp["tool_calls"] = tool_calls
        yield StreamDone(
            response=resp, token_count=5, input_tokens=10, cached_tokens=4,
        )


class _RaisingStreamLLM(FakeLLM):
    """第 1 轮收口（用量计入），第 2 轮抛异常：验证 try/finally 仍记一条。"""

    async def stream_chat(self, messages, tools=None):
        if self.call_count >= 1:
            self.call_count += 1
            raise RuntimeError("boom on round 2")
        content, tool_calls = self.responses[0]
        self.call_count += 1
        resp = {"role": "assistant", "content": content or None}
        if tool_calls:
            resp["tool_calls"] = tool_calls
        yield StreamDone(
            response=resp, token_count=5, input_tokens=10, cached_tokens=4,
        )


class _EchoTool(Tool):
    """ALLOW 级空工具：给第 1 轮一个可执行的工具调用（tool/v1）。"""

    name = "echo"
    description = "echo back"
    parameters = {"type": "object", "properties": {}, "required": []}

    @property
    def permission(self) -> Permission:
        return Permission(level=PermissionLevel.ALLOW)

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(output="echo")


# ── 1. 两条运行路径都记成本字段 ──────────────────────────────────


class TestTurnUsageEmission:
    @pytest.mark.asyncio
    async def test_run_emits_turn_usage(self, tmp_path, sessions_tmp):
        ws = str(tmp_path)
        store = SessionStore.create(ws, "test-model", session_id="tu-run")
        agent = _make_agent(tmp_path, [("first", None)], store=store, sid="tu-run")
        agent.llm = _UsageLLM([("first", None)])

        await agent.run("q1")

        usages = _turn_usages(store)
        assert len(usages) == 1
        u = usages[0]
        assert u["session_id"] == "tu-run"
        assert u["turn_index"] == 1
        assert (u["input_tokens"], u["output_tokens"]) == (11, 7)
        assert u["cached_tokens"] == 3
        assert u["duration_ms"] >= 0
        assert u["plugin_tokens"] >= 0

    @pytest.mark.asyncio
    async def test_stream_run_emits_turn_usage(self, tmp_path, sessions_tmp):
        ws = str(tmp_path)
        store = SessionStore.create(ws, "test-model", session_id="tu-stream")
        agent = _make_agent(tmp_path, [("streamed", None)], store=store, sid="tu-stream")
        agent.llm = _StreamUsageLLM([("streamed", None)])

        chunks = [c async for c in agent.stream_run("q")]
        assert "".join(chunks).strip().startswith("streamed")

        usages = _turn_usages(store)
        assert len(usages) == 1
        assert (usages[0]["input_tokens"], usages[0]["output_tokens"]) == (10, 5)
        assert usages[0]["cached_tokens"] == 4

    @pytest.mark.asyncio
    async def test_turn_index_increments_across_turns(
        self, tmp_path, sessions_tmp
    ):
        ws = str(tmp_path)
        store = SessionStore.create(ws, "test-model", session_id="tu-idx")
        agent = _make_agent(
            tmp_path, [("a", None), ("b", None)], store=store, sid="tu-idx"
        )
        agent.llm = _UsageLLM([("a", None), ("b", None)])

        await agent.run("q1")
        await agent.run("q2")

        indexes = [u["turn_index"] for u in _turn_usages(store)]
        assert indexes == [1, 2]
        # 第二条是**增量**（不是累计）：每次 11/7
        assert [u["input_tokens"] for u in _turn_usages(store)] == [11, 11]


# ── 2. 子代理守卫：无 session_store 不 emit ──────────────────────


@pytest.mark.asyncio
async def test_no_session_store_emits_nothing(tmp_path, monkeypatch):
    """子代理（session_store=None）的回合**不**产生 turn_usage。

    内核是进程级单例：若不过这道闸，子代理的 emit 会串写进父会话账本。
    """
    from openx.kernel import get_kernel, reset_kernel

    monkeypatch.setattr(
        "openx.orchestration.sessions.SESSIONS_DIR", tmp_path / "sessions"
    )
    monkeypatch.setattr(
        "openx.kernel.audit.hooks.SETTINGS_PATH", tmp_path / "no-such-settings.json"
    )
    reset_kernel()
    seen: list = []
    get_kernel().attach_ledger(lambda e: seen.append(e.type), session="parent")

    agent = _make_agent(tmp_path, [("child", None)])  # store=None → 子代理语义
    agent.llm = _UsageLLM([("child", None)])
    await agent.run("subtask")

    assert "turn_usage" not in seen  # 守卫生效
    reset_kernel()


# ── 3. try/finally：回合内异常仍补记 ─────────────────────────────


@pytest.mark.asyncio
async def test_exception_inside_turn_still_records_usage(tmp_path, sessions_tmp):
    ws = str(tmp_path)
    store = SessionStore.create(ws, "test-model", session_id="tu-err")
    agent = _make_agent(tmp_path, [(None, None)], store=store, sid="tu-err")
    echo = _EchoTool()
    agent.tools[echo.name] = echo
    agent.tool_schemas = agent._compute_tool_schemas()
    # 第 1 轮：一个工具调用（用量计入）→ 第 2 轮 LLM 抛异常
    agent.llm = _RaisingStreamLLM([(None, [{
        "id": "c1", "type": "function",
        "function": {"name": "echo", "arguments": "{}"},
    }])])

    with pytest.raises(RuntimeError, match="boom"):
        async for _ in agent.stream_run("q"):
            pass

    usages = _turn_usages(store)
    assert len(usages) == 1
    assert usages[0]["input_tokens"] == 10  # 第 1 轮已收口的用量仍留痕


# ── 4. 端到端：真实回合 → 会话文件 → eval 导出 ────────────────────


@pytest.mark.asyncio
async def test_turn_then_eval_export_roundtrip(tmp_path, sessions_tmp):
    """一整条链：跑一轮（写 turn_usage）→ /export-eval 导出含成本字段的记录。"""
    from openx.services.eval_export import export_workspace

    ws = str(tmp_path)
    store = SessionStore.create(ws, "test-model", session_id="e2e")
    agent = _make_agent(tmp_path, [("done", None)], store=store, sid="e2e")
    agent.llm = _UsageLLM([("done", None)])
    await agent.run("do it")

    out = tmp_path / "eval.jsonl"
    _, count = export_workspace(ws, out_path=out)
    assert count == 1
    rec = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert rec["session_id"] == "e2e"
    turn = rec["turns"][0]
    assert turn["user"] == "do it" and turn["assistant"] == "done"
    assert turn["input_tokens"] == 11 and turn["output_tokens"] == 7  # 成本字段随行
