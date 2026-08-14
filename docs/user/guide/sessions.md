# Sessions

English | [中文](sessions.zh.md)

Every conversation is persisted as append-only JSONL at
`~/.openx/sessions/<workspace-hash>/<session-id>.jsonl` (message events plus periodic
metadata: token counters, todos, first user message). Images are stored as
placeholders, never base64.

```bash
openx --continue              # resume the latest session for this workspace
openx --resume                # interactive picker over this workspace's sessions
openx --resume <SESSION_ID>   # resume one specific session
```

Resumed history is cleaned of orphaned tool messages before being replayed to the
model.

## See also

- [Commands](commands.md) — single-shot chaining via `session_id`
