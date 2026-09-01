"""openai-compat 实现 + LLMClient 兼容门面（命名按 provider kind 与 anthropic.py 对齐）。

分层（模型接入层 P1，见 docs/design/provider-access-design.md）：

- **接口形状与重试在内核**：``kernel/provider.py``（Provider 协议、流
  事件、错误契约）、``kernel/retry.py``（重试策略与 RetryingProvider）；
- **本模块只做协议适配**：``OpenAICompatProvider`` 是单次尝试实现--继承
   ``llm/base.py`` 的 ``LLMProvider`` 编排面，只实现 openai 协议特有的
   钩子（客户端构造 / 请求参数 / 响应与流解析）；SDK 异常 -> 错误契约的
   翻译由基类统一完成；
- **``LLMClient`` 是兼容门面**：实现 + 内核重试包装的组合，对外保持
  旧 API（chat / stream_chat / on_retry / .client / _client），存量
  调用方与测试零改动。门面随 openai-compat 实现同处一模块（设计文档
  定为"组 OpenAICompatProvider + RetryingProvider"的门面）。
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
from ..kernel.reasoning.provider import (
    Provider,
    ProviderFatalError,  # noqa: F401 -- 契约 re-export（实现层可用）
    StreamDone,
    StreamReasoning,
    StreamEvent,
)
from ..kernel.reasoning.retry import MAX_RETRY_DELAY, RetryingProvider, compute_delay
from .base import LLMProvider, parse_retry_after

# 兼容 re-export（存量测试 import 点）：Retry-After 解析已上移基类。
_parse_retry_after = parse_retry_after

# 策略函数兼容 re-export（存量测试 import 点）：计算逻辑已上收内核。
_compute_delay = compute_delay

# 睡眠间接引用：测试按项目惯例 monkeypatch 本模块常量实现瞬时重试。
_sleep = asyncio.sleep


async def _patchable_sleep(delay: float) -> None:
    """解析期晚绑定包装：monkeypatch ``openai_compat._sleep`` 后随之生效。

    门面把它注入 RetryingProvider--内核重试默认用自己的 _sleep，门面
    侧保留本模块的测试 monkeypatch 点不动。
    """
    await _sleep(delay)


# ── 单次尝试实现（继承基类编排面，只实现 openai 协议特有钩子）──────


class OpenAICompatProvider(LLMProvider):
    """openai 兼容端点的单次尝试实现（重试由内核 RetryingProvider 包装）。

    消息进出的格式、流式重组、推理增量、usage 透出语义与原 LLMClient
    逐条一致；唯一的差异是把重试循环交了出去。openai 的线上格式与边界
    （OpenAI 格式）相同，故 ``_build_params`` 不做消息转换--转换是恒等。
    """

    _CONN_ERROR_TYPES = (APIConnectionError,)  # 含 APITimeoutError（其子类）
    _STATUS_ERROR_TYPE = APIStatusError

    def _make_client(self) -> AsyncOpenAI:
        return AsyncOpenAI(
            api_key=self.config.api_key,
            base_url=self.config.api_base,
            timeout=120.0,
            # SDK 内置重试关闭：重试统一归内核（需覆盖流中断、注入可见性
            # 回调、遵循 max_retries），双层重试会把等待时间乘起来且对
            # 用户不可见。
            max_retries=0,
        )

    def _build_params(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"
        return params

    async def _send(self, params: dict[str, Any]) -> Any:
        return await self.client.chat.completions.create(**params)

    def _parse_response(self, response: Any) -> dict[str, Any]:
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

    async def _chat_streaming(self, params: dict[str, Any]) -> dict[str, Any]:
        """流式内部重组（chat(stream=True)）：无可见中间态，任何时刻可重试。"""
        # 请求流式 usage 统计：OpenAI 兼容服务会在最后一个 chunk 附带 usage
        # （prompt_tokens / completion_tokens）。部分后端不支持该选项会被忽略，
        # 因此用 try 兜底，失败则回退到字符估算。
        try:
            stream_params = dict(params, stream_options={"include_usage": True})
        except Exception:
            stream_params = params

        collected_content = ""
        tool_call_buffers: dict[int, dict[str, Any]] = {}
        input_tokens = 0       # 来自 usage chunk 的真实输入 token，0 表示未知
        completion_tokens = 0  # 服务端返回的输出 token，0 表示未知

        stream = await self.client.chat.completions.create(**stream_params, stream=True)

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

        # Finalize tool calls（流式可能乱序到达，按 index 排序固化）
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

    async def _stream_events(
        self, params: dict[str, Any],
    ) -> AsyncIterator[StreamEvent]:
        """流式：yield 文本/reasoning 增量，终止于 StreamDone（单次尝试）。"""
        try:
            stream_params = dict(params, stream_options={"include_usage": True})
        except Exception:
            stream_params = params

        collected_content = ""
        tool_call_buffers: dict[int, dict[str, Any]] = {}
        token_count = 0          # 输出 token 近似计数（按 delta 累加）
        input_tokens = 0         # 来自 usage chunk 的真实输入 token，0 表示未知

        stream = await self.client.chat.completions.create(**stream_params, stream=True)

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

            # 推理内容（reasoning_content / reasoning）-- 先于正文到达，
            # 立即 yield 供展示层折叠呈现。它是已上屏的可见文本：中途断流
            # 后内核不会透明重试（重试会让 thinking 重复）。不计
            # token_count（近似计数只跟踪正文与工具分片）。
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


# 兼容函数：模块级 SDK 异常分类（映射到基类 classmethod，测试 import 点）。
_classify_error = OpenAICompatProvider._classify


# ── 兼容门面 ────────────────────────────────────────────────────


class _ConfigPolicy:
    """config 读写透传的策略对象：max_retries/base_delay 晚绑定。

    构造后再改 config（如运行期调整）重试策略随之生效--与旧 LLMClient
    直接读 self.config 的语义一致。``overrides``（provider 实例配置）可
    按实例覆盖重试字段（M3）：实例显式声明 max_retries/retry_base_delay
    时优先，否则回落 config 实时值。
    """

    def __init__(self, config: OpenXConfig, overrides: Optional[dict] = None) -> None:
        self._cfg = config
        self._overrides = overrides or {}
        self.cap = MAX_RETRY_DELAY

    @property
    def max_retries(self) -> int:
        return self._overrides.get("max_retries", self._cfg.max_retries)

    @property
    def base_delay(self) -> float:
        return self._overrides.get("retry_base_delay", self._cfg.retry_base_delay)


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

    def __init__(
        self,
        config: OpenXConfig,
        impl: Optional[Provider] = None,
        policy_overrides: Optional[dict] = None,
    ):
        self.config = config
        # impl 注入点（模型接入层 M2）：内核 providers 注册表解析出的实现；
        # None -> 直接构造 openai-compat 实现（未过内核的直连路径）。
        self._impl = impl if impl is not None else OpenAICompatProvider(config)
        self._retrying = RetryingProvider(
            self._impl,
            policy=_ConfigPolicy(config, policy_overrides),
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
    print("openx/llm/openai_compat.py OK ✓")
