<div align="center">

<img src="assets/logo.svg" alt="OpenX mascot" width="160">

# OpenX

</div>

[![tests](https://github.com/asmoc-alpha/openx/actions/workflows/test.yml/badge.svg)](https://github.com/asmoc-alpha/openx/actions/workflows/test.yml)

English | [中文](README.zh.md)

**Agentic coding CLI — chat with your codebase using LLMs.**

OpenX is an open-source coding agent for the terminal, built in Python. Give it a task
in natural language; it reads, writes, edits, and searches your code — with permission
controls, sessions, subagents, workflows, and MCP support.

## Features

- 🧠 Agentic loop with parallel tool execution and automatic retry with backoff
- 🛡️ Three permission modes: `manual` / `auto` / `plan`
- 🤖 Subagents — builtin types plus custom `.openx/agents/*.md`, optional structured output
- 🔁 Workflows — deterministic multi-agent orchestration in Python scripts
- 🪝 Hooks — Claude-Code-compatible shell hooks on tool use, prompts, and stop
- 🔌 MCP — stdio servers, zero extra dependencies
- 💾 Sessions — persistence and resume (`--continue` / `--resume`)
- 🌗 Background tasks · 🗜️ auto-compaction · 🧠 persistent memory
- 📤 Headless JSON output for CI (`--output-format json` / `stream-json`)
- 🖼️ Image analysis · 🎨 rich terminal UI with markdown and syntax highlighting
- 🔐 Three-tier permission system · 📁 workspace scoping
- ⚙️ Works with any OpenAI-compatible API (multi-provider)

## Install

```bash
git clone https://github.com/asmoc-alpha/openx.git
cd openx
pip install -e .

# Or from PyPI (coming soon)
pip install openx
```

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
openx "fix the failing test" --output-format json   # headless / CI
```

All flags and slash commands: [docs/user/guide/commands.md](docs/user/guide/commands.md).

## Documentation

| Page | Covers |
|---|---|
| [User guides](docs/user/index.md) | commands, modes & permissions, configuration, sessions |
| [Subsystem reference](docs/subsystems/README.md) | subagents, workflows, background tasks, hooks, MCP |
| [Web 端（openx serve）](docs/openx-serve.md) | browser chat, remote approval, session replay |
| [Architecture](docs/architecture.md) | module tree and runtime loop |
| [Development](docs/development.md) | contributor setup, tests, lint |
| [Cookbook](docs/cookbook/extending.md) | extending OpenX with custom tools |
| [Comparison](docs/comparison.md) | feature parity with Claude Code |
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
- Plugin system

## License

MIT — see [LICENSE](LICENSE).
