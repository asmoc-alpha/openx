"""Plan-mode 工具 —— 进入（模型自判）与退出（用户审批）两个入口。

一对工具，对应计划模式的两端：

``EnterPlanModeTool``（入口，模型自判）
    任务复杂时**模型第一个动作**调用它：不再弹"选哪个模式"的询问，直接切进
    plan mode 并打一行提示。复杂任务的确认点因此从"先选模式"前移到"审批计划
    本身"——用户看到的是具体方案，而不是模式名。
``ExitPlanModeTool``（出口，用户审批）
    1. agent 在 plan mode 下只用只读工具探索代码库；
    2. 探索完成后调用 ``exit_plan_mode(plan=...)``，把完整实现计划以 Markdown
       渲染给用户；
    3. 用户批准 → 退出 plan mode 并开启 auto-approve（批准后自动执行）；
       用户拒绝 → 返回**非错误**输出，让模型根据反馈修订计划后再次调用。

设计要点
========
- 工具持有 ``agent`` 与 ``console`` 引用（构造注入，同 AskUserTool）：
  agent 用于 ``set_mode`` / 切换 auto-approve，console 用于渲染与审批弹窗；
- 权限均为 ``ALLOW``：进入/提交计划本身无副作用，真正的闸门是审批弹窗
  （``console.confirm_plan``）——所以进入计划模式**不需要**额外的确认框；
- 写入类工具在 plan mode 下被 schema 过滤（模型看不见）与 kernel guard
  硬拦截双重防线禁用，``exit_plan_mode`` 是唯一的"出口"；
- ``enter_plan_mode`` 只在 manual/auto 且 ``agent.plan_entry_enabled`` 时可
  见：非交互运行（single-shot / headless）没人能审批计划，那里直接不给模型
  这个工具（见 ``OpenXAgent.plan_entry_enabled``）。
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
from rich.markup import escape

from .base import Tool, ToolResult
from .console_dialog import confirm_plan
from ..permissions import Permission


class EnterPlanModeTool(Tool):
    """任务复杂 → 直接切进计划模式（不弹模式选择；计划本身是审批点）。"""

    name = "enter_plan_mode"
    description = (
        "Switch to plan mode because the task is COMPLEX, then explore read-only "
        "and submit a plan for approval via exit_plan_mode. Call this as your "
        "FIRST action (before any write, and before choose_mode) when the task "
        "needs coordinated changes across several files, a new feature, a "
        "refactor, a migration, or when the approach itself needs the user's "
        "sign-off before anything is touched. Do NOT call it for a small, "
        "obvious, single-file change — just do those. The user is not asked to "
        "pick a mode: the plan you submit is what gets approved. Pass a "
        "one-line `reason` describing why the task is complex."
    )
    parameters = {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": (
                    "One line, shown to the user: why this task is complex "
                    "enough to deserve a plan first (e.g. 'touches 4 files "
                    "across 2 subsystems')."
                ),
            },
        },
        "required": ["reason"],
    }

    def __init__(self, agent: Any, console: Any):
        # 持有 agent（set_mode 切进 plan）与 console（打一行可见提示）
        self._agent = agent
        self._console = console

    @property
    def permission(self) -> Permission:
        # 进入计划模式无副作用；真正的审批在出口（exit_plan_mode）
        return Permission.allow()

    async def execute(self, reason: str = "") -> ToolResult:
        # 纵深防御三条：schema 已按模式/能力位过滤，执行器与 kernel guard
        # 还会再拦（见 guard 的同名条款）。这里保证"就算被直接点名"也安全。
        if self._agent.mode == "plan":
            return ToolResult(
                output="Already in plan mode. Explore read-only and call "
                       "exit_plan_mode with the complete plan."
            )
        if not getattr(self._agent, "plan_entry_enabled", True):
            return ToolResult(
                output="Plan mode is unavailable in this non-interactive run "
                       "(nobody can approve a plan). Proceed in the current "
                       "mode and make the changes directly."
            )

        # 一行可见提示：用户在屏幕上看到"为什么突然开始探索了"。走 raw.print
        # 与 choose_mode/exit_plan_mode 同一路径（流式期经 funnel 打在 Live
        # 区上方；serve 下 raw 是内存 buffer，浏览器由状态层呈现模式）。
        detail = f"：{escape(reason)}" if reason else ""
        self._console.raw.print(
            "[bold yellow]▸ 计划模式[/bold yellow]"
            f"[dim] 已进入（复杂任务）{detail}"
            "  ·  先只读探索，再提交计划待你审批[/dim]"
        )
        self._agent.set_mode("plan")
        return ToolResult(
            output="Plan mode is now ACTIVE: write and execute tools "
                   "(write_file, edit_file, shell) are hidden and hard-blocked. "
                   "Explore with read-only tools only, then call exit_plan_mode "
                   "with the complete plan — files to change, step-by-step "
                   "approach, and how to verify."
        )


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

        # 2. 交互式审批弹窗（async 优先：serve 走 bridge 应答通道）
        approved = await confirm_plan(self._console, plan)

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
        def __init__(self):
            self.lines: list[str] = []

        def print(self, *args, **kwargs):
            # 渲染计划 / 进入提示：自检只关心不抛异常 + 提示确实打了
            self.lines.append(" ".join(str(a) for a in args))

    class _FakeConsole:
        def __init__(self, approve: bool = True):
            self.raw = _FakeRaw()
            self.mode = "plan"
            self._approve = approve

        def confirm_plan(self) -> bool:
            return self._approve

    async def _enter_self_check():
        # manual → 切进 plan，并打出一行含 reason 的提示
        agent, console = _FakeAgent(), _FakeConsole()
        agent.mode = "manual"
        tool = EnterPlanModeTool(agent, console)
        assert tool.permission.level.value == "allow"
        r = await tool.execute(reason="改 4 个文件")
        assert r.success and "exit_plan_mode" in r.output, r.output
        assert agent.mode == "plan", agent.mode
        assert any("改 4 个文件" in line for line in console.raw.lines), console.raw.lines

        # auto → 同样生效（复杂任务的确认点前移到计划本身）
        agent2, console2 = _FakeAgent(), _FakeConsole()
        agent2.mode = "auto"
        r2 = await EnterPlanModeTool(agent2, console2).execute(reason="新功能")
        assert agent2.mode == "plan", agent2.mode
        assert console2.raw.lines

        # 已在 plan → 空操作，不再重复提示
        agent3, console3 = _FakeAgent(), _FakeConsole()
        r3 = await EnterPlanModeTool(agent3, console3).execute(reason="再来一遍")
        assert agent3.mode == "plan" and not console3.raw.lines, console3.raw.lines
        assert "Already in plan mode" in r3.output, r3.output

        # headless（无法审批计划）→ 留在原模式，明确告知模型别规划
        agent4, console4 = _FakeAgent(), _FakeConsole()
        agent4.mode = "manual"
        agent4.plan_entry_enabled = False
        r4 = await EnterPlanModeTool(agent4, console4).execute(reason="x")
        assert agent4.mode == "manual" and not console4.raw.lines
        assert "non-interactive" in r4.output, r4.output

    async def _self_check():
        await _enter_self_check()
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
