"""base bundle 内置插件之一：内置能力工具（builtin-tools）。

内置能力工具集从 ``_build_tools`` 的硬编码列表升格为内置插件：
``apply(ctx)`` 注册**工具工厂**（非实例）——工厂签名
``factory(host) -> list[Tool]``（K3a ToolHost，见 kernel/host.py），
内核注册表按 host 实例化，插件在任何阶段拿不到 agent 本体。

结构性工具（task / workflow / exit_plan_mode / choose_mode / ask_user /
structured_output）**不在本插件**：它们属内核驻留编排核心（混合内核
纪律），由消费方（agent）直接装配——StructuredOutputTool 既有先例。

失败语义：内置插件 apply 抛异常 = 致命（产品带病不该运行），与用户
插件的隔离语义相反；settings 的 plugins.disabled 对内置 id 无效。
"""

from __future__ import annotations

from ..tools.file_tools import (
    EditFileTool, GlobTool, ListDirectoryTool, ReadFileTool, WriteFileTool,
)
from ..tools.git_tools import (
    GitBranchTool, GitDiffTool, GitLogTool, GitStatusTool,
)
from ..tools.memory_tool import MemoryTool
from ..tools.search_tools import GrepTool
from ..tools.shell_tools import ShellTool
from ..tools.task_tools import TaskOutputTool, TaskStopTool
from ..tools.todo_tools import TodoWriteTool
from ..tools.web_tools import WebFetchTool, WebSearchTool


def build_capability_tools(host) -> list:
    """内置能力工具工厂：返回该 host 的内置能力工具实例列表。

    语义与原 ``_build_tools`` 的能力工具部分逐条对齐：TodoWriteTool
    共享 ``host.todos`` 引用、ShellTool 共享 ``host.tasks`` 注册表、
    MemoryTool 共享 ``host.coding_memory``。
    """
    ws = host.workspace
    allow_outside = host.allow_write_outside_workspace
    return [
        # 文件工具
        ReadFileTool(ws),
        WriteFileTool(ws, allow_outside),
        EditFileTool(ws, allow_outside),
        GlobTool(ws),
        ListDirectoryTool(ws),
        # 代码搜索
        GrepTool(ws),
        # Shell（共享 host.tasks 以支持后台模式）
        ShellTool(
            ws,
            host.allowed_commands,
            host.dangerous_commands,
            task_registry=host.tasks,
        ),
        # Git
        GitStatusTool(ws),
        GitDiffTool(ws),
        GitLogTool(ws),
        GitBranchTool(ws),
        # 任务追踪（共享 host.todos）
        TodoWriteTool(host.todos),
        # 联网
        WebFetchTool(),
        WebSearchTool(provider=host.web_search_provider),
        # 自主记忆（agent 决定何时存/取）
        MemoryTool(host.coding_memory),
        # 后台任务（共享 host.tasks）
        TaskOutputTool(host.tasks),
        TaskStopTool(host.tasks),
    ]


def apply(ctx) -> None:
    """内置插件入口：注册能力工具工厂（base bundle 组合挂载）。"""
    ctx.register_tool_factory("core-tools", build_capability_tools)
