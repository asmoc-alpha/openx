# Plugins

English | [中文](plugins.zh.md)

A plugin is a unit of code that registers capabilities (tools, commands,
context fragments, lifecycle hooks, UI panels) into the kernel through
`apply(ctx)`. Everything the agent can do beyond builtins arrives through
this one mechanism — including the model's own generated plugins.

```
<name>.py                       # single-file plugin
<plugins>/<name>/plugin.py      # directory plugin (assets next to the entry)
```

Locations, in discovery order (first id wins):
`~/.openx/plugins/` → `<ws>/.openx/plugins/` → pip entry-points group
`openx.plugins`. Builtins (`openx/builtin/*`) are pre-imported modules, not
on this path.

## The minimal plugin

```python
__openx_meta__ = {
    "type": "capability.tool",
    "mount": "loop.tool-call",
    "trust": "user",
    "summary": "One line the model sees in /plugins",
    "permissions": ["fs:read"],   # capability ceiling — see below
}

from openx.tools.base import Tool, ToolResult

class MyTool(Tool):
    name = "my_tool"
    description = "..."
    parameters = {"type": "object", "properties": {...}}

    @property
    def permission(self):
        from openx.permissions import Permission, PermissionLevel
        return Permission(level=PermissionLevel.ASK, reason="...")

    async def execute(self, **kwargs):
        return ToolResult(output="...")

def apply(ctx):
    ctx.register_tool(MyTool())

def self_test():
    assert True
```

Scaffold one without typing it: `/plugin new <name>`.

## Manifest reference

| Key | Type | Meaning |
|---|---|---|
| `type` | str | Protocol routing: `capability.tool`, `context.memory`, `lifecycle`, `ui.panel`. Unknown → default route + warning. `strategy.planning`/`orchestration` are reserved, no protocol yet |
| `mount` | str | Documentation-only (protocol table derives the real one) |
| `trust` | str | `builtin` / `third-party` / `user` / `auto` (model-produced) |
| `summary` | str | Shown in `/plugins` and the model's catalog |
| `permissions` | list | Capability **attestation** — see below |
| `timeout` | number | Per-call tool timeout (default 60s) |
| `cost` | dict | `schemaTokens` for budget bookkeeping |
| `dependencies` | list | Plugin ids that must be ACTIVE at load time |
| `api` | int | Target kernel plugin API (currently 1); mismatch refuses load |
| `protocol` | str | Explicit protocol pin (e.g. `tool/v1`), type-checked |
| `scaffold` | dict | E1 retirement declaration (`compensates`/`exit_when` required) — see [scaffolds.md](scaffolds.md) |

Unknown top-level keys **warn** (a typo'd key never silently no-ops). Shape
errors **refuse load**.

## Registration surface (`apply(ctx)`)

| Method | Registers |
|---|---|
| `ctx.register_tool(tool)` | A `Tool` instance (validated, wrapped in `ProtectPluginTool`: timeout + output cap + 3-crash circuit breaker) |
| `ctx.register_tool_factory(name, factory)` | `factory(host) -> list[Tool]`; `host` is a read-only `ToolHost` snapshot |
| `ctx.register_command(name, handler, description, aliases)` | A `/slash` command: `async (agent, console, args) -> bool` |
| `ctx.register_context(name, contribute, priority)` | System-prompt fragments; ascending priority, 5s timeout, 3-strike breaker |
| `ctx.register_lifecycle(name, on_session_start=, on_checkpoint=, on_resume=, on_unload=)` | Session lifecycle hooks |
| `ctx.register_ui_slot(name, render, refresh_hz)` | Status-deck panel (max 8 lines; 1s render timeout; 3-strike breaker) |
| `ctx.plugin_dir` | Root of a directory plugin — resolve data assets against it (`None` for single-file/builtin) |

## Security model

- **Trust gate (boot)**: `<ws>/.openx/plugins` loads only if the workspace is
  trusted (interactive CLI asks at startup; `serve` fails closed — use
  `/trust`). User dir and pip entry-points are trusted by design.
- **Admission sandbox**: the first execution of plugin code (import +
  `self_test`) runs in a sandboxed subprocess (macOS `sandbox-exec`: no
  network, no writes outside /tmp; POSIX rlimits; 15s timeout kills it).
  Builtins and entry-points are exempt. `OPENX_PLUGIN_ADMISSION=0` disables
  the gate — debug only.
- **Permission ceiling**: `permissions` is an attestation. A plugin that
  declares none gets its tools' self-declared level capped to ASK and
  `auto_allowed` disabled. Declaring permissions keeps self-declared levels;
  Guard still force-ASKs high-risk calls. The vocab:
  `fs:read`, `fs:write`, `network`, `shell`, `process`.
- **Fail isolation**: a crashing plugin never kills the session — PHASE_FAILED
  with entries purged (transactional apply), module unloaded; tool crashes
  trip the circuit breaker and auto-remove the tool.
- Honest limits: in-process execution (L1) at tool runtime; the sandbox covers
  the admission phase. The permission ceiling is declarative, not behavioral.

## Lifecycle

`/plugin load|unload|reload <name>` (session scope), composition decides boot
scope; `on_unload` runs before entries are purged. `auto-*` plugins
(model-produced) boot disabled and can be promoted (`promote_plugin`) to
persist via overlay, or batch-rolled back.

## Testing & debugging

```
/plugin test <name>     # re-run self_test in the admission sandbox
/plugin reload <name>   # explicit dev reload (boot scope refreshes in place)
/plugins, /plugin_help  # inventory + per-plugin detail incl. warnings
OPENX_PLUGIN_AUTORELOAD=1  # rebuild bundle when plugin files change
```

`self_test` is the admission prerequisite ("self-test is a prerequisite for
admission, not a substitute for it") — make it meaningful: exercise the tool,
assert outcomes, print progress.

## Boundary with Skills

[Skills](skills.md) (`SKILL.md`) are **instruction packs**: prompt-level,
progressively disclosed, no registration surface — they ride one builtin
context fragment. Plugins are **code**: they register capabilities. The two
do not merge; they cooperate (a skill teaches the model when to call a
plugin's tool).

## Protocol pinning

A plugin may pin the contract it was written against:

```python
__openx_meta__ = {"type": "capability.tool", "protocol": "tool/v1", ...}
```

The declaration is **enforced**: an unknown protocol refuses load, and a
protocol whose routed type differs from `manifest.type` refuses load with
both types in the message (`find()` in `kernel/assembly/protocols.py` is the
checker). Without a `protocol` key, routing falls back to the type table —
existing plugins are unaffected. `strategy.planning` / `orchestration` are
reserved type names with no protocol row: they cannot be pinned yet.
Inventory projections (`/plugins`, `plugin_help`) expose `api` (the kernel's
supported plugin API version, currently 1) alongside the pin.
