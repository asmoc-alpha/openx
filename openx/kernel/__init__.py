"""openx.kernel -- 微内核。

四职责（2026-08-24 定稿，详见 docs/design/microkernel-design.md）：
**编排 / 沙箱执行 / 插件维护 / 记账**。本包是 TCB 的一部分，保持小到
可审计：不 import agent / cli / ui，依赖方向单向（核心消费内核，反之
不然）。P1 落地的是插件维护 + 编排的装配段；沙箱执行与记账按切片
（K2 起）逐步就位。

P1 开放两类注册项（目录表驱动，见 registrations.py）：tools 与 slash
commands。混合内核纪律：loop / executor / 安全底线等内核驻留核心不在
本包，也不可插拔。

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
            factory = lambda agent: [tool]  # noqa: E731 -- 实例包一层工厂
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
        """工具工厂注册（base bundle 内置插件路径）。"""
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

    # ── 消费方 API（agent / commands 面对注册表）──────────────

    def build_provider(self, settings: dict) -> Optional[Any]:
        """按 ``settings["kind"]`` 从注册表实例化 provider 实现。

        返回**单次实现**（无重试包装）--重试由调用方组合内核
        RetryingProvider（LLMClient 门面）。未注册的 kind -> None，调用
        方决定回退。缺省 kind = "openai-compat"。
        """
        kind = str(settings.get("kind") or "openai-compat")
        reg = self.registry("providers")
        entry = reg.get(kind) if reg is not None else None
        if entry is None:
            return None
        return entry.value(settings)

    def instantiate_tools(
        self, agent: Any, *, include_builtin: bool = True, include_plugins: bool = True
    ) -> dict:
        """按 agent 实例化 tools 注册表 -> {name: Tool}。

        注册序即优先级（内置恒首挂载）：先产出的工具名先得，后来者
        跳过并记警告--"内置优先"由此成为结构性保证，无需消费方仲裁。
        ``include_plugins=False`` 供子代理使用：只实例化内置，不继承
        用户插件（同 task/exit_plan_mode 等结构性工具待遇）。
        """
        registry: dict = {}
        produced_by: dict[str, str] = {}  # tool name -> plugin id
        tools_reg = self.registry("tools")
        assert tools_reg is not None
        for entry in tools_reg.entries():
            if not include_builtin and self._is_builtin(entry.plugin):
                continue
            if not include_plugins and not self._is_builtin(entry.plugin):
                continue
            for tool in entry.value(agent):
                tname = getattr(tool, "name", "") or "<unnamed>"
                problems = validate_tool(tname, tool)
                if problems:
                    if self._is_builtin(entry.plugin):
                        # 内置插件产出畸形 = 产品坏，带病不该运行
                        raise TypeError(
                            f"builtin tool {tname!r} malformed: {'; '.join(problems)}"
                        )
                    for p in problems:
                        tools_reg.add_warning(
                            entry.name, f"rejected tool {tname!r}: {p}"
                        )
                    continue
                if tname in registry:
                    owner = produced_by[tname]
                    if self._is_builtin(owner):
                        warning = (
                            f"tool {tname!r} conflicts with builtin "
                            f"{owner!r}; builtin wins"
                        )
                    else:
                        warning = (
                            f"tool {tname!r} already provided by {owner!r}; "
                            "first wins"
                        )
                    tools_reg.add_warning(entry.name, warning)
                    continue
                registry[tname] = tool
                produced_by[tname] = entry.plugin
        return registry

    def lookup_command(self, name: str) -> Optional[Any]:
        """命令分发：主名 -> 别名 -> None（内置先查，调用方保证顺序）。"""
        commands = self.registry("commands")
        assert commands is not None
        entry = commands.get(name)
        if entry is not None:
            return entry.value.handler
        for e in commands.entries():
            if name in e.value.aliases:
                return e.value.handler
        return None

    def command_menu_entries(self) -> list[tuple[str, str, list[str]]]:
        """补全菜单数据（插件部分）：[(name, description, aliases)]。"""
        commands = self.registry("commands")
        assert commands is not None
        return [
            (e.name, e.value.description, sorted(e.value.aliases))
            for e in commands.entries()
        ]

    def note_command_conflict(self, name: str) -> None:
        """commands.py 回报内置优先跳过；记入 inventory 警告。"""
        commands = self.registry("commands")
        assert commands is not None
        commands.note_conflict(name, name)

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

    def _is_builtin(self, plugin_id: str) -> bool:
        info = self._plugins.get(plugin_id)
        return bool(info and info.builtin)


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
