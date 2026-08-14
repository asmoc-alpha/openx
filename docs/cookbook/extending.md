# Extending OpenX

English | [中文](extending.zh.md)

Two routes: code (custom tools) and configuration (everything else).

## Add a custom tool

Subclass `Tool`:

```python
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
        # Your logic here
        return ToolResult(output=f"Got: {arg1}")
```

Verify:

1. Register the tool where the builtins are assembled (see `openx/tools/__init__.py`).
2. Run `openx`, then ask the agent to use `my_tool` — the permission prompt shows
   "Running my tool".
3. `pytest` — existing tools keep passing.

## Configuration-only extension points

No code changes needed:

| Extension point | Where | Reference |
|---|---|---|
| Custom subagents | `.openx/agents/*.md` | [Subagents](../subsystems/subagents.md) |
| Workflows | `.openx/workflows/*.py` | [Workflows](../subsystems/workflows.md) |
| Hooks | `hooks` block in `settings.json` | [Hooks](../subsystems/hooks.md) |
| MCP servers | `mcpServers` block in `settings.json` | [MCP](../subsystems/mcp.md) |
