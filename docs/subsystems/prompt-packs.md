# Prompt packs

English | [中文](prompt-packs.zh.md)

A prompt pack is an **on-demand system-prompt fragment** shipped with the product. The
system prompt carries only a **catalog** — one line per pack; a pack's full text enters the
prompt only once it is activated. Same philosophy as [skills](skills.md), different
ownership: skills are *user/project* instruction packs (installable, may carry supporting
files), prompt packs are *built-in* specialist contracts (how to write a plugin, …) that
travel with the release and need no installation.

This is the second channel of progressive disclosure: the model should not have to
memorize every specialist contract at boot to be correct on the rare turn that needs one.

## Built-in packs

| Pack | Covers |
|---|---|
| `plugin-authoring` | Writing an OpenX plugin or component: single-file manifest, the four `apply(ctx)` protocols, `self_test`. Body = `openx/kernel/assembly/plugin_spec.py::PLUGIN_SPEC` (the one source of truth — the same text `write_plugin`'s admission gate validates against) |

Adding one is one entry in `builtin_packs()` (`openx/prompt_packs.py`): name, one-line
summary, body, trigger keywords.

## Three activation channels

All three funnel into **one implementation**, `agent.activate_prompt_pack(name, reason=…)`,
so there is exactly one definition of "what activation does":

| Channel | Trigger | `reason` |
|---|---|---|
| Model-driven | `load_prompt` tool (the model reads the catalog line, decides it needs the body) | `tool` |
| User-explicit | `/prompt <name>` | `command` |
| Keyword | the user's message matches one of the pack's `triggers` | `keyword` |
| Fallback | `write_plugin` is called, so the contract must be in context | `write_plugin` |
| Removal | `deactivate_prompt_pack` | `deactivate` |

Activation does three things, atomically: adds the name to the session's active set,
emits a `prompt_pack` ledger event (pack, reason, body chars), and rebuilds the cached
system prompt. `_sync_system_message` then syncs it into the message sequence before the
next request — so a `load_prompt` call affects *the very next round*, not the next turn.

Activation is **idempotent**: a second `load_prompt` for the same pack returns the body
without re-emitting an event or rebuilding the prompt. Deactivation only removes the
body — the catalog stays, so the model can always load it again.

## Rendering contract

The fragment produced by the `builtin-context` plugin (`contribute_prompt_packs`,
priority 60 — after instructions 10 / memory hints 40 / skills 50):

```markdown
## Prompt Packs (loaded on demand)

The catalog below is always in context; a pack's FULL text is not — ...

- `plugin-authoring` [loaded]: how to write an OpenX plugin or component: ...

<!-- BEGIN PROMPT PACK: plugin-authoring -->
...body...
<!-- END PROMPT PACK: plugin-authoring -->
```

- Catalog always present, `[loaded]` marks the active ones;
- the fragment carries **no leading blank line / separator** — the consumer applies those
  uniformly (same contract as `format_instructions`);
- `render_prompt_packs` is a pure function: no disk reads, never raises; an unknown or
  empty registry yields the empty string ("no packs" and "no contribution" are the same
  thing to prompt assembly).

## Triggers

`triggers` are matched as **case-insensitive substrings** on the user's message. Substring
rather than tokenized, because Chinese has no spaces to split on — 「帮我写个插件」 must hit
`插件`, which is why `prompt_packs.py::PromptPackRegistry.match` lowercases the input and
requires triggers to be written lowercase (a test pins this). Keep them narrow: a false
positive costs a full body of tokens, a false negative only costs a `load_prompt` call.

## Surface

`/prompt [list|<name>|off [name]]` in the CLI. The Web surface exposes the same pair over
HTTP — `GET /api/prompts` (catalog + `active` flags, **bodies are not sent**: the UI has no
reason to pay for tokens the model may never need) and `POST /api/prompts`
(`{"name", "active"}`), which calls the same `activate_prompt_pack`, so the ledger event is
identical to the command's.

## Scope and limits

- **Session-scoped**: the active set lives in memory, is never persisted — a restart goes
  back to "catalog only", which is the point.
- **Top-level only**: a subagent gets an empty registry (capability inheritance = subset of
  the parent's), so it neither inherits nor pays for the contract. `task`/`workflow`
  children therefore have no `load_prompt` tool.
- Bodies do **not** consume the context-plugin budget separately; they ride the
  `builtin-context` fragment like any other built-in source.
- Ledger events are session events (like `memory_recall`), **not** cross-session decisions.

## Implementation

`openx/prompt_packs.py` — data model, registry, matching, catalog/body rendering.
`openx/tools/prompt_tool.py` — the `load_prompt` tool. `openx/agent.py` —
`activate_prompt_pack` / `deactivate_prompt_pack` / `auto_activate_prompt_packs` (the
single implementation all channels share). `openx/app/cli/commands.py` — `/prompt`.
`openx/builtin/context.py` — the context-v1 fragment. `openx/kernel/protocol.py` —
the `prompt_pack` ledger event.

## See also

- [Skills](skills.md) — the user/project counterpart of the same idea
- [Plugins](plugins.md) — what `plugin-authoring` teaches, and the gate it validates against
- [Commands](../user/guide/commands.md) — `/prompt` in the full slash-command list
