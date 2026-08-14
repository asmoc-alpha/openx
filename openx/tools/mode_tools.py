"""Mode 工具 —— manual 模式下让用户选择执行模式（Auto / Plan / 保持手动）。

ChooseModeTool 是三模式权限系统（manual/auto/plan）的"入口询问"：

1. openx 启动即 **manual** 模式——只读工具免确认，写入类工具逐项弹窗；
2. 模型判断用户任务需要修改文件/执行命令时，**第一个动作**调用
   ``choose_mode``，把 Auto / Plan / 保持手动三选项弹给用户；
3. 用户选定后工具把选择应用到 ``agent.set_mode``，结果文本告诉模型
   如何在新模式下继续（plan → 探索后 exit_plan_mode；auto → 正常流程）。

设计要点（镜像 ExitPlanModeTool 先例）
======================================
- 工具持有 ``agent`` 与 ``console`` 引用（构造注入）：agent 用于
  ``set_mode``，console 用于 ``ask_user_question`` 弹窗与摘要渲染；
- 权限为 ``ALLOW``：询问本身无副作用，真正的交互由弹窗完成；
- 仅 manual 模式可见（schema 过滤）且可用（ToolExecutor 第二道防线）；
- ``agent.mode_choice_offered`` 闩防止一次会话重复弹窗；"Other" 自由
  文本按安全默认处理——留在 manual。
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

from .base import Tool, ToolResult
from ..permissions import Permission


class ChooseModeTool(Tool):
    """manual 模式下询问用户选择 Auto / Plan / 保持手动，并应用选择。"""

    name = "choose_mode"
    description = (
        "Let the user pick the permission mode BEFORE you make file changes. "
        "Available only in manual mode; call it ONCE, as your first action, "
        "when the task requires writing files or running commands. The user "
        "chooses Auto (normal permission flow), Plan (read-only exploration "
        "then plan approval via exit_plan_mode), or staying Manual (confirm "
        "every change). Pass a one-line `summary` of the changes you need."
    )
    parameters = {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "One-line summary of the file changes / commands you "
                    "need to make; shown to the user above the choice."
                ),
            },
        },
        "required": [],
    }

    def __init__(self, agent: Any, console: Any):
        # 持有 agent（set_mode 切换）与 console（弹窗 + 渲染摘要）
        self._agent = agent
        self._console = console

    @property
    def permission(self) -> Permission:
        # 询问本身无副作用；真正的交互由 console.ask_user_question 完成
        return Permission.allow()

    async def execute(self, summary: str = "") -> ToolResult:
        # 执行器第二道防线已在非 manual 下拒绝；此处纵深防御：
        if self._agent.mode != "manual":
            return ToolResult(
                output=f"Already in {self._agent.mode} mode — proceed in the "
                       f"current mode; do not call choose_mode again."
            )
        # 防重复弹窗闩：用户已选择保持手动后绝不二次打扰
        if getattr(self._agent, "mode_choice_offered", False):
            return ToolResult(
                output="The user was already asked and chose to stay in "
                       "manual mode. Proceed; each write will be confirmed "
                       "individually. Do not call choose_mode again."
            )
        self._agent.mode_choice_offered = True

        if summary:
            self._console.raw.print(f"[dim]Proposed changes: {summary}[/dim]")

        # 复用 ask_user 弹窗机制：触发 on_dialog_start/end 钩子 → 流式
        # Live + InputCapture 正确暂停；自带 "Other" 自由文本行（落入
        # 安全默认：留在 manual）。
        answer = self._console.ask_user_question(
            "This task needs to modify files or run commands. Choose a mode "
            "(该任务需要修改文件或执行命令，请选择执行模式):",
            [
                {"label": "Auto", "description":
                    "Agent may write/run; normal permission prompts apply "
                    "(stored rules & whitelist respected). "
                    "自动模式：按常规权限流程执行。"},
                {"label": "Plan", "description":
                    "Read-only exploration first, then approve a full plan. "
                    "计划模式：先只读探索，提交计划供你审批。"},
                {"label": "Stay in manual", "description":
                    "Confirm every write/shell call individually. "
                    "保持手动：每次写入/执行都逐项确认。"},
            ],
            multi_select=False,
        )
        text = answer[0] if isinstance(answer, list) and answer else str(answer)

        if text == "Auto":
            self._agent.set_mode("auto")
            return ToolResult(
                output="User chose AUTO mode. Proceed; write tools follow "
                       "the normal permission flow."
            )
        if text == "Plan":
            self._agent.set_mode("plan")
            return ToolResult(
                output="User chose PLAN mode. Explore read-only, then call "
                       "exit_plan_mode with your full implementation plan."
            )
        if text == "Stay in manual":
            return ToolResult(
                output="User chose to STAY IN MANUAL mode. Proceed; each "
                       "write tool call will ask for confirmation. Do not "
                       "call choose_mode again this session."
            )
        # "Other" 自由文本 → 安全默认留在 manual，回显用户原话
        return ToolResult(
            output=f"User declined to switch modes and said: {text!r}. Stay "
                   f"in manual mode; each write will be confirmed "
                   f"individually. Do not call choose_mode again."
        )


if __name__ == "__main__":
    # 独立调试：绝不真的弹窗 —— 用 duck-typed 假 agent/console 验证分发
    import asyncio

    class _FakeAgent:
        mode = "manual"
        mode_choice_offered = False

        def set_mode(self, m: str):
            self.mode = m

    class _FakeRaw:
        def print(self, *args, **kwargs):
            pass

    class _FakeConsole:
        raw = _FakeRaw()

        def __init__(self, answer):
            self._answer = answer
            self.calls = 0

        def ask_user_question(self, question, options, multi_select=False):
            self.calls += 1
            return [self._answer]

    async def _self_check():
        # Auto → set_mode("auto")
        agent, console = _FakeAgent(), _FakeConsole("Auto")
        tool = ChooseModeTool(agent, console)
        assert tool.permission.level.value == "allow"
        r = await tool.execute(summary="edit foo.py")
        assert r.success and "AUTO" in r.output, r.output
        assert agent.mode == "auto" and agent.mode_choice_offered is True

        # Plan → set_mode("plan")，提示 exit_plan_mode
        agent2, console2 = _FakeAgent(), _FakeConsole("Plan")
        r2 = await ChooseModeTool(agent2, console2).execute()
        assert agent2.mode == "plan" and "exit_plan_mode" in r2.output, r2.output

        # Stay in manual → 模式不变，提示勿再调用
        agent3, console3 = _FakeAgent(), _FakeConsole("Stay in manual")
        r3 = await ChooseModeTool(agent3, console3).execute()
        assert agent3.mode == "manual" and "again" in r3.output, r3.output

        # 防重复闩：第二次调用不再弹窗
        r4 = await ChooseModeTool(agent3, console3).execute()
        assert console3.calls == 1 and "already asked" in r4.output, r4.output

        # "Other" 自由文本 → 留在 manual（安全默认），回显原话
        agent5, console5 = _FakeAgent(), _FakeConsole("just do it")
        r5 = await ChooseModeTool(agent5, console5).execute()
        assert agent5.mode == "manual" and "just do it" in r5.output, r5.output

        # 非 manual 模式：纵深防御直接返回，不弹窗
        agent6, console6 = _FakeAgent(), _FakeConsole("Auto")
        agent6.mode = "auto"
        r6 = await ChooseModeTool(agent6, console6).execute()
        assert console6.calls == 0 and "auto mode" in r6.output, r6.output

    asyncio.run(_self_check())
    print("openx/tools/mode_tools.py OK ✓")
