"""模型接入槽的接口形状 -- 内核不变量（槽接口形状）。

形状进内核，实现进零件（``llm/`` 只做协议适配）：Provider 协议、流
事件类型、错误契约全部定义于此，纯定义、零 SDK 依赖--内核重试机制
（retry.py）只认识本文件的错误契约，不认识任何 SDK 异常。

错误契约分工：
- 实现层捕获 SDK 异常，瞬态的（连接/429/可重试 5xx）翻译为
  ``ProviderTransientError``（携带原始异常与 Retry-After），确定性的
  原样上抛；
- 内核重试层只捕获 ``ProviderTransientError`` 决定重试与否，配额耗尽
  时重新抛出 ``.original``--调用方看到的始终是原生错误类型。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional, Protocol, runtime_checkable


# ── 流事件（实现与消费方共用的语言）──────────────────────────────


@dataclass
class StreamReasoning:
    """A reasoning/thinking delta from the model.

    DeepSeek 系后端在正文之前以 ``delta.reasoning_content`` 逐块发送推理
    过程；部分 o-series 风格后端用 ``reasoning`` 字段。展示层默认折叠
    （Ctrl+R 展开）--推理内容是模型输出的一部分，对用户必须可见但不
    应与正文混排。不写入 history（部分后端拒收未知消息键，且 DeepSeek
    官方建议不回放 reasoning_content）。
    """

    text: str


@dataclass
class StreamDone:
    """Signals the end of a streaming response.

    Attributes:
        response: The fully assembled assistant message dict (with ``tool_calls``
            if the model requested any).
        token_count: Approximate number of output tokens in this turn.
        input_tokens: Number of input (prompt) tokens for this turn, if the
            provider returned usage info; otherwise 0 (caller may estimate).
        cached_tokens: Number of prompt tokens served from the provider's
            cache (OpenAI ``prompt_tokens_details.cached_tokens`` / Anthropic
            ``cache_read_input_tokens``), if reported; otherwise 0.
    """

    response: dict[str, Any]
    token_count: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0


# A streaming event is either a text token string, a reasoning/thinking
# delta, or the terminal sentinel.
StreamEvent = str | StreamReasoning | StreamDone


# ── 错误契约 ────────────────────────────────────────────────────


class ProviderTransientError(Exception):
    """瞬态错误（连接失败/429/可重试 5xx）：内核可重试。

    ``original`` 保留原生异常（配额耗尽时由重试层重新抛出，调用方错误
    类型零变化）；``retry_after`` 透传服务端 Retry-After（秒，可 None）。
    """

    def __init__(
        self, original: BaseException, retry_after: Optional[float] = None
    ) -> None:
        super().__init__(f"{type(original).__name__}: {original}")
        self.original = original
        self.retry_after = retry_after


class ProviderFatalError(Exception):
    """确定性错误（401/400/参数错）：重试无意义，直接上抛。

    实现层通常直接上抛原生异常即可（重试层只捕获瞬态契约）；本类用于
    实现层自检出的确定性故障（如配置缺失）。
    """


# ── Provider 协议：模型接入槽的形状 ──────────────────────────────


@runtime_checkable
class Provider(Protocol):
    """模型 provider 的接口形状--内核不变量。

    实现约定：
    - ``messages`` / 返回值 / ``tool_calls`` 均为 OpenAI 消息格式（系统
      其余部分只说这一种格式，非 OpenAI 协议的实现负责在边界双向转换）；
    - 单次尝试语义：实现自身不做重试（重试归内核，见 retry.py）；
    - 错误遵循本模块的契约。
    """

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        stream: bool = True,
    ) -> dict[str, Any]:
        """完整一轮对话：返回组装好的 assistant 消息 dict。

        ``stream=True`` 时实现内部流式重组（调用方只见最终 dict，无可见
        中间态）；``stream=False`` 走非流式端点。
        """
        ...

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> AsyncIterator[StreamEvent]:
        """逐事件流式：yield 文本/reasoning 增量，终止于 ``StreamDone``。

        工具调用分片由实现内部缓冲，只在 StreamDone 的 response 中出现。
        """
        ...
