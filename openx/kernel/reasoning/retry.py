"""重试策略与重试包装 -- "重试/退避/超时归内核"的机械落点。

语义与原 LLMClient 内嵌重试逐条等价（这是硬约束）：

- **请求级**：``chat()`` 抛 ``ProviderTransientError`` -> 指数退避重试，
  至多 ``policy.max_retries`` 次（携带 Retry-After 时优先采用）；
- **流级**：``stream_chat()`` 仅在**尚未 yield 任何事件**时可透明重试--
  已产出文本/reasoning 后断流只能上抛（透明重试会造成 UI 重复）；
- **可见性**：每次重试前触发 ``on_retry(attempt, max_retries, error,
  delay)``（error 为原生异常）；回调异常被吞，绝不影响重试；
- **耗尽**：重新抛出 ``ProviderTransientError.original``--调用方看到的
  始终是原生错误类型；
- **确定性错误**：非瞬态契约的异常（含 ProviderFatalError）原样穿透，
  绝不重试。

只认识 kernel/provider.py 的错误契约，不认识任何 SDK 异常。
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, AsyncIterator, Callable, Optional

from .provider import Provider, ProviderTransientError, StreamEvent

# 单次退避等待封顶（秒）--Retry-After 与指数退避共用上限。
MAX_RETRY_DELAY = 60.0

# 睡眠间接引用：测试 monkeypatch 本模块常量实现瞬时重试。
_sleep = asyncio.sleep


def compute_delay(
    attempt: int,
    base: float,
    retry_after: Optional[float],
    cap: float = MAX_RETRY_DELAY,
) -> float:
    """计算第 ``attempt`` 次重试（0 起）前的等待秒数。

    Retry-After 优先（服务端限流窗口）；否则指数退避 base·2^attempt 加
    均匀抖动 [0, base)（base=0 时无抖动，供测试瞬时重试）。封顶 cap。
    """
    if retry_after is not None:
        return max(0.0, min(retry_after, cap))
    exp = base * (2 ** attempt)
    jitter = random.uniform(0.0, base) if base > 0 else 0.0
    return min(exp + jitter, cap)


class RetryPolicy:
    """重试策略值对象（鸭子类型：任何带这三个属性的对象皆可）。"""

    def __init__(self, max_retries: int = 4, base_delay: float = 1.0) -> None:
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.cap = MAX_RETRY_DELAY


class RetryingProvider:
    """内核所有的重试包装：实现 Provider 接口，组合替代继承。

    单次实现（无重试循环的 provider）经本包装获得完整的重试语义；
    ``on_retry`` 可后置赋值，``sleep`` 可注入（测试/门面用）。
    """

    def __init__(
        self,
        provider: Provider,
        policy: Optional[Any] = None,
        on_retry: Optional[
            Callable[[int, int, BaseException, float], None]
        ] = None,
        sleep: Optional[Callable[[float], Any]] = None,
    ) -> None:
        self.provider = provider
        self.policy = policy if policy is not None else RetryPolicy()
        self.on_retry = on_retry
        self._sleep_fn = sleep

    async def _notify_and_sleep(
        self, attempt: int, error: BaseException, delay: float
    ) -> None:
        """触发 on_retry 通知并等待 delay 秒。

        ``attempt`` 为 1 起的重试序号（第 1 次重试 = 1）。通知回调的任何
        异常都被吞掉--UI 故障绝不能把重试本身搞崩。
        """
        if self.on_retry is not None:
            try:
                self.on_retry(attempt, self.policy.max_retries, error, delay)
            except Exception:
                pass
        if delay > 0:
            if self._sleep_fn is not None:
                await self._sleep_fn(delay)
            else:
                await _sleep(delay)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        stream: bool = True,
    ) -> dict[str, Any]:
        """整请求重试：chat 无可见中间态，任何时刻失败都可重试。"""
        attempt = 0
        while True:
            try:
                return await self.provider.chat(messages, tools=tools, stream=stream)
            except ProviderTransientError as err:
                if attempt >= self.policy.max_retries:
                    raise err.original from err
                delay = compute_delay(
                    attempt, self.policy.base_delay, err.retry_after, self.policy.cap
                )
                await self._notify_and_sleep(attempt + 1, err.original, delay)
                attempt += 1

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> AsyncIterator[StreamEvent]:
        """流级重试：仅未产出任何事件时可透明重试，之后断流只能上抛。"""
        attempt = 0
        emitted = False  # 是否已向调用方 yield 过事件（文本/reasoning 均算）
        while True:
            try:
                async for event in self.provider.stream_chat(messages, tools=tools):
                    emitted = True
                    yield event
                return
            except ProviderTransientError as err:
                if emitted or attempt >= self.policy.max_retries:
                    raise err.original from err
                delay = compute_delay(
                    attempt, self.policy.base_delay, err.retry_after, self.policy.cap
                )
                await self._notify_and_sleep(attempt + 1, err.original, delay)
                attempt += 1
