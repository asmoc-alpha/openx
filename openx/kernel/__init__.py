"""openx.kernel —— 微内核（P1 切片）。

四职责：装配（loader）/ 契约（ctx + 注册表）/ 执行（校验与隔离）/
生命周期与清单（inventory）。TCB 的一部分，保持小到可审计：本包
不 import agent / cli / ui，依赖方向单向（核心消费内核，反之不然）。

P1 开放两个贡献点：tools 与 slash commands。混合内核纪律：loop /
executor / 安全底线等内核驻留核心不在本包，也不可插拔。

组合输入（P1）：用户目录 ~/.openx/plugins、项目 .openx/plugins、
pip entry-points group ``openx.plugins``；settings.json 顶层
``"plugins": {"disabled": [...]}`` 控制开关。
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Optional

from . import loader
from .context import CommandContribution, PluginContext
from .inventory import (
    PHASE_ACTIVE,
    PHASE_DISABLED,
    PHASE_FAILED,
    PHASE_LOADING,
    PluginInfo,
)
from .registry import ContributionRegistry
from .validate import validate_command, validate_tool

__all__ = [
    "PluginKernel",
    "PluginContext",
    "PluginInfo",
    "CommandContribution",
    "get_kernel",
    "reset_kernel",
]

_log = logging.getLogger("openx.kernel")


def _disabled_ids() -> list[str]:
    """settings.json 顶层 "plugins"."disabled"；调用期读，测试可隔离。"""
    try:
        from ..config import OpenXConfig

        return list(OpenXConfig.load_plugin_settings().get("disabled", []))
    except Exception:
        return []


class PluginKernel:
    """微内核本体：注册表 + 加载流水线 + inventory。"""

    def __init__(self) -> None:
        self.tools = ContributionRegistry("tools", validate_tool)
        self.commands = ContributionRegistry("commands", validate_command)
        self._plugins: dict[str, PluginInfo] = {}
        self._load_key: Optional[tuple] = None
        self.workspace = ""

    # ── 装配 / 生命周期 ─────────────────────────────────────

    def ensure_loaded(self, workspace: str) -> None:
        """幂等加载；键 =（用户目录, 项目目录, 禁用表），变则重载。"""
        key = (
            str(loader.user_plugins_dir()),
            str(loader.project_plugins_dir(workspace)),
            tuple(sorted(_disabled_ids())),
        )
        if key == self._load_key:
            return
        self.tools = ContributionRegistry("tools", validate_tool)
        self.commands = ContributionRegistry("commands", validate_command)
        self._plugins = {}
        self.workspace = str(workspace)
        self._load_key = key
        disabled = set(key[2])
        for spec in loader.discover(str(workspace)):
            self._load_one(spec, disabled)

    def _load_one(self, spec: loader.PluginSpec, disabled: set) -> None:
        info = PluginInfo(id=spec.id, source=spec.source, phase=PHASE_LOADING)
        self._plugins[spec.id] = info
        if spec.id in disabled:
            info.phase = PHASE_DISABLED
            return
        try:
            loaded = loader.load_module(spec)
            apply_fn = loader.extract_apply(loaded)
            if apply_fn is None:
                raise TypeError("plugin exports no apply(ctx)")
            ctx = PluginContext(
                self,
                spec.id,
                logging.getLogger(f"openx.plugin.{spec.id}"),
                self.workspace,
            )
            apply_fn(ctx)
        except Exception as exc:  # 失败隔离：用户插件坏 ≠ 主进程死
            info.phase = PHASE_FAILED
            info.error = f"{type(exc).__name__}: {exc}"
            _log.error("plugin %s failed to load: %s", spec.id, info.error)
            return
        info.phase = PHASE_ACTIVE

    # ── 契约：ctx 回调 ──────────────────────────────────────

    def register_tool(self, tool: Any, plugin_id: str) -> None:
        name = getattr(tool, "name", "") or "<unnamed>"
        problems = self.tools.register(name, tool, plugin_id)
        info = self._plugins.get(plugin_id)
        if problems:
            for p in problems:
                _log.warning("plugin %s: rejected tool %r: %s", plugin_id, name, p)
                if info is not None:
                    info.warnings.append(f"rejected tool {name!r}: {p}")
        elif info is not None:
            info.tools.append(name)

    def register_command(
        self, name: str, contrib: CommandContribution, plugin_id: str
    ) -> None:
        problems = self.commands.register(name, contrib, plugin_id)
        info = self._plugins.get(plugin_id)
        if problems:
            for p in problems:
                _log.warning("plugin %s: rejected command %r: %s", plugin_id, name, p)
                if info is not None:
                    info.warnings.append(f"rejected command {name!r}: {p}")
        elif info is not None:
            info.commands.append(name)

    # ── 消费方 API（agent / commands 面对注册表）──────────────

    def merge_tools(self, registry: dict) -> None:
        """并入 agent 工具表：内置优先——重名跳过并记警告，不覆盖。"""
        for entry in self.tools.entries():
            if entry.name in registry:
                self.tools.note_conflict(entry.name, entry.name)
            else:
                registry[entry.name] = entry.value

    def lookup_command(self, name: str) -> Optional[Any]:
        """命令分发：主名 → 别名 → None（内置先查，调用方保证顺序）。"""
        entry = self.commands.get(name)
        if entry is not None:
            return entry.value.handler
        for e in self.commands.entries():
            if name in e.value.aliases:
                return e.value.handler
        return None

    def command_menu_entries(self) -> list[tuple[str, str, list[str]]]:
        """补全菜单数据（插件部分）：[(name, description, aliases)]。"""
        return [
            (e.name, e.value.description, sorted(e.value.aliases))
            for e in self.commands.entries()
        ]

    def note_command_conflict(self, name: str) -> None:
        """commands.py 回报内置优先跳过；记入 inventory 警告。"""
        self.commands.note_conflict(name, name)

    # ── 清单 ────────────────────────────────────────────────

    def inventory(self) -> list[PluginInfo]:
        """只读投影：注册表警告回并 + 浅拷贝，每次读当下。"""
        for reg in (self.tools, self.commands):
            for entry in reg.entries():
                info = self._plugins.get(entry.plugin)
                if info is None:
                    continue
                for w in entry.warnings:
                    if w not in info.warnings:
                        info.warnings.append(w)
        return [copy.copy(p) for p in self._plugins.values()]


_kernel: Optional[PluginKernel] = None


def get_kernel() -> PluginKernel:
    """进程级单例；测试用 reset_kernel() 隔离。"""
    global _kernel
    if _kernel is None:
        _kernel = PluginKernel()
    return _kernel


def reset_kernel() -> None:
    global _kernel
    _kernel = None
