"""Plan-mode 工具 —— 退出计划模式的审批出口。

ExitPlanModeTool 是 Claude Code 风格 Plan 模式的审批入口：
1. agent 在 plan mode 下只用只读工具探索代码库；
2. 探索完成后调用 ``exit_plan_mode(plan=...)``，把完整实现计划以 Markdown
   渲染给用户；
3. 用户批准 → 退出 plan mode 并开启 auto-approve（批准后自动执行）；
   用户拒绝 → 返回**非错误**输出，让模型根据反馈修订计划后再次调用。

设计要点
========
- 工具持有 ``agent`` 与 ``console`` 引用（构造注入，同 AskUserTool）：
  agent 用于 ``set_plan_mode`` / 切换 auto-approve，console 用于渲染与审批弹窗；
- 权限为 ``ALLOW``：提交计划本身无副作用，审批交互由 ``console.confirm_plan`` 完成；
- 写入类工具在 plan mode 下被 schema 过滤（模型看不见）与 ToolExecutor
  闸门（硬拦截）双重防线禁用，本工具是唯一的"出口"。
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

from typing import Any

from rich.markdown import Markdown

from .base import Tool, ToolResult
from ..permissions import Permission


class ExitPlanModeTool(Tool):
    """提交实现计划并请求用户批准，批准后退出 plan mode。"""

    name = "exit_plan_mode"
    description = (
        "Present the complete implementation plan to the user for approval and "
        "exit plan mode. Call this ONLY after you have finished read-only "
        "exploration and know exactly what to change. Writing tools "
        "(write_file, edit_file, shell) remain disabled until the user "
        "approves the plan through this tool. Pass the full plan as markdown "
        "in `plan`."
    )
    parameters = {
        "type": "object",
        "properties": {
            "plan": {
                "type": "string",
                "description": (
                    "The complete implementation plan in markdown: files to "
                    "change, step-by-step approach, and how to verify."
                ),
            },
        },
        "required": ["plan"],
    }

    def __init__(self, agent: Any, console: Any):
        # 持有 agent（退出 plan mode / 切换 auto-approve）与 console（渲染 + 审批）
        self._agent = agent
        self._console = console

    @property
    def permission(self) -> Permission:
        # 提交计划本身无副作用；真正的审批由 console.confirm_plan 完成
        return Permission.allow()

    async def execute(self, plan: str) -> ToolResult:
        # 1. 渲染计划（Markdown，走 console.raw 的 Rich Console）
        self._console.raw.print(Markdown(plan))

        # 2. 交互式审批弹窗
        approved = self._console.confirm_plan()

        if approved:
            # 3. 批准 → 退出 plan mode（set_mode 统一同步 executor/console/
            #    schemas/prompt，并还原进入 plan 前的 auto_approve）；
            #    Claude-Code 式"批准后自动执行"：既然用户已批准整份计划，
            #    执行阶段不再逐个弹窗。
            self._agent.set_mode("auto")
            self._agent.tool_executor.auto_approve = True
            return ToolResult(
                output="Plan approved. Plan mode exited — you may now execute "
                       "the plan with auto-approval enabled."
            )

        # 4. 拒绝 → 非错误输出，让模型继续推理、修订计划后再次提交
        return ToolResult(
            output="User rejected the plan. Revise it based on their feedback "
                   "and call exit_plan_mode again."
        )


if __name__ == "__main__":
    # 独立调试：绝不真的弹窗 —— 用 duck-typed 假 agent/console 验证两条路径
    import asyncio

    class _FakeExecutor:
        auto_approve = False

    class _FakeAgent:
        mode = "plan"
        tool_executor = _FakeExecutor()

        @property
        def plan_mode(self):
            return self.mode == "plan"

        def set_mode(self, m: str):
            self.mode = m

    class _FakeRaw:
        def print(self, *args, **kwargs):
            pass  # 渲染计划：自检只关心不抛异常

    class _FakeConsole:
        raw = _FakeRaw()

        def __init__(self, approve: bool = True):
            self.mode = "plan"
            self._approve = approve

        def confirm_plan(self) -> bool:
            return self._approve

    async def _self_check():
        # 批准路径：退出 plan mode、开启 auto-approve、console 回到 auto
        agent, console = _FakeAgent(), _FakeConsole(approve=True)
        tool = ExitPlanModeTool(agent, console)
        assert tool.permission.level.value == "allow"
        r = await tool.execute(plan="# Plan\n- step 1")
        assert r.success and "approved" in r.output, r.output
        assert agent.plan_mode is False and agent.mode == "auto"
        assert agent.tool_executor.auto_approve is True

        # 拒绝路径：plan mode 保持，返回非错误输出提示修订
        agent2, console2 = _FakeAgent(), _FakeConsole(approve=False)
        tool2 = ExitPlanModeTool(agent2, console2)
        r2 = await tool2.execute(plan="# Plan")
        assert r2.success and "Revise" in r2.output, r2.output
        assert agent2.plan_mode is True

    asyncio.run(_self_check())
    print("openx/tools/plan_tools.py OK ✓")
