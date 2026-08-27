"""ToolHost -- 工具实例化期的给予面（内核详设 v2.1 §1.4）。

注册期拒绝面（PluginContext 不暴露 loop/闸门/console）延伸到实例化期：
能力工具工厂签名 ``factory(host) -> list[Tool]``，插件在任何阶段都拿
不到 agent 本体。host 是纯数据投影--只读 workspace/配置字段 + 共享
状态句柄（todos/tasks/coding_memory），无方法、无回路可顺藤摸回
agent。形状进内核（``provider.py`` 先例），实现只是数据。

面上字段按"首个真实消费方出现才加入"最小化：当前能力工具恰好只需
这些；受限 console、emit 等窄方法待有消费方再补，不提前造无用面。

结构性工具（task / workflow / exit_plan_mode / choose_mode / ask_user /
structured_output）属内核驻留编排核心（混合内核纪律），由消费方
（agent）直接装配，不经本面也不经插件注册--StructuredOutputTool 既有
先例。它们与编排状态同生存期，收窄只会把 host 变成 agent 的替身。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class ToolHost:
    """能力工具实例化的给予面：agent 的只读数据投影。

    ``todos`` / ``tasks`` / ``coding_memory`` 是共享状态句柄（工具与
    agent 持有同一对象，如 TodoWriteTool 与 agent.todos 同一 list）；
    其余为构造期快照。frozen 只约束 host 自身字段不再赋值，共享句柄
    内部可变（这正是它们被共享的意义）。
    """

    workspace: str
    todos: list = field(default_factory=list)
    tasks: Optional[Any] = None          # TaskRegistry（shell 后台 / task 工具）
    coding_memory: Optional[Any] = None  # CodingMemoryStore（memory 工具）
    allow_write_outside_workspace: bool = False
    allowed_commands: list = field(default_factory=list)
    dangerous_commands: list = field(default_factory=list)
    web_search_provider: Optional[Any] = None
