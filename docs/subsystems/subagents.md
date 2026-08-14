# Subagents

English | [中文](subagents.zh.md)

The `task` tool delegates a self-contained piece of work to a child agent with its own
context window.

## Agent types

Builtin types:

- `general-purpose` — all tools except `task` and `ask_user`
- `explore` — read-only tools (`read_file`, `grep`, `glob`, `list_directory`, …)

Custom subagents live in `<workspace>/.openx/agents/*.md` — minimal `key: value`
frontmatter plus a body that becomes extra system prompt:

```markdown
---
name: reviewer
description: Reviews code for quality issues.
tools: read_file, grep, glob
model: gpt-4o-mini
---

You are a strict code reviewer. Focus on correctness, readability, and tests.
Report findings as a numbered list with file:line references.
```

`tools` is an optional comma-separated allowlist (omit for all tools); `model` is an
optional override (omit to inherit the parent's). Children share the parent's console,
permission rules, hooks, and background-task registry — and they cannot spawn their own
subagents (no nesting).

## Structured output

Pass the `task` tool a `schema` (a JSON Schema object) and the sub-agent is contracted
to deliver its result by calling the `structured_output` tool exactly once, with `data`
conforming to the schema — plain-text final answers are discarded. Validation failures
are reported back to the sub-agent as tool errors, so it corrects and retries within the
same run. On success the `task` tool returns the validated object as a JSON string
(instead of free text); a sub-agent that finishes without ever calling
`structured_output` is reported as a failure. In workflows, `agent(prompt, schema=...)`
hands the script the validated **Python object** directly — no parsing needed.

## Live status deck

While the agent streams, a status deck renders **under the input frame** and updates in
real time (5 Hz):

- **Plan panel** — the agent's todos as a checklist: `✓` done (green), a spinner plus
  the task's `activeForm` while in progress, `○` pending; long lists collapse to six
  rows plus `+N more`.
- **Agents rows** — one row per running sub-agent (`task` tool or workflow): spinner,
  description label, tool count, elapsed time; `✓`/`✗` when finished. Rows collapse
  past four agents.

Press **Ctrl-O** during streaming to cycle the main response area into a sub-agent's
detail view (its captured tool activity and text) and back. The deck disappears when
the turn ends; the short-terminal budget trims it before it can crowd out the response.

## See also

- [Workflows](workflows.md) — orchestrate many sub-agents deterministically
- [Modes & permissions](../user/guide/modes-permissions.md) — children snapshot the parent's mode
