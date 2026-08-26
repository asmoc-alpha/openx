"""OpenAI 兼容 provider 实现 + LLMClient 兼容门面。

分层（模型接入层 P1，见 docs/design/provider-access-design.md）：

- **接口形状与重试在内核**：``kernel/provider.py``（Provider 协议、流
  事件、错误契约）、``kernel/retry.py``（重试策略与 RetryingProvider）；
- **本模块只做协议适配**：``OpenAICompatProvider`` 是单次尝试实现--
  捕获 openai SDK 异常并按错误契约翻译（瞬态 -> ProviderTransientError
  携带原生异常与 Retry-After；确定性 -> 原样上抛）；
- **``LLMClient`` 是兼容门面**：实现 + 内核重试包装的组合，对外保持
  旧 API（chat / stream_chat / on_retry / .client / _client），存量
  调用方与测试零改动。
"""

from __future__ import annotations

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

import asyncio
from typing import Any, AsyncIterator, Callable, Optional

from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from ..config import OpenXConfig
from ..kernel.provider import (
    Provider,
    ProviderFatalError,  # noqa: F401 -- 契约 re-export（实现层可用）
    ProviderTransientError,
    StreamDone,
    StreamReasoning,
    StreamEvent,
)
from ..kernel.retry import (
    MAX_RETRY_DELAY,
    RetryingProvider,
    compute_delay,
)

# 策略函数兼容 re-export（存量测试 import 点）：计算逻辑已上收内核。
_compute_delay = compute_delay

# 睡眠间接引用：测试按项目惯例 monkeypatch 本模块常量实现瞬时重试。
_sleep = asyncio.sleep


async def _patchable_sleep(delay: float) -> None:
    """解析期晚绑定包装：monkeypatch ``client._sleep`` 后随之生效。

    门面把它注入 RetryingProvider--内核重试默认用自己的 _sleep，门面
    侧保留本模块的测试 monkeypatch 点不动。
    """
    await _sleep(delay)


# ── 重试策略常量（随计算上收内核，此处保留语义说明）────────────────
# 可重试的 HTTP 状态码（对齐 openai SDK 的默认可重试集合）：
# 408 请求超时 / 409 冲突（部分网关瞬态）/ 429 限流 / 5xx 服务端错误。
# 其余 4xx（400 参数错、401 鉴权、403、404）重试无意义，直接抛。
_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


def _parse_retry_after(response: Any) -> Optional[float]:
    """从 httpx 响应头解析 Retry-After（秒）。

    只接受数值格式；HTTP-date 格式返回 None（回退指数退避）。
    任何异常一律返回 None--重试决策绝不被头解析拖垮。
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


def _classify_error(err: BaseException) -> tuple[bool, Optional[float]]:
    """判定异常是否可重试（SDK 特定分类，留在实现层）。

    返回 ``(retryable, retry_after)``：
    - 连接类错误（含 APITimeoutError，它是 APIConnectionError 子类）-> 可重试；
    - APIStatusError 且状态码在 _RETRYABLE_STATUS -> 可重试，附带 Retry-After（可能 None）；
    - 其他一律不可重试。
    """
    if isinstance(err, APIConnectionError):
        return True, None
    if isinstance(err, APIStatusError) and err.status_code in _RETRYABLE_STATUS:
        return True, _parse_retry_after(getattr(err, "response", None))
    return False, None


def _translate(err: BaseException) -> None:
    """SDK 异常 -> 错误契约（NoReturn 语义：要么抛瞬态契约，要么原样重抛）。

    瞬态错误包装为 ProviderTransientError（携带原生异常与 Retry-After，
    供内核重试层决策）；确定性错误原样上抛（内核重试层只认瞬态契约，
    自然穿透）。
    """
    retryable, retry_after = _classify_error(err)
    if retryable:
        raise ProviderTransientError(err, retry_after) from err
    raise err


# ── 单次尝试实现 ────────────────────────────────────────────────


class OpenAICompatProvider:
    """OpenAI 兼容端点的单次尝试实现（重试由内核 RetryingProvider 包装）。

    消息进出的格式、流式重组、推理增量、usage 透出语义与原 LLMClient
    逐条一致；唯一的差异是把重试循环交了出去。
    """

    def __init__(self, config: OpenXConfig):
        self.config = config
        self._client: Optional[AsyncOpenAI] = None

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self.config.api_key,
                base_url=self.config.api_base,
                timeout=120.0,
                # SDK 内置重试关闭：重试统一归内核（需覆盖流中断、
                # 注入可见性回调、遵循 max_retries），双层重试会
                # 把等待时间乘起来且对用户不可见。
                max_retries=0,
            )
        return self._client

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        stream: bool = True,
    ) -> dict[str, Any]:
        """Send a chat completion request (single attempt).

        Args:
            messages: List of message dicts in OpenAI format.
            tools: Optional list of tool definitions.
            stream: Whether to use streaming.

        Returns:
            The full assistant message dict (with tool_calls if any).
        """
        params: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"

        if stream:
            return await self._stream_response(params)

        try:
            response = await self.client.chat.completions.create(**params)
            return self._parse_response(response)
        except ProviderTransientError:
            raise
        except Exception as err:
            _translate(err)

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> AsyncIterator[StreamEvent]:
        """True token-by-token streaming (single attempt).

        Yields each text token as a ``str`` as it arrives from the API.
        Reasoning/thinking deltas (``reasoning_content`` / ``reasoning``)
        arrive before content and are yielded as :class:`StreamReasoning`.
        When the response is complete, yields a :class:`StreamDone` with
        the assembled message dict and an approximate token count.

        Tool-call fragments are buffered internally; the caller only
        receives text tokens and then the final ``StreamDone`` carrying
        any ``tool_calls``.
        """
        params: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"

        # 请求流式 usage 统计：OpenAI 兼容服务会在最后一个 chunk 附带 usage
        # （prompt_tokens / completion_tokens）。部分后端不支持该选项会被忽略，
        # 因此用 try 兜底，失败则回退到字符估算。
        try:
            params["stream_options"] = {"include_usage": True}
        except Exception:
            pass

        collected_content = ""
        tool_call_buffers: dict[int, dict[str, Any]] = {}
        token_count = 0          # 输出 token 近似计数（按 delta 累加）
        input_tokens = 0         # 来自 usage chunk 的真实输入 token，0 表示未知

        try:
            stream = await self.client.chat.completions.create(**params, stream=True)

            async for chunk in stream:
                # 带 usage 的终止 chunk：choices 可能为空，仅含 usage 字段
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    input_tokens = getattr(usage, "prompt_tokens", 0) or 0
                    # 若服务端给了 completion_tokens，优先采用，覆盖近似值
                    ct = getattr(usage, "completion_tokens", 0)
                    if ct:
                        token_count = ct

                delta = chunk.choices[0].delta if chunk.choices else None
                if delta is None:
                    continue

                # 推理内容（reasoning_content / reasoning）-- 先于正文
                # 到达，立即 yield 供展示层折叠呈现。它是已上屏的可见
                # 文本：中途断流后内核不会透明重试（重试会让 thinking
                # 重复）。不计 token_count（近似计数只跟踪正文与工具分片）。
                reasoning = getattr(delta, "reasoning_content", None) or getattr(
                    delta, "reasoning", None
                )
                if reasoning:
                    yield StreamReasoning(reasoning)

                # 文本内容 -- 立即 yield，实现打字机效果
                if delta.content:
                    collected_content += delta.content
                    token_count += 1  # 每个 delta.content 近似 1 token
                    yield delta.content

                # 工具调用（分片到达 -- 先缓冲，不 yield）
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_call_buffers:
                            tool_call_buffers[idx] = {
                                "id": tc.id or "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        buf = tool_call_buffers[idx]
                        if tc.id:
                            buf["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                buf["function"]["name"] += tc.function.name
                            if tc.function.arguments:
                                buf["function"]["arguments"] += tc.function.arguments
                                token_count += 1
        except ProviderTransientError:
            raise
        except Exception as err:
            _translate(err)

        # 按 index 顺序固化工具调用（流式可能乱序到达）
        collected_tool_calls = [
            tool_call_buffers[idx] for idx in sorted(tool_call_buffers.keys())
        ]

        result: dict[str, Any] = {
            "role": "assistant",
            "content": collected_content or None,
        }
        if collected_tool_calls:
            result["tool_calls"] = collected_tool_calls

        yield StreamDone(
            response=result,
            token_count=max(token_count, 1),
            input_tokens=input_tokens,
        )

    async def _stream_response(self, params: dict[str, Any]) -> dict[str, Any]:
        """Stream the response and reassemble (single attempt)."""
        # 请求流式 usage 统计（最后一个 chunk 附带），镜像 stream_chat 的做法；
        # 不支持该选项的后端会被忽略。
        try:
            params["stream_options"] = {"include_usage": True}
        except Exception:
            pass

        # 本方法内部重组、调用方只看到最终 dict--任何时刻失败对调用方
        # 都无可见中间态，内核重试层可整请求重试。
        try:
            stream = await self.client.chat.completions.create(**params, stream=True)

            collected_content = ""
            tool_call_buffers: dict[int, dict[str, Any]] = {}
            input_tokens = 0       # 来自 usage chunk 的真实输入 token，0 表示未知
            completion_tokens = 0  # 服务端返回的输出 token，0 表示未知

            async for chunk in stream:
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    input_tokens = getattr(usage, "prompt_tokens", 0) or 0
                    completion_tokens = getattr(usage, "completion_tokens", 0) or 0

                delta = chunk.choices[0].delta if chunk.choices else None
                if delta is None:
                    continue

                # Text content
                if delta.content:
                    collected_content += delta.content

                # Tool calls (streamed in pieces)
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_call_buffers:
                            tool_call_buffers[idx] = {
                                "id": tc.id or "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        buf = tool_call_buffers[idx]
                        if tc.id:
                            buf["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                buf["function"]["name"] += tc.function.name
                            if tc.function.arguments:
                                buf["function"]["arguments"] += tc.function.arguments

            # Finalize tool calls
            collected_tool_calls = [
                tool_call_buffers[idx] for idx in sorted(tool_call_buffers.keys())
            ]

            result: dict[str, Any] = {
                "role": "assistant",
                "content": collected_content or None,
            }
            if collected_tool_calls:
                result["tool_calls"] = collected_tool_calls
            # 透出 usage（供 agent.run() 累计 token；调用方读取后须将其剥离消息）
            if input_tokens or completion_tokens:
                result["usage"] = {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": completion_tokens,
                }

            return result
        except ProviderTransientError:
            raise
        except Exception as err:
            _translate(err)

    @staticmethod
    def _parse_response(response: Any) -> dict[str, Any]:
        """Parse a non-streaming response."""
        msg = response.choices[0].message
        result: dict[str, Any] = {
            "role": "assistant",
            "content": msg.content,
        }
        if msg.tool_calls:
            result["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        # 非流式响应通常自带 usage；透出供 agent.run() 累计 token。
        # 注意：调用方读取后必须 pop 掉，usage 绝不能随消息进入对话历史。
        usage = getattr(response, "usage", None)
        if usage is not None:
            result["usage"] = {
                "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
            }
        return result


# ── 兼容门面 ────────────────────────────────────────────────────


class _ConfigPolicy:
    """config 读写透传的策略对象：max_retries/base_delay 晚绑定。

    构造后再改 config（如运行期调整）重试策略随之生效--与旧 LLMClient
    直接读 self.config 的语义一致。
    """

    def __init__(self, config: OpenXConfig) -> None:
        self._cfg = config
        self.cap = MAX_RETRY_DELAY

    @property
    def max_retries(self) -> int:
        return self._cfg.max_retries

    @property
    def base_delay(self) -> float:
        return self._cfg.retry_base_delay


class LLMClient:
    """兼容门面：``OpenAICompatProvider``（单次实现）+ 内核 ``RetryingProvider``。

    重试语义（与旧实现逐条一致，决策与等待已上收内核）：

    - **请求级**：``chat()`` 抛 429/可重试 5xx/连接错误 -> 指数退避重试，
      至多 ``config.max_retries`` 次（429 带 ``Retry-After`` 时优先采用）。
    - **流级**：迭代 chunk 中途断流同样可重试，但仅限"调用方尚未看到任何
      输出"时--``stream_chat`` 一旦 yield 过文本 token 就只能上抛（透明
      重试会造成 UI 重复）；``chat(stream=True)`` 内部重组、不外露中间态，
      任何时刻都可整请求重试。
    - **可见性**：每次重试前触发 ``on_retry(attempt, max_retries, error,
      delay)`` 同步回调（缺省 None = 静默）；回调异常被吞，绝不影响重试。
    """

    def __init__(self, config: OpenXConfig, impl: Optional[Provider] = None):
        self.config = config
        # impl 注入点（模型接入层 M2）：内核 providers 注册表解析出的实现；
        # None -> 直接构造 openai-compat 实现（未过内核的直连路径）。
        self._impl = impl if impl is not None else OpenAICompatProvider(config)
        self._retrying = RetryingProvider(
            self._impl,
            policy=_ConfigPolicy(config),
            sleep=_patchable_sleep,  # 保留本模块 _sleep 的测试 monkeypatch 点
        )

    # on_retry：转发到重试层（可后置赋值）
    @property
    def on_retry(
        self,
    ) -> Optional[Callable[[int, int, BaseException, float], None]]:
        return self._retrying.on_retry

    @on_retry.setter
    def on_retry(
        self, value: Optional[Callable[[int, int, BaseException, float], None]]
    ) -> None:
        self._retrying.on_retry = value

    # _client：转发到实现（测试注入 Fake SDK 客户端的既有路径）
    @property
    def _client(self) -> Optional[AsyncOpenAI]:
        return self._impl._client

    @_client.setter
    def _client(self, value: Any) -> None:
        self._impl._client = value

    @property
    def client(self) -> AsyncOpenAI:
        return self._impl.client

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        stream: bool = True,
    ) -> dict[str, Any]:
        """Send a chat completion request (with kernel-owned retry)."""
        return await self._retrying.chat(messages, tools=tools, stream=stream)

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> AsyncIterator[StreamEvent]:
        """True token-by-token streaming (with kernel-owned retry)."""
        return self._retrying.stream_chat(messages, tools=tools)


if __name__ == "__main__":
    from ..config import OpenXConfig as _Cfg
    cfg = _Cfg(api_key="sk-selftest", api_base="http://127.0.0.1:1/v1", model="fake-model")
    llm = LLMClient(cfg)  # AsyncOpenAI 惰性创建，实例化绝不联网
    assert llm._client is None  # 未触发 .client 属性 -> 绝无网络请求
    done = StreamDone(response={"role": "assistant", "content": "hi"}, token_count=3, input_tokens=7)
    assert done.response["content"] == "hi" and done.token_count == 3 and done.input_tokens == 7
    # 重试策略离线验证：指数退避区间、封顶、Retry-After 优先、base=0 瞬时
    d0 = _compute_delay(0, 1.0, None)
    assert 1.0 <= d0 < 2.0, d0                     # base·2^0 + jitter∈[0,base)
    d3 = _compute_delay(3, 1.0, None)
    assert 8.0 <= d3 < 9.0, d3                     # base·2^3 + jitter
    assert _compute_delay(9, 1.0, None) == MAX_RETRY_DELAY   # 60s 封顶
    assert _compute_delay(0, 1.0, 7.5) == 7.5      # Retry-After 优先
    assert _compute_delay(0, 1.0, 999.0) == MAX_RETRY_DELAY  # 服务端值也封顶
    assert _compute_delay(2, 0.0, None) == 0.0     # base=0 -> 测试瞬时重试
    ok, ra = _classify_error(ValueError("boom"))
    assert not ok and ra is None                   # 非 API 错误不可重试
    print(f"retry policy: delay(0)={d0:.2f}s delay(3)={d3:.2f}s cap={MAX_RETRY_DELAY}s ✓")
    print(f"LLMClient ready (model={llm.config.model!r}, offline, no requests sent)")
    print("openx/llm/client.py OK ✓")
