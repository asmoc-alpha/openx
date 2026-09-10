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

## Resuming an interrupted turn

If OpenX died partway through a turn — crash, power loss, Ctrl-C, `kill` — the
completed tool rounds were checkpointed as they finished. Add `--recover` to pick
the turn back up:

```bash
openx --continue --recover            # resume the latest session's interrupted turn
openx --resume <SESSION_ID> --recover
```

Recovery never replays a tool call that already ran; calls that were still in flight
when the process died are reported to the model as `[status: interrupted]` rather
than retried. It is always opt-in — resuming re-issues model requests, so it should
be a deliberate choice. Without `--recover`, a session that has an interrupted turn
loads normally and says so.

If the checkpoint cannot be used (truncated, stale, from another workspace, or
written against a different system prompt), `--recover-mode` decides what happens:

| Mode | Behaviour |
|---|---|
| `auto` (default) | Warn, then continue with the saved session |
| `strict` | Refuse and exit 1 |
| `drop` | Discard silently |

See [Recovery](../subsystems/recovery.md) for what a checkpoint contains and when it
is written.

## See also

- [Commands](commands.md) — single-shot chaining via `session_id`
- [Recovery](../subsystems/recovery.md) — turn-level checkpoints and interrupts
