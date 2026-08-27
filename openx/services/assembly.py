"""消费方装配策略 -- 内核详设 v2.1 §0"取用通道收敛"的落点。

内核 API 回归四件 + ``registry(kind)`` 只读视图；装配策略住在消费方：
- 工具实例化与冲突仲裁（注册序即优先级、内置恒首、结构性工具占位）；
- provider 实现解析（kind -> 注册表条目 -> 实例；未注册由调用方决定
  回退与告警，UX 策略不进装配层）。

本模块只依赖内核的只读面（registry / inventory），不写注册表。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from ..kernel.validate import validate_tool


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
            registry[tname] = tool
            produced_by[tname] = entry.plugin
    return registry


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
