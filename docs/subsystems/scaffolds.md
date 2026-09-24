# Scaffolds

English | [中文](scaffolds.zh.md)

A **scaffold** is a module that exists only to compensate for a model
shortcoming — history compression, intent routing, subagents, memory retrieval,
the harness loop itself. The design bets that these will retire as models get
stronger; a scaffold declares **why it exists** and **when it should leave**, and
when that condition holds it leaves the loaded set — without being deleted.

## Declaring a scaffold

A scaffold declares a `scaffold` block in its plugin manifest
(`__openx_meta__`), alongside the usual fields:

```python
__openx_meta__ = {
    "type": "capability.tool",
    "trust": "user",
    "summary": "Compress long histories",
    "scaffold": {
        "compensates": "the model's context window is finite",   # required
        "exit_when": "long-session eval passes without compression",  # required
        "eval_set": "evals/long-session.jsonl",                   # optional
        "fallback": "reinstall-on-regression",                    # optional
    },
}
```

`compensates` and `exit_when` are **mandatory** — a module that cannot answer
both has no business existing as a scaffold: either it belongs in the kernel or
it is an ordinary capability plugin. The kernel validates the block's **shape**
only: a missing required field rejects the plugin; an unknown `fallback` value or
extra key is a warning (the same discipline as `type` / `mount` / `permissions`).
The `eval_set` points at the retirement eval task set; `fallback`
(`reinstall-on-regression`) says how the scaffold comes back when the model
regresses.

## Retiring and restoring

```
/scaffolds                    # list declared scaffolds + their retirement status
/scaffolds retire <name>      # retire: record scaffold_retired + skip in composition
/scaffolds restore <name>     # restore: record scaffold_restored + re-include
```

Retirement is a **user-confirmed** action (never taken silently by the model).
It runs in three recorded steps:

1. **Gate** — the scaffold's `eval_set` is compared *with vs without* the
   scaffold. Removing it must not drop the success rate; a drop means it stays.
   The comparison verdict becomes the decision's evidence.
2. **Decision** — a `scaffold_retired` (or `scaffold_restored`) event lands on the
   **global ledger** (`~/.openx/ledger.jsonl`) with the declaration, the evidence
   and the attribution — so "why is this module gone" has exactly one answer.
3. **Unload** — the next composition skips the scaffold (it is not imported), but
   its code and registration remain. Restoration is one event away.

**Removal is not deletion.** A retired scaffold keeps its file, stays discoverable
and is shown as `retired` by `/plugins`; `/scaffolds restore <name>` puts it back.
Because the retired set is folded from the global ledger, the decision survives a
restart — a new process derives the same retired set from the ledger.

## Limits

- **Declaring is not evidence.** The kernel validates the block's shape only; the
  gate's success-rate comparison is what decides a retirement. The current
  release wires the decision, the retired-set bookkeeping and the gate *policy*;
  **running** the tasks in an `eval_set` (and producing the with/without metrics)
  is a separate slice.
- **No profile-linked auto-restore yet.** Automatic re-inclusion when a model
  downgrades needs the model capability profile (P6); today `/scaffolds restore`
  is the manual path.
- **Scaffolds only.** Only plugins that declare a `scaffold` block can be
  retired; ordinary capability plugins have no retirement path.

## See also

- [Extending OpenX](../cookbook/extending.md) — declare a scaffold on a custom plugin
- [Architecture](../architecture.md) — module layout
