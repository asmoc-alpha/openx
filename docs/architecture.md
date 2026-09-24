# Architecture

English | [中文](architecture.zh.md)

OpenX is a single Python package (`openx/`) plus a root-level `tests/` suite. This page
is the ordered map of the module tree, the runtime loop, and the layer responsibilities —
read it before changing `openx/`.

## Module tree

```
openx/
├── openx/
│   ├── main.py            # CLI entry: args, setup wizard, trust, sessions, dispatch
│   ├── agent.py           # Core agent loop (stream + non-stream, plan mode, subagents)
│   ├── config.py          # Config: modelGroups (model/provider) + project non-model knobs
│   ├── permissions.py     # Permission tiers + stored allow/deny rules
│   ├── memory.py          # Persistent memory (~/.openx/memory/)
│   ├── instructions.py    # OPENX.md loading (global / project / subdir)
│   ├── image.py           # Image & clipboard helpers (multimodal)
│   ├── model_groups.py    # modelGroups schema + per-role resolution (the only model/provider config)
│   ├── coding_memory.py   # Coding memory (project conventions/decisions, project-scoped)
│   ├── skills.py          # Skills: installable Markdown instruction packs
│   ├── changelog.py       # CHANGELOG.md parsing (What's-new panel, /release-notes)
│   ├── app/
│   │   ├── cli/           # commands.py (slash registry) / interactive.py (REPL +
│   │   │                  #   streaming) / single_shot.py / setup_wizard.py
│   │   └── serve/         # Web surface (aiohttp optional dep) + static assets
│   ├── kernel/            # Microkernel (five-piece trust base)
│   │   ├── assembly/      #   ② Plugin assembler: loader/registry/manifest/protocols/composition…
│   │   ├── inventory.py   #   ② Plugin inventory: read-only loader-tree projection (/plugins)
│   │   ├── reasoning/     #   ① Reasoning core: provider/retry
│   │   ├── audit/         #   ③ Security audit: guard verdict pipeline + hooks (user hook chain)
│   │   ├── sandbox/       #   ⑤ Sandbox executor: host/protect
│   │   ├── ledger.py      #   ④ Trace: event ledger
│   │   ├── protocol.py    #   ④ Protocol face: event envelope schema (ledger externalized)
│   │   └── recovery/      #   ④ Resumability over the trace: checkpoint model/store/resume verdicts
│   ├── builtin/           # Base-bundle builtin plugin package (tools/providers; everything-is-a-plugin)
│   │   ├── tools.py       #   Builtin tool factory
│   │   └── providers.py   #   Builtin provider implementations (openai-compat / anthropic)
│   ├── orchestration/     # Hard-wired coordination (to be plugin-ized, P2+)
│   │   ├── history.py     # Conversation history + turn-based compaction
│   │   ├── sessions.py    # Session persistence (JSONL, --continue / --resume)
│   │   ├── tasks.py       # Background task registry
│   │   ├── subagent.py    # Subagent specs (builtin + .openx/agents/*.md)
│   │   ├── fleet.py       # Fleet monitor (multi-agent view)
│   │   └── workflow.py    # Workflow engine (deterministic multi-agent orchestration)
│   ├── llm/
│   │   ├── base.py        # Shared provider orchestration (chat/stream skeleton, error mapping)
│   │   ├── openai_compat.py # openai-compat provider + LLMClient compatibility facade
│   │   └── anthropic.py   # anthropic-compat provider (optional `anthropic` extra)
│   ├── mcp/
│   │   ├── transport.py   # stdio NDJSON transport (spawn + line framing)
│   │   ├── client.py      # Zero-dependency JSON-RPC client
│   │   ├── tools.py       # MCPTool wrapper (mcp__<server>__<tool>)
│   │   └── manager.py     # Server lifecycle + config loading
│   ├── tools/
│   │   ├── base.py        # Tool base class + result types
│   │   ├── file_tools.py  # read_file, write_file, edit_file, glob, list_directory
│   │   ├── fs_search.py   # code-search backend (ripgrep + pure-Python fallback)
│   │   ├── shell_tools.py # shell (supports run_in_background)
│   │   ├── search_tools.py# grep
│   │   ├── git_tools.py   # git_status, git_diff, git_log, git_branch
│   │   ├── todo_tools.py  # todo_write
│   │   ├── web_tools.py   # web_fetch, web_search
│   │   ├── ask_user_tool.py # ask_user
│   │   ├── plan_tools.py  # enter_plan_mode, exit_plan_mode
│   │   ├── mode_tools.py  # choose_mode (manual → auto/plan choice)
│   │   ├── task_tools.py  # task_output, task_stop
│   │   ├── subagent_tool.py # task (delegates to a subagent)
│   │   ├── workflow_tool.py # workflow (runs orchestration scripts)
│   │   ├── plugin_tools.py # list/load/unload/plugin_help (model-driven assembly)
│   │   ├── write_plugin_tools.py # write/test/promote_plugin (self-authored plugins)
│   │   ├── structured_output.py # structured_output (JSON-Schema result contract)
│   │   ├── memory_tool.py # memory (agent-decided remember/recall)
│   │   └── console_dialog.py # Async-first dialog channel (ask_user / plan approval)
│   ├── services/
│   │   ├── assembly.py    # Consumption-side assembly: tool instantiation, provider resolution, context/UI collection
│   │   ├── tool_executor.py # Permission + hook gate, serial prepare → parallel execute
│   │   ├── streaming.py   # Stream display service
│   │   ├── checkpoint.py  # Recovery submission policy (what/when to checkpoint)
│   │   ├── interrupt.py   # Interrupt controller (signals / Esc / client interrupt)
│   │   ├── eval_export.py # Trajectory → eval JSONL export (cost fields; /export-eval)
│   │   └── exploration.py # Project overview detection
│   ├── ui/                # Rich TUI: console, inline prompt frame, dialogs, input capture
│   └── utils/             # Path, text, and error helpers
├── tests/                 # Subdirs mirror the openx/ layout (orchestration/ kernel/ serve/ llm/ services/ tools/ ui/ mcp/)
├── docs/
├── pyproject.toml
└── README.md
```

## How a turn works

1. **User sends a message** — natural language request.
2. **Agent explores** — reads files, searches code, lists directories.
3. **Agent plans** — uses LLM reasoning to determine next steps.
4. **Agent executes** — calls tools; independent calls run in parallel via
   `asyncio.gather` after serial preparation (parsing, validation, permission prompts).
5. **Loop** — feeds results back to the LLM, continues until the task is complete.
6. **Responds** — final text response with a summary of what was done.

Permission checks and hook invocations happen during serial preparation, inside
`services/tool_executor.py`; only execution fans out.

## Layers

| Layer | Modules | Responsibility |
|---|---|---|
| Surface | `app/cli/`, `app/serve/`, `ui/` | REPL, single-shot, headless and web entry; terminal rendering |
| Kernel | `kernel/` (five-piece: assembly/reasoning/audit/trace/sandbox, incl. protocol & hooks), `agent.py`, `services/tool_executor.py`, `services/streaming.py` | Turn loop, tool dispatch (serial prepare → parallel execute), stream display, verdicts & ledger |
| Model | `llm/` | Provider implementations (openai-compat / anthropic-compat), streaming; retry/backoff lives in the kernel (`kernel/reasoning/`) |
| Capabilities | `tools/`, `mcp/` | Model-facing tools (fs, shell, search, git, web, todo, plan, task, workflow) and external MCP tools |
| Context & memory | `instructions.py`, `memory.py`, `orchestration/history.py` | OPENX.md instructions, persistent memory, history + compaction |
| Orchestration | `orchestration/subagent.py`, `orchestration/workflow.py`, `orchestration/tasks.py`, `orchestration/fleet.py` | Subagents, deterministic workflows, background tasks (hard-wired, P2+ plugin-ization) |
| State | `orchestration/sessions.py`, `config.py`, `kernel/recovery/` | Session persistence/resume, layered configuration, turn-level checkpoints & interrupt recovery |
| Collaboration | `permissions.py` | Permission tiers, stored rules, dangerous-command gate |

## Code search

`grep` and `glob` share one backend, `tools/fs_search.py`, which has two engines:

- **ripgrep** (`rg`) when it is on `PATH` (or `OPENX_RIPGREP` points at it) —
  parallel, respects `.gitignore`/`.ignore`, skips binaries. Auto-detected,
  never a hard dependency.
- **pure Python** otherwise — `git ls-files` for authoritative ignore semantics
  (inside a git repo), an expanded build/cache prune set, and a thread pool so
  the search never blocks the event loop.

The engine is picked by `search_backend` (`auto` | `ripgrep` | `python`, via
config or `OPENX_SEARCH_BACKEND`); `respect_gitignore` (default on) toggles the
git-aware filtering. Both engines emit the same `path:line: text` output, capped
at 500 matches.

## See also

- [Development guide](development.md) — contributor setup and daily workflow
- [User guides](user/index.md) — commands, modes & permissions, configuration, sessions
- [Subsystem reference](subsystems/README.md) — subagents, workflows, hooks, MCP, background tasks, recovery
