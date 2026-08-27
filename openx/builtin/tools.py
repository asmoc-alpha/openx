"""base bundle 内置插件之一：内置工具（builtin-tools）。

内置工具集从 ``_build_tools`` 的硬编码列表升格为内置插件：``apply(ctx)``
注册**工具工厂**（非实例）——内置工具需按 agent 构造（workspace、
console/tasks/todos 引用、结构性工具看 ``_parent``），内核在
``_build_tools`` 时以 agent 为参实例化。

失败语义：内置插件 apply 抛异常 = 致命（产品带病不该运行），与用户
插件的隔离语义相反；settings 的 plugins.disabled 对内置 id 无效。
"""

from __future__ import annotations

from ..core.subagent import load_subagent_specs
from ..tools.ask_user_tool import AskUserTool
from ..tools.file_tools import (
    EditFileTool, GlobTool, ListDirectoryTool, ReadFileTool, WriteFileTool,
)
from ..tools.git_tools import (
    GitBranchTool, GitDiffTool, GitLogTool, GitStatusTool,
)
from ..tools.memory_tool import MemoryTool
from ..tools.mode_tools import ChooseModeTool
from ..tools.plan_tools import ExitPlanModeTool
from ..tools.search_tools import GrepTool
from ..tools.shell_tools import ShellTool
from ..tools.subagent_tool import TaskTool
from ..tools.task_tools import TaskOutputTool, TaskStopTool
from ..tools.todo_tools import TodoWriteTool
from ..tools.web_tools import WebFetchTool, WebSearchTool
from ..tools.workflow_tool import WorkflowTool


def build_core_tools(agent) -> list:
    """内置工具工厂：返回该 agent 的内置工具实例列表。

    语义与原 ``_build_tools`` 逐条对齐：TodoWriteTool 共享 ``todos``
    引用、AskUserTool 共享 console、ShellTool 共享 tasks 注册表；
    结构性工具（exit_plan_mode/choose_mode/task/workflow）仅顶层。
    """
    ws = str(agent.workspace)
    allow_outside = agent.config.allow_write_outside_workspace

    # 子代理规格表（仅顶层 agent 填充；子代理恒为空 → 无 task 工具）
    agent._subagent_specs: dict = {}

    tools = [
        # 文件工具
        ReadFileTool(ws),
        WriteFileTool(ws, allow_outside),
        EditFileTool(ws, allow_outside),
        GlobTool(ws),
        ListDirectoryTool(ws),
        # 代码搜索
        GrepTool(ws),
        # Shell（共享 agent.tasks 以支持后台模式）
        ShellTool(
            ws,
            agent.config.allowed_commands,
            agent.config.dangerous_commands,
            task_registry=agent.tasks,
        ),
        # Git
        GitStatusTool(ws),
        GitDiffTool(ws),
        GitLogTool(ws),
        GitBranchTool(ws),
        # 任务追踪（共享 agent.todos）
        TodoWriteTool(agent.todos),
        # 联网
        WebFetchTool(),
        WebSearchTool(provider=agent.config.web_search_provider),
        # 主动提问（共享 console）
        AskUserTool(agent.console),
        # 自主记忆（agent 决定何时存/取）
        MemoryTool(agent.coding_memory),
        # 后台任务（共享 agent.tasks）
        TaskOutputTool(agent.tasks),
        TaskStopTool(agent.tasks),
    ]
    # 结构性工具仅顶层 agent 持有（子代理不打断用户、禁套娃、禁嵌套编排）
    if getattr(agent, "_parent", None) is None:
        tools.append(ExitPlanModeTool(agent, agent.console))
        tools.append(ChooseModeTool(agent, agent.console))
        # 子代理规格：内置 + 项目 .openx/agents/*.md（坏文件跳过不报错）
        agent._subagent_specs = load_subagent_specs(ws)
        tools.append(TaskTool(agent, agent._subagent_specs))
        tools.append(WorkflowTool(agent))
    return tools


def apply(ctx) -> None:
    """内置插件入口：注册工具工厂（base bundle 组合挂载）。"""
    ctx.register_tool_factory("core-tools", build_core_tools)
