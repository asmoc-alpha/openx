"""LLM 实现层通用骨架：协议适配的编排面 + 共享错误翻译。

llm/ 下的 provider 实现（openai_compat / anthropic / 未来其他协议）共享的
是**编排面**：chat / stream_chat 的调用骨架、SDK 异常 -> 错误契约的翻译、
客户端惰性创建。真正不同的部分（边界消息转换、响应/流解析）留在各自子类
--那才是各协议各异的协议适配，强行上提只会把差异塞进基类的条件分支。

分层边界：接口形状（``Provider`` 协议）与流事件类型在 ``kernel/provider.py``，
重试（``RetryingProvider``）在 ``kernel/retry.py``--本模块只共享编排，不碰
内核不变量。

子类需实现 6 个钩子：
- ``_make_client``：惰性创建 SDK 客户端（首次访问 ``self.client`` 时调用）；
- ``_build_params``：边界消息（OpenAI 格式）-> 该协议的请求参数；
- ``_send``：非流式协议调用 -> 原生响应对象；
- ``_parse_response``：原生非流式响应 -> OpenAI assistant 消息 dict；
- ``_chat_streaming``：流式内部重组（chat(stream=True)，无可见中间态）；
- ``_stream_events``：协议流 -> StreamEvent 序列（str/StreamReasoning/StreamDone）。

错误翻译只需声明 SDK 异常类型（``_CONN_ERROR_TYPES`` / ``_STATUS_ERROR_TYPE``），
编排面统一分类翻译。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Optional

from ..kernel.reasoning.provider import ProviderTransientError, StreamEvent

# 可重试的 HTTP 状态码（openai/anthropic 共用集合，对齐各自 SDK 的默认
# 可重试集）：408 请求超时 / 409 冲突（部分网关瞬态）/ 429 限流 / 5xx。
# 其余 4xx（400 参数错、401 鉴权、403、404）重试无意义，直接抛。
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


def parse_retry_after(response: Any) -> Optional[float]:
    """从 httpx 响应头解析 Retry-After（秒）。

    只接受数值格式；HTTP-date 格式返回 None（回退指数退避）。任何异常
    一律返回 None--重试决策绝不被头解析拖垮。
    """
    if response is None:
        return None
    try:
        raw = response.headers.get("retry-after")
    except Exception:
        return None
    if not raw:
        return None
    try:
        return float(str(raw).strip())
    except ValueError:
        return None


class LLMProvider(ABC):
    """llm 实现层基类：编排面共享，协议特有面留子类。

    约定（与 kernel/provider.py 的 ``Provider`` 协议一致）：
    - 消息进出均为 OpenAI 格式，非 OpenAI 协议的实现在边界转换；
    - 单次尝试语义（重试归内核 ``RetryingProvider`` 包装）；
    - 错误遵循错误契约（瞬态 -> ProviderTransientError / 确定性原样上抛）。
    """

    # 子类声明其 SDK 的异常类型：连接类（含超时）= 可重试；带 status_code
    # 的 API 异常按 RETRYABLE_STATUS 判定。SDK 缺失时两者皆空 -> 分类恒
    # False（确定性错误原样穿透，绝不误重试）。
    _CONN_ERROR_TYPES: tuple = ()
    _STATUS_ERROR_TYPE: Optional[type] = None

    def __init__(self, config: Any) -> None:
        self.config = config
        self._client: Optional[Any] = None

    # ── 抽象钩子：协议特有面 ─────────────────────────────

    @abstractmethod
    def _make_client(self) -> Any:
        """惰性创建 SDK 客户端（首次访问 ``self.client`` 时调用）。"""

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._make_client()
        return self._client

    @abstractmethod
    def _build_params(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """边界消息（OpenAI 格式）-> 该协议的请求参数。"""

    @abstractmethod
    async def _send(self, params: dict[str, Any]) -> Any:
        """非流式协议调用 -> 原生响应对象。"""

    @abstractmethod
    def _parse_response(self, response: Any) -> dict[str, Any]:
        """原生非流式响应 -> OpenAI assistant 消息 dict。"""

    @abstractmethod
    async def _chat_streaming(self, params: dict[str, Any]) -> dict[str, Any]:
        """流式内部重组（chat(stream=True)）：无可见中间态，任何时刻可重试。"""

    @abstractmethod
    def _stream_events(
        self, params: dict[str, Any],
    ) -> AsyncIterator[StreamEvent]:
        """协议流 -> StreamEvent 序列（str / StreamReasoning / StreamDone）。"""

    # ── 共享编排面 ───────────────────────────────────────

    @classmethod
    def _classify(cls, err: BaseException) -> tuple[bool, Optional[float]]:
        """SDK 异常分类：连接类可重试；可重试状态码带 Retry-After 可重试。"""
        if cls._CONN_ERROR_TYPES and isinstance(err, cls._CONN_ERROR_TYPES):
            return True, None
        st = cls._STATUS_ERROR_TYPE
        if st is not None and isinstance(err, st):
            if err.status_code in RETRYABLE_STATUS:
                return True, parse_retry_after(getattr(err, "response", None))
        return False, None

    @classmethod
    def _translate(cls, err: BaseException) -> None:
        """SDK 异常 -> 错误契约：瞬态包装为 ProviderTransientError，确定性原样上抛。"""
        retryable, retry_after = cls._classify(err)
        if retryable:
            raise ProviderTransientError(err, retry_after) from err
        raise err

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        stream: bool = True,
    ) -> dict[str, Any]:
        """完整一轮对话：返回组装好的 assistant 消息 dict（OpenAI 格式）。

        ``stream=True`` 经 ``_chat_streaming`` 内部重组（调用方只见最终
        dict，无可见中间态）；``stream=False`` 走非流式端点。
        """
        params = self._build_params(messages, tools)
        if stream:
            try:
                return await self._chat_streaming(params)
            except ProviderTransientError:
                raise
            except Exception as err:
                self._translate(err)
        try:
            return self._parse_response(await self._send(params))
        except ProviderTransientError:
            raise
        except Exception as err:
            self._translate(err)

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> AsyncIterator[StreamEvent]:
        """逐事件流式：文本/reasoning 增量随到随 yield，终止于 StreamDone。"""
        params = self._build_params(messages, tools)
        try:
            async for ev in self._stream_events(params):
                yield ev
        except ProviderTransientError:
            raise
        except Exception as err:
            self._translate(err)
