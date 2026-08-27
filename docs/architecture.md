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
│   ├── config.py          # Config: settings.json + config files + env merge
│   ├── permissions.py     # Permission tiers + stored allow/deny rules
│   ├── memory.py          # Persistent memory (~/.openx/memory/)
│   ├── instructions.py    # OPENX.md loading (global / project / subdir)
│   ├── image.py           # Image & clipboard helpers (multimodal)
│   ├── cli/
│   │   ├── commands.py    # Slash-command registry (27 commands)
│   │   ├── interactive.py # REPL loop + streaming display
│   │   ├── single_shot.py # One-shot mode
│   │   └── setup_wizard.py# First-run configuration wizard
│   ├── kernel/            # Microkernel: plugin registration catalog, registries, loader, inventory, ledger
│   │   └── registrations.py etc.
│   ├── builtin/           # Base-bundle builtin plugin package (tools/providers; everything-is-a-plugin)
│   │   ├── tools.py       #   Builtin tool factory
│   │   └── providers.py   #   Builtin provider implementations (openai-compat / anthropic)
│   ├── core/
│   │   ├── protocol.py    # Session protocol schema + event envelope (ledger = externalized protocol)
│   │   ├── history.py     # Conversation history + turn-based compaction
│   │   ├── hooks.py       # User hooks (Claude Code-style schema)
│   │   ├── sessions.py    # Session persistence (JSONL, --continue / --resume)
│   │   ├── tasks.py       # Background task registry
│   │   ├── subagent.py    # Subagent specs (builtin + .openx/agents/*.md)
│   │   └── workflow.py    # Workflow engine (deterministic multi-agent orchestration)
│   ├── llm/
│   │   └── client.py      # Async LLM client (OpenAI-compatible, streaming)
│   ├── mcp/
│   │   ├── transport.py   # stdio NDJSON transport (spawn + line framing)
│   │   ├── client.py      # Zero-dependency JSON-RPC client
│   │   ├── tools.py       # MCPTool wrapper (mcp__<server>__<tool>)
│   │   └── manager.py     # Server lifecycle + config loading
│   ├── tools/
│   │   ├── base.py        # Tool base class + result types
│   │   ├── file_tools.py  # read_file, write_file, edit_file, glob, list_directory
│   │   ├── shell_tools.py # shell (supports run_in_background)
│   │   ├── search_tools.py# grep
│   │   ├── git_tools.py   # git_status, git_diff, git_log, git_branch
│   │   ├── todo_tools.py  # todo_write
│   │   ├── web_tools.py   # web_fetch, web_search
│   │   ├── ask_user_tool.py # ask_user
│   │   ├── plan_tools.py  # exit_plan_mode
│   │   ├── mode_tools.py  # choose_mode (manual → auto/plan choice)
│   │   ├── task_tools.py  # task_output, task_stop
│   │   ├── subagent_tool.py # task (delegates to a subagent)
│   │   └── workflow_tool.py # workflow (runs orchestration scripts)
│   ├── services/
│   │   ├── tool_executor.py # Permission + hook gate, serial prepare → parallel execute
│   │   ├── streaming.py   # Stream display service
│   │   └── exploration.py # Project overview detection
│   ├── ui/                # Rich TUI: console, inline prompt frame, dialogs, input capture
│   └── utils/             # Path, text, and error helpers
├── tests/                 # Subdirs mirror the openx/ layout (core/ llm/ services/ tools/ ui/ mcp/)
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
| Surface | `cli/`, `ui/` | REPL, single-shot and headless entry; terminal rendering |
| Kernel | `kernel/`, `agent.py`, `services/tool_executor.py`, `services/streaming.py` | Turn loop, tool dispatch (serial prepare → parallel execute), stream display |
| Model | `llm/` | OpenAI-compatible async client, streaming, retry with backoff |
| Capabilities | `tools/`, `mcp/` | Model-facing tools (fs, shell, search, git, web, todo, plan, task, workflow) and external MCP tools |
| Context & memory | `instructions.py`, `memory.py`, `core/history.py` | OPENX.md instructions, persistent memory, history + compaction |
| Orchestration | `core/subagent.py`, `core/workflow.py`, `core/tasks.py`, `core/hooks.py` | Subagents, deterministic workflows, background tasks, lifecycle hooks |
| State | `core/sessions.py`, `config.py` | Session persistence/resume, layered configuration |
| Collaboration | `permissions.py` | Permission tiers, stored rules, dangerous-command gate |

## See also

- [Development guide](development.md) — contributor setup and daily workflow
- [User guides](user/index.md) — commands, modes & permissions, configuration, sessions
- [Subsystem reference](subsystems/README.md) — subagents, workflows, hooks, MCP, background tasks
