# Hooks

English | [中文](hooks.zh.md)

Shell hooks divide a session into **phases** and run user-defined commands at
each of them. Hooks are configured in `~/.openx/settings.json` (global) and/or
`<workspace>/.openx/settings.json` (project-level; per-event lists extend the
global ones). The schema mirrors Claude Code:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "shell",
        "hooks": [
          {"type": "command", "command": "./guard.sh", "timeout": 30}
        ]
      }
    ]
  }
}
```

## Session phases

A session is divided into the phases below; each phase maps to one hookable
event:

| Event | Phase | Key payload fields |
| --- | --- | --- |
| `SessionStart` | session begins (process startup `startup` / history cleared `clear`) | `source` |
| `UserPromptSubmit` | before the user prompt reaches the model | `prompt` |
| `PreToolUse` | before each tool call | `tool_name`, `tool_input` |
| `PostToolUse` | after each tool call | `tool_name`, `tool_input`, `tool_response` |
| `SubagentStop` | a delegated subagent finishes (`task` tool) | `subagent_type`, `description` |
| `PreCompact` | before history compaction (manual `/compact` or automatic) | `trigger` (`manual`/`auto`) |
| `Stop` | turn ends (final reply / max rounds reached) | `stop_reason` |
| `SessionEnd` | session ends (CLI exit / serve shutdown) | `reason` |

Every payload is written to the hook process's stdin as JSON, along with
`workspace` and `session_id`.

### Blocking tiers

**Only `UserPromptSubmit` and `PreToolUse` truly block** — the former rejects
the prompt for this turn (same semantics in CLI and web), the latter rejects the
tool call. All other phase events are **notification-only**: exit 2 /
`decision: block` from those hooks is downgraded to a warning and never
intercepts the lifecycle itself — a session cannot be locked up by its own
audit scripts.

## Semantics

- `matcher` is an fnmatch pattern on the tool name (omit or `"*"` for all
  tools); only tool events (`PreToolUse` / `PostToolUse`) use matchers — other
  phase events ignore it.
- **exit 0** → allow; if stdout parses as `{"decision": "block", "reason": "…"}`,
  block or warn per the tiers above.
- **exit 2** → block; the reason is taken from stderr (consumed per the tiers).
- Timeouts kill the hook (warning only); other non-zero exits warn without
  blocking.
- Entries for the same event run in "global first, project second" order; once a
  hook blocks, the remaining hooks do not run.

Inspect configured hooks with `/hooks`. Hook failures never lock up the REPL or
a web session.

## See also

- [Configuration](../user/guide/configuration.md) — where settings files live
- [Modes & permissions](../user/guide/modes-permissions.md) — hooks complement the
  permission tiers
