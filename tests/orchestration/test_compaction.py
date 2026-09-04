"""Phase 2 测试：自动压缩（auto-compaction）及配套修复。

覆盖：compact 按轮（user 边界）切割、agent 超阈值自动触发、低于阈值不误触、
压缩失败绝不打断回合、run()（非流式）token 累计不再恒为 0。

运行：``python -m pytest tests/test_compaction.py -q``
"""

from __future__ import annotations

import pytest

from openx.config import OpenXConfig
from openx.orchestration.history import ConversationHistory, SUMMARY_MARKER
from openx.llm import StreamDone


# ── FakeLLM：按序返回脚本响应（回合响应 + 压缩摘要响应）────────────

class FakeLLM:
    """可脚本化的假 LLM：按顺序返回预设响应。"""

    def __init__(self, responses):
        self.responses = list(responses)  # list of (content, tool_calls)
        self.call_count = 0

    async def stream_chat(self, messages, tools=None):
        content, tool_calls = self.responses[self.call_count]
        self.call_count += 1
        if content:
            for tok in content.split():
                yield tok + " "
        resp = {"role": "assistant", "content": content or None}
        if tool_calls:
            resp["tool_calls"] = tool_calls
        yield StreamDone(response=resp, token_count=5, input_tokens=10)

    async def chat(self, messages, tools=None, stream=True):
        content, tool_calls = self.responses[self.call_count]
        self.call_count += 1
        resp = {"role": "assistant", "content": content}
        if tool_calls:
            resp["tool_calls"] = tool_calls
        return resp


def _make_agent(tmp_path, responses, max_history_tokens=100_000):
    """构造一个挂载 FakeLLM 的 OpenXAgent（绕过真实 API）。"""
    from openx.agent import OpenXAgent
    config = OpenXConfig()
    config.workspace = str(tmp_path)
    config.model = "test-model"
    config.max_history_tokens = max_history_tokens  # 先设再构造：history 同步拿到上限
    agent = OpenXAgent(config)
    agent.llm = FakeLLM(responses)
    return agent


def _full_turn(n: int) -> list[dict]:
    """完整一轮：user / assistant(tool_calls) / tool / assistant 最终回复。"""
    return [
        {"role": "user", "content": f"question {n}"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"call_{n}", "type": "function",
             "function": {"name": "shell", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": f"call_{n}", "content": f"result {n}"},
        {"role": "assistant", "content": f"answer {n}"},
    ]


def _seed_five_short_turns(agent) -> None:
    """预置 5 轮极短对话（共 ~5 token），不触发 fit 裁剪，但凑够压缩的轮数。"""
    for n in range(1, 6):
        agent.history.add([
            {"role": "user", "content": f"q{n}"},
            {"role": "assistant", "content": f"a{n}"},
        ])


# ── 1. 按轮切割 ─────────────────────────────────────────────────

class TestTurnBoundaryCut:
    """4 个完整轮 + keep_last=2：恰好保留最后 2 轮，摘要消息居首。"""

    @pytest.mark.asyncio
    async def test_keeps_last_two_turns_plus_summary(self):
        h = ConversationHistory()
        for n in (1, 2, 3, 4):
            h.messages.extend(_full_turn(n))
        assert len(h.messages) == 16

        llm = FakeLLM([("summary of the older turns", None)])
        summary = await h.compact(llm, keep_last=2)

        assert summary == "summary of the older turns"
        # 摘要哨兵居首（user 角色），随后是最近 2 轮的 8 条原始消息
        assert h.messages[0]["role"] == "user"
        assert h.messages[0]["content"].startswith(SUMMARY_MARKER)
        assert len(h.messages) == 1 + 8
        # 合法序列：validate 通过、无孤立 tool 消息，tool 对成对保留
        assert h.validate()
        tool_ids = [m["tool_call_id"] for m in h.messages if m["role"] == "tool"]
        assert tool_ids == ["call_3", "call_4"]
        # 旧轮（1、2）已消失，近轮（3、4）原文保留
        user_text = " ".join(
            m["content"] for m in h.messages
            if m["role"] == "user" and isinstance(m["content"], str)
        )
        assert "question 1" not in user_text and "question 2" not in user_text
        assert "question 3" in user_text and "question 4" in user_text


# ── 2. 超阈值自动触发 ───────────────────────────────────────────

class TestAutoCompactionTrigger:
    """估算 token 超过 max_history_tokens*0.8 时，两个循环都自动压缩。"""

    @pytest.mark.asyncio
    async def test_stream_run_compacts_over_threshold(self, tmp_path):
        # 脚本：① 本回合最终回复（stream_chat）② 压缩摘要（compact → chat）
        agent = _make_agent(
            tmp_path,
            [("Final answer", None), ("summary of earlier chat", None)],
            max_history_tokens=200,
        )
        _seed_five_short_turns(agent)
        bulky = "x" * 720  # ~188 token：>160（阈值）且 <200（fit 上限，不抢先裁剪）

        out = []
        async for chunk in agent.stream_run(bulky):
            out.append(chunk)
        joined = "".join(out)

        assert "Final answer" in joined
        assert "Compacting conversation" in joined  # UI 通知行
        assert agent.history.messages[0]["content"].startswith(SUMMARY_MARKER)
        assert agent.history.validate()
        assert agent.llm.call_count == 2  # 回合 + 摘要，无多余调用

    @pytest.mark.asyncio
    async def test_run_compacts_over_threshold(self, tmp_path):
        agent = _make_agent(
            tmp_path,
            [("Non-stream answer", None), ("summary of earlier chat", None)],
            max_history_tokens=200,
        )
        _seed_five_short_turns(agent)
        out = await agent.run("y" * 720)

        assert out == "Non-stream answer"
        assert agent.history.messages[0]["content"].startswith(SUMMARY_MARKER)
        assert agent.history.validate()
        assert agent.llm.call_count == 2


# ── 3. 低于阈值不误触 ───────────────────────────────────────────

class TestAutoCompactionNoTrigger:
    """大预算下不压缩：消息数不变、不多吃脚本响应。"""

    @pytest.mark.asyncio
    async def test_large_budget_no_compaction(self, tmp_path):
        # 只脚本 1 条响应——若误触发 compact，chat() 会因脚本耗尽而 IndexError
        agent = _make_agent(tmp_path, [("Just a reply", None)])
        async for _ in agent.stream_run("ordinary message"):
            pass

        assert len(agent.history.messages) == 2  # user + assistant
        assert agent.llm.call_count == 1
        assert all(
            not str(m.get("content", "")).startswith(SUMMARY_MARKER)
            for m in agent.history.messages
        )


# ── 4. 失败安全 ─────────────────────────────────────────────────

class TestAutoCompactionFailureSafe:
    """压缩失败绝不能打断回合：最终回复照常产出，历史完好。"""

    @pytest.mark.asyncio
    async def test_compact_exception_swallowed(self, tmp_path, monkeypatch):
        agent = _make_agent(
            tmp_path, [("Still answered", None)], max_history_tokens=200
        )
        _seed_five_short_turns(agent)

        async def _boom(self, llm, keep_last=4):
            raise RuntimeError("compaction backend down")

        monkeypatch.setattr(ConversationHistory, "compact", _boom)
        before = list(agent.history.messages)

        out = []
        async for chunk in agent.stream_run("z" * 720):
            out.append(chunk)

        # 回合照常完成；失败 → 不发压缩通知
        assert "Still answered" in "".join(out)
        assert "Compacting conversation" not in "".join(out)
        # 历史 = 旧 10 条 + 本轮 2 条，失败的压缩未留任何痕迹
        assert len(agent.history.messages) == len(before) + 2
        assert agent.history.validate()

    @pytest.mark.asyncio
    async def test_summary_llm_error_leaves_history_intact(self, tmp_path):
        """摘要 LLM 调用抛错：compact() 内部吞掉、缓冲不动，run() 正常收尾。"""

        class _RaisingLLM(FakeLLM):
            async def chat(self, messages, tools=None, stream=True):
                # 第 1 次 chat 是回合响应；第 2 次（compact 的摘要调用）才抛错
                if self.call_count > 0:
                    raise RuntimeError("summary call failed")
                return await super().chat(messages, tools, stream)

        agent = _make_agent(tmp_path, [], max_history_tokens=200)
        _seed_five_short_turns(agent)
        agent.llm = _RaisingLLM([("Answered anyway", None)])

        out = await agent.run("w" * 720)

        assert out == "Answered anyway"
        assert agent.history.validate()
        # 未压缩：最后一条仍是本轮最终回复，摘要哨兵不存在
        assert agent.history.messages[-1]["content"] == "Answered anyway"
        assert all(
            not str(m.get("content", "")).startswith(SUMMARY_MARKER)
            for m in agent.history.messages
        )


# ── 5. run() token 累计（--no-stream 的 /cost 修复）─────────────

class TestRunTokenAccounting:
    """run()（非流式）现在也累计 token：/cost 不再恒为 0。"""

    @pytest.mark.asyncio
    async def test_run_accumulates_output_tokens(self, tmp_path):
        agent = _make_agent(tmp_path, [("a somewhat longer final reply here", None)])
        agent.config.stream = False
        out = await agent.run("hello")

        assert out == "a somewhat longer final reply here"
        # FakeLLM 不带 usage → 走字符估算兜底，至少非零
        assert agent.total_output_tokens > 0

    @pytest.mark.asyncio
    async def test_run_prefers_server_usage_and_strips_it(self, tmp_path):
        """有 usage 用 usage；且 usage 绝不能泄漏进历史消息（API 会拒绝）。"""

        class UsageLLM(FakeLLM):
            async def chat(self, messages, tools=None, stream=True):
                resp = await super().chat(messages, tools, stream)
                resp["usage"] = {"prompt_tokens": 11, "completion_tokens": 7}
                return resp

        agent = _make_agent(tmp_path, [])
        agent.config.stream = False
        agent.llm = UsageLLM([("done", None)])
        await agent.run("hello")

        assert agent.total_input_tokens == 11
        assert agent.total_output_tokens == 7
        assert all("usage" not in m for m in agent.history.messages)
