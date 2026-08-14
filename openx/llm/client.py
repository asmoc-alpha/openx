"""LLM client for OpenX.

Uses OpenAI-compatible API with function calling support.
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
import json
import random
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional

from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from ..config import OpenXConfig

# ── 重试策略常量 ─────────────────────────────────────────────────
# 可重试的 HTTP 状态码（对齐 openai SDK 的默认可重试集合）：
# 408 请求超时 / 409 冲突（部分网关瞬态）/ 429 限流 / 5xx 服务端错误。
# 其余 4xx（400 参数错、401 鉴权、403、404）重试无意义，直接抛。
_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})
# 单次退避等待封顶（秒）——Retry-After 与指数退避共用上限。
MAX_RETRY_DELAY = 60.0
# 睡眠间接引用：测试按项目惯例 monkeypatch 本模块常量实现瞬时重试。
_sleep = asyncio.sleep


def _parse_retry_after(response: Any) -> Optional[float]:
    """从 httpx 响应头解析 Retry-After（秒）。

    只接受数值格式；HTTP-date 格式返回 None（回退指数退避）。
    任何异常一律返回 None——重试决策绝不被头解析拖垮。
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
    """判定异常是否可重试。

    返回 ``(retryable, retry_after)``：
    - 连接类错误（含 APITimeoutError，它是 APIConnectionError 子类）→ 可重试；
    - APIStatusError 且状态码在 _RETRYABLE_STATUS → 可重试，附带 Retry-After（可能 None）；
    - 其他一律不可重试。
    """
    if isinstance(err, APIConnectionError):
        return True, None
    if isinstance(err, APIStatusError) and err.status_code in _RETRYABLE_STATUS:
        return True, _parse_retry_after(getattr(err, "response", None))
    return False, None


def _compute_delay(attempt: int, base: float, retry_after: Optional[float]) -> float:
    """计算第 ``attempt`` 次重试（0 起）前的等待秒数。

    Retry-After 优先（服务端限流窗口）；否则指数退避 base·2^attempt 加
    均匀抖动 [0, base)（base=0 时无抖动，供测试瞬时重试）。封顶 MAX_RETRY_DELAY。
    """
    if retry_after is not None:
        return max(0.0, min(retry_after, MAX_RETRY_DELAY))
    exp = base * (2 ** attempt)
    jitter = random.uniform(0.0, base) if base > 0 else 0.0
    return min(exp + jitter, MAX_RETRY_DELAY)


@dataclass
class StreamDone:
    """Signals the end of a streaming response.

    Attributes:
        response: The fully assembled assistant message dict (with ``tool_calls``
            if the model requested any).
        token_count: Approximate number of output tokens in this turn.
        input_tokens: Number of input (prompt) tokens for this turn, if the
            provider returned usage info; otherwise 0 (caller may estimate).
    """

    response: dict[str, Any]
    token_count: int = 0
    input_tokens: int = 0


@dataclass
class StreamReasoning:
    """A reasoning/thinking delta from the model.

    DeepSeek 系后端在正文之前以 ``delta.reasoning_content`` 逐块发送推理
    过程；部分 o-series 风格后端用 ``reasoning`` 字段。展示层默认折叠
    （Ctrl+R 展开）——推理内容是模型输出的一部分，对用户必须可见但不
    应与正文混排。不写入 history（部分后端拒收未知消息键，且 DeepSeek
    官方建议不回放 reasoning_content）。
    """

    text: str


# A streaming event is either a text token string, a reasoning/thinking
# delta, or the terminal sentinel.
StreamEvent = str | StreamReasoning | StreamDone


class LLMClient:
    """Async OpenAI-compatible LLM client with streaming and tool calling.

    重试语义（SDK 自身重试已关闭，统一由本类拥有）：

    - **请求级**：``create()`` 抛 429/可重试 5xx/连接错误 → 指数退避重试，
      至多 ``config.max_retries`` 次（429 带 ``Retry-After`` 时优先采用）。
    - **流级**：迭代 chunk 中途断流同样可重试，但仅限"调用方尚未看到任何
      输出"时——``stream_chat`` 一旦 yield 过文本 token 就只能上抛（透明
      重试会造成 UI 重复）；``_stream_response`` 内部重组、不外露中间态，
      任何时刻都可整请求重试。
    - **可见性**：每次重试前触发 ``on_retry(attempt, max_retries, error,
      delay)`` 同步回调（缺省 None = 静默）；回调异常被吞，绝不影响重试。
    """

    def __init__(self, config: OpenXConfig):
        self.config = config
        self._client: Optional[AsyncOpenAI] = None
        # 重试通知回调：(attempt, max_retries, error, delay_seconds)。
        # 不加类型注解之外的初始化——None 即静默重试。
        self.on_retry: Optional[Callable[[int, int, BaseException, float], None]] = None

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self.config.api_key,
                base_url=self.config.api_base,
                timeout=120.0,
                # SDK 内置重试关闭：本类统一掌管重试（需覆盖流中断、
                # 注入可见性回调、遵循 config.max_retries），双层重试会
                # 把等待时间乘起来且对用户不可见。
                max_retries=0,
            )
        return self._client

    async def _notify_and_sleep(self, attempt: int, error: BaseException, delay: float) -> None:
        """触发 on_retry 通知并等待 delay 秒。

        ``attempt`` 为 1 起的重试序号（第 1 次重试 = 1）。通知回调的任何
        异常都被吞掉——UI 故障绝不能把重试本身搞崩。
        """
        if self.on_retry is not None:
            try:
                self.on_retry(attempt, self.config.max_retries, error, delay)
            except Exception:
                pass
        if delay > 0:
            await _sleep(delay)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        stream: bool = True,
    ) -> dict[str, Any]:
        """Send a chat completion request.

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

        attempt = 0
        while True:
            try:
                response = await self.client.chat.completions.create(**params)
                return self._parse_response(response)
            except Exception as err:
                retryable, retry_after = _classify_error(err)
                if not retryable or attempt >= self.config.max_retries:
                    raise
                delay = _compute_delay(attempt, self.config.retry_base_delay, retry_after)
                await self._notify_and_sleep(attempt + 1, err, delay)
                attempt += 1

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> AsyncIterator[StreamEvent]:
        """True token-by-token streaming.

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

        # 重试循环：create() 失败或流中断（迭代抛错）均可整请求重试，
        # 但一旦 yield 过文本 token（emitted_text > 0）就只能上抛——
        # 透明重试会让已上屏的内容重复。工具调用分片不外露，重试时
        # 随缓冲区一并重置，无副作用。
        attempt = 0
        emitted_text = 0         # 已 yield 给调用方的文本 delta 数
        while True:
            collected_content = ""
            collected_tool_calls: list[dict[str, Any]] = []
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

                    # 推理内容（reasoning_content / reasoning）—— 先于正文
                    # 到达，立即 yield 供展示层折叠呈现。计入 emitted_text：
                    # 它是已上屏的可见文本，中途断流不可透明重试（重试会
                    # 让 thinking 重复）。不计 token_count（近似计数只跟踪
                    # 正文与工具分片）。
                    reasoning = getattr(delta, "reasoning_content", None) or getattr(
                        delta, "reasoning", None
                    )
                    if reasoning:
                        emitted_text += 1
                        yield StreamReasoning(reasoning)

                    # 文本内容 —— 立即 yield，实现打字机效果
                    if delta.content:
                        collected_content += delta.content
                        token_count += 1  # 每个 delta.content 近似 1 token
                        emitted_text += 1
                        yield delta.content

                    # 工具调用（分片到达 —— 先缓冲，不 yield）
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
            except Exception as err:
                retryable, retry_after = _classify_error(err)
                # 已产出可见文本 → 无法透明重试；配额耗尽 → 上抛
                if emitted_text or not retryable or attempt >= self.config.max_retries:
                    raise
                delay = _compute_delay(attempt, self.config.retry_base_delay, retry_after)
                await self._notify_and_sleep(attempt + 1, err, delay)
                attempt += 1
                continue

            # 按 index 顺序固化工具调用（流式可能乱序到达）
            for idx in sorted(tool_call_buffers.keys()):
                collected_tool_calls.append(tool_call_buffers[idx])

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
            return

    async def _stream_response(self, params: dict[str, Any]) -> dict[str, Any]:
        """Stream the response and reassemble."""
        # 请求流式 usage 统计（最后一个 chunk 附带），镜像 stream_chat 的做法；
        # 不支持该选项的后端会被忽略。
        try:
            params["stream_options"] = {"include_usage": True}
        except Exception:
            pass

        # 重试循环：本方法内部重组、调用方只看到最终 dict，任何时刻
        # 失败都可整请求重试（无可见中间态，不存在重复输出问题）。
        attempt = 0
        while True:
            try:
                stream = await self.client.chat.completions.create(**params, stream=True)

                collected_content = ""
                collected_tool_calls: list[dict[str, Any]] = []
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
                for idx in sorted(tool_call_buffers.keys()):
                    collected_tool_calls.append(tool_call_buffers[idx])

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
            except Exception as err:
                retryable, retry_after = _classify_error(err)
                if not retryable or attempt >= self.config.max_retries:
                    raise
                delay = _compute_delay(attempt, self.config.retry_base_delay, retry_after)
                await self._notify_and_sleep(attempt + 1, err, delay)
                attempt += 1

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


if __name__ == "__main__":
    from ..config import OpenXConfig
    cfg = OpenXConfig(api_key="sk-selftest", api_base="http://127.0.0.1:1/v1", model="fake-model")
    llm = LLMClient(cfg)  # AsyncOpenAI 惰性创建，实例化绝不联网
    assert llm._client is None  # 未触发 .client 属性 → 绝无网络请求
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
    assert _compute_delay(2, 0.0, None) == 0.0     # base=0 → 测试瞬时重试
    ok, ra = _classify_error(ValueError("boom"))
    assert not ok and ra is None                   # 非 API 错误不可重试
    print(f"retry policy: delay(0)={d0:.2f}s delay(3)={d3:.2f}s cap={MAX_RETRY_DELAY}s ✓")
    print(f"LLMClient ready (model={llm.config.model!r}, offline, no requests sent)")
    print("openx/llm/client.py OK ✓")
