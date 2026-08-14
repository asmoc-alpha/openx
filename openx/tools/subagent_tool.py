"""TaskTool — delegate a self-contained task to a sub-agent (Phase 8).

``task`` 工具：主代理把一项自包含任务委派给子代理。子代理拥有独立的
上下文与（按规格裁剪的）工具集，跑完整个 agent 循环后把**最终文本**
作为返回值交回主代理。

设计要点
========
- 启动本身权限为 ``ALLOW``：委派无副作用，子代理内部的写入类工具各自
  经共享 console 走正常权限弹窗。
- :func:`build_child_agent` 是**模块级函数**——测试 monkeypatch 它即可
  注入假子代理，无需触碰真实 LLM。
- 子代理流**捕获进内存缓冲**（``child.stream_run`` 的事件逐个喂入父
  agent 的 fleet 视图）：父的 Rich Live 显示仍然活跃，子代理**绝不
  写终端**——缓冲消费是零终端 I/O，"不抢占终端"的约束指终端争用，
  本路径不破。输入框下方的状态层（deck）读 fleet 快照展示子代理运行
  态，Ctrl-O 切到其详情视图（v0.4.0）。
- **prompt 锁传播**：子 executor 复用父 executor 的 ``_prompt_lock``
  ——并行 task 子代理（asyncio.gather 执行）的权限弹窗串行在同一把
  锁上，raw-mode stdin 绝不重叠（镜像 workflow 引擎的既有做法）。
- 弹窗回调传播：把父 executor 当前的 ``on_prompt_start``/``on_prompt_end``
  原样拷给子 executor——子代理弹窗时同样暂停父级 InputCapture
  （Phase 3 bug-10 契约，否则 raw 模式争抢 termios）。
- 子代理与父共享 ``TaskRegistry``，后台任务的退出清理由顶层统一负责，
  本工具不做任何清理。
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
import dataclasses
import json
from typing import TYPE_CHECKING, Any

from .base import Tool, ToolResult
from ..core.subagent import BUILTIN_SUBAGENTS, SubagentSpec
from ..permissions import Permission, PermissionLevel

if TYPE_CHECKING:
    from ..agent import OpenXAgent


class TaskTool(Tool):
    """委派一项自包含任务给子代理，返回其最终报告。"""

    name = "task"
    description = (
        "Delegate a self-contained task to a sub-agent that runs with its own "
        "context and tools, returning a final report. Use for broad multi-file "
        "searches or independent implementation chunks. Params: description "
        "(3-5 word label), prompt (the full task), subagent_type (optional, "
        "default general-purpose; 'explore' is read-only), schema (optional "
        "JSON Schema — the sub-agent then returns via structured_output and "
        "this tool yields the validated object as JSON instead of free text)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "A short 3-5 word label for the task.",
            },
            "prompt": {
                "type": "string",
                "description": (
                    "The full, self-contained task for the sub-agent: what to "
                    "do plus all context it needs (it cannot see this "
                    "conversation)."
                ),
            },
            "subagent_type": {
                "type": "string",
                "description": (
                    "Which sub-agent type to use. Default: 'general-purpose'. "
                    "'explore' is a read-only search agent."
                ),
            },
            "schema": {
                "type": "object",
                "description": (
                    "Optional JSON Schema for the sub-agent's final result. "
                    "When given, the sub-agent MUST deliver its answer via the "
                    "structured_output tool and this tool returns the "
                    "validated object as a JSON string."
                ),
            },
        },
        "required": ["description", "prompt"],
    }

    def __init__(self, agent: Any, specs: dict[str, SubagentSpec]) -> None:
        # agent = 父 agent，惰性解引用（构造期只用 specs 校验类型名）
        self._agent = agent
        self._specs = specs

    @property
    def permission(self) -> Permission:
        # 启动本身无副作用：免询问。子代理的写入类工具各自经共享 console
        # 走正常权限流程。
        return Permission.allow()

    async def execute(
        self,
        description: str = "",
        prompt: str = "",
        subagent_type: str = "general-purpose",
        schema: dict | None = None,
        **_: Any,
    ) -> ToolResult:
        spec = self._specs.get(subagent_type)
        if spec is None:
            return ToolResult(
                error=f"Unknown subagent_type '{subagent_type}'. "
                      f"Available: {sorted(self._specs)}"
            )
        # schema 前置校验：必须是 JSON 对象形式的 schema，否则子代理将
        # 拿到一份无法履行的契约——早失败优于空跑一整轮
        if schema is not None and not isinstance(schema, dict):
            return ToolResult(
                error=f"'schema' must be a JSON Schema object, "
                      f"got {type(schema).__name__}"
            )
        try:
            # 模块级函数引用——测试 monkeypatch 本模块的 build_child_agent
            child = build_child_agent(
                self._agent, spec, structured_schema=schema
            )
            # 弹窗回调传播：子代理权限弹窗必须暂停父级 InputCapture
            # （Phase 3 bug-10 契约）——拷贝父 executor 的**当前**回调值。
            parent_executor = self._agent.tool_executor
            child.tool_executor.on_prompt_start = parent_executor.on_prompt_start
            child.tool_executor.on_prompt_end = parent_executor.on_prompt_end
            # prompt 锁传播（镜像 workflow 引擎）：并行 task 子代理的
            # 权限弹窗串行在父的同一把锁上——raw-mode stdin 绝不重叠
            child.tool_executor._prompt_lock = parent_executor._prompt_lock
            # 登记 fleet 视图并流式捕获子代理事件（零终端写）。
            # 未知类型/坏 schema 的早退发生在此前 → 绝无幽灵行。
            fleet = getattr(self._agent, "fleet", None)
            view = (
                fleet.register(description or subagent_type, subagent_type)
                if fleet is not None else None
            )
            errored = False
            try:
                async for event in child.stream_run(prompt):
                    if view is not None:
                        view.feed(event)
            except asyncio.CancelledError:
                # Esc 打断经取消传播：子代理视图标 error 后原样上抛
                errored = True
                raise
            except Exception:
                errored = True
                raise
            finally:
                if fleet is not None and view is not None:
                    fleet.complete(view, is_error=errored)  # 幂等
        except Exception as e:
            return ToolResult(error=f"Subagent failed: {e}")
        # 结构化契约：带 schema 时只认 structured_output 捕获的结果——
        # 子代理跑完却没调用它（如耗尽轮数）视为失败，绝不把自由文本
        # 冒充结构化返回值
        if schema is not None:
            if not child.has_structured_result():
                return ToolResult(
                    error=f"Subagent '{subagent_type}' finished without "
                          f"producing structured output (never called "
                          f"structured_output or ran out of rounds)"
                )
            return ToolResult(output=json.dumps(
                child.structured_result, ensure_ascii=False
            ))
        final = _child_final_text(child)
        return ToolResult(
            output=f"Subagent '{subagent_type}' finished: {description}\n\n{final}"
        )


def _child_final_text(child: "OpenXAgent") -> str:
    """从历史重建子代理最终文本（与 run() 返回值同源）。

    绝不拼接 token 流：流里含压缩通知 / 最大轮次哨兵行，run() 从不
    返回它们。读历史末条 assistant（先例：``single_shot._run_stream_json``
    从 reversed history 重建 result）；末条非 assistant（最大轮数出口）
    回退 run() 原串。压缩保留最近数轮，最终 assistant 消息不会丢。
    """
    msgs = child.history.messages
    if msgs and msgs[-1].get("role") == "assistant":
        return str(msgs[-1].get("content") or "")
    return "Reached maximum tool call rounds without a final response."


def build_child_agent(
    parent: "OpenXAgent",
    spec: SubagentSpec,
    structured_schema: dict | None = None,
) -> "OpenXAgent":
    """按规格从父 agent 派生子 agent。

    config 经 ``dataclasses.replace`` 浅拷贝，但列表字段
    （``allowed_commands`` / ``dangerous_commands``）**深拷贝**——子代理
    对列表的就地修改绝不能回流污染父配置。``spec.model`` 非空时覆盖
    ``config.model``。console / rules / hooks / tasks 全部与父共享
    （在 ``OpenXAgent`` 的子模式中接线）。``structured_schema`` 非 None
    时子代理携带结构化输出契约（structured_output 工具 + 系统提示）。

    延迟导入 ``OpenXAgent``：``agent`` 模块反过来导入本模块注册工具，
    顶层导入会构成循环。
    """
    from ..agent import OpenXAgent

    overrides: dict[str, Any] = {
        "allowed_commands": list(parent.config.allowed_commands),
        "dangerous_commands": list(parent.config.dangerous_commands),
    }
    if spec.model:
        overrides["model"] = spec.model
    config = dataclasses.replace(parent.config, **overrides)
    return OpenXAgent(
        config,
        console=parent.console,
        parent=parent,
        tool_allowlist=spec.tools,
        subagent_extra=spec.system_prompt_extra,
        structured_schema=structured_schema,
    )


if __name__ == "__main__":
    # 独立调试：绝不构造真实 OpenXAgent —— 用假父/假子验证工具自身逻辑
    import asyncio
    from types import SimpleNamespace
    from ..core.fleet import FleetMonitor

    class _FakeExecutor:
        on_prompt_start = None
        on_prompt_end = None

    _FakeExecutor._prompt_lock = asyncio.Lock()  # 3.10+ 无需 running loop

    class _FakeParent:
        tool_executor = _FakeExecutor()
        fleet = FleetMonitor()

    class _FakeChild:
        def __init__(self):
            self.tool_executor = _FakeExecutor()
            self.history = SimpleNamespace(messages=[])

        async def stream_run(self, prompt):
            text = f"child-result for: {prompt}"
            for tok in text.split():
                yield tok + " "
            self.history.messages.append(
                {"role": "assistant", "content": text}
            )

    _specs = {s.name: s for s in BUILTIN_SUBAGENTS}
    _tool = TaskTool(_FakeParent(), _specs)
    assert _tool.permission.level is PermissionLevel.ALLOW
    assert _tool.to_openai_schema()["function"]["name"] == "task"

    async def _self_check():
        # 未知类型 → 错误列出可用规格（注册之前早退 → 无幽灵 fleet 行）
        bad = await _tool.execute(description="x", prompt="y", subagent_type="nope")
        assert not bad.success and "Unknown subagent_type 'nope'" in bad.error
        assert "general-purpose" in bad.error and "explore" in bad.error
        assert _tool._agent.fleet.snapshot() == []

        # 正常路径：临时替换模块级 build_child_agent（测试同款手法）
        global build_child_agent
        _real = build_child_agent
        _child = _FakeChild()
        build_child_agent = lambda parent, spec, structured_schema=None: _child
        try:
            _tool._agent.tool_executor.on_prompt_start = lambda: None
            ok = await _tool.execute(description="find X", prompt="locate X")
        finally:
            build_child_agent = _real
        assert ok.success
        assert "Subagent 'general-purpose' finished: find X" in ok.output
        # 终值经 history 重建（与 run() 同源，不经 token 拼接）
        assert "child-result for: locate X" in ok.output
        # 弹窗回调 + prompt 锁已从父 executor 传播到子 executor
        assert (
            _child.tool_executor.on_prompt_start
            is _tool._agent.tool_executor.on_prompt_start
        )
        assert (
            _child.tool_executor._prompt_lock
            is _tool._agent.tool_executor._prompt_lock
        )
        # fleet 视图已登记、喂入并完结（token 无 \n → 全在 pending）
        _views = _tool._agent.fleet.snapshot()
        assert len(_views) == 1 and _views[0]["status"] == "done"
        assert _views[0]["label"] == "find X"
        assert _views[0]["pending"] == "child-result for: locate X "

    asyncio.run(_self_check())
    print("openx/tools/subagent_tool.py OK ✓")
