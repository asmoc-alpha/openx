# Comparison with Claude Code

English | [中文](comparison.zh.md)

OpenX is a Python re-implementation of the Claude Code experience against any
OpenAI-compatible API. Feature parity today:

| Feature | Claude Code | OpenX |
|---------|-------------|-------|
| Agentic loop | ✅ | ✅ |
| File read/write/edit | ✅ | ✅ |
| Shell commands | ✅ | ✅ |
| Code search (grep/glob) | ✅ | ✅ |
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
| Anthropic-native API format | ✅ | ❌ (OpenAI-compatible endpoints only) |
| Multi-provider | ❌ (Anthropic only) | ✅ (OpenAI-compatible) |
| Open source | ❌ | ✅ (MIT) |
| Language | TypeScript | Python |

## See also

- [Subagents](subsystems/subagents.md) · [Workflows](subsystems/workflows.md) ·
  [Hooks](subsystems/hooks.md) · [MCP](subsystems/mcp.md)
- [Modes & permissions](user/guide/modes-permissions.md)
