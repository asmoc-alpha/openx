"""插件注册表 -- 微内核的 IPC。

每个注册项类型（tools / commands / …）一张注册表。插件彼此不认识，
消费方（agent loop、命令分发）也只面对注册表。每条注册带 provenance
（来源插件 id）与校验/冲突结果，inventory 据此投影。

冲突仲裁两层：
- 插件 vs 插件：先注册者赢，后注册者被拒并记 problem；
- 插件 vs 内置：内置恒首挂载，注册序即优先级--tools 的名字冲突在
  实例化时结构性解决（先产出者赢），commands 的内置名字只有消费方
  知道，故由消费方在合并时调 :meth:`note_conflict` 回报记警告。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class Entry:
    """一条注册：值 + provenance + 累积警告。

    ``seq`` 是 registered 事件的账本序号（inserted_at_seq）--沿它上溯
    账本即可回答"这个能力什么时候来的、谁装的"。
    """

    name: str
    value: object
    plugin: str
    warnings: list[str] = field(default_factory=list)
    seq: Optional[int] = None


class PluginRegistry:
    """单个注册项类型的注册表。

    ``validator`` 签名 ``(name, value) -> list[str]``：返回形状问题
    列表，空 = 接受。校验是"约束即代码"的落点（无权限声明的工具拒载等）。
    """

    def __init__(
        self,
        kind: str,
        validator: Optional[Callable[[str, object], list[str]]] = None,
    ) -> None:
        self.kind = kind
        self._validator = validator
        self._entries: dict[str, Entry] = {}

    def register(self, name: str, value: object, plugin: str) -> list[str]:
        """注册一条插件贡献；返回问题列表（非空 = 被拒，不入库）。"""
        if not isinstance(name, str) or not name:
            return [f"{self.kind}: empty name"]
        if self._validator is not None:
            problems = self._validator(name, value)
            if problems:
                return problems
        if name in self._entries:
            return [
                f"{self.kind}:{name} already registered by "
                f"{self._entries[name].plugin}; first wins"
            ]
        self._entries[name] = Entry(name, value, plugin)
        return []

    def note_conflict(self, name: str, builtin: str) -> None:
        """消费方回报"与内置重名、内置优先"；记警告（去重）。"""
        entry = self._entries.get(name)
        if entry is None:
            return
        warning = f"conflicts with builtin {builtin!r}; builtin wins"
        if warning not in entry.warnings:
            entry.warnings.append(warning)

    def add_warning(self, name: str, warning: str) -> None:
        """加载/实例化期问题记到对应条目（如注册被校验拒）。"""
        entry = self._entries.get(name)
        if entry is not None and warning not in entry.warnings:
            entry.warnings.append(warning)

    def items(self) -> dict[str, object]:
        """{name: value} -- 消费方取用视图。"""
        return {name: e.value for name, e in self._entries.items()}

    def entries(self) -> list[Entry]:
        """按注册序返回全部条目（注册序即优先级，内置恒首）。"""
        return list(self._entries.values())

    def get(self, name: str) -> Optional[Entry]:
        return self._entries.get(name)

    def __len__(self) -> int:
        return len(self._entries)
