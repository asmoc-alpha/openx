# AGENTS.md — The documentation standard

Defines document structure and writing rules for OpenX documentation. Adapted from the
deepseek-harness documentation standard. Core rule: **each fact has one home**; elsewhere,
link there.

## Tier taxonomy

| Tier | Job | Does NOT belong there |
|---|---|---|
| Root `README.md` / `README.zh.md` | 30-second orientation: positioning, compressed features, install, quick start; the Documentation table links every page | Any subsystem detail, full command tables, configuration reference |
| `docs/architecture.md` | Ordered map: module tree, runtime loop, layer table; read before changing `openx/` | Usage guides, configuration reference |
| `docs/development.md` | Contributor setup and daily workflow: install, tests, lint, dependency constraints | Runtime behavior documentation |
| `docs/user/` | Product-facing guides: commands, modes & permissions, configuration, sessions | Extension how-tos, internal design |
| `docs/subsystems/` | One reference page per subsystem: semantics, configuration, limits | Teaching sequences, step-by-step tutorials |
| `docs/cookbook/` | Step-by-step how-tos (extending OpenX) | Design rationale |
| `docs/comparison.md` | Feature comparison with Claude Code | — |

Placement: usage behavior → `user/`; subsystem semantics → `subsystems/`; how-tos →
`cookbook/`; module structure → `architecture.md`; contributor workflow → `development.md`.

## Writing rules

- **Document current state, not change history.** No "previously / now / no longer" in
  durable prose; name the live mechanism. Change stories belong in commit messages.
- **One home per fact.** When content moves, leave a one-line link at the old location
  instead of restating it.
- **Separate guide from reference.** `user/guide/` pages follow ordered paths to outcomes;
  `subsystems/` pages are lookup references without teaching sequences.
- **Code blocks stay runnable.** Commands are copy-pasteable; JSON examples are complete
  objects, not fragments.
- **The directory tree in `architecture.md` records the current layout.** Update it in the
  same change that reshapes the package.

## Bilingual pairing

Every page exists as a pair: `xxx.md` (English) and `xxx.zh.md` (Chinese). Pairs update
together — a change to one side updates the other in the same commit.

- Header navigation: English pages start with `English | [中文](xxx.zh.md)`; Chinese pages
  start with `[English](xxx.md) | 中文`.
- Technical terms stay in English inside Chinese prose (tool, subagent, workflow, hook,
  MCP, session, …).
- `docs/AGENTS.md` (this file) is English-only.

## Indexes

- `docs/user/index.md` — user documentation index.
- `docs/subsystems/README.md` — subsystem index, one line per page.
