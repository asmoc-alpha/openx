"""PluginContext -- ``apply(ctx)`` 的句柄，特权分隔的给予面。

给予：注册 API、logger、只读 workspace 信息--插件完成注册所需的全部。
拒绝面同等重要，靠*不暴露引用*实现而非插件自律：ctx 上没有 agent loop、
没有权限闸门、没有裸 console、没有他插件状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Union

# 命令 handler 签名与内置一致：async (agent, console, args) -> bool
CommandHandler = Callable[..., Awaitable[bool]]

# context/v1：contribute() -> str | list[str]（上下文片段；pre-inference
# 阶段由消费方 collect_context_fragments 按注册序 + 预算征集）
ContextContribute = Callable[[], Union[str, list[str]]]

# lifecycle/v1：会话状态迁移钩子；异常由内核捕获（插件异常 = observation）
LifecycleHook = Callable[..., Any]

# ui/v1：render() -> deck 行（str | list[str]，支持 Rich markup）。
# 每帧由消费方 UiPanelCollector 征集（崩溃隔离 + 熔断）；行须自满足
# "1 行 ≡ 1 终端行"（no_wrap + ellipsis 由渲染侧统一施加）。
UiRender = Callable[[], Union[str, list[str]]]


@dataclass
class UISlot:
    """一个状态层面板：render + 刷新率（Hz，<= Live 刷新率即节流）。"""

    render: UiRender
    refresh_hz: float = 5.0


@dataclass
class ContextContribution:
    """一条上下文贡献：contribute + 征集优先级（小者先，默认 100）。"""

    contribute: ContextContribute
    priority: int = 100


@dataclass
class LifecycleHooks:
    """一组会话生命周期钩子（lifecycle/v1）；全部可选，按需声明。

    - ``on_session_start``：agent startup 时（顶层 agent 一次）
    - ``on_checkpoint`` / ``on_resume``：会话持久化边界（接线随 P-E）
    - ``on_unload``：unload_plugin 时--有状态插件（Memory 类）的
      状态落盘契约：卸载不是简单删注册，先给插件一次收尾机会。
    """

    on_session_start: Optional[LifecycleHook] = None
    on_checkpoint: Optional[LifecycleHook] = None
    on_resume: Optional[LifecycleHook] = None
    on_unload: Optional[LifecycleHook] = None


@dataclass
class PluginCommand:
    """一条插件命令：handler + 菜单元数据。"""

    handler: CommandHandler
    description: str = ""
    aliases: list[str] = field(default_factory=list)


class PluginContext:
    """传给插件 ``apply(ctx)`` 的上下文对象。"""

    def __init__(self, kernel: Any, plugin_id: str, logger: Any, workspace: str):
        self._kernel = kernel
        self.plugin_id = plugin_id
        self.logger = logger
        self.workspace = workspace  # 只读信息，非 fs 权限

    def register_tool(self, tool: Any) -> None:
        """注册一个工具实例（须自声明 permission，形状校验拒载记警告）。

        实例即时校验；入库时包一层工厂--tools 注册项的值统一为
        ``factory(host) -> list[Tool]``，消费方只有一种取用形态。
        """
        self._kernel.register_tool(tool, self.plugin_id)

    def register_tool_factory(self, name: str, factory: Any) -> None:
        """注册工具工厂：``factory(host) -> list[Tool]``，按 host 实例化。

        host 是 ToolHost（kernel/host.py）--实例化期的收窄给予面，
        插件拿不到 agent 本体；产出在实例化时逐个形状校验。
        """
        self._kernel.register_tool_factory(name, factory, self.plugin_id)

    def register_provider(self, kind: str, factory: Any) -> None:
        """注册一个模型 provider 实现：``factory(settings: dict) -> Provider``。

        键是**实现名**（kind，如 "openai-compat"），不是用户配置名--
        注册表存实现，settings 存实例（两级解耦）。仅 boot 装配
        （hotplug=boundary），会话内不热插。
        """
        self._kernel.register_provider(kind, factory, self.plugin_id)

    def register_command(
        self,
        name: str,
        handler: CommandHandler,
        description: str = "",
        aliases: Optional[list[str]] = None,
    ) -> None:
        """注册一个斜杠命令（内置优先；别名同规则）。"""
        self._kernel.register_command(
            name,
            PluginCommand(handler, description, aliases or []),
            self.plugin_id,
        )

    # ── P-D 协议注册面（context/v1 · lifecycle/v1）─────────────

    def register_context(
        self,
        name: str,
        contribute: ContextContribute,
        priority: int = 100,
    ) -> None:
        """注册一条上下文贡献（context/v1）：``contribute() -> 片段``。

        片段在 pre-inference 阶段（系统提示组装）由消费方按注册序 +
        预算征集--插件不知道、也不需要知道提示怎么拼。contribute 建议
        纯读取无副作用（每次提示重建都会调用）。
        """
        self._kernel.register_context(
            name, ContextContribution(contribute, priority), self.plugin_id
        )

    def register_lifecycle(
        self,
        name: str,
        on_session_start: Optional[LifecycleHook] = None,
        on_checkpoint: Optional[LifecycleHook] = None,
        on_resume: Optional[LifecycleHook] = None,
        on_unload: Optional[LifecycleHook] = None,
    ) -> None:
        """注册一组会话生命周期钩子（lifecycle/v1），至少给一个。

        ``on_unload`` 是卸载时的状态落盘契约：unload_plugin 先回调它
        再清注册。钩子异常由内核捕获记账，绝不炸主流程。
        """
        hooks = LifecycleHooks(
            on_session_start=on_session_start,
            on_checkpoint=on_checkpoint,
            on_resume=on_resume,
            on_unload=on_unload,
        )
        self._kernel.register_lifecycle(name, hooks, self.plugin_id)

    def register_ui_slot(
        self,
        name: str,
        render: UiRender,
        refresh_hz: float = 5.0,
    ) -> None:
        """注册一个状态层面板（ui/v1）：``render() -> deck 行``。

        行支持 Rich markup；每次刷新调用一次 render（refresh_hz 节流，
        上限 = Live 刷新率 ~5Hz）。渲染路径的故障隔离在消费方
        （UiPanelCollector）：render 崩溃跳过、连续失败熔断自动摘除--
        渲染帧绝不能被插件拖死。render 须快速返回（同步、无阻塞 IO）。
        """
        self._kernel.register_ui_slot(
            name, UISlot(render, refresh_hz), self.plugin_id
        )
