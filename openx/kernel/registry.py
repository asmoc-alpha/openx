"""贡献注册表 —— 微内核的 IPC。

每个贡献点（tools / commands / …）一张注册表。插件彼此不认识，
消费方（agent loop、命令分发）也只面对注册表。每条注册带
provenance（来源插件 id）与校验/冲突结果，inventory 据此投影。

冲突仲裁分两层：
- 插件 vs 插件：先注册者赢，后注册者被拒并报 problem；
- 插件 vs 内置：内置优先——但内置名字只有消费方知道，故由消费方
  在合并时调 :meth:`note_conflict` 回报，注册表记警告不记死。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class Entry:
    """一条注册：值 + provenance + 累积警告。"""

    name: str
    value: object
    plugin: str
    warnings: list[str] = field(default_factory=list)


class ContributionRegistry:
    """单个贡献点的注册表。

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
        """注册一条贡献；返回问题列表（非空 = 被拒，不入库）。"""
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
        """加载期问题记到对应条目（如注册被校验拒）。"""
        entry = self._entries.get(name)
        if entry is not None and warning not in entry.warnings:
            entry.warnings.append(warning)

    def items(self) -> dict[str, object]:
        """{name: value} —— 消费方取用视图。"""
        return {name: e.value for name, e in self._entries.items()}

    def entries(self) -> list[Entry]:
        return list(self._entries.values())

    def get(self, name: str) -> Optional[Entry]:
        return self._entries.get(name)

    def __len__(self) -> int:
        return len(self._entries)
