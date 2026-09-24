# Changelog

All notable changes to OpenX. One `## <version> — <title>` section per release,
newest first; parsed at runtime by `openx/changelog.py` into the startup panel
and `/release-notes`.

## 0.1.8 — Assembly report

### Align the loaded composition with actual usage

- Added `/assembly`: an offline report that pairs the current composition
  (`/plugins`) against recent usage (the tool calls in the session ledger). It
  ranks active plugins by how often their tools were invoked, lists
  **assembled-but-never-used** plugins (active, non-builtin, zero calls — stop
  loading them, or batch-roll-back `auto-*`), and shows the top tools
- `/assembly suggestions` prints tuning suggestions as text — e.g. *roll back
  auto plugin 'x'* — for a human to act on; `/assembly json` prints the
  structured report (`openx-assembly-report/v1`)
- Plugins that contribute no tools (`context.memory` / `lifecycle` / `ui.panel`)
  are marked non-measurable rather than falsely reported as unused
- Same discipline as `/gaps`: offline, read-only, **evidence not action** — it
  never writes rules, plugins or config. Writing suggestions back into the
  composition (overlay) is a later slice

## 0.1.7 — Gap report

### Turn the session ledger into a human-readable gap report

- Added `/gaps`: an offline analyzer over recent sessions that clusters four
  kinds of recurring, actionable failure signal — `tool_failure` (a tool that
  keeps erroring), `permission_friction` (the same tool asked-and-approved
  repeatedly), `denied_calls` (the same tool blocked repeatedly) and `detour`
  (the same call repeated in one session). Incidental signals are filtered by
  per-kind thresholds and entries are ordered by severity
- `/gaps context` prints the report as a compact **context fragment** you can
  paste into `OPENX.md` (or contribute from a `context.memory` plugin) to feed
  the gaps back into the next session's system prompt; `/gaps json` prints the
  structured report (`openx-gap-report/v1`)
- The report is **evidence, not action**: it only reads the current workspace's
  session ledger (offline, read-only) and never writes rules, plugins or config.
  Tool-failure detection is a heuristic (a leading `Error:` line in the
  tool-result text)

## 0.1.6 — Scaffold retirement

### Scaffolds can now retire, and the decision is recorded

- Added scaffold **retirement**: `/scaffolds retire <name>` records a
  `scaffold_retired` decision on the global ledger (with the declaration and the
  eval evidence) and the next composition **skips** the scaffold — it is not
  imported, so it no longer contributes, but its code and registration remain.
  Removal is not deletion: `/scaffolds restore <name>` records
  `scaffold_restored` and puts it back
- `/scaffolds` lists every declared scaffold (compensates / exit_when / eval_set)
  with its retirement status. Only plugins that declare a `scaffold` block (E1)
  can be retired, and retirement is **user-confirmed** — it is never taken
  silently by the model
- The retired set is **derived from the global ledger** (single source of truth),
  so a retirement survives a restart — a new process folds the same set from the
  ledger. `/plugins` shows retired scaffolds as `retired`; their declaration stays
  visible (backfilled from the ledger decision)
- Added the retirement-gate **policy** (`services/retirement_gate.py`): a
  with-vs-without success comparison yields a `retire` / `keep` verdict and the
  evidence payload the decision carries. This release wires the decision, the
  retired-set bookkeeping and the gate policy; **running** the tasks in an
  `eval_set` (and the profile-linked auto-restore) is a separate slice

## 0.1.5 — Scaffold retirement declarations

### Every scaffold declares why it exists and when it should retire

- Added an optional `scaffold` block to the plugin manifest (`__openx_meta__`):
  `compensates` (which model shortcoming the module offsets) and `exit_when`
  (when it should leave), plus optional `eval_set` (the retirement eval task set)
  and `fallback` (how it comes back when the model regresses). `compensates` and
  `exit_when` are **mandatory** — per v4.1 standard three, a module that cannot
  answer both has no business existing as a scaffold: either it belongs in the
  kernel or it is an ordinary capability plugin
- The kernel validates the block's **shape** only: a missing required field
  rejects the plugin; an unknown `fallback` value or extra key is a warning
  (the same discipline as `type` / `mount` / `permissions`)
- Surfaced read-only through `/plugins`, `list_plugins` (a `⚑scaffold` marker on
  the directory row) and `plugin_help` (the full declaration). This is the input
  the retirement eval gate consumes — declaring a scaffold is not evidence by
  itself

## 0.1.4 — Trajectory cost fields & eval export

### Every turn records its cost

- Added a per-turn `turn_usage` ledger event carrying the turn's **token delta**
  (input / output / cached / plugin) and its wall-clock duration. It is written by the
  top-level session agent at turn end — including when the turn is interrupted or
  raises — so a turn that went wrong still leaves its cost in the trajectory
- Subagents don't emit it: they share the process-level kernel, so without that guard
  their usage would be attributed to the parent session's ledger
- This is the quantitative baseline the self-evolution tuning line needs (context
  budgeting, compaction payoff). Token cost only — **money cost is deferred** until a
  pricing source exists

### Export trajectories as an eval set

- Added `/export-eval [path]` — exports every session of the current workspace to a
  JSONL eval set (one session per line): each turn's user input, assistant output,
  tools called and cost fields, plus per-session totals. This is the input a
  retirement eval gate needs, drawn from the ledger that already exists
- Default output is `~/.openx/eval-export.jsonl`; sessions with no recorded turns are
  skipped

## 0.1.3 — Global decision ledger

### Cross-session decisions have one home

- Added a **global decision ledger** at `~/.openx/ledger.jsonl`. Promotions, rollbacks,
  scaffold retirement and ratchet tightening are *cross-session* facts — filing them
  under whichever session happened to be running misattributes them. They now land in
  one authoritative place, and the session ledger keeps a `decision_ref` pointing at
  the global entry instead of copying its content
- Added `/ledger` — the read surface for that ledger: the most recent decisions with
  their attribution (what, which session, when), plus an integrity check
- The global ledger reuses the session ledger's `seq` + hash chain, and the chain
  **continues across processes**: a restart resumes both `seq` and `digest` from the
  existing file, so a tampered middle entry is reported rather than silently
  re-anchored from empty
- Decisions wired today: `plugin_promoted` (promotion is now recorded globally) and
  `plugin_rolled_back` (unloading a previously promoted plugin). The `scaffold_*` and
  `ratchet_tightened` emitters arrive with the retirement-eval slice

## 0.1.2 — Recovery checkpoints, interrupt memory & auto plan mode

### Recovery: turn-level checkpoints

- Added turn-level checkpoints — after **every tool round** OpenX persists the
  in-flight turn, so a crash, power loss, Ctrl-C or `kill` no longer throws away
  the work already done. Previously a session was only saved at turn boundaries
- Added `--recover` (with `--continue` / `--resume`) to resume an interrupted turn,
  and `--recover-mode {auto,strict,drop}` to choose what happens when the
  checkpoint is unusable
- Completed tool calls are **never replayed**. A checkpoint is committed only once
  every tool result for the round is in the message log, so the log itself is the
  idempotency unit — no skip-list, no retry heuristics
- Calls that were still in flight when the process died are reported to the model
  as `[status: interrupted]` instead of being re-run, since their side effects are
  genuinely unknown
- Added the `on_checkpoint` / `on_resume` plugin lifecycle hooks — declared since
  the lifecycle protocol landed, now actually triggered
- Added the `checkpoint`, `checkpoint_discarded`, `turn_started` and `resume`
  ledger events, and implemented the `interrupt` / `resource_gate_tripped` events
  that were already reserved in the kernel design
- Interrupts are additive to the existing exit paths: Ctrl-C still raises
  `KeyboardInterrupt` and `SIGTERM` still terminates normally — OpenX only flushes
  the in-flight turn before the process unwinds. Esc and the web client interrupt
  reuse the existing cancellation path
- Fixed the ledger hash chain restarting whenever a session was resumed — `seq`
  continued but `digest` started over from empty, which broke the chain exactly
  when it was needed to prove the history was unmodified

### Interrupts keep their context

- Esc, Ctrl-C and the web client interrupt no longer erase the turn they cut short.
  The interrupted turn — **including the message you typed** — is truncated at the
  last legal point and folded into the conversation and the session file, so the next
  message still has something to refer to. Previously the whole turn was dropped, and
  a follow-up like "继续" had nothing to continue from
- Tool calls that were still in flight are recorded as `[status: interrupted]` — the
  same wording the `--recover` path uses — so the model knows the outcome is unknown
  and does not replay them
- A still-running tool now shows a spinner and a ticking elapsed time
  (`⎿ ✢ Running… · 12s`) instead of a frozen `Running…`, so a long command reads as
  "still working" rather than "stuck". The scrollback form stays static — a frozen
  frame there would read as a broken glyph

### Plan mode

- Added the `enter_plan_mode` tool: for a **complex** task — coordinated changes across
  several files, a new feature, a refactor, a migration, or an approach that needs your
  sign-off — the model now switches straight into plan mode as its first action instead
  of asking which mode you want. It announces the switch in one line, explores
  read-only, and submits the plan through `exit_plan_mode`, so the approval point is the
  plan itself. Small, obvious single-file changes never take this path
- Available in manual and auto mode; disabled in single-shot / headless runs, where
  nobody can approve a plan — the tool is not even offered to the model there
- Fixed the system prompt going stale mid-turn: mode switches, plugin load/unload and
  instruction reloads now reach the message sequence on the next round. Until now a
  turn that switched into plan mode lost the write tools but kept the old prompt, so
  the model never learned it was supposed to explore and submit a plan

### Web UI

- Added file uploads and image attachments to the web chat
- Added graph rendering and artifact/path panels
- Added the three-pane layout, with collapsible sidebars
- Added hook activity to the web surface
- Unified elapsed-time formatting with the CLI (`12ms` → `1.5s` → `1m 2s` → `1h 2m`) —
  the web previously had no sub-second or hour tier

### CLI

- Fixed thinking output not being shown for resumed sessions
- Added the current tool (`name(summary)`) to the fleet view so the deck shows
  what each agent is doing right now
- Tool calls are now grouped: a run of consecutive read-only exploration calls collapses
  into one summary line (`Read 2 files, ran 1 command`) instead of scrolling the whole
  transcript away, while writes, `task`, `todo` and `workflow` calls stay individually
  visible — anything that changes something must be readable line by line. A single call
  is never collapsed, and `Ctrl+T` expands a group until it is committed to scrollback
- Fixed the plan/queue deck crowding the line below it — the spinner now sits one blank
  line further down, and that gap is charged to the viewport budget

### Docs

- Repositioned the README and pyproject summary around the microkernel story: a minimal
  trusted core with the agent loop, providers, context and tools as replaceable plugins

### Search

- `web_search` now routes CJK queries to the Chinese-language region (affects
  ranking only) and detects rate-limit / anomaly challenge pages as backend
  failures, so they fall through the degradation chain instead of being reported
  as "no results"

### Install

- Added `install.sh` — install with
  `curl -fsSL https://raw.githubusercontent.com/asmoc-alpha/openx/main/install.sh | bash`;
  prefers pipx, then uv, then a venv at `~/.openx/venv`, and honours `OPENX_REF`
  (git ref) and `OPENX_EXTRAS` (e.g. `web`)

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
