# Changelog

All notable changes to OpenX. One `## <version> — <title>` section per release,
newest first; parsed at runtime by `openx/changelog.py` into the startup panel
and `/release-notes`.

## 0.1.1 — Model groups & Anthropic-compatible protocol

### Configuration: model groups

- Added model groups as the single model/provider config — `modelGroups` (+ `activeGroup`) in `~/.openx/settings.json`, replacing the legacy single model, flat `api_key`/`api_base`, providers and profiles forms
- Added per-group, per-role models — `main`/`exec`/`mini`/`modal` bindings (`openx-main-model` & friends), each able to override kind / endpoint / credentials; roles that are absent fall back to the group's main
- Added `/model <group>` and `/model <group>:<role>` to switch groups or set a role's model; `/config` views and edits the active group
- Added `env:VAR` indirection for `apiKey`/`apiBase` inside groups — the only external credential channel
- Added the first-run setup wizard, which writes a `default` model group

### Providers & routing

- Added `anthropic-compat` — the anthropic kind now points at any Anthropic-format endpoint via `apiBase` (e.g. DeepSeek) and defaults to Anthropic's official API when blank; the legacy `anthropic` kind remains as an alias
- Added project-scoped group selection — a project's `.openx/settings.json` `activeGroup` picks which group that workspace starts on
- Added multi-modal routing — image-bearing turns use the group's `modal` model when declared, falling back to `main` otherwise

### CLI & Web

- Removed the `--model`/`-m`, `--api-key`, `--api-base`, `--max-rounds` and `--temperature` launch flags — models and tuning live in model groups or project settings; `--image`/`-i` is retained for one-shot image analysis
- Added the web UI — `openx serve` exposes a browser interface to the same agent

## 0.1.0 — First public release

OpenX is an agentic coding CLI in Python — chat with your codebase using any
OpenAI-compatible LLM. This release consolidates the full pre-release history.

### Core loop

- Added the agentic loop — autonomous tool calling, up to 30 rounds per query
- Added parallel tool execution — independent calls run concurrently; permission checks stay serial
- Added retry with exponential backoff for transient API failures (429 / 5xx / connection errors / mid-stream disconnects); Retry-After headers honored, tunable via max_retries / retry_base_delay
- Added auto-compaction at 80% of the history token budget, or manually via /compact

### Tools

- Added file tools — read_file, write_file, edit_file (find-and-replace semantics), glob, list_directory
- Added shell execution, grep code search with regex, and git status/diff/log/branch
- Added web tools — web_fetch, web_search
- Added image analysis — files, clipboard screenshots (/image, /clipboard, --image)
- Added persistent memory — /remember, /memory, /forget
- Added colored unified diffs in write/edit approval dialogs

### Modes & permissions

- Added three permission modes — manual (startup default: reads run free, every write confirms), auto, and plan (read-only exploration, then approve the plan); /mode [manual|auto|plan]
- Added the choose_mode flow — manual mode offers Auto / Plan / Stay in manual on the first task that needs changes
- Dangerous shell commands always prompt — never skipped by rules, whitelist, or -y
- Added a three-tier permission system (allow / ask / deny) with stored rules via /permissions
- Added workspace scoping — no writes outside project boundaries by default

### Subagents & orchestration

- Added subagents — task tool, builtin types (general-purpose, explore) plus custom .openx/agents/*.md; children cannot nest
- Added structured output — task and workflow agent() accept a JSON Schema and return the validated object via structured_output
- Added the live status deck — todos checklist plus one status row per parallel subagent (5 Hz); Ctrl-O cycles into sub-agent detail views
- Added workflows — deterministic multi-agent orchestration in Python scripts (.openx/workflows/), with agent / parallel / pipeline / phase / log hooks
- Added background tasks — shell run_in_background with task_output / task_stop; cleanup on exit

### Extensions

- Added hooks — Claude-Code-compatible shell hooks on PreToolUse / PostToolUse / UserPromptSubmit / Stop
- Added MCP — stdio servers via mcpServers in settings.json, zero extra dependencies (newline-delimited JSON-RPC)
- Added sessions — append-only JSONL under ~/.openx/sessions; resume with --continue / --resume

### Terminal & UI

- Added interrupt & queued input — Esc interrupts while the agent thinks or answers; Enter queues a message during streaming, Esc sends it
- Added slash command completion — type / to browse and filter, ↑↓ to navigate, Tab to complete
- Added terminal-resize handling — streaming re-anchors on window resize; CJK-aware cursor reposition
- Added the visual design — restrained chrome-grey palette with one accent colour, pixel-art mascot, cleaner panels and dialogs
- Added /release-notes (/release) — browse release notes by version; /config edits model, API key, and base URL

### Headless & CI

- Added headless JSON output — --output-format json (one result object) or stream-json (NDJSON events) with CI exit codes
- Added multi-provider support — any OpenAI-compatible API (OpenAI, Anthropic via proxy, DeepSeek, …)
