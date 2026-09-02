"""WorkflowTool — run a Python workflow script that orchestrates sub-agents (Phase 10).

``workflow`` 工具（Phase 10）
=============================
模型侧入口：给定内联 ``script`` 或已保存的 ``name``
（``<workspace>/.openx/workflows/<name>.py``），交由
:class:`~openx.orchestration.workflow.WorkflowEngine` 跑完，把 ``main`` 的返回值
JSON 序列化后连同统计脚标一起回喂模型。

设计要点
========
- 权限为 ``ASK``：工作流脚本**无沙箱**执行，拥有与 shell 同级的完全
  本地权限——执行前必须经用户确认（``validate_args`` 在权限弹窗**之前**
  就拦下非法参数组合，避免用户白批一次注定失败的调用）。
- 仅顶层 agent 持有本工具（``agent._build_tools`` 的 ``_parent is None``
  守卫）：子代理不得运行工作流——禁套娃，与 ``task`` 同级。
- 引擎的任何 :class:`WorkflowError` 都落成 ``ToolResult(error=...)``，
  让模型有机会自我纠正；绝不上抛中断对话轮。
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

import json
from typing import TYPE_CHECKING, Any, Optional

from .base import Tool, ToolResult
from ..orchestration.workflow import WorkflowEngine, WorkflowError, load_workflow
from ..permissions import Permission, PermissionLevel

if TYPE_CHECKING:
    from ..agent import OpenXAgent


class WorkflowTool(Tool):
    """运行一个 Python 工作流脚本，确定性地编排多个子代理。"""

    name = "workflow"
    description = """Run a Python workflow script that orchestrates multiple sub-agents deterministically (fan-out searches, parallel reviews, staged pipelines). The script defines `meta = {...}` and `async def main(agent, parallel, pipeline, phase, log, args)`:
- await agent(prompt, label=None, phase=None, subagent_type="general-purpose", schema=None) → sub-agent's final text, or the validated Python object when `schema` (a JSON Schema) is given (None on failure)
- await parallel([lambda: agent(...), ...]) → barrier, results in order, failed thunk → None
- await pipeline(items, stage1, stage2, ...) → each item through all stages independently (NO barrier); stage(prev_result, original_item, index)
- phase(title) / log(message) → progress
Scripts run unsandboxed with full local access — same trust level as shell. Provide `script` (inline source) or `name` (runs .openx/workflows/<name>.py); optional `args` (any JSON) is passed to main."""
    parameters = {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": "Inline Python workflow source",
            },
            "name": {
                "type": "string",
                "description": "Saved workflow name under .openx/workflows/",
            },
            "args": {
                "description": "Value exposed to the script as `args` (any JSON)",
            },
        },
    }

    def __init__(self, agent: "OpenXAgent") -> None:
        # agent = 父 agent（工作流子代理都作为它的 child 派生），惰性解引用
        self._agent = agent

    @property
    def permission(self) -> Permission:
        # 脚本无沙箱、与 shell 同级信任：必须询问。
        return Permission.ask("Execute a workflow script (arbitrary Python)")

    def validate_args(
        self,
        script: Optional[str] = None,
        name: Optional[str] = None,
        args: Any = None,
        **_: Any,
    ) -> Optional[str]:
        # 在 ASK 权限弹窗之前就拦下非法组合（prepare 阶段先于询问执行）
        if script and name:
            return "Provide either 'script' or 'name', not both."
        if not script and not name:
            return (
                "Provide one of: 'script' (inline workflow source) or "
                "'name' (saved workflow under .openx/workflows/)."
            )
        return None

    async def execute(
        self,
        script: Optional[str] = None,
        name: Optional[str] = None,
        args: Any = None,
        **_: Any,
    ) -> ToolResult:
        validation_error = self.validate_args(script=script, name=name)
        if validation_error:
            return ToolResult(error=validation_error)

        if name:
            try:
                source, path = load_workflow(str(self._agent.workspace), name)
            except WorkflowError as e:
                return ToolResult(error=str(e))
            script_name = str(path)
        else:
            source, script_name = script, "<inline>"

        engine = WorkflowEngine(self._agent)
        try:
            result, stats = await engine.run(source, args=args, script_name=script_name)
        except WorkflowError as e:
            return ToolResult(error=str(e))

        # main 的返回值 JSON 化（default=str 兜底不可序列化对象），
        # 再失败就 repr；末尾附统计脚标供模型与用户核对开销。
        try:
            body = json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            body = repr(result)
        footer = (
            f"\n[workflow: {stats.agents_run} agents, {stats.agents_failed} failed, "
            f"{stats.total_output_tokens} tokens, {stats.elapsed_seconds:.1f}s]"
        )
        return ToolResult(output=body + footer)


if __name__ == "__main__":
    # 独立调试：绝不构造真实 OpenXAgent —— 用假父验证工具自身逻辑
    import asyncio

    class _FakeParent:
        workspace = "."

    _tool = WorkflowTool(_FakeParent())
    assert _tool.to_openai_schema()["function"]["name"] == "workflow"
    assert _tool.permission.level is PermissionLevel.ASK

    # 参数校验：script/name 恰好一个
    assert _tool.validate_args(script="x = 1") is None
    assert _tool.validate_args(name="review") is None
    assert _tool.validate_args()                      # 两者皆无 → 错误消息
    assert _tool.validate_args(script="x", name="y")  # 两者皆有 → 错误消息

    async def _self_check():
        neither = await _tool.execute()
        assert not neither.success and "script" in neither.error
        both = await _tool.execute(script="x = 1", name="y")
        assert not both.success and "not both" in both.error
        missing = await _tool.execute(name="ghost-no-such-workflow")
        assert not missing.success and "not found" in missing.error

    asyncio.run(_self_check())
    print("openx/tools/workflow_tool.py OK ✓")
