"""Inventory —— loader 树的只读投影。

纪律（dsh plugin-inventory 同款）：无缓存、无修改权、无历史——
每次调用读当下状态。修改路径只有一条：改组合配置重载。
``/plugins`` 与 agent 自参照 inspect 读它。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 生命周期阶段
PHASE_PENDING = "pending"
PHASE_LOADING = "loading"
PHASE_ACTIVE = "active"
PHASE_FAILED = "failed"
PHASE_DISABLED = "disabled"


@dataclass
class PluginInfo:
    id: str
    source: str
    phase: str
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
