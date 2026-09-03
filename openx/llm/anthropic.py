"""Anthropic 原生 provider 实现（模型接入层 M4）。

职责边界：本模块**只做协议适配**--在边界把 OpenAI 消息格式与 Anthropic
原生格式双向转换，系统其余部分继续说 OpenAI 格式。重试归内核（kernel/
retry.py），接口形状归内核（kernel/provider.py）：

- **入（OpenAI -> Anthropic）**：system 消息抽为 ``system`` 参数；
  assistant ``tool_calls`` -> ``tool_use`` content block；``role=tool``
  消息 -> user 的 ``tool_result`` block；多模态 ``image_url``(base64) ->
  image block；
- **出（Anthropic -> OpenAI）**：content blocks 组装回
  ``{role, content, tool_calls}`` dict；thinking block -> StreamReasoning；
  usage 字段对齐（input_tokens -> prompt_tokens / output_tokens ->
  completion_tokens）；
- **流式**：SSE 事件（content_block_delta 等）映射为 StreamEvent：
  text delta -> str、thinking delta -> StreamReasoning、tool_use 分片缓冲、
  message_delta 带 stop_reason/usage；
- **错误契约**：SDK 异常按瞬态/确定性翻译（连接/429/可重试 5xx ->
  ProviderTransientError 携带原生异常与 Retry-After；其余原样上抛）--翻译
  由 ``llm/base.py`` 的编排面统一完成，本类只声明 SDK 异常类型；
- **编排面共享**：继承 ``llm/base.py`` 的 ``LLMProvider``，只实现 anthropic
  协议特有的钩子（客户端构造 / 消息转换 / 响应与流解析）。

SDK 是**可选依赖**（``pip install openx[anthropic]``）：本模块顶部 import
失败不报错（异常类型置空使基类分类恒 False，模块照常可导入供离线转换
单测）。注册由 builtin-providers 插件在 SDK 缺失时跳过。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Optional

# ── 独立调试支持：允许直接运行本文件（python openx/.../xxx.py）──────
if __name__ == "__main__" and not __package__:
    import sys as _sys
    from pathlib import Path as _Path
    _file = _Path(__file__).resolve()
    _root = _file.parent
    while _root != _root.parent and not (_root / "pyproject.toml").exists():
        _root = _root.parent
    _sys.path.insert(0, str(_root))
    __package__ = ".".join(_file.relative_to(_root).parts[:-1])

from ..kernel.reasoning.provider import ProviderFatalError, StreamDone, StreamReasoning
from .base import LLMProvider

# 可选依赖：缺失时该实现不注册（builtin-providers 跳过）；异常类型置空使
# 基类分类恒 False（确定性错误原样穿透，绝不误重试）。
try:
    from anthropic import (
        APIConnectionError as _AnthropicConnError,
        APIStatusError as _AnthropicStatusError,
        AsyncAnthropic,
    )
except ImportError:  # pragma: no cover -- 依环境
    _AnthropicConnError = ()
    _AnthropicStatusError = ()
    AsyncAnthropic = None


# ── 消息格式双向转换（纯函数，离线可测）────────────────────────


def _parse_data_url(url: str) -> tuple[str, str]:
    """``data:image/png;base64,AAAA`` -> ``("image/png", "AAAA")``。

    非 data URL 按裸 base64 处理（回退 mime=image/png）；异常一律安全。
    """
    if not url.startswith("data:"):
        return "image/png", url
    meta, _, payload = url.partition(",")
    mime = "image/png"
    if ";" in meta:
        mime = meta[5:].split(";", 1)[0] or "image/png"
    return mime, payload


def _safe_json(raw: Any) -> dict:
    """OpenAI 工具 arguments（JSON 字符串）-> dict；失败回落 {}。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def _tool_result_content(content: Any) -> Any:
    """OpenAI ``role=tool`` 消息 content -> Anthropic ``tool_result.content``。

    Anthropic 的 tool_result.content 接受字符串或 content block 列表；
    多模态 part 列表时保留文本 part，纯文本则收拢为字符串。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[dict[str, Any]] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append({"type": "text", "text": str(part.get("text", ""))})
            elif isinstance(part, str):
                parts.append({"type": "text", "text": part})
        if len(parts) == 1 and parts[0]["type"] == "text":
            return parts[0]["text"]
        return parts or ""
    return str(content or "")


def user_content_to_blocks(content: Any) -> list[dict[str, Any]]:
    """OpenAI 用户消息 content（str 或多模态 parts）-> Anthropic content blocks。"""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    blocks: list[dict[str, Any]] = []
    for part in content or []:
        if isinstance(part, dict):
            ptype = part.get("type")
            if ptype == "text":
                blocks.append({"type": "text", "text": str(part.get("text", ""))})
            elif ptype == "image_url":
                media, data = _parse_data_url(
                    (part.get("image_url") or {}).get("url", "")
                )
                blocks.append(
                    {"type": "image",
                     "source": {"type": "base64", "media_type": media, "data": data}}
                )
        else:
            blocks.append({"type": "text", "text": str(part)})
    return blocks


def messages_to_anthropic(
    messages: list[dict[str, Any]],
) -> tuple[Optional[str], list[dict[str, Any]]]:
    """OpenAI 消息序列 -> ``(system, anthropic 消息列表)``。

    - system 消息抽为 ``system`` 参数（多个以换行拼接），不进 messages；
    - assistant ``tool_calls`` -> ``tool_use`` content block；
    - ``role=tool`` -> user 消息的 ``tool_result`` block（必须挂 user）。
    """
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    for msg in messages or []:
        role = msg.get("role")
        content = msg.get("content")
        if role == "system":
            if isinstance(content, str) and content:
                system_parts.append(content)
            continue
        if role == "user":
            out.append({"role": "user", "content": user_content_to_blocks(content)})
        elif role == "assistant":
            blocks: list[dict[str, Any]] = []
            if content:
                text = content if isinstance(content, str) else str(content)
                if text:
                    blocks.append({"type": "text", "text": text})
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                blocks.append(
                    {"type": "tool_use",
                     "id": tc.get("id", ""),
                     "name": fn.get("name", ""),
                     "input": _safe_json(fn.get("arguments"))}
                )
            out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            out.append(
                {"role": "user",
                 "content": [{"type": "tool_result",
                              "tool_use_id": msg.get("tool_call_id", ""),
                              "content": _tool_result_content(content)}]}
            )
    return ("\n".join(system_parts) if system_parts else None), out


def tools_to_anthropic(
    tools: Optional[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """OpenAI tools schema -> Anthropic tools（name/description/input_schema）。

    兼容两种输入形态：外层 ``{"type": "function", "function": {...}}`` 与
    直接 ``{"name": ..., "parameters": ...}``。
    """
    result: list[dict[str, Any]] = []
    for tool in tools or []:
        fn = tool.get("function", tool)
        result.append(
            {"name": fn.get("name", ""),
             "description": fn.get("description", ""),
             "input_schema": fn.get("parameters")
             or {"type": "object", "properties": {}}}
        )
    return result


def response_to_openai(response: Any) -> dict[str, Any]:
    """Anthropic 非流式响应 -> OpenAI assistant 消息 dict。

    thinking block 不进正文（展示/历史安全的 DeepSeek 同款约定）；
    usage 对齐：input_tokens -> prompt_tokens、output_tokens ->
    completion_tokens（调用方读取后须剥离）。
    """
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in getattr(response, "content", []) or []:
        btype = getattr(block, "type", "")
        if btype == "text":
            text_parts.append(getattr(block, "text", ""))
        elif btype == "tool_use":
            tool_calls.append(
                {"id": getattr(block, "id", ""),
                 "type": "function",
                 "function": {"name": getattr(block, "name", ""),
                              "arguments": json.dumps(
                                  getattr(block, "input", {}) or {},
                                  ensure_ascii=False,
                              )}}
            )
    result: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts) or None,
    }
    if tool_calls:
        result["tool_calls"] = tool_calls
    usage = getattr(response, "usage", None)
    if usage is not None:
        result["usage"] = {
            "prompt_tokens": getattr(usage, "input_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "output_tokens", 0) or 0,
            # 缓存命中（prompt caching）：cache_read_input_tokens 为可选字段
            "cached_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        }
    return result


# ── 流事件装配（stream_chat 与 chat(stream=True) 共用）────────────


class _StreamAssembler:
    """流事件装配器：按 index 缓冲分片、累计 usage，两处消费方共用。

    ``on_event`` 返回 ``(kind, value)`` 供调用方 yield：
    ``("text", str)`` / ``("reasoning", str)`` / ``None``（无需上屏）。
    """

    def __init__(self) -> None:
        self.blocks: dict[int, dict[str, Any]] = {}
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0  # cache_read_input_tokens（可选字段，0 = 未报告）
        self.stop_reason: Optional[str] = None

    def on_event(self, event: Any) -> Optional[tuple[str, str]]:
        etype = getattr(event, "type", "")
        if etype == "message_start":
            msg = getattr(event, "message", None)
            usage = getattr(msg, "usage", None) if msg is not None else None
            if usage is not None:
                self.input_tokens = getattr(usage, "input_tokens", 0) or 0
                self.cached_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0
        elif etype == "content_block_start":
            block = getattr(event, "content_block", None)
            idx = getattr(event, "index", 0)
            buf = self.blocks.setdefault(
                idx, {"type": "", "text": "", "input_json": "", "name": "", "id": ""}
            )
            if block is not None:
                buf["type"] = getattr(block, "type", "")
                if buf["type"] == "tool_use":
                    buf["name"] = getattr(block, "name", "")
                    buf["id"] = getattr(block, "id", "")
        elif etype == "content_block_delta":
            delta = getattr(event, "delta", None)
            idx = getattr(event, "index", 0)
            buf = self.blocks.get(idx)
            if buf is None:
                buf = self.blocks[idx] = {
                    "type": "", "text": "", "input_json": "", "name": "", "id": ""
                }
            dtype = getattr(delta, "type", "")
            if dtype == "text_delta":
                text = getattr(delta, "text", "")
                buf["text"] += text
                buf["type"] = "text"
                return ("text", text)
            if dtype == "thinking_delta":
                thinking = getattr(delta, "thinking", "")
                buf["text"] += thinking
                buf["type"] = "thinking"
                return ("reasoning", thinking)
            if dtype == "input_json_delta":
                buf["input_json"] += getattr(delta, "partial_json", "")
                buf["type"] = "tool_use"
        elif etype == "message_delta":
            delta = getattr(event, "delta", None)
            if delta is not None:
                self.stop_reason = getattr(delta, "stop_reason", self.stop_reason)
            usage = getattr(event, "usage", None)
            if usage is not None:
                self.output_tokens = getattr(usage, "output_tokens", 0) or 0
                # 部分版本 message_delta 也回 cache_read_input_tokens——有则以新值为准
                cr = getattr(usage, "cache_read_input_tokens", None)
                if cr:
                    self.cached_tokens = cr
        return None

    def build_response(self) -> dict[str, Any]:
        """按 index 顺序装配 OpenAI assistant 消息 dict（流式乱序安全）。"""
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for idx in sorted(self.blocks):
            buf = self.blocks[idx]
            if buf["type"] == "text":
                text_parts.append(buf["text"])
            elif buf["type"] == "tool_use":
                tool_calls.append(
                    {"id": buf["id"], "type": "function",
                     "function": {"name": buf["name"],
                                  "arguments": buf["input_json"]}}
                )
        result: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(text_parts) or None,
        }
        if tool_calls:
            result["tool_calls"] = tool_calls
        return result


# ── 实现（继承基类编排面，只实现 anthropic 协议特有钩子）─────────


class AnthropicProvider(LLMProvider):
    """Anthropic 原生实现（单次尝试；重试由内核 RetryingProvider 包装）。

    进出消息均以 OpenAI 格式为准，本类在边界双向转换（见模块 docstring）；
    SDK 异常 -> 错误契约的翻译由基类统一完成（声明 SDK 异常类型即可）。
    """

    _CONN_ERROR_TYPES = (_AnthropicConnError,) if _AnthropicConnError else ()
    _STATUS_ERROR_TYPE = _AnthropicStatusError if _AnthropicStatusError else None

    def _make_client(self) -> Any:
        if AsyncAnthropic is None:  # pragma: no cover -- SDK 缺失由插件跳过注册
            # 缺 SDK 是确定性故障，重试无意义 -> 致命契约（重试层穿透）
            raise ProviderFatalError(
                "anthropic SDK not installed; run `pip install openx[anthropic]`"
            )
        return AsyncAnthropic(
            api_key=self.config.api_key,
            timeout=120.0,
            # SDK 内置重试关闭：重试统一归内核（双层重试会乘等待时间）
            max_retries=0,
        )

    def _build_params(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        system, anth_messages = messages_to_anthropic(messages)
        params: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "messages": anth_messages,
        }
        if system:
            params["system"] = system
        if tools:
            params["tools"] = tools_to_anthropic(tools)
        return params

    async def _send(self, params: dict[str, Any]) -> Any:
        return await self.client.messages.create(**params)

    def _parse_response(self, response: Any) -> dict[str, Any]:
        return response_to_openai(response)

    async def _chat_streaming(self, params: dict[str, Any]) -> dict[str, Any]:
        """流式内部重组（chat(stream=True)）：无可见中间态，任何时刻可重试。"""
        assembler = _StreamAssembler()
        stream = await self.client.messages.create(**params, stream=True)
        async for event in stream:
            assembler.on_event(event)
        result = assembler.build_response()
        if assembler.input_tokens or assembler.output_tokens:
            result["usage"] = {
                "prompt_tokens": assembler.input_tokens,
                "completion_tokens": assembler.output_tokens,
                "cached_tokens": assembler.cached_tokens,
            }
        return result

    async def _stream_events(
        self, params: dict[str, Any],
    ) -> AsyncIterator[Any]:
        """流式：文本/thinking 增量随到随 yield，终止于 StreamDone（单次尝试）。"""
        assembler = _StreamAssembler()
        stream = await self.client.messages.create(**params, stream=True)
        async for event in stream:
            out = assembler.on_event(event)
            if out is None:
                continue
            kind, value = out
            if kind == "text":
                yield value
            elif kind == "reasoning":
                yield StreamReasoning(value)
        yield StreamDone(
            response=assembler.build_response(),
            token_count=max(assembler.output_tokens, 1),
            input_tokens=assembler.input_tokens,
            cached_tokens=assembler.cached_tokens,
        )


if __name__ == "__main__":
    # 离线自检：纯转换函数，绝不联网。
    system, msgs = messages_to_anthropic(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "thinking",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "f", "arguments": '{"a": 1}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "42"},
        ]
    )
    assert system == "sys"
    assert msgs[0]["content"] == [{"type": "text", "text": "hi"}]
    assert msgs[1]["content"][1]["type"] == "tool_use"
    assert msgs[1]["content"][1]["input"] == {"a": 1}
    assert msgs[2]["content"][0]["type"] == "tool_result"
    assert msgs[2]["content"][0]["tool_use_id"] == "c1"

    tools = tools_to_anthropic(
        [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
    )
    assert tools[0]["name"] == "f" and tools[0]["input_schema"] == {"type": "object"}

    image_blocks = user_content_to_blocks(
        [{"type": "text", "text": "t"},
         {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
    )
    assert image_blocks[1]["source"] == {"type": "base64", "media_type": "image/png", "data": "AAAA"}

    # 流装配：text + thinking + tool_use 分片 -> build_response
    class _E:
        def __init__(self, **kw):
            self.__dict__.update(kw)
    asm = _StreamAssembler()
    asm.on_event(_E(type="message_start", message=_E(usage=_E(input_tokens=3))))
    asm.on_event(_E(type="content_block_start", index=0,
                    content_block=_E(type="thinking", thinking="", name="", id="")))
    assert asm.on_event(_E(type="content_block_delta", index=0,
                           delta=_E(type="thinking_delta", thinking="step")))[0] == "reasoning"
    asm.on_event(_E(type="content_block_start", index=1,
                    content_block=_E(type="text", text="")))
    assert asm.on_event(_E(type="content_block_delta", index=1,
                           delta=_E(type="text_delta", text="hello")))[0] == "text"
    asm.on_event(_E(type="content_block_start", index=2,
                    content_block=_E(type="tool_use", name="f", id="c1")))
    asm.on_event(_E(type="content_block_delta", index=2,
                    delta=_E(type="input_json_delta", partial_json='{"a":')))
    assert asm.on_event(_E(type="content_block_delta", index=2,
                           delta=_E(type="input_json_delta", partial_json="1}"))) is None
    asm.on_event(_E(type="message_delta", delta=_E(stop_reason="tool_use"),
                    usage=_E(output_tokens=7)))
    resp = asm.build_response()
    assert resp["content"] == "hello"
    assert resp["tool_calls"] == [{"id": "c1", "type": "function",
                                   "function": {"name": "f", "arguments": '{"a":1}'}}]
    assert asm.input_tokens == 3 and asm.output_tokens == 7
    print(f"AnthropicProvider ready (offline; tools={len(tools)}, blocks={len(image_blocks)})")
    print("openx/llm/anthropic.py OK ✓")
