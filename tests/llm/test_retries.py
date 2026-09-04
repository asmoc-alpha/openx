"""LLM 客户端重试/退避测试（v0.3.2）。

覆盖：
- 请求级重试：429 / 可重试 5xx / 连接错误 → 指数退避重试后成功；
- 不可重试错误（400/401/非 API 异常）立即上抛，绝不重试；
- 重试配额耗尽后抛出最后一次错误（总尝试 = 1 + max_retries）；
- Retry-After 头优先于指数退避，同样受 60s 封顶；
- 指数退避区间 base·2^attempt + jitter∈[0,base)；
- 流级重试：_stream_response（内部重组）任何时刻断流都可整请求重试；
  stream_chat 仅在"尚未 yield 任何文本 token"时可透明重试——
  已上屏文本后的断流必须上抛（防 UI 重复），纯工具分片不算可见输出；
- on_retry 回调：序号 1 起、异常被吞不影响重试。

风格：pytest-asyncio auto、手写 Fake（禁 unittest.mock）、
monkeypatch 模块常量（openai_compat._sleep）实现瞬时重试。

运行：``python -m pytest tests/test_retries.py -q``
"""

from __future__ import annotations

import httpx
import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    InternalServerError,
    RateLimitError,
)

from openx.config import OpenXConfig
from openx.llm import StreamDone
from openx.llm import openai_compat as client_mod
from openx.llm.openai_compat import (
    MAX_RETRY_DELAY,
    LLMClient,
    StreamReasoning,
    _cached_tokens_of,
    _classify_error,
    _compute_delay,
    _parse_retry_after,
)


# ── 异常构造助手 ─────────────────────────────────────────────────

def _http_error(status: int, headers: dict | None = None) -> APIStatusError:
    """构造指定状态码的 openai APIStatusError（含真实 httpx 响应）。"""
    resp = httpx.Response(
        status,
        request=httpx.Request("POST", "https://api.test/v1/chat/completions"),
        headers=headers or {},
    )
    if status == 429:
        return RateLimitError("rate limited", response=resp, body=None)
    if status == 500:
        return InternalServerError("server error", response=resp, body=None)
    return APIStatusError(f"http {status}", response=resp, body=None)


def _conn_error() -> APIConnectionError:
    return APIConnectionError(
        request=httpx.Request("POST", "https://api.test/v1/chat/completions")
    )


# ─- Fake API 面 ──────────────────────────────────────────────────

class Obj:
    """通用属性袋：模拟 openai SDK 的 pydantic 对象。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _text_response(text: str) -> Obj:
    """非流式成功响应（_parse_response 消费）。"""
    return Obj(
        choices=[Obj(message=Obj(content=text, tool_calls=None))],
        usage=Obj(prompt_tokens=3, completion_tokens=5),
    )


def _text_chunk(text: str) -> Obj:
    return Obj(choices=[Obj(delta=Obj(content=text, tool_calls=None))], usage=None)


def _reasoning_chunk(text: str, field: str = "reasoning_content") -> Obj:
    """推理 delta：DeepSeek 系用 reasoning_content，o-series 风格用 reasoning。"""
    delta = Obj(content=None, tool_calls=None)
    setattr(delta, field, text)
    return Obj(choices=[Obj(delta=delta)], usage=None)


def _usage_chunk(prompt: int = 3, completion: int = 5) -> Obj:
    return Obj(choices=[], usage=Obj(prompt_tokens=prompt, completion_tokens=completion))


def _tool_chunk(index: int, tc_id: str, name: str, args: str) -> Obj:
    return Obj(
        choices=[
            Obj(delta=Obj(
                content=None,
                tool_calls=[Obj(
                    index=index, id=tc_id,
                    function=Obj(name=name, arguments=args),
                )],
            ))
        ],
        usage=None,
    )


class FakeAsyncStream:
    """可中途抛错的异步 chunk 迭代器。"""

    def __init__(self, chunks: list, fail_with: Exception | None = None):
        self._chunks = list(chunks)
        self._fail = fail_with

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._chunks:
            return self._chunks.pop(0)
        if self._fail is not None:
            err, self._fail = self._fail, None
            raise err
        raise StopAsyncIteration


class FakeCompletions:
    """按序消费 outcomes 的 chat.completions.create 桩。

    outcome 为 Exception → create 抛错；否则作为返回值（非流式响应对象
    或 FakeAsyncStream）。记录调用次数供断言。
    """

    def __init__(self, outcomes: list):
        self._outcomes = list(outcomes)
        self.calls = 0

    async def create(self, **params):
        self.calls += 1
        assert self._outcomes, "FakeCompletions 收到超出脚本的调用"
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _make_client(outcomes, max_retries: int = 3, base_delay: float = 0.0):
    """构造挂 Fake 的 LLMClient（不触发真实网络）。

    凭据/模型经 policy_overrides（解析出的 settings dict）给实现；config 只
    承载 retry 晚绑定字段。
    """
    cfg = OpenXConfig()
    cfg.max_retries = max_retries
    cfg.retry_base_delay = base_delay
    llm = LLMClient(
        cfg,
        policy_overrides={
            "api_key": "sk-test",
            "api_base": "https://api.test/v1",
            "model": "fake-model",
        },
    )
    completions = FakeCompletions(outcomes)
    llm._client = Obj(chat=Obj(completions=completions))
    return llm, completions


@pytest.fixture(autouse=True)
def _instant_sleep(monkeypatch):
    """默认瞬时重试；需要记录延迟的测试自行再覆盖。"""
    sleeps: list[float] = []

    async def fake_sleep(delay: float):
        sleeps.append(delay)

    monkeypatch.setattr(client_mod, "_sleep", fake_sleep)
    return sleeps


# ── 单元测试：策略函数 ───────────────────────────────────────────

class TestClassifyError:
    def test_429_retryable_with_retry_after(self):
        ok, ra = _classify_error(_http_error(429, {"retry-after": "7"}))
        assert ok is True and ra == 7.0

    def test_5xx_retryable_without_header(self):
        for status in (500, 502, 503, 504, 408, 409):
            ok, ra = _classify_error(_http_error(status))
            assert ok is True and ra is None, status

    def test_other_4xx_not_retryable(self):
        for status in (400, 401, 403, 404, 422):
            ok, _ = _classify_error(_http_error(status))
            assert ok is False, status

    def test_connection_error_retryable(self):
        ok, ra = _classify_error(_conn_error())
        assert ok is True and ra is None

    def test_non_api_error_not_retryable(self):
        ok, ra = _classify_error(ValueError("boom"))
        assert ok is False and ra is None


class TestComputeDelay:
    def test_exponential_backoff_bounds(self):
        # delay = base·2^attempt + jitter∈[0, base)
        assert 1.0 <= _compute_delay(0, 1.0, None) < 2.0
        assert 2.0 <= _compute_delay(1, 1.0, None) < 4.0
        assert 8.0 <= _compute_delay(3, 1.0, None) < 9.0

    def test_capped_at_max(self):
        assert _compute_delay(20, 1.0, None) == MAX_RETRY_DELAY

    def test_retry_after_takes_priority(self):
        assert _compute_delay(0, 1.0, 7.5) == 7.5

    def test_retry_after_capped_too(self):
        assert _compute_delay(0, 1.0, 999.0) == MAX_RETRY_DELAY

    def test_negative_retry_after_clamped(self):
        assert _compute_delay(0, 1.0, -3.0) == 0.0

    def test_zero_base_instant(self):
        assert _compute_delay(5, 0.0, None) == 0.0


class TestParseRetryAfter:
    def test_numeric(self):
        resp = httpx.Response(429, request=httpx.Request("POST", "https://x"), headers={"retry-after": "12"})
        assert _parse_retry_after(resp) == 12.0

    def test_garbage_returns_none(self):
        resp = httpx.Response(429, request=httpx.Request("POST", "https://x"), headers={"retry-after": "soon-ish"})
        assert _parse_retry_after(resp) is None

    def test_missing_header(self):
        resp = httpx.Response(429, request=httpx.Request("POST", "https://x"))
        assert _parse_retry_after(resp) is None

    def test_none_response(self):
        assert _parse_retry_after(None) is None


# ── 非流式 chat() 重试 ───────────────────────────────────────────

class TestChatRetries:
    async def test_retries_429_then_succeeds(self):
        llm, comp = _make_client([
            _http_error(429), _http_error(429), _text_response("ok"),
        ])
        result = await llm.chat([{"role": "user", "content": "hi"}], stream=False)
        assert result["content"] == "ok"
        assert comp.calls == 3

    async def test_retries_5xx_then_succeeds(self):
        llm, comp = _make_client([_http_error(503), _text_response("back")])
        result = await llm.chat([{"role": "user", "content": "hi"}], stream=False)
        assert result["content"] == "back" and comp.calls == 2

    async def test_retries_connection_error(self):
        llm, comp = _make_client([_conn_error(), _text_response("ok")])
        result = await llm.chat([{"role": "user", "content": "hi"}], stream=False)
        assert result["content"] == "ok" and comp.calls == 2

    async def test_400_raises_immediately(self):
        llm, comp = _make_client([_http_error(400), _text_response("never")])
        with pytest.raises(APIStatusError):
            await llm.chat([{"role": "user", "content": "hi"}], stream=False)
        assert comp.calls == 1

    async def test_exhausts_retries_then_raises(self):
        llm, comp = _make_client(
            [_http_error(500)] * 5, max_retries=2,
        )
        with pytest.raises(InternalServerError):
            await llm.chat([{"role": "user", "content": "hi"}], stream=False)
        assert comp.calls == 3  # 1 次原始 + 2 次重试

    async def test_max_retries_zero_disables(self):
        llm, comp = _make_client(
            [_http_error(429), _text_response("never")], max_retries=0,
        )
        with pytest.raises(RateLimitError):
            await llm.chat([{"role": "user", "content": "hi"}], stream=False)
        assert comp.calls == 1

    async def test_retry_after_header_respected(self, _instant_sleep):
        llm, comp = _make_client([
            _http_error(429, {"retry-after": "7"}), _text_response("ok"),
        ], base_delay=1.0)
        await llm.chat([{"role": "user", "content": "hi"}], stream=False)
        assert _instant_sleep == [7.0]

    async def test_exponential_delays_recorded(self, _instant_sleep):
        llm, comp = _make_client(
            [_http_error(500)] * 3 + [_text_response("ok")],
            max_retries=3, base_delay=1.0,
        )
        await llm.chat([{"role": "user", "content": "hi"}], stream=False)
        assert len(_instant_sleep) == 3
        assert 1.0 <= _instant_sleep[0] < 2.0
        assert 2.0 <= _instant_sleep[1] < 4.0
        assert 4.0 <= _instant_sleep[2] < 8.0

    async def test_usage_survives_retry(self):
        llm, comp = _make_client([_http_error(500), _text_response("ok")])
        result = await llm.chat([{"role": "user", "content": "hi"}], stream=False)
        # 兼容 usage 无 prompt_tokens_details → cached_tokens 归 0
        assert result["usage"] == {
            "prompt_tokens": 3, "completion_tokens": 5, "cached_tokens": 0,
        }


# ── on_retry 回调 ────────────────────────────────────────────────

class TestOnRetryCallback:
    async def test_fires_with_attempt_numbers(self):
        llm, comp = _make_client([
            _http_error(429), _http_error(500), _text_response("ok"),
        ])
        events: list[tuple[int, int, str]] = []
        llm.on_retry = lambda a, m, e, d: events.append((a, m, type(e).__name__))
        await llm.chat([{"role": "user", "content": "hi"}], stream=False)
        assert events == [(1, 3, "RateLimitError"), (2, 3, "InternalServerError")]

    async def test_callback_exception_swallowed(self):
        llm, comp = _make_client([_http_error(429), _text_response("ok")])

        def bad_callback(a, m, e, d):
            raise RuntimeError("UI on fire")

        llm.on_retry = bad_callback
        result = await llm.chat([{"role": "user", "content": "hi"}], stream=False)
        assert result["content"] == "ok" and comp.calls == 2


# ── 流式重试 ─────────────────────────────────────────────────────

class TestStreamRetries:
    async def test_reassemble_retries_mid_stream(self):
        """_stream_response：内部重组不外露 → 断流任何时刻都可整请求重试。"""
        llm, comp = _make_client([
            FakeAsyncStream([_text_chunk("par")], fail_with=_conn_error()),
            FakeAsyncStream([_text_chunk("hel"), _text_chunk("lo"), _usage_chunk()]),
        ])
        result = await llm.chat([{"role": "user", "content": "hi"}], stream=True)
        # 第一次的部分内容被整体丢弃，绝不拼接
        assert result["content"] == "hello"
        assert comp.calls == 2
        # 兼容 usage 无 prompt_tokens_details → cached_tokens 归 0
        assert result["usage"] == {
            "prompt_tokens": 3, "completion_tokens": 5, "cached_tokens": 0,
        }

    async def test_stream_chat_retries_before_any_text(self):
        llm, comp = _make_client([
            FakeAsyncStream([], fail_with=_http_error(429)),
            FakeAsyncStream([_text_chunk("hi "), _text_chunk("there"), _usage_chunk()]),
        ])
        events = [e async for e in llm.stream_chat([{"role": "user", "content": "x"}])]
        texts = [e for e in events if isinstance(e, str)]
        done = events[-1]
        assert "".join(texts) == "hi there"
        assert isinstance(done, StreamDone)
        assert done.response["content"] == "hi there"
        assert comp.calls == 2

    async def test_stream_chat_tool_only_chunks_still_retryable(self):
        """纯工具分片不算可见输出：断流后重试不产生重复。"""
        llm, comp = _make_client([
            FakeAsyncStream(
                [_tool_chunk(0, "c1", "read_", "")], fail_with=_conn_error(),
            ),
            FakeAsyncStream([
                _tool_chunk(0, "c1", "read_", '{"p": 1}'), _usage_chunk(),
            ]),
        ])
        events = [e async for e in llm.stream_chat([{"role": "user", "content": "x"}])]
        done = events[-1]
        assert isinstance(done, StreamDone)
        tcs = done.response["tool_calls"]
        assert len(tcs) == 1  # 重试丢弃第一份缓冲 → 无重复合并
        assert tcs[0]["id"] == "c1"
        assert tcs[0]["function"] == {"name": "read_", "arguments": '{"p": 1}'}
        assert comp.calls == 2

    async def test_stream_chat_raises_after_text_emitted(self):
        """已 yield 文本后的断流必须上抛——透明重试会造成 UI 重复。"""
        llm, comp = _make_client([
            FakeAsyncStream([_text_chunk("partial")], fail_with=_http_error(429)),
            FakeAsyncStream([_text_chunk("never")]),
        ], max_retries=3)
        collected: list[str] = []
        with pytest.raises(RateLimitError):
            async for event in llm.stream_chat([{"role": "user", "content": "x"}]):
                if isinstance(event, str):
                    collected.append(event)
        assert collected == ["partial"]
        assert comp.calls == 1  # 配额充足也绝不重试

    async def test_stream_chat_exhausts_retries(self):
        llm, comp = _make_client(
            [FakeAsyncStream([], fail_with=_http_error(500)) for _ in range(3)],
            max_retries=2,
        )
        with pytest.raises(InternalServerError):
            async for _ in llm.stream_chat([{"role": "user", "content": "x"}]):
                pass
        assert comp.calls == 3

    async def test_stream_chat_on_retry_fires(self):
        llm, comp = _make_client([
            FakeAsyncStream([], fail_with=_http_error(429)),
            FakeAsyncStream([_text_chunk("ok"), _usage_chunk()]),
        ])
        events: list[int] = []
        llm.on_retry = lambda a, m, e, d: events.append(a)
        _ = [e async for e in llm.stream_chat([{"role": "user", "content": "x"}])]
        assert events == [1]


# ── 推理内容（reasoning_content / reasoning）──────────────────────

class TestStreamReasoning:
    """reasoning delta 解析：先于正文 yield 为 StreamReasoning 事件。"""

    async def test_reasoning_events_precede_content(self):
        llm, comp = _make_client([
            FakeAsyncStream([
                _reasoning_chunk("step 1; "),
                _reasoning_chunk("step 2."),
                _text_chunk("answer"),
                _usage_chunk(),
            ]),
        ])
        events = [e async for e in llm.stream_chat([{"role": "user", "content": "x"}])]
        # 事件序：两个 StreamReasoning → 正文 str → StreamDone
        assert isinstance(events[0], StreamReasoning)
        assert isinstance(events[1], StreamReasoning)
        assert events[0].text + events[1].text == "step 1; step 2."
        assert [e for e in events if isinstance(e, str)] == ["answer"]
        done = events[-1]
        assert isinstance(done, StreamDone)
        assert done.response["content"] == "answer"
        # reasoning 不进重组消息（history 回放安全）
        assert "reasoning_content" not in done.response
        assert "reasoning" not in done.response
        assert comp.calls == 1

    async def test_reasoning_field_o_series_variant(self):
        """o-series 风格后端以 reasoning 字段发送。"""
        llm, comp = _make_client([
            FakeAsyncStream([_reasoning_chunk("hmm", field="reasoning"), _usage_chunk()]),
        ])
        events = [e async for e in llm.stream_chat([{"role": "user", "content": "x"}])]
        assert isinstance(events[0], StreamReasoning)
        assert events[0].text == "hmm"

    async def test_reasoning_counts_as_emitted_no_transparent_retry(self):
        """reasoning 已上屏 → 中途断流必须上抛（重试会让 thinking 重复）。"""
        llm, comp = _make_client([
            FakeAsyncStream(
                [_reasoning_chunk("visible thinking")], fail_with=_http_error(429),
            ),
            FakeAsyncStream([_text_chunk("never")]),
        ], max_retries=3)
        collected: list = []
        with pytest.raises(RateLimitError):
            async for event in llm.stream_chat([{"role": "user", "content": "x"}]):
                if isinstance(event, StreamReasoning):
                    collected.append(event.text)
        assert collected == ["visible thinking"]
        assert comp.calls == 1  # 配额充足也绝不重试

    async def test_content_only_stream_unaffected(self):
        """无 reasoning 字段的 delta（既有后端）行为零变化。"""
        llm, comp = _make_client([
            FakeAsyncStream([_text_chunk("a"), _text_chunk("b"), _usage_chunk()]),
        ])
        events = [e async for e in llm.stream_chat([{"role": "user", "content": "x"}])]
        assert not any(isinstance(e, StreamReasoning) for e in events)
        assert "".join(e for e in events if isinstance(e, str)) == "ab"


# ── cached_tokens 提取 ────────────────────────────────────────────


class TestCachedTokenExtraction:
    """cached_tokens 从 OpenAI 兼容 usage 透出（可选字段，缺省归 0）。"""

    def test_missing_or_null_details_is_zero(self):
        # 后端不回 prompt_tokens_details / usage 缺失 → 归 0（非 0 字段绝不造假）
        assert _cached_tokens_of(Obj(prompt_tokens=3)) == 0
        assert _cached_tokens_of(Obj()) == 0
        assert _cached_tokens_of(None) == 0
        # details 里 cached_tokens 为 0（本轮未命中）→ 0
        assert _cached_tokens_of(Obj(prompt_tokens_details=Obj(cached_tokens=0))) == 0

    def test_reported_cache_hit_is_taken(self):
        usage = Obj(
            prompt_tokens=11, completion_tokens=7,
            prompt_tokens_details=Obj(cached_tokens=4),
        )
        assert _cached_tokens_of(usage) == 4

    def test_parse_response_exposes_cached(self):
        from openx.llm.openai_compat import OpenAICompatProvider

        provider = OpenAICompatProvider({
            "api_key": "sk-test",
            "api_base": "https://example.com/v1",
            "model": "test-model",
        })
        raw = Obj(
            choices=[Obj(message=Obj(content="hi", tool_calls=None))],
            usage=Obj(
                prompt_tokens=11, completion_tokens=7,
                prompt_tokens_details=Obj(cached_tokens=4),
            ),
        )
        result = provider._parse_response(raw)
        assert result["usage"] == {
            "prompt_tokens": 11, "completion_tokens": 7, "cached_tokens": 4,
        }
