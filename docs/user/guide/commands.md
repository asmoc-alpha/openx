# Commands

English | [中文](commands.zh.md)

Two surfaces: slash commands inside the REPL, and machine-readable output for
headless / CI runs.

## Interactive commands

All commands registered in the REPL (see `openx/cli/commands.py`):

| Command | Description |
|---------|-------------|
| `/quit` (aliases `/exit`, `/q`) | Exit OpenX |
| `/help` | Show all available commands |
| `/clear` | Clear screen and conversation history |
| `/model <name>` | Switch LLM model (e.g., `/model gpt-4o`) |
| `/workspace <path>` | Change workspace directory |
| `/auto-approve` | Toggle auto-approve mode |
| `/mode [mode]` | Show or switch permission mode (manual / auto / plan) |
| `/explore` | Show project overview |
| `/image <path>` | Load and analyze an image file |
| `/clipboard` | Paste and analyze a clipboard screenshot |
| `/init` | Create an OPENX.md instruction file |
| `/instructions` | Show loaded OPENX.md instructions |
| `/memory` | Show all stored memories |
| `/remember <fact>` | Save a fact to persistent memory |
| `/forget <name>` | Delete a memory by name |
| `/permissions` (alias `/perms`) | Show and manage stored permission rules |
| `/hooks` | Show configured hooks |
| `/mcp` | Show MCP server status |
| `/workflow [name]` (alias `/workflows`) | List or run saved workflows (`.openx/workflows/`) |
| `/todos` | Show the agent's task list |
| `/cost` | Show cumulative token usage |
| `/compact` | Summarize history to free up context |
| `/git` | Show git status |
| `/diff` | Show git diff |
| `/config` | Show configuration; interactively change model, API key, API base URL |
| `/tips` | Show usage tips |
| `/release-notes` (alias `/release`) | Browse release notes — pick a version to view, or `/release <version>` |

Type `/` in the input box to browse commands with completion: filters as you type
(matches names and aliases), **↑↓** to navigate, **Tab** to complete, **Enter** to run
the selected command, **Esc** to dismiss.

## Machine-readable output (`--output-format`)

Single-shot mode can emit JSON instead of the human-facing UI — stdout carries **only**
JSON (all human noise goes to stderr), and the exit code tells CI whether the run
succeeded (`0`) or failed (`1`).

| Format | Output |
|--------|--------|
| `text` (default) | Human-readable: banner, thinking indicator, assistant reply |
| `json` | Exactly one result object: `{"type": "result", "subtype": "success", "is_error": false, "duration_ms": …, "num_turns": …, "result": "final text", "session_id": "…", "usage": {"input_tokens": …, "output_tokens": …}}` (on failure: `"is_error": true` plus an `"error"` field) |
| `stream-json` | NDJSON events: `system/init` (model, session id, tool list), `text_delta` (assistant text increments), `tool_use` / `tool_result` (name, error flag, capped output), then the same `result` object |

```bash
# Machine-readable output for CI / scripting (single-shot only)
openx "fix the failing test in tests/test_api.py" --output-format json
openx "refactor module X" --output-format stream-json

# Chain runs: feed one session's answer into the next
SID=$(openx "analyze the auth module" --output-format json | jq -r .session_id)
```

## See also

- [Modes & permissions](modes-permissions.md) — what `/mode` and `/auto-approve` control
- [Sessions](sessions.md) — `--continue` / `--resume`
