"""插件注册目录 -- 插件能往内核注册什么，由内核枚举。

目录表驱动：每类注册项一份元数据（kind / 校验器 / 冲突规则 / 热插档），
内核按目录自动生成注册表；新增一类注册项 = 目录加一行 + 一个校验器，
内核主体不改。冲突规则与热插档 P1 只声明不消费（Guard / 晋升门到位后
接线），但结构一次定形，后续只消费不迁移。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .validate import validate_command, validate_provider

# 冲突规则：同名先注册者赢（加载序 = 优先级，内置恒首）
CONFLICT_FIRST_WINS = "first-wins"
# 热插档：会话内可插拔（回滚 = 卸载）
HOTPLUG_SESSION = "session"
# 热插档：会话边界换（有连接状态，热换撕裂引用--providers 属此档）
HOTPLUG_BOUNDARY = "boundary"


def _validate_factory(name: str, value: object) -> list[str]:
    return [] if callable(value) else [f"{name}: tool factory not callable"]


@dataclass(frozen=True)
class PluginRegistration:
    """一类插件注册项的元数据。"""

    kind: str                 # "tools" / "commands" / ...
    validator: Callable[[str, object], list[str]]
    conflict: str = CONFLICT_FIRST_WINS
    hotplug: str = HOTPLUG_SESSION


# 注册目录。tools 的值统一为工厂 ``factory(agent) -> list[Tool]``：
# 内置工具需 agent 构造参数（workspace/console/tasks 引用），用户插件
# 裸传的实例由 ctx 包一层工厂入库--消费方只有一种取用形态。
# providers 的值是实现工厂 ``create(settings: dict) -> Provider``：注册表
# 存"有哪几种实现"（键 = kind，如 openai-compat），settings 存"用户配了
# 哪几个实例"--注册与配置两级解耦。热插档 boundary：仅 boot 装配。
REGISTRATIONS: tuple[PluginRegistration, ...] = (
    PluginRegistration("tools", _validate_factory),
    PluginRegistration("commands", validate_command),
    PluginRegistration("providers", validate_provider, hotplug=HOTPLUG_BOUNDARY),
)
