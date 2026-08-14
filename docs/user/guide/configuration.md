# Configuration

English | [中文](configuration.zh.md)

Configuration merges lowest-to-highest priority: `settings.json` env →
`~/.openx/config.json` → project settings → legacy `.openx.json` → environment
variables → CLI flags.

## Environment variables

```bash
OPENAI_API_KEY      # Required: your API key
OPENAI_API_BASE     # API base URL (any OpenAI-compatible endpoint)
OPENX_MODEL         # Model name (default: gpt-4o)
OPENX_AUTO_APPROVE  # Set to 'true' to skip prompts
```

## Settings (`~/.openx/settings.json`)

Written by the first-run setup wizard; also home to `hooks`, `mcpServers`, and the
list of trusted workspace directories:

```json
{
  "env": {
    "OPENX_API_KEY": "sk-...",
    "OPENX_BASE_URL": "https://api.openai.com/v1",
    "OPENX_DEFAULT_MODEL": "gpt-4o"
  }
}
```

## Config file (`~/.openx/config.json`)

```json
{
  "api_key": "sk-...",
  "api_base": "https://api.openai.com/v1",
  "model": "gpt-4o",
  "temperature": 0.0,
  "auto_approve": false,
  "max_retries": 4,
  "retry_base_delay": 1.0
}
```

### Retries

Transient API errors — HTTP 429/408/409/5xx, connection failures, timeouts, and
streams that disconnect before anything is on screen — retry automatically up to
`max_retries` times (0 disables). Delays use exponential backoff `base·2^attempt`
plus jitter from `retry_base_delay` (seconds), capped at 60 s; a `Retry-After`
response header takes priority over the formula. Errors that can't succeed on retry
(400/401/403/404) raise immediately. Once a streaming response has produced visible
text, a disconnect surfaces as an error instead of silently restarting (that would
duplicate output). Every retry prints a one-line warning so long pauses are never a
mystery.

## Project config (`<workspace>/.openx/settings.json`)

```json
{
  "model": "gpt-4o",
  "allowed_commands": ["npm", "npx", "docker", "make"]
}
```

Project-level `allowed_commands` pre-approve matching shell commands (no prompt).
Legacy `.openx.json` project files are still read but deprecated.

## See also

- [Development guide](../../development.md) — first-run setup wizard
- [Hooks](../../subsystems/hooks.md) — the `hooks` settings block
- [MCP](../../subsystems/mcp.md) — the `mcpServers` settings block
