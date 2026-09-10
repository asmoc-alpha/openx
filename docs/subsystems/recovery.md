# Recovery (checkpoints)

English | [中文](recovery.zh.md)

A turn that runs tools can take a long time. If the process dies partway through —
a crash, a power cut, Ctrl-C, `kill` — the work done so far used to be lost
entirely: OpenX persists a session at **turn boundaries only**, and a partial turn
is simply discarded.

Recovery closes that window. After **every tool round**, OpenX writes a checkpoint
holding the turn's in-flight messages. Restart and resume with `--recover`, and the
completed rounds are restored — **without replaying tool calls that already ran**.

```bash
openx --continue --recover          # resume the most recent session's turn
openx --resume <session-id> --recover
```

## What a checkpoint holds

A checkpoint is a sidecar file next to the session ledger:

```
~/.openx/sessions/<workspace-hash>/<session-id>.jsonl       ← the session ledger
~/.openx/sessions/<workspace-hash>/<session-id>.ckpt.json   ← the checkpoint
```

The sidecar carries the **snapshot body** — the current turn's messages exactly as
the provider saw them, the tool-round count, todos, and token counters. The ledger
carries the **facts**: a `checkpoint` event per write, recording the phase, the
reason, and a digest of the snapshot. The two reference each other, so a tampered
or truncated file on either side is detectable.

The ledger also stays small this way: a checkpoint's body can hold megabytes of tool
output, and the web replay view streams every ledger payload — inlining snapshots
into the event stream would bloat each replay.

## Why completed calls are never replayed

A checkpoint is committed only **after** all tool results for a round have been
appended to the message list. At that instant every `tool_call` has a matching
`tool` result, so the message log itself is the idempotency unit — there is no
"skip list" anywhere in the loop. Resume re-enters the loop with that prefix and the
model cannot re-issue an already-answered call.

Two phases are written per round:

| Phase | Written | Meaning |
|---|---|---|
| `inflight` | before tools execute | these calls are running; their outcome is unknown |
| `committed` | after results are appended | the round is complete and safe |

If the process dies **during** tool execution, the calls in flight are genuinely
unknown — a write may have landed, a request may have been sent. Recovery does not
guess and does not retry: it synthesizes a `[status: interrupted]` result for each
such call so the model can see what happened and decide for itself. That keeps the
no-replay promise absolute rather than approximate.

## When checkpoints are written

| Trigger | Result |
|---|---|
| Each tool round | A checkpoint is committed |
| Turn ends normally | The sidecar is deleted — no stale checkpoint is the default state |
| Ctrl-C / `SIGTERM` / Esc / web interrupt | The in-flight turn is flushed before the process unwinds |
| Tool-round limit reached | A `resource_gate_tripped` event plus a resumable checkpoint |

Because a checkpoint is already on disk after every round, `kill -9` and power loss
recover to the last completed round without any signal handling at all. Signals only
add the current partial round and the audit record of *why* the turn stopped.

## Unusable checkpoints

Recovery never blocks startup. If a checkpoint is missing, stale, truncated,
belongs to another session or workspace, or was written against a different system
prompt (plugins loaded or unloaded in between), it is discarded and the session
resumes without the interrupted turn. `--recover-mode` decides how loud that is:

| Mode | Behaviour |
|---|---|
| `auto` (default) | Warn, then continue with the saved session |
| `strict` | Refuse and exit 1 — for scripts that require resumability |
| `drop` | Discard silently |

A checkpoint is also dropped without comment when the turn it describes already
landed in the session file — that is the normal outcome of a crash between
persisting the turn and deleting the sidecar.

## Writing a plugin that observes checkpoints

Plugins can register `on_checkpoint` / `on_resume` lifecycle hooks:

```python
ctx.register_lifecycle(
    "my-state",
    on_checkpoint=lambda payload: flush_my_state(),
    on_resume=lambda payload: reload_my_state(),
)
```

The payload is a dict with `reason`, `phase`, `ledger_seq`, `tool_rounds`,
`session_id`, `workspace` for checkpoints, plus `repaired_calls`, `todos`, and
`counters` on resume. Hooks that take no arguments keep working — the payload is
only passed when the hook accepts it.

These hooks are **observers and self-flushers, not mutators**: they must not write
the checkpoint (the kernel owns that), must not mutate `todos`, and must return
quickly — this runs on the turn's critical path. On the `SIGINT` path an async hook
may not get to finish before the process exits; anything that must be durable
should be written by the plugin itself at this boundary.

## Limits

- Checkpoints are capped at 4 MiB. An oversize snapshot is **skipped, never
  truncated** — a cropped snapshot would rebuild a different prompt than the one
  the model actually saw. Recovery then anchors to the previous round.
- A checkpoint write failure is always silent and never interrupts the turn;
  persistence is an optimisation, not a critical path.
- Recovery is opt-in. A turn is never resumed automatically, because resuming
  re-issues model requests.
- Subagents never checkpoint — their intermediate work deliberately stays out of
  the session file.
- The checkpoint is not a replacement for the session ledger: the ledger is the
  append-only audit truth, the sidecar is its resumable index.

## See also

- [Sessions](../user/guide/sessions.md) — session persistence and `--continue` / `--resume`
- [Modes & permissions](../user/guide/modes-permissions.md) — checkpointing never changes what a tool may do
- [Hooks](hooks.md) — the user-facing shell hook chain
- [Background tasks](background-tasks.md) — detached work that survives a turn
