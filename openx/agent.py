"""OpenX 核心 agent 循环。

agent 的工作循环
================
1. 用户发消息；
2. LLM 思考，可能调用工具；
3. 执行工具，把结果回喂给 LLM；
4. 循环直到 LLM 给出最终文本回复。

与早期版本的关键差异（本次补全）
================================
- **跨轮对话记忆**：历史消息持久化在 ``self.history.messages`` 上，每轮不再从零开始，
  REPL 多轮对话不再失忆。
- **权限真正生效**：``ASK`` 级工具在非 auto-approve 模式下会弹出用户确认。
- **token 用量统计**：累加输入/输出 token，供 ``/cost`` 展示。
- **任务追踪**：``self.todos`` 与 ``todo_write`` 工具共享，系统提示注入进度。
- **历史压缩**：``compact_history()`` 把旧历史摘要成一条消息，腾出上下文。

系统提示由 OPENX.md 指令文件动态构建，详见 :mod:`openx.instructions`。

子代理（child）模式（Phase 8）
==============================
``OpenXAgent(config, parent=parent_agent, ...)`` 派生子代理，由 ``task``
工具（:mod:`openx.tools.subagent_tool`）驱动。子模式的语义：

- **共享**父的 console、PermissionRules（"don't ask again" 双向传播）、
  HookRunner 与 TaskRegistry（后台任务退出清理由顶层统一负责）；
- 工具集先按 :data:`~openx.orchestration.subagent.CHILD_EXCLUDED_TOOLS` 结构性
  裁剪（task/ask_user/exit_plan_mode/choose_mode——禁套娃、不打断用户、
  不触审批流与模式询问），再按规格的 ``tools`` 白名单取交集；
- 子代理**无** ``task`` / ``workflow`` 工具 → 天然无法派生孙代理
  （只许一层委派，工作流亦禁嵌套）；
- 子代理不落盘会话（``session_store`` 恒 None）；
- 系统提示追加规格的角色指令 + SUBAGENT_INSTRUCTIONS 行为契约
  （最终文本即返回值）。

MCP 支持（Phase 9）
===================
``settings.json`` 的 ``mcpServers`` 配置的远程工具经 :mod:`openx.mcp`
接入：``__init__`` 只解析配置；CLI 入口调用 ``await agent.startup()``
建连并把 ``mcp__*`` 工具登记进 ``self.tools``，``await agent.shutdown()``
幂等收尾。MCP 故障一律降级为警告、绝不打断主流程。子代理直接复用父
已连接的 MCP 工具实例（不自己建连）。
"""

from __future__ import annotations

# ── 独立调试支持：允许直接运行本文件（python openx/.../xxx.py）──────
if __name__ == "__main__" and not __package__:
    import sys as _sys
    from pathlib import Path as _Path
    _file = _Path(__file__).resolve()
    _root = _file.parent
    while _root != _root.parent and not (_root / "pyproject.toml").exists():
        _root = _root.parent
    _sys.path.insert(0, str(_root))
    __package__ = ".".join(_file.relative_to(_root).parts[:-1])

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from . import model_groups as _model_groups
from .config import OpenXConfig
from .kernel.audit.hooks import HookRunner, build_stop_payload
from .instructions import (
    build_system_prompt,
    load_instructions,
    reload_instructions,
    InstructionRegistry,
    ProjectInfo,
    PLAN_MODE_INSTRUCTIONS,
    MANUAL_MODE_INSTRUCTIONS,
    SUBAGENT_INSTRUCTIONS,
    STRUCTURED_OUTPUT_INSTRUCTIONS,
    detect_project_type,
    _CONFIG_FILES,
)
from .llm import LLMClient, StreamDone
from .mcp import MCPManager
from .permissions import PermissionLevel
from .orchestration.fleet import FleetMonitor
from .orchestration.history import ConversationHistory, SUMMARY_MARKER
from .orchestration.sessions import SessionMeta, SessionStore
from .orchestration.subagent import CHILD_EXCLUDED_TOOLS, load_subagent_specs
from .kernel.sandbox.host import ToolHost
from .kernel.assembly.plugin_spec import PLUGIN_SPEC
from .services import assembly
from .tools.ask_user_tool import AskUserTool
from .tools.mode_tools import ChooseModeTool
from .tools.plan_tools import ExitPlanModeTool
from .tools.plugin_tools import (
    ListPluginsTool,
    LoadPluginTool,
    PluginHelpTool,
    UnloadPluginTool,
)
from .tools.write_plugin_tools import (
    PromotePluginTool,
    TestPluginTool,
    WritePluginTool,
)
from .tools.subagent_tool import TaskTool
from .tools.workflow_tool import WorkflowTool
from .orchestration.tasks import TaskRegistry
from .memory import MemoryStore
from .coding_memory import CodingMemoryStore
from .services.exploration import explore_project as _explore_project
from .services.tool_executor import ToolExecutor
from .skills import Skill, load_skills, build_skills_prompt
from .tools.base import Tool
from .tools.memory_tool import MEMORY_INSTRUCTIONS
from .tools.structured_output import StructuredOutputTool
# 注：具体工具类（file/shell/git/…）已迁至 openx/builtin/tools.py——
# 内置工具集是 base bundle 内置插件，agent 经内核消费注册表（"一切能力皆插件"）。
from .ui.console import Console


@dataclass
class AgentState:
    """单轮对话的临时状态。

    ``messages`` 是本轮发给 LLM 的完整消息序列（含 system + 历史 + 本轮新增）。
    ``tool_rounds`` 记录本轮已发生的工具往返次数，防止无限循环。
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_rounds: int = 0


# 结构化输出捕获哨兵：区别于"捕获到 None"（schema 允许 null 时合法）。
_UNSET: Any = object()


@dataclass
class ToolStartEvent:
    """stream_run 结构化事件：一次工具调用开始。

    REPL 把它渲染成 ``● <name>`` 暗色指示行；headless stream-json 模式
    直接序列化成 ``tool_use`` 事件——展示格式不再混进模型数据流。
    """

    name: str
    arguments: str = ""


@dataclass
class ToolResultEvent:
    """stream_run 结构化事件：一次工具结果返回。

    ``output`` 为完整结果文本（截断与着色由消费方按场景决定：REPL
    回显前 500 字符，stream-json 另设上限）。
    """

    name: str
    output: str
    is_error: bool = False


class OpenXAgent:
    """OpenX 核心 agent —— 管理带工具调用的对话循环。"""

    def __init__(
        self,
        config: OpenXConfig,
        session_store: SessionStore | None = None,
        session_id: str | None = None,
        console: Console | None = None,
        parent: "OpenXAgent | None" = None,
        tool_allowlist: list[str] | None = None,
        subagent_extra: str = "",
        structured_schema: dict | None = None,
        binding_role: str = "main",
        binding_model_override: str | None = None,
    ):
        self.config = config
        # console：子代理共享父的 console（同一终端）；顶层自建
        self.console = console if console is not None else Console(config)
        self.workspace = Path(config.workspace).resolve()

        # ── LLM 接入（模型组 modelGroups 角色绑定）─────────────────
        # 本 agent 自己的 ``self.llm``/run 循环绑定的角色：顶层 = main，
        # 子代理 = exec（task/任务委派），经 config.role_settings 解析出
        # （组内共享 key/base、per-role 可覆盖；角色缺席回落 main 绑定）。
        # 须在 ensure_loaded 之后取实现（内核注册表就绪）。
        from .kernel import get_kernel

        self._bind_role = _model_groups.canonical_role(binding_role) or binding_role
        self._binding_model_override = binding_model_override
        self._group_name, settings = config.role_settings(binding_role)
        if binding_model_override:
            settings["model"] = binding_model_override
        self._provider_name = self._group_name
        self._provider_settings = settings
        # 角色客户端惰性缓存（exec/mini/modal 只在用到时按组内配置创建）
        self._role_clients: dict[str, Any] = {}
        self._role_settings_cache: dict[str, dict] = {}

        kernel = get_kernel()
        kernel.ensure_loaded(str(self.workspace))
        impl = assembly.resolve_provider_impl(kernel, self._provider_settings)
        if impl is None:
            # kind 未注册（如 anthropic SDK 未装）：警告并回落 openai-compat。
            # LLMClient(impl=None) 自动退到直连实现；product 不带病运行。
            warn = getattr(self.console, "print_warning", None)
            if callable(warn):
                try:
                    warn(
                        f"Provider implementation "
                        f"'{self._provider_settings.get('kind')}' unavailable "
                        "(missing SDK?); falling back to openai-compat."
                    )
                except Exception:
                    pass
        self.llm = LLMClient(
            config,
            impl=impl,
            policy_overrides=self._provider_settings,
        )
        # LLM 重试可见性：每次重试打印一行警告（_notify_retry 内部经弹窗
        # 钩子暂停流式 Live，避免重绘区夹杂输出；钩子缺省 → 零行为变化）
        self.llm.on_retry = self._notify_retry
        # 把生效绑定投影回 config，供 header/init event/serve 展示
        config.active_group = self._group_name
        config.model = self._provider_settings.get("model") or config.model
        config.api_key = self._provider_settings.get("api_key") or config.api_key
        config.api_base = self._provider_settings.get("api_base") or config.api_base

        # ── 子代理（child）模式状态（Phase 8，必须在 _build_tools 之前就位）──
        # parent 非 None → 本 agent 是 task 工具派生的子代理：工具集裁剪、
        # 共享父的 rules/hooks/tasks、系统提示追加子代理契约（见模块 docstring）
        self._parent = parent
        # 规格声明的工具白名单（None = 除结构性排除外全部保留）
        self._tool_allowlist = tool_allowlist
        # 规格 .md 正文 → 追加进系统提示的角色指令
        self._subagent_extra = subagent_extra
        # 结构化输出契约（仅子代理路径注入）：非 None 时 _build_tools 追加
        # structured_output 工具、系统提示追加强制契约；StructuredOutputTool
        # 校验通过后把结果写入 _structured_result 并终止运行循环。
        self._structured_schema = structured_schema
        self._structured_result: Any = _UNSET

        # ── 会话标识与 hooks（Phase 5）──────────────────────────
        # session_id：钩子 payload 里标识本次会话；Phase 6 会话持久化复用它
        # 作为会话文件名（恢复会话时由 CLI 传入原 id，保持三者一致）
        self.session_id = session_id or uuid.uuid4().hex[:12]
        # 会话持久化存储（Phase 6）：None → 不落盘（测试/嵌入式用法）。
        # 子代理恒为 None——委派任务的中间过程不进会话文件。
        self.session_store = session_store
        # 记账接线（K2b）：内核 emit -> 会话账本。须在 _build_tools 之前
        # 挂接--组合决议与插件装载事件在首次 ensure_loaded 时产生；seq
        # 从既有信封条目续起（恢复会话不重号）。子代理不落盘，自然不挂。
        if session_store is not None:
            from .kernel import get_kernel

            get_kernel().attach_ledger(
                session_store.append_event,
                session=self.session_id,
                start_seq=session_store.ledger_start_seq(),
            )
        # provider_selected（M5，origin=kernel）：agent 绑定 provider 留痕--
        # "这次回答用了哪个模型"的答案来源。须在 attach_ledger 之后 emit
        # 才落账本；emit 本身安全（未挂接 sink 时仅内存计数，绝不炸）。
        self._emit_provider_selected(origin="kernel")
        if parent is not None:
            # 子代理复用父的 HookRunner（同一 settings 合并结果）；绝不覆盖
            # 共享对象上的 session_id——钩子 payload 仍归属父会话。
            self.hooks = parent.hooks
        else:
            # hooks：全局 + 项目 settings.json 合并加载；注入 ToolExecutor 供
            # PreToolUse/PostToolUse 使用，UserPromptSubmit/Stop 由 REPL/agent 触发
            self.hooks = HookRunner.load(str(self.workspace))
            self.hooks.session_id = self.session_id

        # ── 权限模式（manual/auto/plan，唯一状态源）────────────────
        # 必须在 executor/schema/prompt 计算之前就位。启动默认 manual
        # （只读放行、写入逐项授权）；子代理**快照**派生时刻的父模式
        # （共享 console + rules，manual 父的子代理写入照样逐项弹窗）。
        # plan_mode 属性与 set_plan_mode 包装器保留旧 API 兼容。
        self._mode: str = parent._mode if parent is not None else "manual"
        # 进入 plan 前的 auto_approve 与模式，退出时原样还原（重复进入
        # plan 不覆盖最初的保存值）
        self._pre_plan_auto_approve: bool | None = None
        self._pre_plan_mode: str | None = None
        # choose_mode 本会话是否已弹过（防重复打扰；主动 /mode manual
        # 回切时复位，允许再次询问）
        self.mode_choice_offered: bool = False

        self.tool_executor = ToolExecutor(
            self.console, auto_approve=config.auto_approve,
            hook_runner=self.hooks,
            # 子代理共享父的 PermissionRules 对象：父批准的 "don't ask again"
            # 规则对子代理立即生效，子代理批准的同理回流（同一对象，双向传播）
            rules=parent.tool_executor.rules if parent is not None else None,
            mode=self._mode,
        )

        # ── 会话级状态（跨轮持久）──────────────────────────────
        # 对话历史：不含 system 消息，只存 user/assistant/tool 序列
        self.history = ConversationHistory(max_tokens=config.max_history_tokens)
        # 缓存系统提示，避免每轮重新拼装；指令/工作区变更时重建
        self._system_prompt: str = ""
        # 累计 token 用量（供 /cost 与退出用量面板展示）
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        # 累计缓存命中 token（provider 报告的可选字段，未报告恒 0）
        self.total_cached_tokens: int = 0
        # 累计插件 schema token（装配预算口径估算：每轮 LLM 调用把当时
        # ACTIVE 插件的 schemaTokens 之和记一笔；内置恒 0 = 基线不归插件）
        self.total_plugin_tokens: int = 0
        # 任务清单：与 TodoWriteTool 共享同一 list 对象
        self.todos: list[dict[str, Any]] = []
        # 最近一次 run() 的工具往返数（headless JSON 输出的 num_turns）
        self.last_tool_rounds: int = 0
        # 子代理舰队监视器（v0.4.0 状态层）：task 工具 / 工作流在此登记
        # 子代理视图，StreamingService 5Hz 取快照渲染输入框下方的 deck。
        # 子代理也各自构造（永不渲染）——接线方 getattr 兜底，无行为变化。
        self.fleet = FleetMonitor()

        # ── MCP 支持（Phase 9）──────────────────────────────────
        # 只解析配置、不建连接（连接发生在 startup()）。子代理同样加载
        # 配置，但子代理从不调用 startup()/shutdown()——它们的 MCP 工具
        # 直接复用父已连接的共享实例（见 _build_tools）。
        self.mcp = MCPManager.load(str(self.workspace))
        # startup()/shutdown() 幂等守卫（防重连、防重复关闭）
        self._started = False

        # 持久化记忆（~/.openx/memory/）——必须先于工具注册表：
        # MemoryTool 以引用方式持有 coding_memory（_build_tools 内构造）
        self.memory = MemoryStore()

        # Coding Agent 结构化记忆（项目级 + 全局）
        self.coding_memory = CodingMemoryStore(workspace=str(self.workspace))

        # 构建工具注册表（todos 与 console 以引用方式注入相应工具）
        self.tools: dict[str, Tool] = self._build_tools()
        self.tool_schemas = self._compute_tool_schemas()

        # 插件 UI 面板征集器（ui/v1，P-D）：交互层把它传给 StreamingService，
        # deck 每帧征集插件面板（崩溃跳过/熔断在收集器内，渲染帧不被插件
        # 拖死）。子代理不消费（面板只进顶层渲染），接线方 getattr 兜底。
        from .kernel import get_kernel

        self.ui_panels = assembly.UiPanelCollector(get_kernel())

        # 已安装的 skills（全局 + 项目级）
        self.skills: dict[str, Skill] = load_skills(self.workspace)

        # 加载 OPENX.md 指令文件，并构建初始系统提示
        self._instructions: InstructionRegistry = load_instructions(self.workspace)
        self._system_prompt = self._build_system_prompt()

        # 单一 Console 同步：状态行/弹窗与 agent 共用一个 console 实例
        # （main.py 传入；子代理共享父 console → 模式相同，赋值幂等）
        self.console.mode = self._mode

    # ── 结构化输出 ─────────────────────────────────────────────

    def has_structured_result(self) -> bool:
        """是否已捕获经校验的结构化结果（schema 契约是否履行）。"""
        return self._structured_result is not _UNSET

    @property
    def structured_result(self) -> Any:
        """已捕获的结构化结果对象（任意 JSON 值，含合法的 null）。

        未捕获时访问抛 ``RuntimeError``——调用方须先查
        :meth:`has_structured_result`（_UNSET 哨兵不外泄）。
        """
        if self._structured_result is _UNSET:
            raise RuntimeError(
                "structured_result accessed before capture; "
                "check has_structured_result() first"
            )
        return self._structured_result

    # ── 系统提示与指令 ───────────────────────────────────────────

    def _build_system_prompt(self) -> str:
        """构建系统提示：基础提示 + 记忆 + OPENX.md 指令 + 当前 todo 进度。"""
        prompt = build_system_prompt(self.workspace, self._instructions)

        # 注入持久化记忆
        memory_context = self.memory.build_context_prompt()
        if memory_context:
            prompt += memory_context

        # 注入 Coding Agent 结构化记忆（带 token 预算控制）
        coding_mem_prompt = self.coding_memory.build_context_prompt()
        if coding_mem_prompt:
            prompt += coding_mem_prompt

        # 注入记忆系统使用指令（告诉 agent 何时自主记忆/召回）
        prompt += MEMORY_INSTRUCTIONS

        # 注入已安装的 skills 指令
        skills_prompt = build_skills_prompt(self.skills)
        if skills_prompt:
            prompt += skills_prompt

        # P-D 上下文协议（context/v1）：征集插件上下文片段（pre-inference
        # 组装点）。子代理不继承用户插件贡献（能力继承 = 父集的子集，
        # 与工具实例化同款口径）；单插件崩溃由征集侧隔离，不炸提示组装。
        try:
            from .kernel import get_kernel

            fragments = assembly.collect_context_fragments(
                get_kernel(),
                assembly.CONTEXT_BUDGET,
                include_plugins=self._parent is None,
            )
        except Exception:
            fragments = []
        for fragment in fragments:
            prompt += "\n\n" + fragment

        # 若存在任务清单，注入进度摘要，让模型感知当前状态
        if self.todos:
            lines = ["", "## Current Task List", ""]
            for t in self.todos:
                mark = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}.get(
                    t.get("status", "pending"), "[ ]"
                )
                lines.append(f"{mark} {t.get('content', '')}")
            lines.append(
                "\nKeep this list updated via the todo_write tool. "
                "Have exactly one task in_progress at a time."
            )
            prompt += "\n".join(lines)

        # 模式指令（仅顶层 agent 注入——子代理无 choose_mode/exit_plan_mode，
        # 模式仅影响其闸门行为，不改变提示）
        if self._parent is None:
            if self._mode == "plan":
                prompt += PLAN_MODE_INSTRUCTIONS
            elif self._mode == "manual":
                prompt += MANUAL_MODE_INSTRUCTIONS
            # P-F 模型自产插件：write_plugin 的编写契约常驻（体积极小，
            # 只读这一份就能生成合规插件）。
            prompt += "\n\n" + PLUGIN_SPEC

        # 子代理模式（Phase 8）：规格 .md 正文的角色指令 + 子代理行为契约
        # （最终文本即返回值、不得提问、结果自包含）
        if self._subagent_extra:
            prompt += "\n\n## Sub-agent role instructions\n\n"
            prompt += self._subagent_extra.strip()
        if self._parent is not None:
            prompt += SUBAGENT_INSTRUCTIONS

        # 结构化输出契约（带 schema 的子代理）：覆盖"最终文本即返回值"
        # 的默认语义——结果只认 structured_output 一次调用。坏 schema
        # 序列化失败时降级跳过（_build_tools 侧仍注入工具，双保险）。
        if self._structured_schema is not None:
            try:
                schema_json = json.dumps(
                    self._structured_schema, ensure_ascii=False, indent=2
                )
            except (TypeError, ValueError):
                schema_json = str(self._structured_schema)
            prompt += STRUCTURED_OUTPUT_INSTRUCTIONS.format(schema=schema_json)

        return prompt

    def reload_instructions(self) -> InstructionRegistry:
        """从磁盘重新加载 OPENX.md。

        在运行期创建/编辑 OPENX.md（如 /init）后调用，使下一轮查询生效。
        同时重建缓存的系统提示。
        """
        self._instructions = reload_instructions(self.workspace)
        self._system_prompt = self._build_system_prompt()
        return self._instructions

    @property
    def instructions(self) -> InstructionRegistry:
        """当前已加载的指令注册表。"""
        return self._instructions

    # ── 多模态用户消息构造 ───────────────────────────────────────

    @staticmethod
    def build_user_content(
        text: str,
        images: list[str] | None = None,
    ) -> str | list[dict[str, Any]]:
        """构造用户消息内容，可选附带图片。

        无图片时返回纯字符串；有图片时返回 OpenAI 多模态 content parts 列表。

        Args:
            text: 用户文本。
            images: 可选的 base64 data URL 列表（如 ``data:image/png;base64,...``）。

        Returns:
            字符串（纯文本）或 content dict 列表（多模态）。
        """
        if not images:
            return text

        content: list[dict[str, Any]] = [
            {"type": "text", "text": text},
        ]
        for url in images:
            content.append({
                "type": "image_url",
                "image_url": {"url": url, "detail": "auto"},
            })
        return content

    # ── 工具注册表 ───────────────────────────────────────────────

    def _build_tools(self) -> dict[str, Tool]:
        """组装工具注册表：结构性工具 + 注册表能力工具 + MCP/子代理裁剪。

        注意：``TodoWriteTool`` 经 ToolHost 接收 ``self.todos`` 的引用，
        ``AskUserTool`` 接收 ``self.console`` 的引用，``ExitPlanModeTool``
        接收 agent 与 console 的引用——这样工具内部操作直接作用于
        agent 状态。
        """
        # 后台任务注册表（Phase 7）：shell 后台模式与 task_output/task_stop
        # 工具共享同一实例；目录惰性创建，构造本身不触碰磁盘。
        # 子代理复用父的注册表（Phase 8）：子代理派生的后台任务归顶层统一
        # 退出清理，避免孤儿进程。
        self.tasks = self._parent.tasks if self._parent is not None else TaskRegistry()

        from .kernel import get_kernel

        kernel = get_kernel()
        kernel.ensure_loaded(str(self.workspace))

        # 结构性工具（内核驻留编排核心，非插件；仅顶层 agent 持有）——
        # 恒先占位：插件同名工具在实例化时被拒并记警告（assembly 的
        # reserved），"内置优先"是结构性保证。子代理规格表副作用保持
        # 原语义：仅顶层填充（load_subagent_specs 坏文件跳过不报错）。
        registry: dict[str, Tool] = {}
        reserved: dict[str, str] = {}
        self._subagent_specs: dict = {}
        if self._parent is None:
            self._subagent_specs = load_subagent_specs(str(self.workspace))
            for tool in (
                AskUserTool(self.console),
                ExitPlanModeTool(self, self.console),
                ChooseModeTool(self, self.console),
                TaskTool(self, self._subagent_specs),
                WorkflowTool(self),
                # 模型驱动装配元工具（P-A）：插件管理暴露给模型。list/help
                # 只读；load/unload 为 ASK，成功后经 _rebuild_tools 重建工具集。
                ListPluginsTool(kernel),
                PluginHelpTool(kernel),
                LoadPluginTool(kernel, self),
                UnloadPluginTool(kernel, self),
                # 模型自产插件（P-F）：write（ASK，admit 管线）/ test /
                # promote（ASK，决策记账）。
                WritePluginTool(kernel, self),
                TestPluginTool(kernel),
                PromotePluginTool(kernel),
            ):
                registry[tool.name] = tool
                reserved[tool.name] = "builtin-structural"

        # 能力工具 = base bundle 内置插件 + 用户插件（"一切能力皆插件"）：
        # 工厂按 ToolHost 实例化（K3a）——插件拿不到 agent 本体；注册序
        # 即优先级、内置恒首。用户插件工具仅顶层 agent 载入，子代理不
        # 继承（能力继承 = 父集的子集，内核详设 §2.5）。
        host = ToolHost(
            workspace=str(self.workspace),
            todos=self.todos,
            tasks=self.tasks,
            coding_memory=self.coding_memory,
            allow_write_outside_workspace=self.config.allow_write_outside_workspace,
            allowed_commands=self.config.allowed_commands,
            dangerous_commands=self.config.dangerous_commands,
            web_search_provider=self.config.web_search_provider,
        )
        registry.update(
            assembly.instantiate_tools(
                kernel,
                host,
                include_plugins=self._parent is None,
                reserved=reserved,
            )
        )

        # 已连接的 MCP 工具并入注册表（Phase 9）：__init__ 时尚未连接
        # （self.mcp.tools 为空）；startup() 之后的注册表重建（/workspace）
        # 借此不丢失 MCP 工具。
        mcp = getattr(self, "mcp", None)
        if mcp is not None:
            registry.update(mcp.tools)

        # 子代理工具裁剪（Phase 8）：先结构性排除（task/ask_user/
        # exit_plan_mode），再按规格白名单取交集（None → 保留全部剩余）。
        if self._parent is not None:
            for excluded in CHILD_EXCLUDED_TOOLS:
                registry.pop(excluded, None)
            # MCP 工具继承父（Phase 9）：共享实例即可——transport 的
            # future 按 id 分发，父子并发调用互不干扰。放在白名单交集
            # **之前**，显式白名单可自然纳入/排除 mcp__* 工具。
            for name, tool in self._parent.tools.items():
                if name.startswith("mcp__"):
                    registry[name] = tool
            if self._tool_allowlist is not None:
                allowed = set(self._tool_allowlist)
                registry = {n: t for n, t in registry.items() if n in allowed}

        # 结构化输出出口（带 schema 的子代理专属）：放在白名单交集
        # **之后**——规格的工具白名单绝不能把它裁掉，否则结构化契约
        # 无法履行。无 schema 的 agent（含一切顶层 agent）永远没有它。
        if self._structured_schema is not None:
            registry[StructuredOutputTool.name] = StructuredOutputTool(
                self, self._structured_schema
            )
        return registry

    # ── Plan 模式与 schema 中心计算 ─────────────────────────────

    def _compute_tool_schemas(self) -> list:
        """Build OpenAI tool schemas with mode-based visibility.

        中心计算点：agent.__init__ 与 /workspace 重建都走这里。
        - plan mode：模型"看不见" ASK/DENY 级工具（write_file/edit_file/
          shell），只余只读工具与 exit_plan_mode 审批出口——第一道防线；
        - choose_mode 仅 manual 模式可见（非 manual 下模型看不见它，
          ToolExecutor 还有第二道防线）；
        - manual/auto：其余工具全部可见（manual 只改变弹窗行为）。
        """
        return [
            t.to_openai_schema() for t in self.tools.values()
            if not (
                (
                    self._mode == "plan"
                    and t.permission.level
                    in (PermissionLevel.ASK, PermissionLevel.DENY)
                )
                or (t.name == "choose_mode" and self._mode != "manual")
            )
        ]

    def _rebuild_tools(self) -> None:
        """重建工具注册表与 schema（load/unload 插件后调用；/workspace 同款）。

        P-A 模型驱动装配：元工具 load/unload 成功后触发，让新装配的工具
        下一轮进入模型视野。重建后 meta-tools 自身仍是结构占位，恒在。
        P-D：context/v1 插件的片段挂在系统提示上，故系统提示一并重建--
        装配/卸载上下文类插件后，片段下一轮即生效/消失。
        """
        self.tools = self._build_tools()
        self.tool_schemas = self._compute_tool_schemas()
        self._system_prompt = self._build_system_prompt()

    # ── 权限模式（manual/auto/plan）──────────────────────────────

    _VALID_MODES = ("manual", "auto", "plan")

    @property
    def mode(self) -> str:
        """当前权限模式（唯一状态源）：manual / auto / plan。"""
        return self._mode

    @property
    def plan_mode(self) -> bool:
        """兼容属性：True 当且仅当 mode == "plan"（只读）。"""
        return self._mode == "plan"

    def set_mode(self, mode: str) -> None:
        """切换权限模式；统一同步 executor/console/schemas/系统提示。

        manual: ASK 工具永远逐项弹窗（规则/白名单/auto_approve 被绕过）。
        auto:   常规权限流 + 危险 shell 命令永远弹窗。
        plan:   ASK/DENY 工具 schema 隐藏 + executor 硬拦截；经
                exit_plan_mode 审批退出。

        auto_approve 的保存/还原**严格限定在 plan 进出**：进入 plan 时
        保存一次并强制关闭（审批必须走 exit_plan_mode），离开 plan 时
        原样还原。manual 不动 auto_approve——闸门层直接忽略它。

        Enter/exit plan mode semantics are preserved from the old
        set_plan_mode; manual leaves auto_approve untouched (the gate
        ignores it instead).
        """
        if mode not in self._VALID_MODES:
            raise ValueError(
                f"Unknown mode {mode!r}; choose from {self._VALID_MODES}"
            )
        # 进入 plan：保存 auto_approve（只在尚无保存值时记录——重复进入
        # 绝不覆盖最初的保存值）与来源模式，并强制关闭 auto_approve
        if mode == "plan" and self._mode != "plan":
            if self._pre_plan_auto_approve is None:
                self._pre_plan_auto_approve = self.tool_executor.auto_approve
                self._pre_plan_mode = self._mode
        if mode == "plan":
            self.tool_executor.auto_approve = False
        elif self._mode == "plan" and self._pre_plan_auto_approve is not None:
            # 离开 plan：原样还原
            self.tool_executor.auto_approve = self._pre_plan_auto_approve
            self._pre_plan_auto_approve = None
            self._pre_plan_mode = None
        # 主动回切 manual（/mode manual）：复位 choose_mode 防重复闩，
        # 允许下一个变更任务再次询问 plan/auto
        if mode == "manual" and self._mode != "manual":
            self.mode_choice_offered = False
        self._mode = mode
        self.tool_executor.mode = mode
        self.console.mode = mode
        self.tool_schemas = self._compute_tool_schemas()
        self._system_prompt = self._build_system_prompt()

    def set_plan_mode(self, on: bool) -> None:
        """兼容包装：进入 plan，或退出 plan 还原进入前的模式。

        ``on=False`` 且当前并非 plan 时为空操作（绝不意外切换 manual→auto）。
        """
        if on:
            self.set_mode("plan")
        elif self._mode == "plan":
            self.set_mode(self._pre_plan_mode or "auto")

    # ── 生命周期（MCP，Phase 9）─────────────────────────────────

    async def startup(self) -> None:
        """连接配置的 MCP servers 并登记其远程工具。

        幂等：``_started`` 守卫防止重连。connect_all 内部逐 server 吞
        异常（警告后继续），MCP 故障绝不阻塞 agent 启动。登记完成后
        重算 tool_schemas，让模型立即可见新工具。

        仅顶层 agent 调用（CLI 入口负责）；子代理从不调用 startup()。
        """
        if self._started:
            return
        self._started = True
        await self.mcp.connect_all(self.console)
        self.tools.update(self.mcp.tools)
        self.tool_schemas = self._compute_tool_schemas()
        # P-D 生命周期协议（lifecycle/v1）：会话启动钩子按注册序回调。
        # 钩子异常由内核捕获记账（插件异常 = observation），不炸启动。
        try:
            from .kernel import get_kernel

            get_kernel().trigger_lifecycle("session_start")
        except Exception:
            pass

    async def shutdown(self) -> None:
        """关闭所有 MCP 连接。幂等、绝不抛出。"""
        if not self._started:
            return
        self._started = False
        try:
            await self.mcp.shutdown()
        except Exception:
            pass

    # ── Stop 钩子（Phase 5）─────────────────────────────────────

    async def _fire_stop_hook(self, stop_reason: str) -> None:
        """触发 Stop 钩子：v1 仅打印警告（blocked 标志忽略）。

        在 ``run()`` / ``stream_run()`` 的最终回复点与达到最大轮次点调用。
        钩子系统绝不能打断回合收尾——任何异常全部吞掉。
        """
        try:
            if not self.hooks.has_hooks("Stop"):
                return
            outcome = await self.hooks.run(
                "Stop",
                build_stop_payload(
                    stop_reason,
                    workspace=self.hooks.workspace,
                    session_id=self.hooks.session_id,
                ),
            )
            warn = getattr(self.console, "print_warning", None)
            if callable(warn):
                for w in outcome.warnings:
                    try:
                        warn(w)
                    except Exception:
                        pass
        except Exception:
            pass

    # ── 历史管理（委托给 ConversationHistory）───────────────────

    def clear_history(self) -> None:
        """清空对话历史（``/clear`` 调用）。"""
        self.history.clear()

    def history_token_estimate(self) -> int:
        """当前历史的近似 token 数。"""
        return self.history.estimate_tokens()

    async def compact_history(self, keep_last: int = 4) -> str:
        """压缩历史：把旧历史摘要成一条 user 消息，保留最近若干轮原文。

        走 mini 角色（最简模型做廉价摘要）；mini 未配置回落 main。
        """
        return await self.history.compact(
            self.client_for("mini"), keep_last=keep_last
        )

    async def _maybe_auto_compact(self) -> bool:
        """历史逼近上限时自动压缩；失败绝不打断本轮对话。

        触发条件：估算 token 数超过 ``max_history_tokens`` 的 80%。
        动作：``compact(keep_last=4)``（Phase 1 语义——保留最近 4 轮原文，
        轮以 user 消息为界，tool_call/tool_result 对永不被拆散）。

        压缩只是优化、不在回合关键路径上：任何异常都被吞掉、历史保持
        原样（``compact`` 自身亦在失败时不改动缓冲）。

        Returns:
            是否真的完成了压缩（摘要哨兵已落库），供调用方决定是否发 UI 提示。
        """
        threshold = int(self.config.max_history_tokens * 0.8)
        if self.history.estimate_tokens() <= threshold:
            return False
        try:
            await self.history.compact(self.client_for("mini"), keep_last=4)
        except Exception:
            # 压缩失败必须留给下一轮重试，而不是毁掉当前回合
            return False
        # 区分"真的压缩了"与"历史太短的 no-op"：只有摘要哨兵落库才通知 UI
        return bool(self.history.messages) and str(
            self.history.messages[0].get("content") or ""
        ).startswith(SUMMARY_MARKER)

    def _notify_retry(
        self, attempt: int, max_retries: int, error: BaseException, delay: float
    ) -> None:
        """LLM 重试一行通知（``LLMClient.on_retry`` 回调）。

        流式 Live 活动期间经控制台弹窗钩子暂停重绘再打印（与权限弹窗同
        一路径，避免输出落进重绘区被擦掉）；钩子缺省（非流式/单次模式）
        时直接打印。所有异常一律吞掉——通知失败绝不能影响重试本身。
        """
        status = getattr(error, "status_code", None)
        label = f"HTTP {status}" if status else type(error).__name__
        wait = f" in {delay:.0f}s" if delay >= 0.5 else ""
        try:
            start = getattr(self.console, "on_dialog_start", None)
            end = getattr(self.console, "on_dialog_end", None)
            if start is not None:
                start()
            try:
                self.console.print_warning(
                    f"LLM API error ({label}); retry {attempt}/{max_retries}{wait}"
                )
            finally:
                if end is not None:
                    end()
        except Exception:
            pass

    # ── 模型组绑定与切换（modelGroups 角色路由）──────────────────

    def _emit_provider_selected(self, origin: str) -> None:
        """记账：provider_selected 事件（agent 绑定 / /model 切换）。

        payload：组名、kind、model、role、origin（kernel=绑定 / user=切换）。
        切换留痕是将来"为什么这次回答换了模型"的答案来源；emit 只依赖
        内核出口，任何异常绝不影响绑定/切换本身。
        """
        try:
            from .kernel import get_kernel

            get_kernel().emit(
                "provider_selected",
                {
                    "type": "provider_selected",
                    "provider": self._provider_name,
                    "group": self._group_name,
                    "role": self._bind_role,
                    "kind": self._provider_settings.get("kind", "openai-compat"),
                    "model": self._provider_settings.get("model", ""),
                    "origin": origin,
                },
                origin=origin,
            )
        except Exception:
            pass

    def role_settings(self, role: str) -> dict:
        """取某角色在**当前组**下的设置 dict（缓存在内存）。

        ``role`` 为绑定角色（"main"/"exec"…）时直接返回当前绑定设置；其余
        角色经 config 重解析（读到的是当前组的文件状态）。内部一律用长键。
        """
        role_key = _model_groups.canonical_role(role) or role
        if role_key == self._bind_role:
            return self._provider_settings
        cached = self._role_settings_cache.get(role_key)
        if cached is None:
            cached = self.config.role_settings(role_key)[1]
            self._role_settings_cache[role_key] = cached
        return cached

    def client_for(self, role: str) -> Any:
        """取某角色的 LLMClient（惰性建；缺席/与 main 绑定全等 → self.llm）。

        mini/exec/modal 未配置或与 main 绑定完全相同（kind/key/base/model）
        时复用主客户端——组里只有 main 就绝不额外建对象。角色 kind 未注册
        （如 anthropic SDK 缺失）按现有语义告警并回落主客户端。
        """
        role_key = _model_groups.canonical_role(role) or role
        if role_key == _model_groups.MAIN_ROLE or role_key == self._bind_role:
            return self.llm
        cached = self._role_clients.get(role_key)
        if cached is not None:
            return cached
        settings = self.role_settings(role_key)
        if self._same_binding(settings):
            return self.llm
        from .kernel import get_kernel

        kernel = get_kernel()
        kernel.ensure_loaded(str(self.workspace))
        impl = assembly.resolve_provider_impl(kernel, settings)
        if impl is None:
            try:
                warn = getattr(self.console, "print_warning", None)
                if callable(warn):
                    warn(
                        f"Role '{role_key}' implementation "
                        f"'{settings.get('kind')}' unavailable; using main model."
                    )
            except Exception:
                pass
            return self.llm
        client = LLMClient(self.config, impl=impl, policy_overrides=settings)
        client.on_retry = self._notify_retry
        self._role_clients[role_key] = client
        return client

    def _same_binding(self, settings: dict) -> bool:
        """settings 是否与当前绑定等价（kind/key/base/model 全同）。"""
        cur = self._provider_settings
        return (
            settings.get("kind") == cur.get("kind")
            and (settings.get("api_key") or "") == (cur.get("api_key") or "")
            and (settings.get("api_base") or "") == (cur.get("api_base") or "")
            and settings.get("model") == cur.get("model")
        )

    def _drop_role_clients(self) -> None:
        """清空惰性角色客户端与缓存（组/角色切换后调用）。"""
        self._role_clients.clear()
        self._role_settings_cache.clear()

    def _rebuild_llm(self) -> bool:
        """按当前组 + 绑定角色重建 self.llm（主/子代理自身的客户端）。

        同步 config 投影并 drop 角色缓存。kind 解析失败返回 False。
        """
        from .kernel import get_kernel

        settings = dict(
            self.config.role_settings(self._bind_role, group_name=self._group_name)[1]
        )
        if self._binding_model_override:
            settings["model"] = self._binding_model_override
        kernel = get_kernel()
        kernel.ensure_loaded(str(self.workspace))
        impl = assembly.resolve_provider_impl(kernel, settings)
        if impl is None:
            return False
        self._provider_name = self._group_name
        self._provider_settings = settings
        self.llm = LLMClient(self.config, impl=impl, policy_overrides=settings)
        self.llm.on_retry = self._notify_retry
        self.config.model = settings.get("model") or self.config.model
        self.config.api_key = settings.get("api_key") or self.config.api_key
        self.config.api_base = settings.get("api_base") or self.config.api_base
        self._drop_role_clients()
        return True

    def switch_group(self, name: str) -> bool:
        """把本 agent 绑定切到模型组 ``name``（/model <组>）。

        校验组存在 + main 的 kind 可解析；重建 self.llm、投影 config、清空
        角色缓存并记 ``provider_selected``（origin=user）。持久化
        ``activeGroup`` 由命令层负责（set_active_group），切组即换掉整个
        组的角色绑定（main/exec/mini/modal 下一轮都读新组）。
        """
        from .config import OpenXConfig

        groups, active, _ = OpenXConfig.load_model_groups()
        if name not in groups:
            return False
        self._group_name = name
        if not self._rebuild_llm():
            return False
        self.config.active_group = name
        self._emit_provider_selected(origin="user")
        return True

    def set_role_model(self, role: str, model: str) -> bool:
        """持久化 (当前组, 角色) 的模型为 ``model``；影响当前绑定时重建客户端。

        角色可为别名或长键。写入 settings.json 的 modelGroups（该角色若有
        旧值转成对象覆盖 model）；然后若改的是本 agent 绑定角色则重建
        self.llm，否则仅清掉该角色的惰性缓存。
        """
        from .config import OpenXConfig

        role_key = _model_groups.canonical_role(role)
        if role_key is None:
            return False
        raw = OpenXConfig.load_model_groups_raw()
        group_name = self._group_name or self.config.active_group_name()
        if group_name not in raw:
            return False  # 内存合成组（无 settings 组）不可持久化
        group_raw = dict(raw.get(group_name) or {})
        # 保留既有简写/对象形态：原字符串简写仍写字符串，原对象/缺席写对象
        if isinstance(group_raw.get(role_key), str):
            group_raw[role_key] = model
        else:
            group_raw[role_key] = {"model": model}
        raw[group_name] = group_raw
        OpenXConfig.save_model_groups(raw)
        # 更新内存投影（main 角色的 config.model）
        if role_key == _model_groups.MAIN_ROLE:
            self.config.model = model
        self._drop_role_clients()
        if role_key == _model_groups.MAIN_ROLE or role_key == self._bind_role:
            return self._rebuild_llm()
        return True

    def set_role_cred(self, role: str, field: str, value: str) -> bool:
        """持久化 (当前组, 角色) 的连接覆盖（apiKey/apiBase）；空值=清除覆盖。

        ``field`` ∈ {"api_key","api_base"} → 落盘键 "apiKey"/"apiBase"。角色
        尚无显式对象（走 main 回落）时，先落一条带 main 当前 model 的对象，
        再挂覆盖——覆盖值以解析层 role > group 优先级生效。改 main/绑定角色
        会重建客户端；非绑定角色只清该角色缓存，下次 client_for 按新覆盖重建。
        """
        from .config import OpenXConfig

        if field not in ("api_key", "api_base"):
            return False
        role_key = _model_groups.canonical_role(role)
        if role_key is None:
            return False
        raw = OpenXConfig.load_model_groups_raw()
        group_name = self._group_name or self.config.active_group_name()
        if group_name not in raw:
            return False
        group_raw = dict(raw.get(group_name) or {})
        existing = group_raw.get(role_key)
        if isinstance(existing, dict):
            entry = dict(existing)
        elif isinstance(existing, str):
            entry = {"model": existing}
        else:
            entry = {}
        if "model" not in entry:
            main_raw = group_raw.get(_model_groups.MAIN_ROLE)
            if isinstance(main_raw, dict):
                entry["model"] = str(main_raw.get("model") or "")
            elif isinstance(main_raw, str):
                entry["model"] = main_raw
        disk_key = "apiKey" if field == "api_key" else "apiBase"
        if value:
            entry[disk_key] = value
        else:
            entry.pop(disk_key, None)
        group_raw[role_key] = entry
        raw[group_name] = group_raw
        OpenXConfig.save_model_groups(raw)
        self._drop_role_clients()
        if role_key == _model_groups.MAIN_ROLE or role_key == self._bind_role:
            return self._rebuild_llm()
        return True

    def sync_provider_config(self) -> None:
        """把 agent.config 的连接字段同步进已建实现（/model、/config 后调用）。

        内核 providers 工厂构造实现时**复制**了 settings（builtin/providers.py
        的 ``_create_*``），实现与 agent.config 解耦；运行期改配置后必须
        回写实现侧，否则下次请求仍用旧值。缺实现/异常一律静默。
        """
        impl = getattr(self, "llm", None)
        provider = getattr(impl, "_impl", None)
        icfg = getattr(provider, "config", None)
        if icfg is None:
            return
        for key in ("api_key", "api_base", "model", "temperature", "max_tokens"):
            try:
                setattr(icfg, key, getattr(self.config, key))
            except Exception:
                pass

    def _accumulate_tokens(self, response: dict[str, Any]) -> None:
        """累计本轮 token 用量（镜像 stream_run 从 StreamDone 取的统计）。

        优先采用服务端 ``usage``（prompt_tokens / completion_tokens，由
        ``LLMClient._parse_response`` / ``_stream_response`` 透出）；缺失时
        退回字符估算，保证 ``--no-stream`` 路径的 /cost 不再恒为 0。

        注意：本方法会**就地 pop 掉** ``usage`` 字段——它绝不能随 assistant
        消息进入历史，否则下一次请求会被 API 以非法字段拒绝。
        """
        usage = response.pop("usage", None) or {}
        self.total_input_tokens += usage.get("prompt_tokens") or 0
        self.total_cached_tokens += usage.get("cached_tokens") or 0
        # 插件 schema 随本次请求重发（工具 schema 就在 prompt 里）：把当时
        # ACTIVE 插件的 schemaTokens 之和记入插件累计——装配预算口径
        self.total_plugin_tokens += _active_plugin_schema_tokens()
        output = usage.get("completion_tokens") or 0
        if not output:
            # 无 usage（后端未返回或测试假 LLM）——按字符估算，镜像流式路径的近似计数
            content = response.get("content")
            text = content if isinstance(content, str) else ""
            args_len = sum(
                len(json.dumps(tc.get("function", {}).get("arguments", "")))
                for tc in response.get("tool_calls") or []
            )
            output = max(1, (len(text) + args_len) // 4)
        self.total_output_tokens += output

    def session_token_usage(self) -> dict[str, int]:
        """本次会话 token 用量四项汇总（/cost 与退出用量面板共用）。

        - input / output：服务端 usage 累计（缺失时输出按字符估算）；
        - cached：provider 报告的缓存命中累计（未报告恒 0）；
        - plugin：装配预算口径的插件 schema 累计——每轮 LLM 调用把当时
          ACTIVE 插件的 ``cost.schemaTokens`` 之和记一笔（内置恒 0 =
          基线不归插件），即"本次对话因插件而额外占用的上下文 token"
          估算。四项皆为累计值，随会话持久化。
        """
        return {
            "input": self.total_input_tokens,
            "output": self.total_output_tokens,
            "cached": self.total_cached_tokens,
            "plugin": self.total_plugin_tokens,
        }

    # ── 会话恢复（Phase 6）───────────────────────────────────────

    def load_session(self, meta: SessionMeta, messages: list[dict[str, Any]]) -> None:
        """从会话文件恢复上下文：历史消息 + token 用量 + todos + session_id。

        由 CLI 在 ``--continue`` / ``--resume`` 路径上、构造 agent 之后调用。
        ``todos`` 原地替换（TodoWriteTool 共享同一 list 对象）；hooks 的
        session_id 一并同步，保证钩子 payload 与恢复的会话一致。
        """
        self.history.clear()
        self.history.messages.extend(messages)
        self.total_input_tokens = meta.total_input_tokens
        self.total_output_tokens = meta.total_output_tokens
        self.total_cached_tokens = meta.total_cached_tokens
        self.total_plugin_tokens = meta.total_plugin_tokens
        self.todos[:] = meta.todos
        self.session_id = meta.session_id
        self.hooks.session_id = meta.session_id
        # 低提示行（Rich [dim]）；console 异常绝不影响恢复本身
        try:
            self.console._console.print(
                f"[dim]Resumed session {meta.session_id} — "
                f"{len(messages)} messages[/dim]"
            )
        except Exception:
            pass

    def _persist_turn(self, new_turn: list[dict[str, Any]]) -> None:
        """把本轮新消息与元数据增量写入会话文件（Phase 6）。

        只写**本轮新增**消息（压缩产生的摘要不重复落盘——恢复后历史若
        超限，下一轮的自动压缩会自然触发）。持久化是优化、不在回合
        关键路径上：任何异常全部吞掉，绝不打断对话。
        """
        store = self.session_store
        if store is None:
            return
        try:
            store.append_messages(new_turn)
            fields: dict[str, Any] = {
                "total_input_tokens": self.total_input_tokens,
                "total_output_tokens": self.total_output_tokens,
                "total_cached_tokens": self.total_cached_tokens,
                "total_plugin_tokens": self.total_plugin_tokens,
                "todos": self.todos,
            }
            # 首条用户消息回填（meta 里尚为空时），取纯文本前 80 字符
            if (
                not store.meta.first_user_message
                and new_turn
                and new_turn[0].get("role") == "user"
            ):
                fields["first_user_message"] = _plain_text_preview(
                    new_turn[0].get("content")
                )
            store.update_meta(**fields)
        except Exception:
            pass

    # ── 非流式运行 ───────────────────────────────────────────────

    async def run(self, user_message: str | list[dict[str, Any]]) -> str:
        """运行一轮对话（非流式）。

        基于 ``self.history.messages``：把历史 + 本轮用户消息一并发给 LLM，循环执行
        工具直到得到最终文本，最后把本轮消息并入历史。
        """
        state = AgentState()
        user_msg = {"role": "user", "content": user_message}
        # 消息序列 = system + 历史 + 本轮用户消息
        state.messages = [
            {"role": "system", "content": self._system_prompt},
            *self.history.messages,
            user_msg,
        ]
        new_turn: list[dict[str, Any]] = [user_msg]  # 本轮待并入历史的新消息
        # 多模回合（带图）走 modal 角色；整轮固定同一客户端——绝不中途换
        # provider（tool-call 序列对 provider 格式敏感）。
        turn_llm = self.llm if not _has_image(user_message) else self.client_for("modal")

        while state.tool_rounds < self.config.max_tool_rounds:
            response = await turn_llm.chat(
                messages=state.messages,
                tools=self.tool_schemas,
                stream=self.config.stream,
            )
            # 累计 token 用量（并就地剥离 usage 字段，避免其进入历史消息）
            self._accumulate_tokens(response)

            # 模型请求调用工具：串行准备（解析/校验/权限询问安全串行），
            # 再 asyncio.gather 并行执行——gather 保持参数顺序，tool 结果
            # 消息按原 tool_call 顺序追加，OpenAI 消息序列依然合法。
            if response.get("tool_calls"):
                state.messages.append(response)
                new_turn.append(response)

                prepared = []
                for tc in response["tool_calls"]:          # SERIAL: prompts safe
                    fn = tc["function"]
                    tool = self.tools.get(fn["name"])
                    prepared.append(await self.tool_executor.prepare(
                        fn["name"], tool, fn["arguments"], tc.get("id", ""),
                    ))

                results = await asyncio.gather(*(
                    self.tool_executor.execute_prepared(pc) for pc in prepared
                ))

                for pc, tool_result in zip(prepared, results):  # ORIGINAL order
                    msg = {
                        "role": "tool",
                        "tool_call_id": pc.tc_id,
                        "content": tool_result.to_message(),
                    }
                    state.messages.append(msg)
                    new_turn.append(msg)

                state.tool_rounds += 1
                # 结构化输出已捕获 → 立即结束本轮：structured_output 已
                # 校验并落库结果，继续循环只会产出被丢弃的废话
                if self._structured_result is not _UNSET:
                    payload = json.dumps(
                        self._structured_result, ensure_ascii=False
                    )
                    new_turn.append({"role": "assistant", "content": payload})
                    self.history.add(new_turn)
                    self._persist_turn(new_turn)
                    self.last_tool_rounds = state.tool_rounds
                    return payload
                continue

            # 无工具调用 —— 最终回复
            content = response.get("content") or ""
            final_msg = {"role": "assistant", "content": content}
            new_turn.append(final_msg)
            self.history.add(new_turn)
            # 会话持久化（Phase 6）：只写本轮新增消息 + 元数据增量；失败静默
            self._persist_turn(new_turn)
            # 逼近上限就自动压缩（失败静默，绝不打断回合）
            await self._maybe_auto_compact()
            # Stop 钩子（v1 仅警告；失败静默）
            await self._fire_stop_hook("end_turn")
            self.last_tool_rounds = state.tool_rounds
            return content

        await self._fire_stop_hook("max_rounds")
        self.last_tool_rounds = state.tool_rounds
        return "Reached maximum tool call rounds without a final response."

    # ── 流式运行（REPL 主路径）───────────────────────────────────

    async def stream_run(
        self, user_message: str | list[dict[str, Any]]
    ) -> AsyncIterator[str]:
        """流式运行一轮对话，逐 token yield 文本。

        - 文本 token 随到随 yield（打字机效果）；
        - 工具调用以紧凑指示行内显示；
        - 本轮消息最终并入 ``self.history.messages``，实现跨轮记忆；
        - 生成器耗尽即表示本轮完成，无需哨兵值。
        """
        state = AgentState()
        user_msg = {"role": "user", "content": user_message}
        state.messages = [
            {"role": "system", "content": self._system_prompt},
            *self.history.messages,
            user_msg,
        ]
        new_turn: list[dict[str, Any]] = [user_msg]
        # 多模回合（带图）走 modal 角色；整轮固定同一客户端——绝不中途换
        # provider（tool-call 序列对 provider 格式敏感）。
        turn_llm = self.llm if not _has_image(user_message) else self.client_for("modal")

        while state.tool_rounds < self.config.max_tool_rounds:
            done: StreamDone | None = None

            async for event in turn_llm.stream_chat(
                messages=state.messages,
                tools=self.tool_schemas,
            ):
                if isinstance(event, StreamDone):
                    done = event
                else:
                    yield event  # 文本 token → 打字机

            if done is None:  # 理论上不会发生，兜底
                return

            # 累计 token 用量
            self.total_output_tokens += done.token_count
            self.total_input_tokens += done.input_tokens
            self.total_cached_tokens += done.cached_tokens
            # 插件 schema 随本次流请求重发（工具 schema 就在 prompt 里）：
            # 把当时 ACTIVE 插件的 schemaTokens 之和记入插件累计
            self.total_plugin_tokens += _active_plugin_schema_tokens()

            response = done.response

            # ── 工具往返：串行准备（权限弹窗安全串行）→ 并行执行 ────
            if response.get("tool_calls"):
                state.messages.append(response)
                new_turn.append(response)

                prepared = []
                for tc in response["tool_calls"]:          # SERIAL: prompts safe
                    fn = tc["function"]
                    # 结构化事件：展示格式由消费方决定（REPL 渲染暗色
                    # 指示行，headless stream-json 序列化为 tool_use 事件）
                    yield ToolStartEvent(
                        name=fn["name"], arguments=fn.get("arguments", "")
                    )
                    tool = self.tools.get(fn["name"])
                    prepared.append(await self.tool_executor.prepare(
                        fn["name"], tool, fn["arguments"], tc.get("id", ""),
                    ))

                results = await asyncio.gather(*(
                    self.tool_executor.execute_prepared(pc) for pc in prepared
                ))

                for pc, tool_result in zip(prepared, results):  # ORIGINAL order
                    result_text = tool_result.to_message()
                    yield ToolResultEvent(
                        name=pc.tool_name,
                        # 完整消息（output + Error 行 + 截断注记）：失败
                        # 命令的 stdout/stderr 正文不再丢失（Claude Code
                        # 对齐：输出 + 退出码同屏）。事件仅用于展示——
                        # 模型历史走下方 state.messages 的 result_text。
                        output=result_text,
                        is_error=bool(tool_result.error),
                    )

                    msg = {
                        "role": "tool",
                        "tool_call_id": pc.tc_id,
                        "content": result_text,
                    }
                    state.messages.append(msg)
                    new_turn.append(msg)

                state.tool_rounds += 1
                # 结构化输出已捕获 → 立即收尾（同 run() 语义）：结果不
                # 经文本流——调用方从 structured_result 属性读取对象
                if self._structured_result is not _UNSET:
                    payload = json.dumps(
                        self._structured_result, ensure_ascii=False
                    )
                    new_turn.append({"role": "assistant", "content": payload})
                    self.history.add(new_turn)
                    self._persist_turn(new_turn)
                    self.last_tool_rounds = state.tool_rounds
                    return
                continue

            # ── 最终文本回复 —— 本轮结束 ──────────────────────
            final_msg = {"role": "assistant", "content": response.get("content") or ""}
            new_turn.append(final_msg)
            self.history.add(new_turn)
            # 会话持久化（Phase 6）：只写本轮新增消息 + 元数据增量；失败静默
            self._persist_turn(new_turn)
            # 逼近上限就自动压缩；通知行只用白名单内的 [dim] 标签
            # （StreamingService._RICH_TAG 只剥离这些标签）
            if await self._maybe_auto_compact():
                yield "\n\n[dim]● Compacting conversation…[/dim]\n"
            # Stop 钩子：生成器里必须 await 在收尾 yield 之后、return 之前
            # （v1 仅警告；失败静默，绝不打断回合）
            await self._fire_stop_hook("end_turn")
            self.last_tool_rounds = state.tool_rounds
            return

        yield "\n\n[dim]Max tool rounds reached[/dim]"
        await self._fire_stop_hook("max_rounds")
        self.last_tool_rounds = state.tool_rounds

    # ── 项目探索（委托给 services/exploration）───────────────────

    async def explore_project(self) -> ProjectInfo:
        """扫描工作区，返回结构化的项目信息供 UI 展示。"""
        return await _explore_project(
            self.workspace,
            self.tools["git_status"],
            self.tools["git_log"],
        )


def _active_plugin_schema_tokens() -> int:
    """当前 ACTIVE 插件的 schemaTokens 之和（单次 LLM 调用的插件开销估算）。

    装配预算口径：插件工具的 schema 每次请求都随 prompt 重发，schemaTokens
    是插件声明的上下文占用。内置插件恒 0（base bundle 的 schema 是基线，
    不归插件）；内核未就绪或任何异常一律按 0 兜底——统计绝不能被插件装载
    拖垮（展示前仍可调用方兜底）。
    """
    try:
        from .kernel import get_kernel
        from .kernel.inventory import PHASE_ACTIVE

        total = 0
        for p in get_kernel().list_plugins():
            if p.get("phase") == PHASE_ACTIVE:
                total += int((p.get("cost") or {}).get("schemaTokens", 0) or 0)
        return total
    except Exception:
        return 0


def _has_image(user_message: Any) -> bool:
    """用户消息（str 或多模 content parts 列表）是否含图（modal 角色判定）。"""
    parts = user_message if isinstance(user_message, list) else []
    return any(
        isinstance(p, dict) and p.get("type") == "image_url" for p in parts
    )


def _plain_text_preview(content: Any, limit: int = 80) -> str:
    """从用户消息内容（字符串或多模态 part 列表）提取纯文本前 *limit* 字符。

    供会话 meta 的 ``first_user_message`` 字段使用——列表页预览只需要文本。
    """
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    else:
        text = str(content or "")
    return text.strip()[:limit]


if __name__ == "__main__":
    import tempfile

    # build_user_content：纯静态方法，无依赖
    assert OpenXAgent.build_user_content("hello") == "hello"
    _parts = OpenXAgent.build_user_content("look", images=["data:image/png;base64,AAAA"])
    assert _parts[0] == {"type": "text", "text": "look"}
    assert _parts[1]["type"] == "image_url" and _parts[1]["image_url"]["detail"] == "auto"
    print(f"build_user_content: text→str ✓, with image→{len(_parts)} content parts ✓")

    # 尝试用临时 workspace 构造 agent 并列出工具（纯本地、不联网；失败则退化）
    try:
        import openx.memory as _mem
        with tempfile.TemporaryDirectory() as _td:
            _old, _mem._MEMORY_DIR = _mem._MEMORY_DIR, Path(_td) / "memory"
            try:  # 把 MemoryStore() 的默认目录临时改到 tmp，避免写真实 ~/.openx
                _agent = OpenXAgent(OpenXConfig.load(workspace=_td))
                print(f"OpenXAgent tools({len(_agent.tools)}): {', '.join(sorted(_agent.tools))}")
            finally:
                _mem._MEMORY_DIR = _old
    except Exception as _e:
        print(f"skip agent construction (self-test still passes): {type(_e).__name__}: {_e}")

    print("openx/agent.py OK ✓")
