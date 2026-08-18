"""PluginContext —— ``apply(ctx)`` 的句柄，特权分隔的给予面。

给予：注册 API、logger、只读 workspace 信息——插件完成贡献所需的
全部。拒绝面同等重要，靠*不暴露引用*实现而非插件自律：ctx 上没有
agent loop、没有权限闸门、没有裸 console、没有他插件状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

# 命令 handler 签名与内置一致：async (agent, console, args) -> bool
CommandHandler = Callable[..., Awaitable[bool]]


@dataclass
class CommandContribution:
    """一条命令贡献：handler + 菜单元数据。"""

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
        """注册一个工具实例（须自声明 permission，校验拒载记警告）。"""
        self._kernel.register_tool(tool, self.plugin_id)

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
            CommandContribution(handler, description, aliases or []),
            self.plugin_id,
        )
