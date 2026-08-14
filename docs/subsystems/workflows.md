# Workflows

English | [中文](workflows.zh.md)

Workflows are Python scripts that orchestrate multiple sub-agents **deterministically** —
fan-out searches, parallel reviews, staged pipelines — with ordinary Python control flow
instead of model improvisation (a Python-native adaptation of Claude Code's Workflow
tool). A workflow defines an optional `meta` dict and an async `main` entry point that
receives five hooks:

| Hook | Signature | Behavior |
|------|-----------|----------|
| `agent` | `await agent(prompt, label=None, phase=None, subagent_type="general-purpose", schema=None)` | Runs one sub-agent, returns its final text — or, when `schema` (a JSON Schema) is given, the **validated Python object** the sub-agent delivered via `structured_output` (`None` on failure or unfulfilled schema) |
| `parallel` | `await parallel([lambda: agent(...), ...])` | Barrier: thunks run concurrently, results in original order, failed thunk → `None` |
| `pipeline` | `await pipeline(items, stage1, stage2, ...)` | Each item runs through all stages independently (**no barrier** between stages); stages are called as `stage(prev_result, original_item, index)`, failed item → `None` |
| `phase` | `phase(title)` | Records a phase marker (stats + dim progress line) |
| `log` | `log(message)` | Emits a dim progress line |

Saved workflows live in `<workspace>/.openx/workflows/<name>.py`:

```python
# .openx/workflows/review.py
meta = {
    "name": "review",
    "description": "Review changed files and verify findings",
    "phases": [{"title": "Review"}, {"title": "Verify"}],
}

async def main(agent, parallel, pipeline, phase, log, args):
    phase("Review")
    findings = await parallel([
        lambda: agent("Review module X for bugs", label="review:x"),
        lambda: agent("Review module Y for bugs", label="review:y"),
    ])
    phase("Verify")
    verified = await pipeline(
        [f for f in findings if f],
        lambda f, orig, i: agent(f"Verify this finding: {orig[:200]}"),
    )
    log(f"done: {len([v for v in verified if v])} verified")
    return {"findings": findings, "verified": verified}
```

Run one with `/workflow review` (plain `/workflow` lists what is saved), or let the
agent run one inline through the `workflow` tool: `script` = inline source, `name` =
saved workflow, and optional `args` (any JSON) is passed straight to `main`. The tool
returns `main`'s return value as JSON plus a stats footer (agents run/failed, tokens,
elapsed).

## Semantics and limits

- **Concurrency** is capped at `max(2, min(16, cpu_count − 2))` concurrent agents
  (Claude Code's formula), with a 500-agents-per-run backstop that aborts runaway
  scripts.
- **Trust**: workflow scripts run unsandboxed with full local access — the same trust
  level as shell. The `workflow` tool always asks for permission before executing.
- All concurrent sub-agents of one run share a single prompt lock, so interactive
  permission prompts never overlap on the terminal.
- **v1 limits**: no resume caching, no budget object — yet.

## See also

- [Subagents](subagents.md) — what each `agent(...)` call spawns
- [Background tasks](background-tasks.md) — the shell-side counterpart of async work
