# Subsystems

English | [中文](README.zh.md)

One reference page per subsystem: semantics, configuration, limits. Usage guides live
in [user/](../user/index.md); step-by-step how-tos in [cookbook/](../cookbook/extending.md).

| Subsystem | Page | One-liner |
|---|---|---|
| Subagents | [subagents.md](subagents.md) | The `task` tool: builtin and custom agent types, structured output, live status deck |
| Workflows | [workflows.md](workflows.md) | Deterministic multi-agent orchestration in Python scripts |
| Background tasks | [background-tasks.md](background-tasks.md) | Detached shell commands with log tailing and stop control |
| Hooks | [hooks.md](hooks.md) | Claude-Code-compatible shell hooks on eight lifecycle events |
| MCP | [mcp.md](mcp.md) | Model Context Protocol servers over stdio, zero extra dependencies |
| Skills | [skills.md](skills.md) | `SKILL.md` instruction packs: progressive disclosure, session-scoped `allowed-tools`, on-demand loading |
| Scaffolds | [scaffolds.md](scaffolds.md) | Declare why a compensating module exists and when it retires; retire/restore with a ledger trail |
| Composition | [composition.md](composition.md) | Resolve the load bundle from model profile × user/project overlay; promotion writes back |
| Offline analysis | [offline-analysis.md](offline-analysis.md) | Cluster the session ledger into human-readable reports (gap report `/gaps`, assembly report `/assembly`) |
| Experience distillation | [experience.md](experience.md) | Mine candidate experiences from sessions into memory; recall accounting (`/distill`) |
| Recovery | [recovery.md](recovery.md) | Turn-level checkpoints: resume an interrupted turn without replaying completed tool calls |
