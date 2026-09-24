"""openx.kernel -- 微内核（TCB）。

本包按架构五件套分 package（docs/design/microkernel-design.md §0）：
- ``assembly/``  ② 插件装配器（loader/registry/registrations/context/
  validate/manifest/protocols/plugin_spec）
- ``reasoning/`` ① 推理核心（provider/retry）
- ``audit/``     ③ 安全审计（guard 裁决管线 / hooks 用户钩子链）
- ``sandbox/``   ⑤ 沙箱执行（host/protect）
- ``ledger.py``  ④ 轨迹跟踪（事件账本，emit/attach_ledger 委托）
- ``global_ledger.py`` ④ 全局账本（决策事件族的跨会话留痕，emit_decision 委托）
- ``protocol.py`` ④ 轨迹跟踪的协议面（事件信封 schema，账本外化的单一真源）
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

import asyncio
import copy
import inspect
import logging
from typing import Any, Callable, Optional

from .assembly import loader
from .assembly.context import PluginCommand, PluginContext
from .assembly.manifest import scaffold_of, validate_manifest
from .assembly.protocols import PROTOCOLS, route
from .assembly.registrations import REGISTRATIONS
from .assembly.registry import PluginRegistry
from .assembly.validate import validate_tool
from .inventory import (
    PHASE_ACTIVE,
    PHASE_DISABLED,
    PHASE_FAILED,
    PHASE_LOADING,
    PHASE_RETIRED,
    PluginInfo,
)
from .ledger import Ledger
from .global_ledger import GlobalLedgerStore, retired_scaffolds
from .protocol import Event, decision_ref
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


def _log_hook_task_failure(task: "asyncio.Task[Any]") -> None:
    """吞掉即发即忘钩子任务的异常（生命周期钩子绝不炸调用方）。

    与同步钩子的异常隔离同构：插件的异步钩子失败 = observation，
    记日志即可，不向事件循环抛 "Task exception was never retrieved"。
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _log.warning("async lifecycle hook failed: %r", exc)


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
        # ④ 全局账本（K5，§3.2）：决策事件族的跨会话留痕。与会话账本分开：
        # 决策是跨会话事实，不落任一会话。惰性自挂接默认文件 sink（见
        # emit_decision）——保证"记账先于动作"不被漏接线破坏。
        self._global_ledger = Ledger()
        self._global_attached = False
        # 已晋升（trust 由 auto -> user）的插件名：卸载它们 = 回滚，记
        # plugin_rolled_back（回答"这个插件为什么没了"）。
        self._promoted: set[str] = set()
        # E4 退场登记表：``{scaffold_id: 退场条目 payload}``。**账本即单一真源**，
        # 惰性从全局账本折叠（首次 _reload 时；见 _retired_of），随后由
        # retire/restore 就地增删。组合据此跳过退场脚手架（PHASE_RETIRED）。
        self._retired: Optional[dict[str, dict[str, Any]]] = None

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
        retired = self._retired_of()   # E4：退场集合从全局账本折叠（惰性、一次）
        self.registries = {
            r.kind: PluginRegistry(r.kind, r.validator) for r in REGISTRATIONS
        }
        self._plugins = {}
        self.workspace = workspace
        # base bundle 内置插件恒先挂载（列表序即优先级的结构性前提）：
        # builtin-tools 在前--组合决议/首条注册事件的既有次序不变
        from ..builtin import BUILTIN_PLUGINS

        for spec in BUILTIN_PLUGINS:
            self._load_one(spec, disabled, retired)
        for spec in loader.discover(workspace):
            self._load_one(spec, disabled, retired)
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

    def _retired_of(self) -> dict[str, dict[str, Any]]:
        """退场登记表（E4）：首次从全局账本折叠，此后就地增删（单一真源）。

        惰性推导让内核构造不触发 IO；跨进程"摘除可恢复"由账本续接保证——
        新进程首次重组即从 ``~/.openx/ledger.jsonl`` 折出退场集合。
        """
        if self._retired is None:
            self._retired = retired_scaffolds(GlobalLedgerStore().read_all())
        return self._retired

    def _force_reload(self) -> None:
        """强制重组（retire/restore 后让组合反映新状态）。未装载过则留给首次
        ``ensure_loaded``——退场集合已在内存，重组自会应用。"""
        self._load_key = None
        if self.workspace:
            self.ensure_loaded(self.workspace)

    def _load_one(
        self, spec: loader.PluginSpec, disabled: set, retired: dict[str, dict]
    ) -> None:
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
        # E4 退场脚手架：组合跳过（不导入、不贡献），代码与注册仍在——摘除
        # 不是删除。声明从账本条目回填，故 /plugins 与 plugin_help 仍能展示
        # 其 compensates/exit_when（插件本体未被导入）。
        if spec.id in retired and not spec.builtin:
            info.phase = PHASE_RETIRED
            payload = retired[spec.id]
            info.scaffold = {
                key: payload[key]
                for key in ("compensates", "exit_when", "eval_set", "fallback")
                if payload.get(key)
            }
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
        可选的 ``scaffold`` 演进声明（E1）另行落 ``info.scaffold`` 供读面消费。
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
        info.scaffold = scaffold_of(meta)

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

    def trigger_lifecycle(
        self,
        event: str,
        plugin_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        """按注册序触发生命周期钩子（lifecycle/v1 的消费入口）。

        ``event``：session_start / checkpoint / resume / unload；``plugin_id``
        限定只触发某插件的钩子（unload 用）。故障隔离：单个钩子异常 ->
        记 warning + ``plugin_error`` 事件后继续--对主流程而言插件异常
        与"没这个钩子"同构（§3 核心原则）。

        ``payload``：可选上下文（checkpoint / resume 用，见
        ``services/checkpoint.py`` 的 payload 契约）。**零参钩子照常工作**--
        只有形参容纳时才传（见 ``_call_hook``），故既有插件零改动。

        钩子契约：**观察者 + 自我落盘点，不是修改器**。不得写 checkpoint
        （那是内核的活）、不得改 ``todos``，且必须快速返回--这条路径在
        回合的关键路径上，不是插件做 IO 的地方。**SIGINT 路径上的异步钩子
        可能来不及跑完就被进程退出丢弃**--需要持久化的插件状态请落在自己
        的位置，别指望这个回调。
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
                self._call_hook(hook, payload)
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

    @staticmethod
    def _call_hook(hook: Callable[..., Any], payload: Optional[dict[str, Any]]) -> None:
        """调用一个生命周期钩子，按**形参容量**决定是否传 payload。

        向后兼容的落点：``on_session_start`` / ``on_unload`` 历史上都是零参，
        不能因为多了个 payload 就要求既有插件改签名。有 payload 且钩子收得下
        才传；签名不可内省（C 实现、functools.partial 等）时退回零参。

        异步钩子是**即发即忘**：本方法是同步的（信号路径上不能 await）。
        过去这类钩子被静默跳过，现在至少会被调度--但没有事件循环时只能
        丢弃并告警（诚实失败胜过静默吞掉）。
        """
        accepts = 0
        try:
            params = inspect.signature(hook).parameters.values()
            accepts = sum(
                1 for p in params
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            )
        except (TypeError, ValueError):
            accepts = 0
        result = hook(payload) if (payload is not None and accepts >= 1) else hook()
        if not inspect.isawaitable(result):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None or loop.is_closed():
            _log.warning("lifecycle hook returned an awaitable but no loop is running")
            close = getattr(result, "close", None)
            if callable(close):
                close()
            return
        task = loop.create_task(result)
        task.add_done_callback(_log_hook_task_failure)

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
        start_digest: str = "",
    ) -> None:
        """挂接账本出口：内核只依赖 Callable，不 import 存储（④ 委托 Ledger）。

        宿主（agent）把 ``SessionStore.append_event`` 接进来；seq 从
        ``start_seq`` 续起、哈希链从 ``start_digest`` 续起（恢复会话时由
        存储侧清点既有条目与末条摘要）。重复挂接 = 换 sink/会话，计数器
        与哈希链重置到给定起点。

        恢复会话必须传 ``start_digest``（取 ``SessionStore.ledger_tail_digest()``），
        否则链会从头发起--seq 续上了、摘要链却断了，恰好在我们最需要它
        证明"这段历史没被改过"的时候失效。
        """
        self._ledger.attach(sink, session, start_seq, start_digest)

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

    # ── 全局账本（K5，§3.2）：决策事件族的唯一出口 ──────────────

    def attach_global_ledger(
        self,
        sink: Callable[[Event], None],
        start_seq: int = 0,
        start_digest: str = "",
    ) -> None:
        """挂接全局账本出口（显式覆盖默认文件 sink；测试/嵌入用）。

        幂等：已挂接即忽略——全局账本是进程级事实，多 agent 重复挂接不应
        重置计数器。缺省路径（``~/.openx/ledger.jsonl``）由 ``emit_decision``
        惰性自挂接，故生产无需显式调用；测试挂自己的 sink 以观察。
        """
        if self._global_attached:
            return
        self._global_ledger.attach(sink, session="", start_seq=start_seq,
                                   start_digest=start_digest)
        self._global_attached = True

    def _ensure_global_ledger(self) -> None:
        """惰性自挂接默认文件 sink：从既有账本续 seq 与哈希链（跨进程也续链）。"""
        if self._global_attached:
            return
        store = GlobalLedgerStore()
        count, tail = store.scan()
        self._global_ledger.attach(
            store.append, session="", start_seq=count, start_digest=tail
        )
        self._global_attached = True

    def emit_decision(
        self,
        type_: str,
        payload: dict[str, Any],
        cause: Optional[int] = None,
        origin: str = "user",
    ) -> Event:
        """决策事件（K5，§3.2）：全文上**全局账本**，会话账本留引用。

        - 全局账本（``~/.openx/ledger.jsonl``）：跨会话事实的唯一权威所在地，
          payload 补 ``session`` 归因字段（"哪次会话做的这个决定"）。
        - 会话账本：只记一条 ``decision_ref``（``(ledger, seq)`` 引用，
          **不复制内容**）——回放单会话时按需展开全局条目。

        返回全局账本条目（其 ``seq`` 是全局 seq，不是会话 seq）。
        """
        self._ensure_global_ledger()
        enriched = dict(payload)
        enriched.setdefault("session", self._ledger.session)
        g_event = self._global_ledger.emit(type_, enriched, cause, origin)
        self.emit(
            "decision_ref",
            decision_ref(type_, g_event.seq, session=self._ledger.session),
            cause=cause,
            origin=origin,
        )
        return g_event

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
                # E1：是否带脚手架演进声明（轻量标记；详情经 plugin_help 展开）
                # E4：退场脚手架未被导入，标记改由账本登记表回填
                "scaffold": bool(p.scaffold) or p.id in self._retired_of(),
                "retired": p.phase == PHASE_RETIRED,
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
        info.scaffold = {}
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
        # 回滚 = 卸载（§5.2）：卸载一个曾晋升的插件是**跨会话决策**，
        # 落全局账本（K5，§3.2）——回答"这个插件为什么没了"。
        if name in self._promoted:
            self._promoted.discard(name)
            self.emit_decision(
                "plugin_rolled_back",
                {"type": "plugin_rolled_back", "plugin": name},
                origin="user",
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
            # E1：脚手架演进声明（compensates/exit_when/eval_set/fallback）
            # E4：退场脚手架未被导入，声明已在 _load_one 从账本条目回填
            "scaffold": dict(info.scaffold),
            "retired": info.phase == PHASE_RETIRED,
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

        决策（``plugin_promoted``）落**全局账本**（K5，§3.2）：晋升是跨会话
        事实，会话账本只留引用。记账先于改 trust（守"记账先于动作"）。
        boot 持久化（写回组合/overlay）列后续。回滚仍走 unload_plugin。
        """
        info = self._plugins.get(name)
        if info is None or info.phase != PHASE_ACTIVE:
            return (False, f"plugin not active: {name}")
        if not name.startswith("auto-"):
            return (False, "only auto-* (model-produced) plugins can be promoted")
        self.emit_decision(
            "plugin_promoted",
            {"type": "plugin_promoted", "plugin": name, "trust": "user"},
            origin="user",
        )
        info.manifest = dict(info.manifest)
        info.manifest["trust"] = "user"
        self._promoted.add(name)
        return (True, f"plugin promoted: {name} (trust=user)")

    # ── 退场（E4）：脚手架消融线的组合层动作 ──────────────────

    def retire_scaffold(
        self,
        name: str,
        evidence: Optional[dict[str, Any]] = None,
        origin: str = "user",
    ) -> tuple[bool, str]:
        """用户确认退场（E4）：``scaffold_retired`` 上全局账本 + 组合跳过。

        仅对**已装载且带 scaffold 声明**（E1）的插件合法——退场是脚手架
        专属的消融动作，普通能力插件无资格。决策（``scaffold_retired``）落
        **全局账本**（跨会话事实），payload 带声明与可选 ``evidence``（退场
        评测门的对照结论，见 ``services/retirement_gate``）——"为什么这个模块
        没了"的答案在账本里。记账先于动作（守"记账先于动作"）。摘除不是删除：
        只是重组时跳过（PHASE_RETIRED），代码与注册仍在，``restore_scaffold``
        一键回挂。
        """
        info = self._plugins.get(name)
        if name in self._retired_of():
            return (False, f"scaffold already retired: {name}")
        if info is None or info.phase != PHASE_ACTIVE:
            return (False, f"plugin not active: {name}")
        if not info.scaffold:
            return (False, f"{name} is not a scaffold (no scaffold declaration)")
        payload: dict[str, Any] = {
            "type": "scaffold_retired",
            "plugin": name,
            "compensates": info.scaffold.get("compensates", ""),
            "exit_when": info.scaffold.get("exit_when", ""),
            "eval_set": info.scaffold.get("eval_set", ""),
            "fallback": info.scaffold.get("fallback", ""),
        }
        if evidence:
            payload["evidence"] = dict(evidence)
        self.emit_decision("scaffold_retired", payload, origin=origin)
        self._retired_of()[name] = payload
        self._force_reload()
        return (True, f"scaffold retired: {name} (composition now skips it)")

    def restore_scaffold(
        self, name: str, reason: str = "", origin: str = "user"
    ) -> tuple[bool, str]:
        """回挂一个已退场脚手架（E4）：``scaffold_restored`` 上全局账本 + 重新装载。

        摘除可恢复（§5.2）：回挂 = 后续一条 ``scaffold_restored``——账本折叠
        因此丢弃该名，重组时脚手架重新进入应载清单。模型降级自动回挂（档案
        联动）待 P6；本动作是它的人工出口。
        """
        if name not in self._retired_of():
            return (False, f"scaffold not retired: {name}")
        payload: dict[str, Any] = {"type": "scaffold_restored", "plugin": name}
        if reason:
            payload["reason"] = reason
        self.emit_decision("scaffold_restored", payload, origin=origin)
        self._retired_of().pop(name, None)
        self._force_reload()
        return (True, f"scaffold restored: {name} (composition re-includes it)")

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
