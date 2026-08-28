"""openx.kernel -- 微内核。

四职责（2026-08-24 定稿，详见 docs/design/microkernel-design.md）：
**编排 / 沙箱执行 / 插件维护 / 记账**。本包是 TCB 的一部分，保持小到
可审计：不 import agent / cli / ui，依赖方向单向（核心消费内核，反之
不然）。已就位：插件维护 + 编排装配（K1）、信封与突变记账（K2）、
ToolHost 与取用通道收敛（K3a）、执行闸裁决管线（K3，`guard.py`）。

P1 开放三类注册项（目录表驱动，见 registrations.py）：tools、slash
commands、providers。混合内核纪律：loop / executor / 安全底线等内核
驻留核心不在本包，也不可插拔。

内核 API（v2.1 §0 取用通道收敛，K3a）：装配（ensure_loaded）、
``registry(kind)`` 只读视图、记账（emit/attach_ledger）、清单
（inventory），外加 ctx 注册回调。消费方装配策略（工具实例化仲裁、
provider 解析、命令菜单合并）不住内核--见 ``services/assembly.py``
与 ``app/cli/commands.py``。

组合输入（P1）：用户目录 ~/.openx/plugins、项目 .openx/plugins、
pip entry-points group ``openx.plugins``；settings.json 顶层
``"plugins": {"disabled": [...]}`` 控制开关。
"""

from __future__ import annotations

import copy
import logging
import time
from typing import Any, Callable, Optional

from ..core.protocol import Event, digest_of
from . import loader
from .context import PluginCommand, PluginContext
from .inventory import (
    PHASE_ACTIVE,
    PHASE_DISABLED,
    PHASE_FAILED,
    PHASE_LOADING,
    PluginInfo,
)
from .registrations import REGISTRATIONS
from .registry import PluginRegistry
from .validate import validate_tool
from ..builtin import BUILTIN_PROVIDERS_ID, BUILTIN_TOOLS_ID

__all__ = [
    "PluginKernel",
    "PluginContext",
    "PluginInfo",
    "PluginCommand",
    "get_kernel",
    "reset_kernel",
    # base bundle 内置插件 id：失败=致命，禁用表对其无效，用户插件不得占用
    "BUILTIN_TOOLS_ID",
    "BUILTIN_PROVIDERS_ID",
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
        # 注册目录驱动：每类注册项一张注册表，目录加一行即得
        self.registries: dict[str, PluginRegistry] = {
            r.kind: PluginRegistry(r.kind, r.validator) for r in REGISTRATIONS
        }
        self._plugins: dict[str, PluginInfo] = {}
        self._load_key: Optional[tuple] = None
        self.workspace = ""
        # 记账（K2b）：唯一事件出口 + 可挂接的账本 sink
        self._ledger_sink: Optional[Callable[[Event], None]] = None
        self._ledger_session: str = ""
        self._seq: int = 0
        self._prev_digest: str = ""

    def registry(self, kind: str) -> Optional[PluginRegistry]:
        """取某类注册项的注册表（消费方唯一取用通道）。"""
        return self.registries.get(kind)

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
        self._reload(str(workspace), key)

    def _reload(self, workspace: str, key: tuple) -> None:
        disabled = set(key[2])
        self.registries = {
            r.kind: PluginRegistry(r.kind, r.validator) for r in REGISTRATIONS
        }
        self._plugins = {}
        self.workspace = workspace
        # base bundle 内置插件恒先挂载（列表序即优先级的结构性前提）：
        # builtin-tools 在前--组合决议/首条注册事件的既有次序不变
        from ..builtin import BUILTIN_PLUGINS

        for spec in BUILTIN_PLUGINS:
            self._load_one(spec, disabled)
        for spec in loader.discover(workspace):
            self._load_one(spec, disabled)
        # 组合决议记账：每次实际重组（键变化）固化为一条事件，任何一次
        # 会话的组合都能事后复现。幂等跳过（键未变）不记。
        self.emit(
            "composition_resolved",
            {
                "type": "composition_resolved",
                "workspace": workspace,
                "plugins": list(self._plugins),  # 加载序（优先级序）
                "disabled": sorted(disabled),
            },
        )
        # 全部插件处理完成才提交加载键：中途异常（含内置致命）保持旧键，
        # 下次 ensure_loaded 完整重试，不留半载状态。
        self._load_key = key

    def _load_one(self, spec: loader.PluginSpec, disabled: set) -> None:
        if spec.id in self._plugins:  # 重复 id（含撞内置）：先见者赢
            _log.warning("duplicate plugin id %r; first wins", spec.id)
            return
        info = PluginInfo(
            id=spec.id, source=spec.source, phase=PHASE_LOADING, builtin=spec.builtin
        )
        self._plugins[spec.id] = info
        if spec.id in disabled and not spec.builtin:
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
        except Exception as exc:
            if spec.builtin:  # 内置插件坏 = 产品坏，带病不该运行
                _log.exception("builtin plugin %s failed; fatal", spec.id)
                raise
            info.phase = PHASE_FAILED  # 失败隔离：用户插件坏 ≠ 主进程死
            info.error = f"{type(exc).__name__}: {exc}"
            _log.error("plugin %s failed to load: %s", spec.id, info.error)
            self.emit(
                "plugin_failed",
                {
                    "type": "plugin_failed",
                    "plugin": spec.id,
                    "source": spec.source,
                    "error": info.error,
                },
            )
            return
        info.phase = PHASE_ACTIVE
        self.emit(
            "plugin_loaded",
            {"type": "plugin_loaded", "plugin": spec.id, "source": spec.source},
        )

    # ── 契约：ctx 回调 ──────────────────────────────────────

    def register_tool(self, tool: Any, plugin_id: str) -> None:
        """工具实例注册：形状即时校验，包工厂入库（统一值形态）。"""
        name = getattr(tool, "name", "") or "<unnamed>"
        info = self._plugins.get(plugin_id)
        problems = validate_tool(name, tool)
        if not problems:
            factory = lambda host: [tool]  # noqa: E731 -- 实例包一层工厂
            problems = self.registry("tools").register(name, factory, plugin_id)
        self._note_registered("tools", name, plugin_id, problems)
        if problems:
            for p in problems:
                _log.warning("plugin %s: rejected tool %r: %s", plugin_id, name, p)
                if info is not None:
                    info.warnings.append(f"rejected tool {name!r}: {p}")
        elif info is not None:
            info.tools.append(name)

    def register_tool_factory(self, name: str, factory: Any, plugin_id: str) -> None:
        """工具工厂注册：``factory(host) -> list[Tool]``（K3a ToolHost）。"""
        problems = self.registry("tools").register(name, factory, plugin_id)
        info = self._plugins.get(plugin_id)
        self._note_registered("tools", name, plugin_id, problems)
        if problems:
            for p in problems:
                _log.warning("plugin %s: rejected factory %r: %s", plugin_id, name, p)
                if info is not None:
                    info.warnings.append(f"rejected tool factory {name!r}: {p}")
        elif info is not None:
            info.tools.append(f"<factory:{name}>")

    def register_command(
        self, name: str, contrib: PluginCommand, plugin_id: str
    ) -> None:
        problems = self.registry("commands").register(name, contrib, plugin_id)
        info = self._plugins.get(plugin_id)
        self._note_registered("commands", name, plugin_id, problems)
        if problems:
            for p in problems:
                _log.warning("plugin %s: rejected command %r: %s", plugin_id, name, p)
                if info is not None:
                    info.warnings.append(f"rejected command {name!r}: {p}")
        elif info is not None:
            info.commands.append(name)

    def register_provider(self, kind: str, factory: Any, plugin_id: str) -> None:
        """provider 实现注册（base bundle 内置插件路径）。"""
        problems = self.registry("providers").register(kind, factory, plugin_id)
        info = self._plugins.get(plugin_id)
        self._note_registered("providers", kind, plugin_id, problems)
        if problems:
            for p in problems:
                _log.warning("plugin %s: rejected provider %r: %s", plugin_id, kind, p)
                if info is not None:
                    info.warnings.append(f"rejected provider {kind!r}: {p}")
        elif info is not None:
            info.providers.append(kind)

    def _note_registered(
        self, kind: str, name: str, plugin_id: str, problems: list[str]
    ) -> None:
        """注册结果记账：registered / rejected，Entry.seq 回填事件序号。

        Entry.seq 即 provenance 的 inserted_at_seq--"这个工具什么时候来
        的、谁装的"答案在账本里（沿 seq 查 registered 事件）。
        """
        origin = f"plugin:{plugin_id}"
        if problems:
            self.emit(
                "rejected",
                {
                    "type": "rejected",
                    "kind": kind,
                    "name": name,
                    "plugin": plugin_id,
                    "problems": problems,
                },
                origin=origin,
            )
            return
        event = self.emit(
            "registered",
            {"type": "registered", "kind": kind, "name": name, "plugin": plugin_id},
            origin=origin,
        )
        reg = self.registry(kind)
        assert reg is not None
        entry = reg.get(name)
        if entry is not None:
            entry.seq = event.seq

    # ── 记账（K2b）：唯一事件出口 + 可挂接的账本 sink ──────────

    def attach_ledger(
        self,
        sink: Callable[[Event], None],
        session: str = "",
        start_seq: int = 0,
    ) -> None:
        """挂接账本出口：内核只依赖 Callable，不 import 存储。

        宿主（agent）把 ``SessionStore.append_event`` 接进来；seq 从
        ``start_seq`` 续起（恢复会话时由存储侧清点既有条目）。重复挂接
        = 换 sink/会话，计数器与哈希链重置。
        """
        self._ledger_sink = sink
        self._ledger_session = session
        self._seq = start_seq
        self._prev_digest = ""

    def emit(
        self,
        type_: str,
        payload: dict[str, Any],
        cause: Optional[int] = None,
        origin: str = "kernel",
    ) -> Event:
        """唯一事件出口：分配 seq/ts/digest，append-only 投递到 sink。

        sink 故障不炸内核（记日志降级丢弃）--账本是证据系统，不该成为
        单点；未挂接时事件仅在内存计数，boot 前的组合事件自然落空。
        """
        event = Event(
            seq=self._seq + 1,
            ts=time.time(),
            session=self._ledger_session,
            type=type_,
            payload=payload,
            cause=cause,
            origin=origin,
        )
        event.digest = digest_of(self._prev_digest, event)
        self._seq = event.seq
        self._prev_digest = event.digest
        if self._ledger_sink is not None:
            try:
                self._ledger_sink(event)
            except Exception:
                _log.exception("ledger sink failed; event %r dropped", type_)
        return event

    # ── 清单 ────────────────────────────────────────────────

    def inventory(self) -> list[PluginInfo]:
        """只读投影：注册表警告回并 + 浅拷贝，每次读当下。"""
        for reg in self.registries.values():
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
