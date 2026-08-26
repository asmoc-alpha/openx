"""PluginContext -- ``apply(ctx)`` 的句柄，特权分隔的给予面。

给予：注册 API、logger、只读 workspace 信息--插件完成注册所需的全部。
拒绝面同等重要，靠*不暴露引用*实现而非插件自律：ctx 上没有 agent loop、
没有权限闸门、没有裸 console、没有他插件状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

# 命令 handler 签名与内置一致：async (agent, console, args) -> bool
CommandHandler = Callable[..., Awaitable[bool]]


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
        ``factory(agent) -> list[Tool]``，消费方只有一种取用形态。
        """
        self._kernel.register_tool(tool, self.plugin_id)

    def register_tool_factory(self, name: str, factory: Any) -> None:
        """注册工具工厂：``factory(agent) -> list[Tool]``，按 agent 实例化。

        内置工具需 agent 构造参数（console/tasks/todos 引用），故 base
        bundle 走工厂而非实例；产出在实例化时逐个形状校验。
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
