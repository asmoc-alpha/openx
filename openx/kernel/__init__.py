"""openx.kernel -- 微内核（TCB）。

本包按架构五件套分 package（docs/design/microkernel-design.md §0）：
- ``assembly/``  ② 插件装配器（loader/registry/registrations/context/
  validate/manifest/protocols/plugin_spec）
- ``reasoning/`` ① 推理核心（provider/retry）
- ``audit/``     ③ 安全审计（guard）
- ``sandbox/``   ⑤ 沙箱执行（host/protect）
- ``ledger.py``  ④ 轨迹跟踪（事件账本，emit/attach_ledger 委托）
- ``inventory.py`` PluginInfo 共享模型

本文件是 **facade（编排面）**：PluginKernel 持有注册表、Ledger 与管理 API
（ensure_loaded / registry(kind) / emit / inventory / list-load-unload-help /
promote）。保持小到可审计：不 import agent / cli / ui，依赖方向单向。

P1 开放三类注册项（目录表驱动，见 assembly/registrations.py）：tools、slash
commands、providers。混合内核纪律：loop / executor / 安全底线等内核驻留
核心不在本包，也不可插拔。消费方装配策略（工具实例化仲裁、provider 解析、
命令菜单合并）不住内核--见 ``services/assembly.py`` 与 ``app/cli/commands.py``。

组合输入（P1）：用户目录 ~/.openx/plugins、项目 .openx/plugins、
pip entry-points group ``openx.plugins``；settings.json 顶层
``"plugins": {"disabled": [...]}`` 控制开关。
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Callable, Optional

from .assembly import loader
from .assembly.context import PluginCommand, PluginContext
from .assembly.manifest import validate_manifest
from .assembly.protocols import PROTOCOLS, route
from .assembly.registrations import REGISTRATIONS
from .assembly.registry import PluginRegistry
from .assembly.validate import validate_tool
from .inventory import (
    PHASE_ACTIVE,
    PHASE_DISABLED,
    PHASE_FAILED,
    PHASE_LOADING,
    PluginInfo,
)
from .ledger import Ledger
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
        # ④ 轨迹跟踪（K2b）：事件账本委托 Ledger（kernel/ledger.py）
        self._ledger = Ledger()

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
        if not self._load_apply(spec, info):
            return
        self.emit(
            "plugin_loaded",
            {"type": "plugin_loaded", "plugin": spec.id, "source": spec.source},
        )

    def _load_apply(self, spec: loader.PluginSpec, info: PluginInfo) -> bool:
        """五阶段应用主体（load_module → extract_apply → apply）。

        成功置 ACTIVE、失败置 FAILED（内置=致命）。boot 装载与 P-A
        session 装载共用，无第二条加载路径（"同源同门"）。
        """
        try:
            loaded = loader.load_module(spec)
            apply_fn = loader.extract_apply(loaded)
            if apply_fn is None:
                raise TypeError("plugin exports no apply(ctx)")
            self._apply_plugin_meta(info, loaded)
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
            return False
        self._check_protocol_consistency(info)
        info.phase = PHASE_ACTIVE
        return True

    @staticmethod
    def _check_protocol_consistency(info: Any) -> None:
        """P-D 协议一致性：显式声明的 type 与实际注册面不符 -> 记警告。

        只对显式声明且在协议目录内的 type 检查（无 type 的旧插件不查，
        避免误伤命令/纯 provider 插件）。不拒载--boot 侧沿用 P-B 容忍
        哲学，生成侧（write_plugin）才强校验拒绝。
        """
        ptype = info.manifest.get("type")
        if not ptype:
            return
        proto = route(ptype)
        if proto.ptype != ptype:  # 未知 type 走默认路由，不按默认协议要求它
            return
        registered = {
            "tools": info.tools,
            "contexts": info.contexts,
            "lifecycle": info.lifecycle,
            "ui_slots": info.ui_slots,
        }.get(proto.registry_kind)
        if not registered:
            warning = (
                f"declared type {ptype!r} but registered no {proto.registry_kind}"
            )
            if warning not in info.manifest_warnings:
                info.manifest_warnings.append(warning)

    @staticmethod
    def _apply_plugin_meta(info: PluginInfo, loaded: object) -> None:
        """插件自描述：``__openx_meta__`` → manifest 校验 + 存储（P-B）。

        problems → 抛 ValueError（调用方落 FAILED，拒载）；warnings →
        info.manifest_warnings（未知 type/mount/permission 只记不拒）。
        """
        meta = getattr(loaded, "__openx_meta__", None)
        if meta is None:
            meta = {}
        problems, warnings = validate_manifest(meta)
        if problems:
            raise ValueError(f"invalid plugin manifest: {'; '.join(problems)}")
        info.manifest = dict(meta)
        info.manifest_warnings = list(warnings)
        info.summary = str(meta.get("summary") or "")
        cost = meta.get("cost")
        if isinstance(cost, dict):
            info.cost = dict(cost)

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

    # ── P-D 协议注册面（context/v1 · lifecycle/v1）──────────────

    def register_context(self, name: str, contrib: Any, plugin_id: str) -> None:
        """上下文贡献注册（ctx.register_context 的契约回调）。

        值统一为 ``ContextContribution``（contribute + priority），消费方
        ``services.assembly.collect_context_fragments`` 征集。
        """
        problems = self.registry("contexts").register(name, contrib, plugin_id)
        info = self._plugins.get(plugin_id)
        self._note_registered("contexts", name, plugin_id, problems)
        if problems:
            for p in problems:
                _log.warning("plugin %s: rejected context %r: %s", plugin_id, name, p)
                if info is not None:
                    info.warnings.append(f"rejected context {name!r}: {p}")
        elif info is not None:
            info.contexts.append(name)

    def register_lifecycle(self, name: str, hooks: Any, plugin_id: str) -> None:
        """生命周期钩子注册（ctx.register_lifecycle 的契约回调）。"""
        problems = self.registry("lifecycle").register(name, hooks, plugin_id)
        info = self._plugins.get(plugin_id)
        self._note_registered("lifecycle", name, plugin_id, problems)
        if problems:
            for p in problems:
                _log.warning("plugin %s: rejected lifecycle %r: %s", plugin_id, name, p)
                if info is not None:
                    info.warnings.append(f"rejected lifecycle {name!r}: {p}")
        elif info is not None:
            info.lifecycle.append(name)

    def register_ui_slot(self, name: str, slot: Any, plugin_id: str) -> None:
        """UI 面板注册（ctx.register_ui_slot 的契约回调，ui/v1）。

        值统一为 ``UISlot``（render + refresh_hz），消费方
        ``services.assembly.UiPanelCollector`` 每帧征集（渲染路径故障隔离）。
        """
        problems = self.registry("ui_slots").register(name, slot, plugin_id)
        info = self._plugins.get(plugin_id)
        self._note_registered("ui_slots", name, plugin_id, problems)
        if problems:
            for p in problems:
                _log.warning("plugin %s: rejected ui slot %r: %s", plugin_id, name, p)
                if info is not None:
                    info.warnings.append(f"rejected ui slot {name!r}: {p}")
        elif info is not None:
            info.ui_slots.append(name)

    def unregister_ui_slot(self, name: str) -> None:
        """按名摘除一个 UI 面板（消费方熔断触发的自动卸载）；未注册 no-op。"""
        reg = self.registry("ui_slots")
        if reg is None:
            return
        entry = reg.get(name)
        if entry is None:
            return
        reg.unregister(name)
        self.emit(
            "unregistered",
            {"type": "unregistered", "kind": "ui_slots", "name": name,
             "plugin": entry.plugin},
            origin=f"plugin:{entry.plugin}",
        )

    def trigger_lifecycle(self, event: str, plugin_id: Optional[str] = None) -> None:
        """按注册序触发生命周期钩子（lifecycle/v1 的消费入口）。

        ``event``：session_start / checkpoint / resume / unload；``plugin_id``
        限定只触发某插件的钩子（unload 用）。故障隔离：单个钩子异常 ->
        记 warning + ``plugin_error`` 事件后继续--对主流程而言插件异常
        与"没这个钩子"同构（§3 核心原则）。
        """
        hook_attr = f"on_{event}"
        reg = self.registry("lifecycle")
        if reg is None:
            return
        for entry in reg.entries():
            if plugin_id is not None and entry.plugin != plugin_id:
                continue
            hook = getattr(entry.value, hook_attr, None)
            if not callable(hook):
                continue
            try:
                hook()
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                _log.warning(
                    "plugin %s: lifecycle %s/%s failed: %s",
                    entry.plugin, entry.name, hook_attr, error,
                )
                reg.add_warning(
                    entry.name, f"lifecycle {hook_attr} failed: {error}"
                )
                self.emit(
                    "plugin_error",
                    {
                        "type": "plugin_error",
                        "plugin": entry.plugin,
                        "where": f"lifecycle.{hook_attr}",
                        "error": error,
                    },
                    origin=f"plugin:{entry.plugin}",
                )

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
        """挂接账本出口：内核只依赖 Callable，不 import 存储（④ 委托 Ledger）。

        宿主（agent）把 ``SessionStore.append_event`` 接进来；seq 从
        ``start_seq`` 续起（恢复会话时由存储侧清点既有条目）。重复挂接
        = 换 sink/会话，计数器与哈希链重置。
        """
        self._ledger.attach(sink, session, start_seq)

    def emit(
        self,
        type_: str,
        payload: dict[str, Any],
        cause: Optional[int] = None,
        origin: str = "kernel",
    ) -> Event:
        """唯一事件出口（④ 委托 Ledger）：append-only，seq/digest 哈希链。

        sink 故障不炸内核（记日志降级丢弃）--账本是证据系统，不该成为
        单点；未挂接时事件仅在内存计数，boot 前的组合事件自然落空。
        """
        return self._ledger.emit(type_, payload, cause, origin)

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

    # ── 模型驱动装配（P-A）：元工具面的内核管理 API ───────────

    def list_plugins(self) -> list[dict]:
        """轻量插件目录（list_plugins 元工具的模型认知入口），加载序。

        只暴露轻量字段（id/phase/scope/summary/cost/注册项名），不给
        schema/代码——模型先看目录，详情经 plugin_help 按需展开。
        """
        return [
            {
                "id": p.id,
                "phase": p.phase,
                "builtin": p.builtin,
                "scope": p.scope,
                "summary": p.summary,
                "cost": dict(p.cost),
                "tools": list(p.tools),
                "commands": list(p.commands),
                "providers": list(p.providers),
                # P-D 协议注册面（模型经 plugin_help 按需展开详情）
                "contexts": list(p.contexts),
                "lifecycle": list(p.lifecycle),
                "ui_slots": list(p.ui_slots),
                # P-B：模型按 type 分组浏览的轻量面
                "type": p.manifest.get("type", ""),
                "mount": p.manifest.get("mount", ""),
                "trust": p.manifest.get("trust", "user"),
            }
            for p in self._plugins.values()
        ]

    def load_plugin(self, name: str) -> tuple[bool, str]:
        """会话内动态装载一个插件（P-A）。

        fresh discover（可找到 boot 之后新写的插件文件）；**跳过 disabled
        表**、标记 ``scope="session"``、复用五阶段校验（同源同门）。failed
        重试前先清残留注册（前次 apply 可能已入库部分条目）。已 ACTIVE →
        ``(False, "already loaded")``。
        """
        specs = {s.id: s for s in loader.discover(self.workspace)}
        spec = specs.get(name)
        if spec is None:
            return (False, f"plugin not found: {name}")
        if name in self._plugins and self._plugins[name].phase == PHASE_ACTIVE:
            return (False, f"plugin already loaded: {name}")
        # 预清理：重试 failed 时的残留注册 + 清空 info 注册项列表
        self._purge_plugin_entries(name)
        info = self._plugins.get(name)
        if info is None:
            info = PluginInfo(
                id=name, source=spec.source, phase=PHASE_LOADING, builtin=spec.builtin
            )
            self._plugins[name] = info
        info.phase = PHASE_LOADING
        info.error = ""
        info.tools = []
        info.commands = []
        info.providers = []
        info.contexts = []
        info.lifecycle = []
        info.ui_slots = []
        if not self._load_apply(spec, info):
            return (False, f"plugin failed to load: {info.error}")
        info.scope = "session"
        self.emit(
            "plugin_loaded",
            {"type": "plugin_loaded", "plugin": name, "source": spec.source},
        )
        return (True, f"plugin loaded: {name} (session)")

    def unload_plugin(self, name: str) -> tuple[bool, str]:
        """会话内卸载（P-A）：仅限 ``scope="session"`` 的插件增量。

        boot 插件属于组合输入，运行时卸载会与组合语义冲突——走组合重载
        （ensure_loaded / /workspace），不在此卸载。按 provenance 清全部
        注册条目并记账（unregistered / plugin_unloaded）。
        """
        info = self._plugins.get(name)
        if info is None or info.phase != PHASE_ACTIVE:
            return (False, f"plugin not active: {name}")
        if info.scope != "session":
            return (
                False,
                f"plugin {name} is boot-scoped; reload via composition, "
                "not session unload",
            )
        # 状态落盘契约（§1.2 卸载的有状态性）：先给插件一次 on_unload
        # 收尾（Memory 类插件落盘状态），再清注册。钩子异常被
        # trigger_lifecycle 吞掉记账，绝不阻塞卸载。
        self.trigger_lifecycle("unload", plugin_id=name)
        self._purge_plugin_entries(name)
        del self._plugins[name]
        self.emit(
            "plugin_unloaded",
            {"type": "plugin_unloaded", "plugin": name, "source": info.source},
        )
        return (True, f"plugin unloaded: {name}")

    def plugin_help(self, name: str) -> Optional[dict]:
        """插件详情（plugin_help 元工具）；未注册返回 None。"""
        info = self._plugins.get(name)
        if info is None:
            return None
        return {
            "id": info.id,
            "phase": info.phase,
            "scope": info.scope,
            "source": info.source,
            "builtin": info.builtin,
            "summary": info.summary,
            "cost": dict(info.cost),
            "warnings": list(info.warnings),
            "error": info.error,
            "tools": list(info.tools),
            "commands": list(info.commands),
            "providers": list(info.providers),
            # P-D 协议注册面
            "contexts": list(info.contexts),
            "lifecycle": list(info.lifecycle),
            "ui_slots": list(info.ui_slots),
            # P-B：manifest 全量 + 校验警告
            "manifest": dict(info.manifest),
            "manifest_warnings": list(info.manifest_warnings),
        }

    def _purge_plugin_entries(self, plugin_id: str) -> None:
        """按 provenance 清除某插件的全部注册条目并记账（unload 与 failed
        重试共用）。撤销纪律：仅贡献者自身或用户显式操作合法——本方法由
        kernel.load_plugin/unload_plugin 把关后调用。"""
        for reg in self.registries.values():
            for entry in list(reg.entries()):
                if entry.plugin == plugin_id:
                    reg.unregister(entry.name)
                    self.emit(
                        "unregistered",
                        {
                            "type": "unregistered",
                            "kind": reg.kind,
                            "name": entry.name,
                            "plugin": plugin_id,
                        },
                        origin=f"plugin:{plugin_id}",
                    )

    def promote_plugin(self, name: str) -> tuple[bool, str]:
        """用户确认晋升（P-F）：``auto-*`` 插件 trust 升 user + 决策记账。

        只记决策留痕（plugin_promoted 事件）与 trust 升级；boot 持久化
        （写回组合/overlay）列后续。回滚仍走 unload_plugin。
        """
        info = self._plugins.get(name)
        if info is None or info.phase != PHASE_ACTIVE:
            return (False, f"plugin not active: {name}")
        if not name.startswith("auto-"):
            return (False, "only auto-* (model-produced) plugins can be promoted")
        info.manifest = dict(info.manifest)
        info.manifest["trust"] = "user"
        self.emit(
            "plugin_promoted",
            {"type": "plugin_promoted", "plugin": name, "trust": "user"},
            origin="user",
        )
        return (True, f"plugin promoted: {name} (trust=user)")

    def unregister_tool(self, name: str) -> None:
        """按名摘除一个工具（P-C 熔断触发的自动卸载）；未注册 no-op。"""
        reg = self.registry("tools")
        if reg is None:
            return
        entry = reg.get(name)
        if entry is None:
            return
        reg.unregister(name)
        self.emit(
            "unregistered",
            {"type": "unregistered", "kind": "tools", "name": name,
             "plugin": entry.plugin},
            origin=f"plugin:{entry.plugin}",
        )


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
