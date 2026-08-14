# Hooks

English | [中文](hooks.zh.md)

Shell hooks run at four events — `PreToolUse`, `PostToolUse`, `UserPromptSubmit`,
`Stop` — configured in `~/.openx/settings.json` (global) and/or
`<workspace>/.openx/settings.json` (project-level; per-event lists extend the global
ones). The schema mirrors Claude Code:

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

## Semantics

- `matcher` is an fnmatch pattern on the tool name (omit or `"*"` for all tools);
  only tool events use matchers.
- The event payload is written to the hook's stdin as JSON.
- **exit 0** → allow; if stdout parses as `{"decision": "block", "reason": "…"}` the
  call is blocked.
- **exit 2** → block; the reason is taken from stderr.
- Timeouts kill the hook (warning only); other non-zero exits warn without blocking.

Inspect configured hooks with `/hooks`. Hook failures never lock up the REPL.

## See also

- [Configuration](../user/guide/configuration.md) — where settings files live
- [Modes & permissions](../user/guide/modes-permissions.md) — hooks complement the
  permission tiers
