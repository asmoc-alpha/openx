# Modes & permissions

English | [中文](modes-permissions.zh.md)

## Permission modes

OpenX starts in **manual** mode and switches via `/mode [manual|auto|plan]`
(no argument prints the current mode).

| | manual (default) | auto | plan |
|---|---|---|---|
| Read-only tools (read_file, grep, glob, list_directory, git_*, web_*) | run free | run free | run free |
| Write tools (write_file, edit_file, shell, workflow, MCP) | **confirm every call** — stored rules, the shell whitelist and `-y` are ignored | normal flow: ask unless a stored rule / whitelist / `-y` allows | hidden from the model **and** hard-gated at the executor |
| Dangerous shell commands (`config.dangerous_commands`: rm -rf, sudo, mkfs…) | always confirm | **always confirm** — never skipped by rules, whitelist or `-y`; approved → runs | blocked (plan gate) |

**The choose_mode flow.** When you give the agent a task that needs file changes or
commands, its first action in manual mode is a `choose_mode` prompt offering **Auto** /
**Plan** / **Stay in manual**. The choice is asked once and sticks until you switch
modes yourself; switching back to `/mode manual` re-arms it. Pure questions are answered
directly in manual mode without any prompt.

**Plan mode.** Tools that modify anything are removed from the model's tool schemas
*and* gated at the executor, so the agent can only explore read-only. When it is done
exploring it calls `exit_plan_mode` with a proposed plan; you approve or reject it
interactively. Approval switches to auto mode with auto-approval enabled and the agent
executes the plan; rejection keeps it in plan mode.

**Notes.** Single-shot / headless runs (`openx "..."`) force auto mode so permission
dialogs can't block on non-TTY stdin. Subagents snapshot the parent's mode at spawn
time (a manual parent's children still confirm writes). Mode is never persisted across
sessions — every start is a fresh manual consent.

## Permission tiers

OpenX has a three-tier permission system:

- **Allow** — Always runs (reading files, listing directories, grep, git status)
- **Ask** — Prompts for confirmation (writing files, shell commands, MCP tools)
- **Deny** — Always blocked (`rm -rf`, `sudo`, fork bombs, etc.)

When you approve a prompt you can choose "don't ask again" — the rule is stored and
managed with `/permissions` (`/permissions rm <pattern>`, `/permissions clear`). Use
`--auto-approve` or `/auto-approve` to skip prompts entirely.

## See also

- [Commands](commands.md) — `/mode`, `/auto-approve`, `/permissions`
- [Hooks](../../subsystems/hooks.md) — PreToolUse hooks can also block calls
