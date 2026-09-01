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
    builtin: bool = False  # 内置插件：失败=致命，禁用表对其无效
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    # P-D 协议注册面：上下文贡献、生命周期钩子与 UI 面板的注册项名
    contexts: list[str] = field(default_factory=list)
    lifecycle: list[str] = field(default_factory=list)
    ui_slots: list[str] = field(default_factory=list)
    # P-A 模型驱动装配：轻量自描述与会话级装载标记
    summary: str = ""            # 插件 __openx_meta__["summary"]（list_plugins 的模型认知入口）
    cost: dict = field(default_factory=dict)  # 装配预算元数据（P-A 只读存，不消费）
    scope: str = "boot"          # "boot" | "session"（P-A 会话级 load/unload 标记）
    # P-B 插件自描述：校验后的 manifest 全量（timeout/type/mount/trust/
    # permissions/dependencies 由消费方经 manifest.get 读）
    manifest: dict = field(default_factory=dict)
    manifest_warnings: list[str] = field(default_factory=list)
