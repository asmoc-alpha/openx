# Skills

English | [中文](skills.zh.md)

A skill is a **directory** holding a `SKILL.md` (frontmatter + instruction body) plus any
optional supporting files (scripts, templates, reference material). The layout follows the
open **Agent Skills** standard, so skills written in the `SKILL.md` format load as-is
(read-only interop) from the Claude skill directories.

```
~/.openx/skills/<name>/SKILL.md            # personal level (available in every project)
<workspace>/.openx/skills/<name>/SKILL.md  # project level (commit it to share)
```

```markdown
---
name: docker-expert
description: Best practices for Dockerfile and compose files.
allowed-tools: read_file grep
license: MIT
---
When working with Docker files, always:
- Use multi-stage builds ...
```

The frontmatter is parsed by a minimal hand-written reader (no PyYAML): one `key: value`
per line. `allowed-tools` accepts commas or whitespace; `trigger`, `license` and
`argument-hint` are optional. The **directory name is the command name** — `name:` is only
a fallback for the legacy flat layout.

## Progressive disclosure

The system prompt receives only a **catalog** — each skill's name, description, and any
pre-approved-tools / legacy marker. Skill **bodies are never injected**. The body is loaded
on demand, when the skill is actually used:

- the user types `/<skill-name>` (a dynamic command), or
- the model calls the `skill` tool.

Long reference material therefore costs no context until it is needed. Inside the body,
`$ARGUMENTS` is replaced with whatever the user (or the model) passed.

A body may also request a **dynamic context injection** with `` !`cmd` ``. Commands are
extracted at load time but **never executed then** — they run through the normal tool gate
(`shell`, i.e. Guard / permission / hooks / ledger) when the skill activates, and their
output replaces the placeholder. A failed injection degrades to an inline note; it never
blocks the skill from loading.

## Pre-approved tools (`allowed-tools`)

Activating a skill adds a **session-scoped** allow rule (`<tool>(*)`) for each listed tool.
The rule lives in memory only — it is **never written to `settings.json`**, so installing a
skill can never permanently widen permissions — and it is removed when the skill is
deactivated, uninstalled, or skills are reloaded. `deny` rules still take precedence, and
high-risk tools still always confirm.

## Precedence

Sources are layered low → high; a same-name skill from a higher source wins (project over
personal, native over interop, directory layout over legacy flat):

| Source | Level |
|---|---|
| `~/.claude/skills/<name>/SKILL.md` | `claude` (interop, read-only) |
| `<ws>/.claude/skills/<name>/SKILL.md` | `claude-project` (interop, read-only) |
| `~/.openx/skills/<name>.md` | `global` (legacy flat) |
| `<ws>/.openx/skills/<name>.md` | `project` (legacy flat) |
| `~/.openx/skills/<name>/SKILL.md` | `global` |
| `<ws>/.openx/skills/<name>/SKILL.md` | `project` |

## Managing skills

| Command | Description |
|---|---|
| `/skill` (alias `/skills`) | List installed skills with level, description, pre-approved tools, triggers |
| `/skill add` | Create one interactively (name, description, triggers, pre-approved tools, body, scope) |
| `/skill install <path>` | Install from a skill directory, a `SKILL.md`, or a legacy flat `.md` (migrated to the directory layout, supporting files copied). Add `--project` for project scope |
| `/skill show <name>` | Print a skill's metadata and body — this does **not** run injections |
| `/skill remove <name>` | Uninstall (project level first, then personal) |

Any installed skill also becomes a `/<skill-name>` command, and the model can load it with
the `skill` tool. Builtin command names are never hijacked — a skill named `help` does not
override `/help` (use `/skill show help` for it instead). The Web UI exposes the same surface
under Settings → Skills (`GET` / `POST` / `DELETE /api/skills`).

## Limits and guarantees

- **Name**: `[a-z0-9]+(-[a-z0-9]+)*`, at most 64 characters.
- **Description**: truncated to 1024 characters.
- Only the catalog enters the system prompt; bodies are loaded on demand.
- A malformed skill file is reported as a warning and skipped — **skill loading never breaks
  startup**.
- Files written for the older flat layout (`<name>.md`) are still read (tagged `legacy`) and
  are migrated to `<name>/SKILL.md` when reinstalled.

## Implementation

`openx/skills.py` — parsing, loading, catalog, install/uninstall. `openx/tools/skill_tool.py`
— the `skill` tool (the model-side entry). `openx/agent.py` — `activate_skill` /
`deactivate_skill` / `reload_skills` (the single implementation shared by `/<name>` and the
tool).

## See also

- [Commands](../user/guide/commands.md) — the full slash-command list
- [Architecture](../architecture.md) — where skills sit in the tree
