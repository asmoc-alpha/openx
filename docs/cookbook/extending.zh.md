# 扩展 OpenX

[English](extending.md) | 中文

三条路线：**插件**（写代码、不动内核）、配置（子代理/hooks/MCP/workflows）、
以及——仅当您打算向内核贡献时——直接改内置组装。

## 添加自定义 tool——以插件方式（推荐）

往 `<workspace>/.openx/plugins/my_tool.py` 放一个文件即可（或
`/plugin new my_tool` 生成骨架）：

```python
__openx_meta__ = {
    "type": "capability.tool",
    "trust": "user",
    "summary": "Does something useful",
    "permissions": ["fs:read"],   # 能力声明——见文档
}

from openx.tools.base import Tool, ToolResult
from openx.permissions import Permission

class MyTool(Tool):
    name = "my_tool"
    description = "Does something useful"
    parameters = {
        "type": "object",
        "properties": {
            "arg1": {"type": "string", "description": "First argument"}
        },
        "required": ["arg1"]
    }

    @property
    def permission(self) -> Permission:
        return Permission.ask("Running my tool")

    async def execute(self, arg1: str) -> ToolResult:
        # 你的逻辑
        return ToolResult(output=f"Got: {arg1}")

def apply(ctx):
    ctx.register_tool(MyTool())

def self_test():
    assert True
```

验证：

1. `/plugin test my_tool`——在 admission 沙箱里跑 `self_test`。
2. `/plugin reload my_tool`（或重启）——然后让 agent 使用 `my_tool`，
   权限弹窗会显示 "Running my tool"。
3. `/plugins`——清单、警告与单插件详情。

完整参考——manifest 全键、`apply(ctx)` 全部注册方法、安全模型、带资产的
目录插件：[subsystems/plugins.zh.md](../subsystems/plugins.zh.md)。

## 添加自定义 tool——进内核（仅限内核贡献）

若该 tool 本就该随 OpenX 发行，请在内置组装处注册
（`openx/builtin/tools.py`），而非做成插件。跑 `pytest`——既有工具保持通过。

## 声明脚手架

只为**补偿模型短板**而存在的插件（历史压缩、意图路由、子代理、记忆检索、
harness loop……）必须自带讣告。在 `__openx_meta__` 里声明：

```python
__openx_meta__ = {
    "type": "capability.tool",
    "trust": "user",
    "summary": "压缩长历史",
    "scaffold": {
        "compensates": "模型上下文有限",
        "exit_when": "长会话免压缩评测通过",
        "eval_set": "evals/long-session.jsonl",     # 可选
        "fallback": "reinstall-on-regression",       # 可选
    },
}
```

`compensates` 与 `exit_when` 是**必答项**——答不出两者的模块没有资格以脚手架
身份存在：要么进内核，要么就是普通能力插件。内核只校验块的形状（必答项缺失即
拒载；值不在词汇表只警告），并经 `/plugins`、`list_plugins`、`plugin_help`
暴露。声明是退场评测门的输入——声明本身不是证据。

## 纯配置扩展点

无需改代码：

| 扩展点 | 位置 | 参考 |
|---|---|---|
| 自定义 subagents | `.openx/agents/*.md` | [Subagents](../subsystems/subagents.zh.md) |
| Workflows | `.openx/workflows/*.py` | [Workflows](../subsystems/workflows.zh.md) |
| Hooks | `settings.json` 的 `hooks` 块 | [Hooks](../subsystems/hooks.zh.md) |
| MCP servers | `settings.json` 的 `mcpServers` 块 | [MCP](../subsystems/mcp.zh.md) |
