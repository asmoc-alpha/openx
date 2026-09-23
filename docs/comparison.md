# Comparison with Claude Code

English | [中文](comparison.zh.md)

OpenX and Claude Code share much of the same feature surface, but OpenX targets a
different architecture: a **verifiable, evolvable agent runtime** — a minimal auditable
core with everything else in replaceable plugins — rather than a clone of any single
product. How the feature surface compares today:

| Feature | Claude Code | OpenX |
|---------|-------------|-------|
| Agentic loop | ✅ | ✅ |
| File read/write/edit | ✅ | ✅ |
| Shell commands | ✅ | ✅ |
| Code search (grep/glob) | ✅ (ripgrep) | ✅ (ripgrep, pure-Python fallback) |
| Git integration | ✅ | ✅ |
| Permission system | ✅ | ✅ |
| Streaming output | ✅ | ✅ |
| Retry with backoff (429/5xx, Retry-After, mid-stream) | ✅ | ✅ |
| Rich terminal UI | ✅ | ✅ |
| Parallel tool calls | ✅ | ✅ |
| Manual mode (confirm-every-write default) | ✅ | ✅ |
| Plan mode | ✅ | ✅ |
| Subagents (Task) | ✅ | ✅ (builtin + `.openx/agents/*.md`) |
| Workflows (deterministic orchestration) | ✅ | ✅ (Python scripts, `.openx/workflows/`) |
| Hooks | ✅ | ✅ (same event schema) |
| MCP | ✅ | ✅ (stdio servers) |
| Session resume | ✅ | ✅ |
| Headless JSON output (json / stream-json, exit codes) | ✅ | ✅ |
| Background tasks | ✅ | ✅ |
| Auto-compaction | ✅ | ✅ |
| Notebook editing | ✅ | ❌ |
| Anthropic-native API format | ✅ | ✅ (anthropic-compat: official + compatible endpoints) |
| Multi-provider | ❌ (Anthropic only) | ✅ (`modelGroups`: openai-compat + anthropic-compat) |
| Open source | ❌ | ✅ (MIT) |
| Language | TypeScript | Python |

## See also

- [Subagents](subsystems/subagents.md) · [Workflows](subsystems/workflows.md) ·
  [Hooks](subsystems/hooks.md) · [MCP](subsystems/mcp.md)
- [Modes & permissions](user/guide/modes-permissions.md)
