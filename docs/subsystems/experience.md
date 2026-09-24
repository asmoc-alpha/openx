# Experience distillation

English | [中文](experience.zh.md)

A session's ledger is not just an audit trail — it is the raw material for
**experience**. The distillation loop turns it into durable memory so a lesson
learned once survives into the next session:

```
session ledger (events)
  → distill: mine candidate experiences
  → save:    write them into coding memory (human-confirmed)
  → recall:  context injection into the next session's system prompt
  → account: record which memories were recalled (memory_recall event)
```

```bash
/distill            # candidate experiences from recent sessions (human-readable)
/distill json       # the structured report (schema openx-distill-report/v1)
/distill save       # persist the candidates into coding memory (source=distill)
/distill recall      # recall accounting: which memories were injected, how often
```

## What gets distilled

`/distill` scans the most recent sessions and mines three kinds of **candidate**
experience (conservative heuristics — better to miss than to invent):

| Kind | Signal | Becomes |
|---|---|---|
| `workflow` | a shell command that succeeded repeatedly | *"Frequently used command: `pytest -q`"* |
| `debug_pattern` | a tool that first errored, then succeeded | *"`grep` failed with … — retry with adjusted args"* |
| `project_fact` | a file edited repeatedly | *"Frequently edited file: …"* |

Commands run only once are ignored; candidates are deduplicated and ordered by
kind then frequency. Candidates are **suggestions, not facts** — nothing is
persisted until you say so.

## Saving (human-confirmed)

`/distill save` writes the candidates into coding memory with
`source="distill"`. That source tag keeps distilled memories distinguishable from
model-written (`source="agent"`) and user-written ones, so they can be reviewed
or batch-removed later. Deduplication is handled by the memory store itself
(identical content updates in place).

## Recall accounting

When the agent assembles a system prompt, every coding-memory entry it includes
is recorded as a `memory_recall` event in the session ledger (id, category and
character count). `/distill recall` aggregates those events into a frequency
ranking — the data source for the "memory quality" question *"is a
frequently-recalled memory actually useful?"*. The event records recall facts
only; it makes no judgement.

## Limits

- **Evidence, not action.** Distillation proposes; a human (`/distill save`)
  disposes. Read-only over the session ledger.
- **Heuristic and conservative.** Kinds are mined from tool traffic; a command
  or file that appears once is not a candidate. Expect misses, not noise.
- **Recall accounting, not recall scoring.** `memory_recall` says what was
  injected; whether a memory *helped* is a later analysis.
- **Writes go to home.** `/distill save` writes to `~/.openx/coding-memory/`,
  never into a project `.openx`.

## See also

- [Offline analysis](offline-analysis.md) — the sibling reports (gaps, assembly)
- [Self-evolution design](../design/openx-self-evolution-design.md) — §4.4
