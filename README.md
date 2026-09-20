<div align="center">

<img src="assets/logo.svg" alt="OpenX mascot" width="160">

# OpenX

</div>

[![tests](https://github.com/asmoc-alpha/openx/actions/workflows/test.yml/badge.svg)](https://github.com/asmoc-alpha/openx/actions/workflows/test.yml)

English | [中文](README.zh.md)

**A verifiable, evolvable agent runtime — chat with your codebase using LLMs.**

OpenX is an open-source agent runtime for the terminal, built in Python — designed to be
**verifiable and evolvable**. A minimal trusted core (the microkernel: orchestration,
sandboxed execution, plugin maintenance, and audit) stays fixed, while tools, commands,
context and provider protocols live in replaceable plugins the agent can extend at
runtime. Give it a task in natural language; it reads, writes, edits, and searches your
code — with permission controls, sessions, subagents, workflows, and MCP support.

## Features

- 🧠 Agentic loop with parallel tool execution and automatic retry with backoff
- 🛡️ Three permission modes: `manual` / `auto` / `plan` — a complex task switches into
  plan mode on its own and hands you a plan to approve before anything is written
- 🤖 Subagents — builtin types plus custom `.openx/agents/*.md`, optional structured output
- 🔁 Workflows — deterministic multi-agent orchestration in Python scripts
- 🧩 Plugins — tools, commands, contexts and providers load and unload at runtime; the
  agent can write, test and promote its own. Loading, writing and promoting each ask for
  your approval first
- 🪝 Hooks — Claude-Code-compatible shell hooks at every session phase (SessionStart/UserPromptSubmit/tool use/SubagentStop/PreCompact/Stop/SessionEnd)
- 🔌 MCP — stdio servers, zero extra dependencies
- 💾 Sessions — persistence and resume (`--continue` / `--resume`). Turn-level
  checkpoints persist the turn in flight, so `--recover` can pick up a turn killed by a
  crash, `kill` or Ctrl-C (opt-in, never automatic)
- 🌐 Web UI — `openx serve` for browser chat, remote plan/permission approval and
  session replay (needs `OPENX_EXTRAS=web`)
- 🌗 Background tasks · 🗜️ auto-compaction · 🧠 persistent memory
- 📤 Headless JSON output for CI (`--output-format json` / `stream-json`)
- 🖼️ Image analysis · 🎨 rich terminal UI with markdown and syntax highlighting —
  runs of read-only tool calls collapse into one summary line (`Ctrl+T` expands)
- 🔐 Three-tier permission system · 📁 workspace scoping
- ⚙️ Works with any OpenAI-compatible API; Anthropic-compatible endpoints via the
  optional `anthropic` extra

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/asmoc-alpha/openx/main/install.sh | bash
```

The installer picks pipx, then `uv`, then a virtualenv at `~/.openx/venv`, and
verifies the result. Set `OPENX_EXTRAS=web` to include the web UI
(`curl … | OPENX_EXTRAS=web bash`), or `OPENX_REF=<git-ref>` to install something
other than the pinned release tag (`v0.1.2` by default).

From a clone, instead:

```bash
git clone https://github.com/asmoc-alpha/openx.git
cd openx
pip install -e .
```

Or pin the release straight from git — this is what the installer runs underneath:

```bash
pip install "openx @ git+https://github.com/asmoc-alpha/openx.git@v0.1.2"
```

> **Do not `pip install openx`.** The `openx` name on PyPI belongs to an unrelated
> project; OpenX is distributed through the git source above.

Requires Python ≥ 3.10. On first run, `openx` launches an interactive setup wizard that
writes a `default` model group to `~/.openx/settings.json`. Model & provider
configuration lives only in that file's `modelGroups` block — each group can share a
key/endpoint and define per-role models (`main`/`exec`/`mini`/`modal`). Keys may
reference the environment via `env:VAR`:

```bash
export OPENAI_API_KEY=sk-your-key-here   # group's "apiKey": "env:OPENAI_API_KEY"
```

See [Configuration](docs/user/guide/configuration.md) for the schema.

## Quick Start

```bash
openx                                          # interactive REPL
openx "add type hints to all functions in src/"   # single-shot mode
openx --workspace /path/to/project "explain this codebase"
openx --continue                               # resume the most recent session
openx --continue --recover                     # pick up a turn a crash/Ctrl-C interrupted
openx serve                                    # web UI (needs OPENX_EXTRAS=web)
openx "fix the failing test" --output-format json   # headless / CI
```

`openx --version` prints the version. Model selection is a slash command (`/model`),
not a launch flag — `--model` was removed in 0.1.1.

All flags and slash commands: [docs/user/guide/commands.md](docs/user/guide/commands.md).

## Documentation

| Page | Covers |
|---|---|
| [User guides](docs/user/index.md) | commands, modes & permissions, configuration, sessions |
| [Subsystem reference](docs/subsystems/README.md) | subagents, workflows, background tasks, hooks, MCP, recovery |
| [Web UI (`openx serve`)](docs/openx-serve.md) | browser chat, remote approval, session replay |
| [Architecture](docs/architecture.md) | module tree and runtime loop |
| [Development](docs/development.md) | contributor setup, tests, lint |
| [Cookbook](docs/cookbook/extending.md) | extending OpenX with custom tools |
| [Comparison](docs/comparison.md) | how OpenX compares to Claude Code |
| [Changelog](openx/CHANGELOG.md) | release history (data source of `/release-notes`) |

## Development

Start with the [development guide](docs/development.md) and
[architecture documentation](docs/architecture.md).

For agents, follow [docs/AGENTS.md](docs/AGENTS.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and PR guidelines. PRs welcome! Areas to help:

- More tools (linting, testing frameworks, package managers)
- Notebook editing support
- More provider protocol families — openai-compat & anthropic-compat are built in
- HTTP/SSE MCP transport (currently stdio only)
- Process-isolated plugin execution — plugin code currently runs in-process

## License

MIT — see [LICENSE](LICENSE).
