"""退出 token 用量统计回归测试。

覆盖本次功能的四条链：
- provider 透出的 cached_tokens 经 ``run()``（非流式）与 ``stream_run()``
  各自累计到 agent 计数器（total_input/output/cached/plugin）；
- 插件 token 走装配预算口径：每轮 LLM 调用把 ``_active_plugin_schema_tokens()``
  （测试里 monkeypatch 成常量 400）记一笔——多轮即多次累加；
- ``session_token_usage()`` 汇总四项，供 /cost 与退出面板；
- 真实 SessionStore 落盘 + ``load_session`` 恢复时 cached/plugin 计数无损。

SESSIONS_DIR 与 hooks SETTINGS_PATH 均 monkeypatch 到 tmp_path，
绝不触碰真实 ~/.openx。

运行：``python -m pytest tests/orchestration/test_token_usage.py -q``
"""

from __future__ import annotations

import pytest

from openx.config import OpenXConfig
from openx.llm import StreamDone
from openx.orchestration.sessions import SessionStore
from openx.permissions import PermissionRules

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


def _make_agent(tmp_path, responses, session_store=None, session_id=None):
    """构造挂载 FakeLLM 的 OpenXAgent（绕过真实 API 与 settings.json）。"""
    from openx.agent import OpenXAgent

    config = OpenXConfig()
    config.workspace = str(tmp_path)
    config.api_key = "sk-test"
    config.api_base = "https://example.com/v1"
    config.model = "test-model"
    agent = OpenXAgent(
        config, session_store=session_store, session_id=session_id,
    )
    agent.llm = FakeLLM(responses)
    agent.tool_executor._rules = PermissionRules()  # 忽略真实 settings.json
    return agent


class _UsageLLM(FakeLLM):
    """非流式替身：每次 chat() 都带服务端 usage（含 cached_tokens）。"""

    async def chat(self, messages, tools=None, stream=True):
        resp = await super().chat(messages, tools, stream)
        resp["usage"] = {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "cached_tokens": 3,
        }
        return resp


class _StreamCachedLLM(FakeLLM):
    """流式替身：StreamDone 携带 cached_tokens（provider 透出的统计）。"""

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


@pytest.fixture
def plugin_400(monkeypatch):
    """把装配预算口径的插件单轮 schema 开销钉死为 400 tokens。"""
    monkeypatch.setattr(
        "openx.agent._active_plugin_schema_tokens", lambda: 400
    )


# ── 1. run()（非流式）累计 cached + plugin ──────────────────────


class TestRunAccounting:
    @pytest.mark.asyncio
    async def test_run_accumulates_cached_and_plugin_per_call(
        self, tmp_path, sessions_tmp, plugin_400
    ):
        agent = _make_agent(tmp_path, [("first", None), ("second", None)])
        agent.llm = _UsageLLM([("first", None), ("second", None)])

        out1 = await agent.run("q1")
        out2 = await agent.run("q2")
        assert out1 == "first" and out2 == "second"

        # 两次 LLM 调用各记：usage(11/7/3) + 插件 schema 400
        assert agent.total_input_tokens == 22
        assert agent.total_output_tokens == 14
        assert agent.total_cached_tokens == 6
        assert agent.total_plugin_tokens == 800
        # usage 字段绝不该泄漏进历史（OpenAI API 会拒绝非法字段）
        assert all("usage" not in m for m in agent.history.messages)

        assert agent.session_token_usage() == {
            "input": 22, "output": 14, "cached": 6, "plugin": 800,
        }

    @pytest.mark.asyncio
    async def test_no_usage_no_plugins_yields_zero_estimates(
        self, tmp_path, sessions_tmp
    ):
        # 假 LLM 不带 usage、未钉插件开销 → cached/plugin 恒 0（后端未报告
        # 或无插件装载），session_token_usage 仍给全四项、展示不炸。
        agent = _make_agent(tmp_path, [("fine", None)])
        await agent.run("hello")

        assert agent.total_cached_tokens == 0
        assert agent.total_plugin_tokens == 0
        assert agent.session_token_usage() == {
            "input": agent.total_input_tokens,
            "output": agent.total_output_tokens,
            "cached": 0,
            "plugin": 0,
        }


# ── 2. stream_run()（流式，REPL 主路径）累计 cached + plugin ─────


class TestStreamAccounting:
    @pytest.mark.asyncio
    async def test_stream_run_accumulates_cached_and_plugin(
        self, tmp_path, sessions_tmp, plugin_400
    ):
        agent = _make_agent(tmp_path, [("streamed answer", None)])
        agent.llm = _StreamCachedLLM([("streamed answer", None)])

        chunks = [c async for c in agent.stream_run("stream me")]
        assert "".join(chunks).startswith("streamed")

        assert agent.total_input_tokens == 10
        assert agent.total_output_tokens == 5
        assert agent.total_cached_tokens == 4
        assert agent.total_plugin_tokens == 400
        assert agent.session_token_usage()["cached"] == 4


# ── 3. 持久化与恢复 ──────────────────────────────────────────────


class TestPersistRestore:
    """cached/plugin 计数随会话落盘，load_session 无损恢复。"""

    @pytest.mark.asyncio
    async def test_plugin_and_cached_persist_across_session(
        self, tmp_path, sessions_tmp, plugin_400
    ):
        ws = str(tmp_path)
        store = SessionStore.create(ws, "test-model", session_id="tok-sess")
        agent = _make_agent(
            tmp_path, [("first", None), ("second", None)],
            session_store=store, session_id="tok-sess",
        )
        agent.llm = _UsageLLM([("first", None), ("second", None)])
        await agent.run("q1")
        await agent.run("q2")
        assert agent.total_cached_tokens == 6
        assert agent.total_plugin_tokens == 800

        meta, messages = SessionStore.load(store.path)
        assert meta.total_cached_tokens == agent.total_cached_tokens
        assert meta.total_plugin_tokens == agent.total_plugin_tokens

        # 第二个 agent 从文件恢复：四项计数原样还原
        agent2 = _make_agent(tmp_path, [])
        agent2.load_session(meta, messages)
        assert agent2.total_input_tokens == agent.total_input_tokens
        assert agent2.total_output_tokens == agent.total_output_tokens
        assert agent2.total_cached_tokens == agent.total_cached_tokens
        assert agent2.total_plugin_tokens == agent.total_plugin_tokens
        assert agent2.session_token_usage() == agent.session_token_usage()
