"""插件调用防护（P-C）—— 调用包装器 + 结构化错误。

对插件工具的每次调用加：**timeout**（manifest 声明或默认）、**输出上限**
（资源限额）、**熔断**（连续崩溃自动摘除）。异常/超时 → 结构化错误供模型
决策（内核详设 §3.3）：``[tool]/[status]/[error]/[suggestion]``。

熔断语义：只计"插件崩溃"（异常/超时），**不计正常业务错误**——插件返回
``ToolResult(error=...)`` 是业务结果（如"文件不存在"），不是插件故障。
连续 ``MAX_FAILURES`` 次崩溃 → 熔断打开，后续调用短路返回 circuit open；
``on_trip`` 回调（装配侧接 ``kernel.unregister_tool``）把工具从注册表摘除，
防止模型反复调用坏插件。

进程隔离（执行隔离，D9 分层定价）不在本切片——这里是"异常可收敛"+
"错误语义化"。内置/结构工具不经本包装器（可信）。
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
from typing import Any, Callable, Optional

from ...tools.base import Tool, ToolResult

DEFAULT_TIMEOUT = 60.0        # 秒；manifest.timeout 覆盖
MAX_OUTPUT = 100_000          # 输出字符上限（资源限额）
MAX_FAILURES = 3              # 连续崩溃熔断阈值

_SUGGEST = "retry | unload_plugin this plugin or use a builtin alternative"


def structured_error(tool: str, status: str, error: str, suggestion: str = "") -> str:
    """结构化错误（§3.3 的文本化）：模型可据此决策（重试/卸载/换方案）。"""
    lines = [f"[tool: {tool}]", f"[status: {status}]", f"[error: {error}]"]
    if suggestion:
        lines.append(f"[suggestion: {suggestion}]")
    return "\n".join(lines)


class ProtectPluginTool(Tool):
    """包一个插件工具：timeout + 输出上限 + 熔断 + 结构化错误。

    全量委托 Tool 表面（name/description/parameters/permission/
    validate_args/auto_allowed/is_high_risk/preview_diff/to_openai_schema），
    对 Guard / executor 透明——权限裁决照旧，只是执行多了防护。
    """

    def __init__(
        self,
        inner: Tool,
        timeout: Optional[float] = None,
        max_output: int = MAX_OUTPUT,
        max_failures: int = MAX_FAILURES,
        on_trip: Optional[Callable[["ProtectPluginTool"], None]] = None,
    ) -> None:
        self._inner = inner
        self.name = inner.name
        self.description = inner.description
        self.parameters = inner.parameters
        self._timeout = float(timeout or DEFAULT_TIMEOUT)
        self._max_output = max_output
        self._max_failures = max_failures
        self._failures = 0
        self._tripped = False
        self._on_trip = on_trip

    # ── 委托 Tool 表面 ──────────────────────────────────────────

    @property
    def permission(self) -> Any:
        return self._inner.permission

    def validate_args(self, **kwargs: Any) -> Optional[str]:
        return self._inner.validate_args(**kwargs)

    def auto_allowed(self, args: dict) -> bool:
        return self._inner.auto_allowed(args)

    def is_high_risk(self, args: dict) -> bool:
        return self._inner.is_high_risk(args)

    def preview_diff(self, args: dict) -> Optional[tuple]:
        return self._inner.preview_diff(args)

    def to_openai_schema(self) -> dict:
        return self._inner.to_openai_schema()

    # ── 执行：防护主体 ──────────────────────────────────────────

    async def execute(self, **kwargs: Any) -> ToolResult:
        if self._tripped:
            return ToolResult(error=structured_error(
                self.name, "circuit_open",
                "Plugin tool tripped its circuit breaker after repeated crashes.",
                _SUGGEST,
            ))
        try:
            result = await asyncio.wait_for(
                self._inner.execute(**kwargs), self._timeout
            )
        except asyncio.TimeoutError:
            return self._fail(structured_error(
                self.name, "timeout",
                f"timed out after {self._timeout:.0f}s",
                _SUGGEST,
            ))
        except Exception as e:
            return self._fail(structured_error(
                self.name, "plugin_error",
                f"{type(e).__name__}: {e}",
                _SUGGEST,
            ))
        # 资源限额：输出字符上限
        if result.output and len(result.output) > self._max_output:
            result.output = result.output[:self._max_output]
            result.truncated = True
            result.truncated_notice = (
                f"(output truncated: >{self._max_output} chars)"
            )
        return result

    def _fail(self, err: str) -> ToolResult:
        """记一次插件崩溃；达到阈值 → 熔断 + on_trip 回调。"""
        self._failures += 1
        if self._failures >= self._max_failures:
            self._tripped = True
            if self._on_trip is not None:
                try:
                    self._on_trip(self)
                except Exception:
                    pass
        return ToolResult(error=err)


if __name__ == "__main__":
    import asyncio

    async def _check() -> None:
        class _OkTool(Tool):
            name = "ok"
            async def execute(self, **kw):
                return ToolResult(output="fine")

        class _CrashTool(Tool):
            name = "crash"
            async def execute(self, **kw):
                raise RuntimeError("boom")

        class _SlowTool(Tool):
            name = "slow"
            async def execute(self, **kw):
                await asyncio.sleep(5)
                return ToolResult(output="late")

        class _BizErrTool(Tool):
            name = "bizerr"
            async def execute(self, **kw):
                return ToolResult(error="file not found")  # 业务错，不计熔断

        trips = []
        # 正常
        r = await ProtectPluginTool(_OkTool()).execute()
        assert r.success and r.output == "fine"
        # 超时 → 结构化 timeout（短 timeout）
        r = await ProtectPluginTool(_SlowTool(), timeout=0.1).execute()
        assert "timeout" in r.error and "[status: timeout]" in r.error
        # 业务错不计熔断：反复调用不 trip
        w = ProtectPluginTool(_BizErrTool(), max_failures=3)
        for _ in range(5):
            assert (await w.execute()).error == "file not found"
        assert not w._tripped
        # 崩溃计熔断：3 次 → trip（第 3 次返回 plugin_error，on_trip 触发）
        w = ProtectPluginTool(_CrashTool(), max_failures=3, on_trip=lambda t: trips.append(t.name))
        for _ in range(2):
            assert (await w.execute()).error
            assert not w._tripped
        r = await w.execute()  # 触发熔断：返回本次崩溃，标记 tripped
        assert "plugin_error" in r.error and trips == ["crash"] and w._tripped
        # 短路：后续直接 circuit open
        r = await w.execute()
        assert "[status: circuit_open]" in r.error
        # 输出上限
        big = _OkTool.__new__(_OkTool); big.name = "big"
        async def _big(**kw):
            return ToolResult(output="x" * 1000)
        big.execute = _big  # type: ignore[method-assign]
        r = await ProtectPluginTool(big, max_output=100).execute()
        assert len(r.output) == 100 and r.truncated

    asyncio.run(_check())
    print("openx/kernel/protect.py OK ✓")
