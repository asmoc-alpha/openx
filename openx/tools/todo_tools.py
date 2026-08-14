"""Todo 任务追踪工具 —— 参考 claude-code 的 TodoWriteTool。

设计动机
========
claude-code 用一个 ``todo_write`` 工具让 agent 在处理多步任务时维护一个
结构化任务清单：开始一项任务前标记为 ``in_progress``，完成后标记为
``completed``。这既帮助 agent 自身规划与追踪进度，也让用户能直观看到当前
进展。

实现要点
========
- 工具本身无状态，真正的任务列表存放在 ``OpenXAgent.todos`` 上；
  构造时传入该列表的 *引用*，``execute`` 通过 ``store[:] = todos`` 原地替换，
  这样 agent 始终看到最新值（Python 列表按引用传递，原地赋值对外可见）。
- 权限为 ``ALLOW``：写任务清单是纯内存操作，无副作用，无需询问用户。
- 状态机：``pending`` → ``in_progress`` → ``completed``；约定同一时刻只应有
  一个 ``in_progress``，由模型自律（提示词中强调）。
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


# 允许的任务状态 —— 与 claude-code 保持一致
_VALID_STATUSES = {"pending", "in_progress", "completed"}


class TodoWriteTool(Tool):
    """更新当前会话的任务清单。

    模型每次调用都会用 *完整* 的新清单覆盖旧清单（而非增量更新），这与
    claude-code 一致：让模型始终基于全量现状决策，避免增量 patch 的复杂度。
    """

    name = "todo_write"
    description = (
        "Create and manage a structured task list for the current session. "
        "Pass the FULL updated todo list every call (it replaces the previous one). "
        "Use proactively for tasks with 3+ steps. Keep exactly one task "
        "in_progress at a time. Mark tasks completed only when fully done."
    )
    parameters = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "The full, updated todo list.",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "Imperative form, e.g. 'Fix the login bug'.",
                        },
                        "activeForm": {
                            "type": "string",
                            "description": (
                                "Present-continuous form shown while working, "
                                "e.g. 'Fixing the login bug'."
                            ),
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                            "description": "Current state of the task.",
                        },
                    },
                    "required": ["content", "activeForm", "status"],
                },
            },
        },
        "required": ["todos"],
    }

    def __init__(self, store: list[dict[str, Any]]):
        # 持有 agent.todos 的引用 —— 原地修改即可让 agent 感知
        self._store = store

    @property
    def permission(self) -> Permission:
        return Permission.allow()

    async def execute(self, todos: list[dict[str, Any]]) -> ToolResult:
        """用新清单覆盖旧清单。

        Args:
            todos: 完整的新任务列表，每项含 content/activeForm/status。

        Returns:
            确认信息，提示模型继续推进任务。
        """
        # 校验与规范化：补全缺失字段、剔除非法状态
        normalized: list[dict[str, Any]] = []
        for item in todos:
            status = item.get("status", "pending")
            if status not in _VALID_STATUSES:
                status = "pending"
            normalized.append({
                "content": item.get("content", ""),
                "activeForm": item.get("activeForm", item.get("content", "")),
                "status": status,
            })

        # 原地替换：store[:] = ... 保留同一 list 对象，agent.todos 立即可见
        self._store[:] = normalized

        # 统计进度，回写给模型一个简短确认（claude-code 风格文案）
        done = sum(1 for t in normalized if t["status"] == "completed")
        total = len(normalized)
        return ToolResult(
            output=(
                f"Todos updated ({done}/{total} completed). "
                "Continue using the todo list to track progress; keep exactly one "
                "task in_progress at a time and proceed with the current task."
            )
        )


if __name__ == "__main__":
    # 独立调试：建共享 list，TodoWriteTool 写入任务并打印（纯内存，无副作用）
    import asyncio

    async def _self_check():
        store: list[dict[str, Any]] = []
        tool = TodoWriteTool(store)
        r = await tool.execute(todos=[
            {"content": "Write tests", "activeForm": "Writing tests", "status": "completed"},
            {"content": "Ship it", "activeForm": "Shipping it", "status": "in_progress"},
        ])
        assert r.success and len(store) == 2 and store[1]["status"] == "in_progress"
        print(r.output)
        for item in store:
            print(f"  [{item['status']}] {item['content']}")

    asyncio.run(_self_check())
    print("openx/tools/todo_tools.py OK ✓")
