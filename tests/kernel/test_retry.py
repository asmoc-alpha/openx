"""内核重试层（kernel/retry.py）单测：策略计算与 RetryingProvider 契约。

与 tests/llm/test_retries.py 的分工：那边经 LLMClient 门面验证
"SDK 异常 -> 契约翻译 -> 重试"全链路；这边只用伪 Provider 验证
"契约 -> 重试决策"的内核语义，不依赖任何 SDK。

运行：``python -m pytest tests/kernel/test_retry.py -q``
"""

from __future__ import annotations

import pytest

from openx.kernel.provider import (
    ProviderTransientError,
    StreamDone,
    StreamReasoning,
)
from openx.kernel.retry import MAX_RETRY_DELAY, RetryingProvider, compute_delay

import openx.kernel.retry as retry_mod


class FlakyProvider:
    """按脚本出招的伪 Provider：outcomes 依次为异常或事件列表。"""

    def __init__(self, chat_outcomes=None, stream_outcomes=None):
        self.chat_outcomes = list(chat_outcomes or [])
        self.stream_outcomes = list(stream_outcomes or [])
        self.chat_calls = 0
        self.stream_calls = 0

    async def chat(self, messages, tools=None, stream=True):
        self.chat_calls += 1
        outcome = self.chat_outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def stream_chat(self, messages, tools=None):
        self.stream_calls += 1
        outcome = self.stream_outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        for ev in outcome:
            if isinstance(ev, Exception):
                raise ev
            yield ev


def _transient(original=None, retry_after=None):
    return ProviderTransientError(original or RuntimeError("flaky"), retry_after)


@pytest.fixture(autouse=True)
def _instant_sleep(monkeypatch):
    """内核级瞬时重试；记录每次等待秒数。"""
    sleeps: list[float] = []

    async def fake_sleep(delay: float):
        sleeps.append(delay)

    monkeypatch.setattr(retry_mod, "_sleep", fake_sleep)
    return sleeps


class TestComputeDelay:
    def test_exponential_backoff_bounds(self):
        assert 1.0 <= compute_delay(0, 1.0, None) < 2.0
        assert 2.0 <= compute_delay(1, 1.0, None) < 4.0
        assert 8.0 <= compute_delay(3, 1.0, None) < 9.0

    def test_capped(self):
        assert compute_delay(20, 1.0, None) == MAX_RETRY_DELAY

    def test_retry_after_priority_and_cap(self):
        assert compute_delay(0, 1.0, 7.5) == 7.5
        assert compute_delay(0, 1.0, 999.0) == MAX_RETRY_DELAY
        assert compute_delay(0, 1.0, -3.0) == 0.0

    def test_zero_base_instant(self):
        assert compute_delay(5, 0.0, None) == 0.0


class TestChatRetry:
    async def test_transient_retried_then_success(self):
        p = FlakyProvider(chat_outcomes=[_transient(), _transient(), {"content": "ok"}])
        r = RetryingProvider(p)
        result = await r.chat([{"role": "user", "content": "x"}])
        assert result == {"content": "ok"} and p.chat_calls == 3

    async def test_fatal_error_passes_through_untouched(self):
        err = ValueError("deterministic")
        p = FlakyProvider(chat_outcomes=[err])
        r = RetryingProvider(p)
        with pytest.raises(ValueError):
            await r.chat([{"role": "user", "content": "x"}])
        assert p.chat_calls == 1  # 非契约异常绝不重试

    async def test_exhaustion_reraises_original(self):
        original = RuntimeError("always down")
        p = FlakyProvider(chat_outcomes=[_transient(original)] * 5)
        r = RetryingProvider(p)
        with pytest.raises(RuntimeError, match="always down"):
            await r.chat([{"role": "user", "content": "x"}])
        assert p.chat_calls == 5  # 1 + 4 次重试（默认策略）

    async def test_zero_retries_disabled(self):
        p = FlakyProvider(chat_outcomes=[_transient(), {"content": "never"}])
        r = RetryingProvider(p)
        r.policy.max_retries = 0
        with pytest.raises(RuntimeError):
            await r.chat([{"role": "user", "content": "x"}])
        assert p.chat_calls == 1

    async def test_retry_after_flows_to_delay(self, _instant_sleep):
        p = FlakyProvider(
            chat_outcomes=[_transient(retry_after=7.0), {"content": "ok"}]
        )
        r = RetryingProvider(p)
        await r.chat([{"role": "user", "content": "x"}])
        assert _instant_sleep == [7.0]

    async def test_on_retry_gets_original_and_numbers(self):
        p = FlakyProvider(
            chat_outcomes=[_transient(), _transient(), {"content": "ok"}]
        )
        r = RetryingProvider(p)
        events: list[tuple[int, int, str]] = []
        r.on_retry = lambda a, m, e, d: events.append((a, m, type(e).__name__))
        await r.chat([{"role": "user", "content": "x"}])
        assert events == [(1, 4, "RuntimeError"), (2, 4, "RuntimeError")]

    async def test_on_retry_exception_swallowed(self):
        p = FlakyProvider(chat_outcomes=[_transient(), {"content": "ok"}])

        def bad(a, m, e, d):
            raise RuntimeError("UI on fire")

        r = RetryingProvider(p, on_retry=bad)
        result = await r.chat([{"role": "user", "content": "x"}])
        assert result == {"content": "ok"} and p.chat_calls == 2


class TestStreamRetry:
    async def test_retry_before_any_event(self):
        done = StreamDone(response={"role": "assistant", "content": "hi"})
        p = FlakyProvider(
            stream_outcomes=[
                _transient(),
                ["hi ", done],
            ]
        )
        r = RetryingProvider(p)
        events = [e async for e in r.stream_chat([{"role": "user", "content": "x"}])]
        assert events == ["hi ", done] and p.stream_calls == 2

    async def test_no_retry_after_event_emitted(self):
        """已 yield 事件后的断流必须上抛--透明重试会造成 UI 重复。"""
        done = StreamDone(response={"role": "assistant", "content": "never"})
        p = FlakyProvider(
            stream_outcomes=[
                ["partial", _transient()],  # yield "partial" 后再抛瞬态
                ["never", done],
            ]
        )
        r = RetryingProvider(p)
        collected: list = []
        with pytest.raises(RuntimeError):
            async for ev in r.stream_chat([{"role": "user", "content": "x"}]):
                collected.append(ev)
        assert collected == ["partial"]
        assert p.stream_calls == 1  # 配额充足也绝不重试

    async def test_reasoning_counts_as_emitted(self):
        thinking = StreamReasoning("visible thinking")
        p = FlakyProvider(
            stream_outcomes=[
                [thinking, _transient()],
                ["never"],
            ]
        )
        r = RetryingProvider(p)
        with pytest.raises(RuntimeError):
            async for _ in r.stream_chat([{"role": "user", "content": "x"}]):
                pass
        assert p.stream_calls == 1

    async def test_fatal_stream_error_passes_through(self):
        p = FlakyProvider(stream_outcomes=[KeyError("boom")])
        r = RetryingProvider(p)
        with pytest.raises(KeyError):
            async for _ in r.stream_chat([{"role": "user", "content": "x"}]):
                pass
        assert p.stream_calls == 1

    async def test_exhaustion_reraises_original(self):
        original = RuntimeError("stream always down")
        p = FlakyProvider(stream_outcomes=[_transient(original)] * 5)
        r = RetryingProvider(p)
        with pytest.raises(RuntimeError, match="stream always down"):
            async for _ in r.stream_chat([{"role": "user", "content": "x"}]):
                pass
        assert p.stream_calls == 5  # 1 + 4 次重试（默认策略）
