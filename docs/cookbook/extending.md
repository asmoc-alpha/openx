# Extending OpenX

English | [中文](extending.zh.md)

Three routes: **plugins** (code, no core changes), **configuration**
(subagents, hooks, MCP, workflows), and — only if you mean to contribute to
the core — modifying the builtin assembly directly.

## Add a custom tool — as a plugin (recommended)

Drop a single file into `<workspace>/.openx/plugins/my_tool.py` (or generate
the skeleton with `/plugin new my_tool`):

```python
__openx_meta__ = {
    "type": "capability.tool",
    "trust": "user",
    "summary": "Does something useful",
    "permissions": ["fs:read"],   # capability attestation — see docs
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
        # Your logic here
        return ToolResult(output=f"Got: {arg1}")

def apply(ctx):
    ctx.register_tool(MyTool())

def self_test():
    assert True
```

Verify:

1. `/plugin test my_tool` — runs `self_test` in the admission sandbox.
2. `/plugin reload my_tool` (or restart) — then ask the agent to use
   `my_tool`; the permission prompt shows "Running my tool".
3. `/plugins` — inventory, warnings, and per-plugin detail.

Full reference — manifest keys, all `apply(ctx)` registration methods,
security model, directory plugins with assets: [subsystems/plugins.md]
(../subsystems/plugins.md).

## Add a custom tool — in core (only for contributions)

If you intend to ship the tool as part of OpenX itself, register it where
the builtins are assembled (`openx/builtin/tools.py`), not as a plugin.
Run `pytest` — existing tools keep passing.

## Declare a scaffold

A plugin that exists only to **compensate for a model shortcoming** (history
compression, intent routing, subagents, memory retrieval, a harness loop…) must
carry its own obituary. Declare it in `__openx_meta__`:

```python
__openx_meta__ = {
    "type": "capability.tool",
    "trust": "user",
    "summary": "Compress long histories",
    "scaffold": {
        "compensates": "the model's context window is finite",
        "exit_when": "long-session eval passes without compression",
        "eval_set": "evals/long-session.jsonl",     # optional
        "fallback": "reinstall-on-regression",       # optional
    },
}
```

`compensates` and `exit_when` are **mandatory** — a module that cannot answer
both has no business existing as a scaffold: either it belongs in the kernel or
it is an ordinary capability plugin. The kernel validates the block's shape
(missing required fields reject the plugin; unknown values are warnings only) and
surfaces it through `/plugins`, `list_plugins` and `plugin_help`. The declaration
is the input to the retirement eval gate; declaring one is not evidence by itself.

## Configuration-only extension points

No code changes needed:

| Extension point | Where | Reference |
|---|---|---|
| Custom subagents | `.openx/agents/*.md` | [Subagents](../subsystems/subagents.md) |
| Workflows | `.openx/workflows/*.py` | [Workflows](../subsystems/workflows.md) |
| Hooks | `hooks` block in `settings.json` | [Hooks](../subsystems/hooks.md) |
| MCP servers | `mcpServers` block in `settings.json` | [MCP](../subsystems/mcp.md) |
