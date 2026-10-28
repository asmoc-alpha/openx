"""消费方装配策略 -- 内核详设 v2.1 §0"取用通道收敛"的落点。

内核 API 回归四件 + ``registry(kind)`` 只读视图；装配策略住在消费方：
- 工具实例化与冲突仲裁（注册序即优先级、内置恒首、结构性工具占位）；
- provider 实现解析（kind -> 注册表条目 -> 实例；未注册由调用方决定
  回退与告警，UX 策略不进装配层）。

本模块只依赖内核的只读面（registry / inventory），不写注册表。
"""

from __future__ import annotations

import concurrent.futures
import time
from typing import Any, Mapping, Optional

from ..kernel.sandbox.protect import DEFAULT_TIMEOUT, ProtectPluginTool
from ..kernel.assembly.context import call_with_payload
from ..kernel.assembly.validate import validate_tool

# 插件上下文片段的默认字符预算（manifest cost 消费前的护栏：上下文类
# 插件多装时防止把系统提示撑爆）
CONTEXT_BUDGET = 8000

# UI 面板（ui/v1）征集护栏：单面板行数上限（deck 行不变量的资源限额）
UI_PANEL_MAX_LINES = 8
# 连续渲染失败熔断阈值（同 ProtectPluginTool 语义：崩溃计数，业务空
# 返回不计）——触发即 unregister，防坏插件每帧刷警告拖慢渲染
UI_PANEL_MAX_FAILURES = 3

# ── S1d 超时隔离：contribute / render 限时执行 ────────────────────
# "必须快速返回"从约定变成约束：贡献与渲染在共享线程池里限时跑，超时
# 按崩溃语义计（熔断同款）。挂死的 worker 线程无法终止（Python 无线程
# 终止原语）——熔断保证每个坏 entry 至多泄漏 _CONTRIBUTE_MAX_FAILURES 根。
_CONTRIBUTE_TIMEOUT = 5.0          # 秒；系统提示组装的每贡献上限
UI_RENDER_TIMEOUT = 1.0            # 秒；5Hz 渲染帧的每面板上限
_CONTRIBUTE_MAX_FAILURES = 3
_FuturesTimeout = concurrent.futures.TimeoutError
_CALL_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="openx-plugin-call"
)
_CONTRIBUTE_FAILS: dict[str, int] = {}  # "plugin:name" -> 连续失败计数


def _call_bounded(fn: Any, *args: Any, timeout: float) -> Any:
    """在共享线程池里执行 fn 并限时；超时抛 ``concurrent.futures.TimeoutError``。

    阻塞调用方线程直到完成/超时——collect_context_fragments（事件循环）
    与 panels()（Live 刷新线程）本就是同步路径，此前坏插件可以把它们挂到
    永远；现在上界明确。fn 内的异常原样穿透（调用方按各自崩溃语义处理）。
    """
    future = _CALL_POOL.submit(fn, *args)
    return future.result(timeout=timeout)


def _builtin_ids(kernel: Any) -> set:
    """内置插件 id 集（inventory 只读投影；内置恒首是加载序保证的）。"""
    return {p.id for p in kernel.inventory() if p.builtin}


def instantiate_tools(
    kernel: Any,
    host: Any,
    *,
    include_builtin: bool = True,
    include_plugins: bool = True,
    reserved: Optional[Mapping[str, str]] = None,
) -> dict:
    """按 host 实例化 tools 注册表 -> {name: Tool}。

    注册序即优先级（内置恒首挂载）：先产出的工具名先得，后来者跳过并
    记警告--"内置优先"由此成为结构性保证，无需消费方仲裁。
    ``include_plugins=False`` 供子代理使用：只实例化内置，不继承用户
    插件（能力继承 = 父集的子集，内核详设 §2.5）。
    ``reserved``：调用方已占位的名字 -> 占位者标签（结构性工具）；与
    之冲突的注册产出被拒并记警告，结构性恒赢。
    """
    registry: dict = {}
    produced_by: dict[str, str] = {}  # tool name -> plugin id
    reserved = reserved or {}
    builtin_ids = _builtin_ids(kernel)
    tools_reg = kernel.registry("tools")
    assert tools_reg is not None
    for entry in tools_reg.entries():
        if not include_builtin and entry.plugin in builtin_ids:
            continue
        if not include_plugins and entry.plugin not in builtin_ids:
            continue
        for tool in entry.value(host):
            tname = getattr(tool, "name", "") or "<unnamed>"
            problems = validate_tool(tname, tool)
            if problems:
                if entry.plugin in builtin_ids:
                    # 内置插件产出畸形 = 产品坏，带病不该运行
                    raise TypeError(
                        f"builtin tool {tname!r} malformed: {'; '.join(problems)}"
                    )
                for p in problems:
                    tools_reg.add_warning(
                        entry.name, f"rejected tool {tname!r}: {p}"
                    )
                continue
            if tname in reserved:
                tools_reg.add_warning(
                    entry.name,
                    f"tool {tname!r} conflicts with structural tool "
                    f"{reserved[tname]!r}; structural wins",
                )
                continue
            if tname in registry:
                owner = produced_by[tname]
                if owner in builtin_ids:
                    warning = (
                        f"tool {tname!r} conflicts with builtin "
                        f"{owner!r}; builtin wins"
                    )
                else:
                    warning = (
                        f"tool {tname!r} already provided by {owner!r}; "
                        "first wins"
                    )
                tools_reg.add_warning(entry.name, warning)
                continue
            # P-C：非内置插件工具套调用防护（timeout/输出上限/熔断/结构化
            # 错误）；内置/结构工具可信，不包。
            if entry.plugin in builtin_ids:
                registry[tname] = tool
            else:
                wrapper = _wrap_plugin_tool(kernel, entry.plugin, tool)
                registry[tname] = wrapper
                # S1c：收紧要在装配面留痕（/plugins 可见），不然用户只看到
                # "突然开始弹窗"而不知道为什么。
                if getattr(wrapper, "_cap", False):
                    tools_reg.add_warning(
                        entry.name,
                        f"tool {tname!r} capped to ASK: manifest declares "
                        "no permissions (self-declared level not trusted)",
                    )
            produced_by[tname] = entry.plugin
    return registry


def _wrap_plugin_tool(kernel: Any, plugin_id: str, tool: Any) -> Any:
    """P-C：插件工具包 ProtectPluginTool，timeout 取插件 manifest 声明。

    on_trip：熔断触发 → ``kernel.unregister_tool(name)``（自动摘除，
    防止模型反复调用坏插件）。S1c 能力上限：manifest 未声明 permissions
    → 工具的自声明权限级不被信任（压 ASK、auto_allowed 失效）——
    permissions 是能力上限的声明（attestation），不声明则不授予信任。
    """
    timeout = DEFAULT_TIMEOUT
    cap = False
    for info in kernel.inventory():
        if info.id == plugin_id:
            timeout = info.manifest.get("timeout") or DEFAULT_TIMEOUT
            cap = not info.manifest.get("permissions")
            break
    tname = getattr(tool, "name", "") or "<unnamed>"
    return ProtectPluginTool(
        tool,
        timeout=timeout,
        on_trip=_make_trip(kernel, tname),
        cap=cap,
    )


def _make_trip(kernel: Any, tname: str) -> Any:
    def on_trip(_wrapper: Any) -> None:
        try:
            kernel.unregister_tool(tname)
        except Exception:
            pass

    return on_trip


def resolve_provider_impl(kernel: Any, settings: dict) -> Optional[Any]:
    """按 ``settings["kind"]`` 从 providers 注册表实例化实现。

    返回**单次实现**（无重试包装）--重试由调用方组合内核
    RetryingProvider（LLMClient 门面）。未注册的 kind -> None：警告与
    回退是调用方的 UX 策略，不进装配层。缺省 kind = "openai-compat"。
    """
    kind = str(settings.get("kind") or "openai-compat")
    reg = kernel.registry("providers")
    entry = reg.get(kind) if reg is not None else None
    if entry is None:
        return None
    return entry.value(settings)


def collect_context_fragments(
    kernel: Any,
    budget: Optional[int] = None,
    *,
    include_plugins: bool = True,
    payload: Optional[Any] = None,
) -> list[str]:
    """征集插件上下文片段（context/v1 的消费面，P-D）。

    pre-inference 阶段（系统提示组装）由 agent 调用：按 **priority 升序**
    （小者先；同 priority 保注册序，内置恒首挂载）遍历 ``contexts`` 注册表，
    逐个 ``contribute()``；结果展平为片段列表。消费方装配策略住消费方--
    内核只存注册，不管提示怎么拼。
    ``include_plugins=False`` 供子代理使用（能力继承 = 父集的子集，
    与 instantiate_tools 同款口径：只征集内置插件的贡献）。

    ``payload``：可选只读 :class:`~openx.kernel.assembly.context.PromptHost`
    （workspace / instructions / skills / budget）。**零参贡献照常工作**--
    只有形参容纳时才传（见 :func:`~openx.kernel.assembly.context.call_with_payload`），
    故既有插件零改动；想收 host 的贡献声明一个形参即可。

    故障隔离（与插件调用防护同构）：单个 contribute 崩溃 -> 跳过 +
    注册表 warning，绝不炸主流程--对提示组装而言，插件异常与"没有
    这条贡献"同构。**超时隔离（S1d）**：contribute 在共享线程池里限时
    执行（缺省 5s）——挂死的贡献不再拖住系统提示组装；连续超时/崩溃
    达 3 次 → 本会话内永久跳过该贡献（镜像 UI 面板熔断）。
    ``budget`` 为字符预算（可选）：**按 priority 顺序消耗**
    （高优先片段先占额度），超预算即停止征集，防止上下文类插件把提示撑爆。
    """
    fragments: list[str] = []
    used = 0
    reg = kernel.registry("contexts")
    if reg is None:
        return fragments
    builtin_ids = _builtin_ids(kernel) if not include_plugins else frozenset()
    entries = [
        e for e in reg.entries() if include_plugins or e.plugin in builtin_ids
    ]
    # priority 升序（小者先）；sorted 稳定 -> 同 priority 按注册序（内置恒首）
    entries.sort(key=lambda e: getattr(e.value, "priority", 100))
    for entry in entries:
        fail_key = f"{entry.plugin}:{entry.name}"
        if _CONTRIBUTE_FAILS.get(fail_key, 0) >= _CONTRIBUTE_MAX_FAILURES:
            continue  # 熔断：连续失败/超时的贡献本会话内不再征集
        try:
            out = _call_bounded(
                call_with_payload, entry.value.contribute, payload,
                timeout=_CONTRIBUTE_TIMEOUT,
            )
        except _FuturesTimeout:
            _CONTRIBUTE_FAILS[fail_key] = _CONTRIBUTE_FAILS.get(fail_key, 0) + 1
            reg.add_warning(
                entry.name,
                f"contribute timed out (>{_CONTRIBUTE_TIMEOUT:.0f}s); "
                f"failures: {_CONTRIBUTE_FAILS[fail_key]}/{_CONTRIBUTE_MAX_FAILURES}",
            )
            continue
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            reg.add_warning(entry.name, f"contribute failed: {error}")
            continue
        _CONTRIBUTE_FAILS[fail_key] = 0
        parts = [out] if isinstance(out, str) else list(out or [])
        for part in parts:
            text = str(part)
            if not text.strip():
                continue
            if budget is not None and used + len(text) > budget:
                reg.add_warning(
                    entry.name,
                    f"context budget exhausted ({budget} chars); "
                    "remaining fragments dropped",
                )
                return fragments
            fragments.append(text)
            used += len(text)
    return fragments


def admitter_from_console(console: Any) -> Any:
    """把 UI console 的审批对话包成晋升门的 admitter（K6，§2.4）。

    消费方在"有人可问"时把这枚回调挂到内核（``set_admitter``）：交互式
    CLI 启动即挂；serve 交由 Web 权限桥承接（无客户端时 ``ask_permission``
    已 fail-closed 返回拒绝）。**UI 不进内核**——本函数是端层与内核之间
    的那座桥，内核只认 ``async (Contribution) -> bool`` 的签名。

    只读档（observe）贡献不经审批即可热插，故不需要有人可问；本桥只服务
    变更档（act）贡献。MCP 工具当前走 observe（用户写在 settings.json =
    已授权），此桥为后续 act 档生产者就位。
    """

    async def _admit(contribution: Any) -> bool:
        approved, _remember = await console.ask_permission(
            f"{contribution.kind}/{contribution.name}",
            contribution.summary
            or f"dynamic plugin contribution from {contribution.plugin}",
            # 准入批准不是可持久化的放行规则（不存在"以后都同意"语义）
            can_remember=False,
        )
        return bool(approved)

    return _admit


class UiPanelCollector:
    """UI 面板征集器（ui/v1 的消费面）——deck 每帧调 ``panels()``。

    渲染路径的故障隔离是硬性要求（渲染帧绝不能被插件拖死）：

    - **崩溃跳过**：单个 render() 抛异常 → 该面板本帧缺席 + 注册表
      warning，其余面板照常；对渲染而言插件异常与"没有这个面板"同构；
    - **熔断摘除**：连续 ``UI_PANEL_MAX_FAILURES`` 次崩溃 → 调
      ``kernel.unregister_ui_slot`` 自动摘除（防止坏插件每帧刷异常）；
    - **资源限额**：单面板行数截断到 ``UI_PANEL_MAX_LINES``；
    - **节流**：``refresh_hz`` 低于帧率时沿用上次渲染结果（缓存行）。

    线程：panels() 由 Live 刷新线程调用（~5Hz）；状态字典按面板名
    键控，GIL 下 dict 读写原子，与 load/unload 的注册表变更竞争最坏
    丢一帧，可接受。
    """

    def __init__(
        self,
        kernel: Any,
        max_lines: int = UI_PANEL_MAX_LINES,
        max_failures: int = UI_PANEL_MAX_FAILURES,
    ) -> None:
        self._kernel = kernel
        self._max_lines = max_lines
        self._max_failures = max_failures
        self._fails: dict[str, int] = {}      # 面板名 -> 连续崩溃计数
        self._cache: dict[str, tuple[float, list[str]]] = {}  # 节流缓存

    def panels(self) -> list[tuple[str, list[str]]]:
        """征集本帧全部面板 → ``[(name, deck 行列表), ...]``（注册序）。"""
        out: list[tuple[str, list[str]]] = []
        reg = self._kernel.registry("ui_slots")
        if reg is None:
            return out
        now = time.monotonic()
        for entry in reg.entries():
            lines = self._render_entry(reg, entry, now)
            if lines is not None:
                out.append((entry.name, lines))
        return out

    # ── internals ───────────────────────────────────────────────

    def _render_entry(self, reg: Any, entry: Any, now: float) -> Optional[list[str]]:
        """渲染单个面板：节流 → 隔离执行 → 限额 → 熔断。None = 本帧缺席。"""
        name = entry.name
        slot = entry.value
        # 节流：refresh_hz 决定重渲染周期；未到点沿用缓存行
        interval = 1.0 / float(getattr(slot, "refresh_hz", 5.0) or 5.0)
        cached = self._cache.get(name)
        if cached is not None and now - cached[0] < interval:
            return cached[1]
        try:
            # S1d：渲染限时（1s）——挂死的 render 不再拖住 5Hz 刷新帧；
            # 超时按崩溃语义计入既有熔断。
            rendered = _call_bounded(slot.render, timeout=UI_RENDER_TIMEOUT)
            raw = [rendered] if isinstance(rendered, str) else list(rendered or [])
            lines = [str(ln) for ln in raw if str(ln).strip()]
        except _FuturesTimeout:
            return self._fail(
                reg, entry, f"render timed out (>{UI_RENDER_TIMEOUT:.0f}s)"
            )
        except Exception as exc:
            return self._fail(reg, entry, f"{type(exc).__name__}: {exc}")
        # 资源限额：行数截断（不截字符宽——no_wrap/ellipsis 由渲染侧统一施加）
        if len(lines) > self._max_lines:
            lines = lines[: self._max_lines]
        self._fails[name] = 0  # 成功即复位连续崩溃计数
        self._cache[name] = (now, lines)
        return lines

    def _fail(self, reg: Any, entry: Any, error: str) -> None:
        """记一次渲染崩溃；达到阈值 → 熔断摘除 + unregistered 记账。"""
        name = entry.name
        self._fails[name] = self._fails.get(name, 0) + 1
        reg.add_warning(name, f"render failed: {error}")
        if self._fails[name] >= self._max_failures:
            self._fails.pop(name, None)
            self._cache.pop(name, None)
            try:
                self._kernel.unregister_ui_slot(name)
            except Exception:
                pass
