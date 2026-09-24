# Offline analysis (reports)

English | [中文](offline-analysis.zh.md)

The self-evolution loop turns on **evidence** — and the raw evidence is already
recorded: every session's ledger holds its transcripts, permission decisions and
cost fields. Offline analysis reads that ledger and clusters it into
**human-readable reports**. The system only ever produces evidence; the human
reads it and decides (whether to save a rule, write a plugin, or change the
assembly). Nothing here acts on its own.

```bash
/gaps            # human-readable gap report over recent sessions
/gaps context    # the same, as a context fragment you can feed back
/gaps json       # the structured report (schema openx-gap-report/v1)

/assembly              # assembly report: what's loaded vs actually used
/assembly suggestions  # tuning suggestions (human-decided)
/assembly json         # the structured report (schema openx-assembly-report/v1)
```

## Gap report (`/gaps`)

`/gaps` scans the most recent sessions of the current workspace and clusters four
kinds of recurring, actionable signal:

| Kind | Signal | Detected from |
|---|---|---|
| `tool_failure` | a tool that errors again and again | tool-result messages (`Error:` line) joined back to the tool name |
| `permission_friction` | the same tool asked-and-approved repeatedly | `permission_decision` (verdict ASK, approved) |
| `denied_calls` | the same tool blocked repeatedly | `permission_decision` (not approved) |
| `detour` | the same call (tool + args) repeated in one session | assistant `tool_calls` |

Incidental, one-off signals are filtered out by per-kind thresholds, and the
report is ordered by severity (count, then kind). Each entry carries a short
recommendation — e.g. *frequently approved — consider saving a permission rule*.

## Assembly report (`/assembly`)

`/assembly` aligns the **current composition** (`/plugins`) with **recent usage**
(the tool calls in the session ledger) to answer *"we assembled it — did anyone
use it?"*:

- **usage** — each active plugin's tool calls, ranked (the data source for
  ordering the catalog by usage frequency);
- **assembled but never used** — active, non-builtin plugins whose tools were
  never invoked; candidates to stop loading (or batch-roll-back, for `auto-*`);
- **top tools** — the most-invoked tools across sessions.

`/assembly suggestions` prints the tuning suggestions as text — e.g. *roll back
auto plugin 'x'* — for a human to act on. Only plugins that **contribute tools**
are judged "unused": a `context.memory` / `lifecycle` / `ui.panel` plugin
contributes no tools, so it is marked non-measurable rather than falsely reported
as dead.

## Feeding the report back

The report is a prompt-side artifact too: `as_context_fragment` turns it into a
compact bullet list (`/gaps context` prints it). Paste it into your `OPENX.md`,
or contribute it from a `context.memory` plugin, and the next session's system
prompt carries "here are the gaps we saw lately". An empty report yields an empty
fragment — nothing is injected when there is nothing to say.

## Limits

- **Evidence, not action.** The reports propose; a human (or a later online step,
  still gated) disposes. Neither `/gaps` nor `/assembly` writes rules, plugins or
  config — `/assembly suggestions` prints text for you to act on.
- **Read-only, offline.** They read the session ledger of the current workspace
  (plus the read-only plugin inventory) and nothing else; they never create a
  project `.openx`.
- **Heuristic error detection.** Tool failures are read from a leading `Error:`
  line in the tool-result text (the ledger does not store a separate error flag);
  pathological outputs could be misread. It is a hint, not a verdict.
- **Usage = tool calls.** The assembly report counts assistant `tool_calls`; a
  plugin that contributes no tools is marked non-measurable rather than counted
  as unused.
- **Text suggestions today.** `/assembly suggestions` emits text; overlay edit
  primitives (writing suggestions back into the composition) are a later slice.

## See also

- [Scaffolds](scaffolds.md) — the retirement gate consumes eval data
- [Self-evolution design](../design/openx-self-evolution-design.md) — the full loop
