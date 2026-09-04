# Configuration

English | [中文](configuration.zh.md)

Model & provider configuration has exactly **one** source: the `modelGroups` block in
`~/.openx/settings.json`. The legacy flat shapes (`env`-section LLM keys,
`~/.openx/config.json`, `.openx.json`, or auto-reading
`OPENAI_API_KEY`/`OPENAI_API_BASE`/`OPENX_MODEL`) are **not** read as model config.

On first run, `openx` detects that no group is configured and launches an interactive
setup wizard, which writes a `default` model group and sets `activeGroup`.

## Model groups (`~/.openx/settings.json`)

`modelGroups` maps a group name to a group definition; `activeGroup` names the group in
use. Each group shares an optional `kind`/`apiKey`/`apiBase` and can define up to four
role models:

| role key | short | purpose |
|---|---|---|
| `openx-main-model` | `main` | the agent's own reasoning model (required) |
| `openx-exec-model` | `exec` | sub-agents / task delegation |
| `openx-mini-model` | `mini` | cheap tasks (history compaction) |
| `openx-modal-model` | `modal` | image-bearing turns |

```json
{
  "activeGroup": "default",
  "modelGroups": {
    "default": {
      "kind": "openai-compat",
      "apiKey": "env:OPENAI_API_KEY",
      "apiBase": "https://api.openai.com/v1",
      "openx-main-model": "gpt-4o",
      "openx-exec-model": { "model": "gpt-4o-mini" },
      "openx-mini-model": { "model": "gpt-4o-mini" },
      "openx-modal-model": "gpt-4o"
    },
    "local": {
      "kind": "openai-compat",
      "apiBase": "http://localhost:11434/v1",
      "apiKey": "env:OPENAI_API_KEY",
      "openx-main-model": "llama3.1"
    }
  }
}
```

A role value is either a model-string shorthand or an object:

```json
"openx-exec-model": {
  "model": "claude-sonnet-5",
  "kind": "anthropic-compat",
  "apiKey": "env:ANTHROPIC_API_KEY",
  "temperature": 0.2,
  "max_tokens": 4096,
  "max_retries": 3,
  "retry_base_delay": 1.0
}
```

A role object may override `kind`/`apiKey`/`apiBase` (even switching provider/endpoint
for one role) and request tuning. Roles that are absent fall back to the group's `main`
binding (model + credentials).

`anthropic-compat` speaks the Anthropic-format protocol — many vendors expose it (e.g.
DeepSeek's `https://api.deepseek.com/anthropic`). Point it at any compatible endpoint by
setting `apiBase`; leave it out to default to Anthropic's official endpoint. The legacy
kind `anthropic` is still accepted as an alias.

### Secrets via `env:VAR`

Any `apiKey`/`apiBase` value may be `env:VARNAME`, resolved from the process
environment at runtime — the only external credential channel. A group can omit
credentials entirely and pull the key from an environment variable.

### Group names

Letters, digits, `.`, `_`, `-` (no `:` — that prefix selects roles). Use
`/model <group>` to switch and `/model <group>:<role>` to set a role's model at runtime.

## Environment variables

Only non-provider knobs are read directly from the environment:

```bash
export OPENX_AUTO_APPROVE=true   # skip permission prompts
export OPENX_WEB_SEARCH=ddg      # or 'bing' / 'auto'
```

Provider model / key / base must be configured in a model group (optionally via
`env:VAR`); they are never auto-read from `OPENAI_*`.

## Project settings (`<workspace>/.openx/settings.json`)

The project-level file may set non-model knobs such as `allowed_commands`
(pre-approved shell commands). It cannot define models or credentials — those belong
to model groups in the global `~/.openx/settings.json` — but it **can pick which of
those global groups this workspace should start with** via `activeGroup`:

```json
{
  "allowed_commands": ["npm", "npx", "docker", "make"],
  "activeGroup": "work"
}
```

Group definitions stay global; only the *selection* is per-workspace. Activation order:
a `/model` switch during the session (current group) > this project default > the global
`activeGroup`. Launching openx in a project with a default group always starts on that
group; switching away with `/model` lasts for the session (it still writes the global
`activeGroup`) and the project default re-applies next launch. A default naming a group
that doesn't exist falls back to the global active group.

## Removed CLI flags

Launch-time `--model`/`--api-key`/`--api-base` (plus `--max-rounds`/`--temperature`) have
been removed — model, credentials and endpoint are configured only through model groups,
and request tuning lives with the group/role or the project file. At runtime use
`/model` and `/config`; a project may also pick its group via `activeGroup`. With no
group configured at all, openx runs the setup wizard instead.

## Retries

Transient API errors — HTTP 429/408/409/5xx, connection failures, timeouts, and
streams that disconnect before anything is on screen — retry automatically up to
`max_retries` times (default 4; 0 disables). Delays use exponential backoff
`base·2^attempt` plus jitter from `retry_base_delay` (default 1.0 s), capped at 60 s; a
`Retry-After` response header takes priority over the formula. Errors that can't
succeed on retry (400/401/403/404) raise immediately. Once a streaming response has
produced visible text, a disconnect surfaces as an error instead of silently
restarting. `max_retries`/`retry_base_delay` may be declared at group or role level.

## See also

- [Development guide](../../development.md) — first-run setup wizard
- [Hooks](../../subsystems/hooks.md) — the `hooks` settings block
- [MCP](../../subsystems/mcp.md) — the `mcpServers` settings block
