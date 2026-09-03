"""Anthropic 适配层（llm/anthropic.py）离线单测：双向转换 + 流事件映射。

绝不联网：非流式响应 / 流事件均用手写 Fake 对象驱动；SDK 异常分类测试
用真实 anthropic 异常类（importorskip，SDK 缺失时该测试类整体跳过）。

运行：``python -m pytest tests/llm/test_anthropic.py -q``
"""

from __future__ import annotations

import pytest

from openx.config import OpenXConfig
from openx.kernel.reasoning.retry import RetryingProvider
from openx.llm.anthropic import (
    AnthropicProvider,
    messages_to_anthropic,
    response_to_openai,
    tools_to_anthropic,
)
from openx.kernel.reasoning.provider import ProviderTransientError, StreamDone, StreamReasoning


# ── 转换单测（纯函数）──────────────────────────────────────────


class TestMessagesToAnthropic:
    def test_system_extracted(self):
        system, msgs = messages_to_anthropic(
            [{"role": "system", "content": "sys-a"}, {"role": "system", "content": "sys-b"},
             {"role": "user", "content": "hi"}]
        )
        assert system == "sys-a\nsys-b"
        assert [m["role"] for m in msgs] == ["user"]

    def test_assistant_tool_calls_to_tool_use(self):
        system, msgs = messages_to_anthropic(
            [{"role": "assistant", "content": "think",
              "tool_calls": [{"id": "c1", "type": "function",
                              "function": {"name": "f", "arguments": '{"a": 1}'}}]}]
        )
        assert system is None
        blocks = msgs[0]["content"]
        assert blocks[0] == {"type": "text", "text": "think"}
        assert blocks[1]["type"] == "tool_use"
        assert blocks[1]["id"] == "c1" and blocks[1]["name"] == "f"
        assert blocks[1]["input"] == {"a": 1}  # 参数 JSON -> dict

    def test_tool_result_wrapped_in_user(self):
        system, msgs = messages_to_anthropic(
            [{"role": "user", "content": "q"},
             {"role": "assistant", "content": "c",
              "tool_calls": [{"id": "c1", "type": "function",
                              "function": {"name": "f", "arguments": "{}"}}]},
             {"role": "tool", "tool_call_id": "c1", "content": "42"}]
        )
        last = msgs[-1]
        assert last["role"] == "user"
        block = last["content"][0]
        assert block["type"] == "tool_result"
        assert block["tool_use_id"] == "c1" and block["content"] == "42"

    def test_multimodal_image_url_to_image_block(self):
        system, msgs = messages_to_anthropic(
            [{"role": "user", "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64,AAAA"}},
            ]}]
        )
        blocks = msgs[0]["content"]
        assert blocks[0] == {"type": "text", "text": "look"}
        assert blocks[1] == {"type": "image",
                             "source": {"type": "base64",
                                        "media_type": "image/png", "data": "AAAA"}}


class TestToolsToAnthropic:
    def test_openai_schema_shapes(self):
        tools = tools_to_anthropic(
            [{"type": "function",
              "function": {"name": "f", "description": "d",
                           "parameters": {"type": "object", "properties": {}}}}]
        )
        assert tools == [{"name": "f", "description": "d",
                          "input_schema": {"type": "object", "properties": {}}}]

    def test_none_tools_empty(self):
        assert tools_to_anthropic(None) == []
        assert tools_to_anthropic([]) == []


class TestResponseToOpenAI:
    def test_text_and_tool_use_blocks(self):
        class Block:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class Usage:
            input_tokens = 11
            output_tokens = 22

        class Resp:
            content = [
                Block(type="text", text="hello"),
                Block(type="tool_use", id="c1", name="f",
                      input={"x": 1, "nested": [2]}),
            ]
            usage = Usage()

        result = response_to_openai(Resp())
        assert result["content"] == "hello"
        tc = result["tool_calls"][0]
        assert tc["id"] == "c1" and tc["function"]["name"] == "f"
        assert tc["function"]["arguments"] == '{"x": 1, "nested": [2]}'
        # Usage 未带 cache_read_input_tokens（可选字段）→ cached_tokens 归 0
        assert result["usage"] == {
            "prompt_tokens": 11, "completion_tokens": 22, "cached_tokens": 0,
        }

    def test_cache_read_input_tokens_flow_into_usage(self):
        """usage 报告 cache_read_input_tokens → 原样透出 cached_tokens。"""

        class Block:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class Usage:
            input_tokens = 11
            output_tokens = 22
            cache_read_input_tokens = 7

        class Resp:
            content = [Block(type="text", text="hello")]
            usage = Usage()

        result = response_to_openai(Resp())
        assert result["usage"] == {
            "prompt_tokens": 11, "completion_tokens": 22, "cached_tokens": 7,
        }

    def test_thinking_block_excluded(self):
        class Block:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class Resp:
            content = [Block(type="thinking", thinking="hidden"),
                       Block(type="text", text="answer")]
            usage = None

        result = response_to_openai(Resp())
        assert result["content"] == "answer"  # thinking 不进正文


class TestUserContentHelpers:
    def test_parse_data_url(self):
        from openx.llm.anthropic import _parse_data_url

        assert _parse_data_url("data:image/jpeg;base64,xyz") == ("image/jpeg", "xyz")
        assert _parse_data_url("data:image/png;base64,") == ("image/png", "")
        assert _parse_data_url("bare-data") == ("image/png", "bare-data")

    def test_tool_result_content_list_text_only(self):
        from openx.llm.anthropic import _tool_result_content

        assert _tool_result_content("plain") == "plain"
        assert _tool_result_content([{"type": "text", "text": "one"}]) == "one"
        parts = _tool_result_content([{"type": "text", "text": "a"},
                                      {"type": "text", "text": "b"}])
        assert parts == [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]


# ── 流事件映射（伪 SSE 序列驱动）────────────────────────────────


class _E:
    """通用属性袋：模拟 anthropic SDK 的流事件对象。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeStream:
    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._events:
            return self._events.pop(0)
        raise StopAsyncIteration


class FakeMessages:
    """按脚本出招的 messages.create 桩：result 为 Exception 则抛（SDK 同款）。"""

    def __init__(self, result):
        self.result = result
        self.calls = 0
        self.last_params: dict = {}

    async def create(self, **params):
        self.calls += 1
        self.last_params = params
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakeClient:
    def __init__(self, messages):
        self.messages = messages


def _provider(result):
    """AnthropicProvider with fake client; ``result`` = 响应或 FakeStream。"""
    cfg = OpenXConfig(api_key="sk-test", model="claude-3")
    p = AnthropicProvider(cfg)
    p._client = FakeClient(FakeMessages(result))
    return p


def _stream(*events):
    return FakeStream(list(events))


def _text_stream(texts):
    events = [
        _E(type="message_start", message=_E(usage=_E(input_tokens=3))),
        _E(type="content_block_start", index=0,
           content_block=_E(type="text", text="")),
    ]
    for t in texts:
        events.append(_E(type="content_block_delta", index=0,
                         delta=_E(type="text_delta", text=t)))
    events.append(_E(type="message_delta", delta=_E(stop_reason="end_turn"),
                     usage=_E(output_tokens=7)))
    return _stream(*events)


class TestStreamMapping:
    async def test_text_deltas_and_done(self):
        p = _provider(_text_stream(["hello ", "world"]))
        events = [e async for e in p.stream_chat([{"role": "user", "content": "q"}])]
        assert [e for e in events if isinstance(e, str)] == ["hello ", "world"]
        done = events[-1]
        assert isinstance(done, StreamDone)
        assert done.response["content"] == "hello world"
        assert done.input_tokens == 3 and done.token_count == 7

    async def test_thinking_delta_maps_to_reasoning(self):
        stream = _stream(
            _E(type="content_block_start", index=0,
               content_block=_E(type="thinking", thinking="")),
            _E(type="content_block_delta", index=0,
               delta=_E(type="thinking_delta", thinking="step 1; ")),
            _E(type="content_block_delta", index=0,
               delta=_E(type="thinking_delta", thinking="step 2.")),
            _E(type="content_block_start", index=1,
               content_block=_E(type="text", text="")),
            _E(type="content_block_delta", index=1,
               delta=_E(type="text_delta", text="answer")),
            _E(type="message_delta", delta=_E(stop_reason="end_turn"),
               usage=_E(output_tokens=5)),
        )
        p = _provider(stream)
        events = [e async for e in p.stream_chat([{"role": "user", "content": "q"}])]
        thinking = [e for e in events if isinstance(e, StreamReasoning)]
        assert "".join(t.text for t in thinking) == "step 1; step 2."
        assert [e for e in events if isinstance(e, str)] == ["answer"]
        done = events[-1]
        assert done.response["content"] == "answer"  # thinking 不进正文

    async def test_tool_use_fragments_buffered_into_done(self):
        stream = _stream(
            _E(type="content_block_start", index=0,
               content_block=_E(type="tool_use", name="read_file", id="c1")),
            _E(type="content_block_delta", index=0,
               delta=_E(type="input_json_delta", partial_json='{"path":')),
            _E(type="content_block_delta", index=0,
               delta=_E(type="input_json_delta", partial_json='"a.py"}')),
            _E(type="message_delta", delta=_E(stop_reason="tool_use"),
               usage=_E(output_tokens=4)),
        )
        p = _provider(stream)
        events = [e async for e in p.stream_chat([{"role": "user", "content": "q"}])]
        assert not any(isinstance(e, str) for e in events)  # 分片不外露
        done = events[-1]
        assert isinstance(done, StreamDone)
        assert done.response["content"] is None
        assert done.response["tool_calls"] == [
            {"id": "c1", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}
        ]

    async def test_params_are_anthropic_format(self):
        p = _provider(_text_stream(["ok"]))
        _ = [e async for e in p.stream_chat(
            [{"role": "system", "content": "SYS"},
             {"role": "user", "content": "q"}],
            tools=[{"type": "function",
                    "function": {"name": "f", "parameters": {"type": "object"}}}],
        )]
        params = p._client.messages.last_params
        assert params["system"] == "SYS"
        assert params["messages"] == [{"role": "user", "content": [{"type": "text", "text": "q"}]}]
        assert params["tools"] == [{"name": "f", "description": "",
                                    "input_schema": {"type": "object"}}]


class TestChat:
    async def test_non_streaming_assembles_response(self):
        class Block:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class Usage:
            input_tokens = 3
            output_tokens = 5

        class Resp:
            content = [Block(type="text", text="hi")]
            usage = Usage()

        p = _provider(Resp())
        result = await p.chat([{"role": "user", "content": "q"}], stream=False)
        assert result["content"] == "hi"
        # 非流式 usage 未报告 cache_read → cached_tokens 归 0（透出字段）
        assert result["usage"] == {
            "prompt_tokens": 3, "completion_tokens": 5, "cached_tokens": 0,
        }
        assert p._client.messages.calls == 1

    async def test_chat_stream_true_reassembles(self):
        p = _provider(_text_stream(["a", "b"]))
        result = await p.chat([{"role": "user", "content": "q"}], stream=True)
        assert result["content"] == "ab"
        # 流式无缓存命中事件 → cached_tokens 归 0
        assert result["usage"] == {
            "prompt_tokens": 3, "completion_tokens": 7, "cached_tokens": 0,
        }

    async def test_retrying_provider_retries_transient(self):
        """内核 RetryingProvider 认识 anthropic 的瞬态契约（M1 集成）。"""
        from anthropic import APIConnectionError
        import httpx

        err = APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        )
        first = FakeMessages(err)
        second = FakeMessages(_text_stream(["ok"]))

        class SeqClient:
            def __init__(self, first, second):
                self._calls = [first, second]

            @property
            def messages(self):
                return self._calls.pop(0)

        cfg = OpenXConfig(api_key="sk-test", model="claude-3")
        provider = AnthropicProvider(cfg)
        provider._client = SeqClient(first, second)
        r = RetryingProvider(provider)
        r.policy.base_delay = 0.0
        done = [e async for e in r.stream_chat([{"role": "user", "content": "q"}])]
        assert "".join(e for e in done if isinstance(e, str)) == "ok"


class TestErrorClassification:
    def test_429_rate_limit_transient(self):
        pytest.importorskip("anthropic")
        from anthropic import RateLimitError
        import httpx

        resp = httpx.Response(
            429, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
            headers={"retry-after": "7"},
        )
        err = RateLimitError("rl", response=resp, body=None)

        ok, ra = AnthropicProvider._classify(err)
        assert ok is True and ra == 7.0

    def test_401_authentication_fatal(self):
        pytest.importorskip("anthropic")
        from anthropic import AuthenticationError
        import httpx

        resp = httpx.Response(
            401, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        )
        err = AuthenticationError("no", response=resp, body=None)

        ok, _ = AnthropicProvider._classify(err)
        assert ok is False

    def test_translation_wraps_transient(self):
        pytest.importorskip("anthropic")
        from anthropic import RateLimitError
        import httpx

        resp = httpx.Response(
            429, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        )
        err = RateLimitError("rl", response=resp, body=None)
        with pytest.raises(ProviderTransientError) as ei:
            AnthropicProvider._translate(err)
        assert ei.value.original is err
