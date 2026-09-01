"""P-D 协议目录 -- 类别 -> 协议 -> 装配层三级映射（microkernel-design §4）。

与操作系统驱动模型同构：插件面向协议编程，装配器按 ``manifest.type``
路由到对应注册面与消费点。目录表驱动（"加一行"纪律，与 registrations.py
同款）：新增一个插件类别 = 目录加一行 + 一个校验器，内核五件套不动。

三协议 + UI 协议先行（决断 N4 + ui/v1 扩展），其余占位（``strategy.planning``
/ ``orchestration`` 的单例协议在 cardinality 结构就绪后落地）：

- ``tool/v1``       ``capability.tool`` -> ``tools`` 注册表（多例：注册即累加）
- ``context/v1``    ``context.memory``  -> ``contexts`` 注册表（多例：片段
  按注册序征集，pre-inference 阶段由消费方 ``collect_context_fragments``
  并入系统提示）
- ``lifecycle/v1``  ``lifecycle`` -> ``lifecycle`` 注册表（多例：会话状态
  迁移时按序回调，on_unload 是卸载时的状态落盘契约）
- ``ui/v1``         ``ui.panel`` -> ``ui_slots`` 注册表（多例：状态层面板，
  ``render() -> deck 行``，消费方 ``UiPanelCollector`` 每帧征集，渲染
  路径强制故障隔离 + 熔断--渲染帧绝不能被插件拖死）

路由默认：未知/缺失 type -> ``tool/v1``（向后兼容 P-D 之前的插件；未知
type 的 warning 由 P-B manifest 校验照记）。mount 由本目录派生，模型与
write_plugin 都不手填--协议表是 type -> mount 的唯一真源。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# 基数：多例（注册即累加）vs 单例（同一时刻只能有一个生效，装配即替换）
CARD_MULTI = "multi"
CARD_SINGLETON = "singleton"


@dataclass(frozen=True)
class ProtocolSpec:
    """一类插件协议的元数据（manifest.type 的路由目标）。"""

    protocol: str                 # 协议版本号，如 "tool/v1"（版本化平滑升级）
    ptype: str                    # manifest.type 路由键
    mount: str                    # 挂载点（只给内核用，模型不感知）
    registry_kind: str            # 装配层注册表 kind（registrations.py 目录键）
    cardinality: str = CARD_MULTI
    # apply(ctx) 内应出现的注册调用（write_plugin 生成侧的契约检查锚点）
    register_calls: tuple[str, ...] = ()


PROTOCOLS: tuple[ProtocolSpec, ...] = (
    ProtocolSpec(
        protocol="tool/v1",
        ptype="capability.tool",
        mount="loop.tool-call",
        registry_kind="tools",
        register_calls=("register_tool_factory", "register_tool"),
    ),
    ProtocolSpec(
        protocol="context/v1",
        ptype="context.memory",
        mount="loop.pre-inference",
        registry_kind="contexts",
        register_calls=("register_context",),
    ),
    ProtocolSpec(
        protocol="lifecycle/v1",
        ptype="lifecycle",
        mount="lifecycle.session",
        registry_kind="lifecycle",
        register_calls=("register_lifecycle",),
    ),
    ProtocolSpec(
        protocol="ui/v1",
        ptype="ui.panel",
        mount="ui.deck",
        registry_kind="ui_slots",
        register_calls=("register_ui_slot",),
    ),
)

_BY_TYPE: dict[str, ProtocolSpec] = {p.ptype: p for p in PROTOCOLS}

# 路由默认：向后兼容（P-D 之前的插件无 type 或 type 仅为展示元数据）
DEFAULT_PROTOCOL = _BY_TYPE["capability.tool"]


def route(ptype: Any) -> ProtocolSpec:
    """manifest.type -> 协议（未知/缺失 -> tool/v1 默认路由）。"""
    return _BY_TYPE.get(str(ptype or ""), DEFAULT_PROTOCOL)


def derive_mount(ptype: Any) -> str:
    """manifest.type -> 挂载点（协议表派生，不手填）。"""
    return route(ptype).mount


def find(protocol: str) -> Optional[ProtocolSpec]:
    """按协议版本号反查（如 write_plugin 校验 protocol 声明时）。"""
    for p in PROTOCOLS:
        if p.protocol == protocol:
            return p
    return None
