# Full-screen TUI (Textual)

English | [中文](tui.zh.md)

The default interface is an **inline REPL**: output accumulates in the terminal's
scrollback and the input frame is redrawn in place. That model is cheap to get
right and impossible to make rich — once a line is in the scrollback it can never
be rewritten, so "collapse this thinking block", "re-render on resize" and
"show a live subagent panel" all become cursor arithmetic against a region the
program does not own.

`openx --tui` runs a **full-screen interface** instead (`openx/tui/`, built on
Textual): the screen is a widget tree, so folding a block is a widget property
rather than a state machine, and the framework owns layout, focus and scrolling.

> **Status: stage 0 (experimental).** The surface exists and is tested, but the
> inline REPL is still the default and still the source of truth for anything not
> listed below. Turns run **non-streaming** in `--tui` (see [Limits](#limits)).

```bash
openx --tui                     # full-screen interface
openx --tui --auto-approve      # ... with no ASK dialogs (see Limits)
openx --tui --continue          # resume a session, as usual
python -m openx.tui             # offline demo: no model, no network
```

## Layout

```
┌ transcript ─────────────────────────── VerticalScroll ──┐
│  ❯ explain the streaming layer                          │
│  ▸ ● Thought for 3.4s            ← collapsed by default │
│  ▾ ● read_file (services/…)      ← expanded on click    │
│      1 2200 lines / watermark                           │
│  ▸ ● grep (pattern=…)                                   │
│  answer (Rich Markdown, math pre-rendered)              │
├─────────────────────────────────────────────────────────┤
│  ✻ Thinking…  (3.4s)  ·  esc to interrupt               │  status line
├─────────────────────────────────────────────────────────┤
│  Ask OpenX…                                             │  Input
├─────────────────────────────────────────────────────────┤
│  ^Q quit   ^R thinking   ^T expand all                  │  Footer
└─────────────────────────────────────────────────────────┘
```

## The three seams

The full-screen interface does not reimplement the agent; it reroutes three
things the inline implementation already abstracted.

| Seam | Inline | `--tui` |
|---|---|---|
| Output | `Console._console` is a Rich `Console` writing to stdout | `Console._console` is `TuiSink`: `print()` enqueues a renderable, the app mounts it as a widget |
| Input | `print_user_prompt` draws a frame and runs the prompt_toolkit editor | `print_user_prompt` delegates to `app.ask()` — the `Input` widget's next submission |
| Session | `asyncio.run(run_interactive(...))` | `run_tui` runs the app and the same REPL coroutine concurrently in **one event loop** |

**Output.** `TuiSink` (`openx/tui/sink.py`) keeps the whole rendering layer
untouched: the ~90 `console.print_*` call sites still hand Rich renderables over,
and each one becomes a `Static`. `print()` only enqueues — a bounded queue
(4000 entries, oldest dropped with a counter, never blocking the caller) — and
the app drains it on a 30 Hz timer from the app thread. So **only one consumer
ever mutates widgets**, which is the same discipline the inline implementation
enforces for its `Live` region, with the thread moved. The measurement surface
(`render_lines` / `capture` / `width` / `height`) is served by an internal Rich
console whose file is a `StringIO`: a stray direct write lands in the buffer, not
on the screen.

**Input.** `Console.attach_tui(app)` sets `_tui`, and `print_user_prompt` returns
`await app.ask()` instead of drawing the frame. `ask()` resolves to the next
submitted line, or `None` when the interface is closed — the same `None` the
inline path returns on EOF, so `interactive.py` needs no special case and `/quit`
still exits cleanly.

**Session.** `run_tui` starts the app and `run_interactive` as two tasks in the
same loop and stops both when either finishes: Ctrl+Q wakes a pending `ask()`
with `None` (REPL exits normally), and `/quit` or EOF closes the app.

`Console.detach_tui()` restores the original Rich console and the inline input
path. That is the migration escape hatch — the inline implementation stays
wired and working until stage 6 deletes it.

## Blocks

`openx/tui/blocks.py` maps the two things the inline implementation spends the
most cursor arithmetic on:

| Block | Default | Toggle | Content |
|---|---|---|---|
| `ThinkingBlock` | collapsed | click the title bar, or focus it and press Enter | plain dim `Text` — **never** Markdown (fences and backticks get reflowed) |
| `ToolBlock` | collapsed | same | result text; the title carries the state dot (`○` running, `●` done, `✕` error) plus `name (summary)` |

`feed()` appends tokens, `freeze(elapsed)` rewrites the title to `● Thought for
Ns`. Unlike the inline implementation, a frozen block **stays foldable** — the
widget is still there to re-render.

## Keys

| Key | Action |
|---|---|
| `Enter` | submit the input line |
| `Ctrl+Q` | quit (wakes a pending input with EOF) |
| `Ctrl+R` | toggle every thinking block |
| `Ctrl+T` | expand all blocks, or collapse all if any is collapsed |
| `Tab` / `Shift+Tab` | move focus between block titles and the input line |
| `Esc` | reserved for interrupting a turn — **not wired yet** (stage 2) |

Clicking a block's title bar toggles it; the vertical scrollbar and mouse wheel
scroll the transcript, and the view stops following the bottom while you are
reading history.

## Limits

- **Turns are non-streaming.** The inline `StreamingService` drives a Rich `Live`
  region, which would fight Textual for the same terminal. `--tui` therefore
  forces `config.stream = False`; the streaming widget pipeline is stage 2.
  Consequences you will notice: no token-by-token answers, no live thinking
  block, and `Esc` does not interrupt.
- **Dialogs are not migrated.** Permission ASK prompts, plan approval,
  `ask_user`, and the settings overlay still come from the inline implementation,
  which switches the tty to raw mode and writes escape sequences directly —
  incompatible with the app owning the screen. Run `--tui` with `-y` /
  `--auto-approve` (or in auto mode) so no dialog is needed until stage 5 lands
  the modal screens.
- **The input line is single-line.** No history, no completion, no multi-line
  paste yet (stage 3).
- **`--tui` needs a real terminal.** On a pipe or in CI it prints a warning and
  falls back to the inline REPL; `python -m openx.tui` exits 2 instead.
- `--tui` is mutually exclusive with `--serve` and with a one-shot prompt
  argument — those surfaces own stdout in ways a full-screen app cannot share.
- Plugin UI panels (`ctx.register_ui_slot`) and the subagent fleet deck are
  inline-only for now: their renderers write through `Console`, so their output
  shows up in the transcript as text rather than as a pinned panel.

## Testing

`tests/tui/test_spike.py` pins the load-bearing behaviour: title click and
Enter-toggle, `Ctrl+T`, thinking content staying plain text, tool result states,
Rich/Markdown/math fidelity inside `Static`, sink delivery, attach/detach, and
the `ask()` contract (submitted text, or `None` on quit).

Textual's `App.run_test()` + `Pilot` assert **widget state** directly, which is
what the inline implementation cannot do — there, stream rendering is verified
through `pyte` screen snapshots. The file is `pytest.importorskip("textual")`-
guarded, so the default path never goes red for a missing optional dependency.

Dependency: `textual>=8,<9` (it requires `rich>=14.2`, compatible with this
repo's `rich>=14,<16`). The API surface relied on — `Collapsible` with reactive
`title`/`collapsed`, `CollapsibleTitle`'s click and Enter handlers, `run_async`,
`run_test`, `Static.update`, `VerticalScroll` — is listed in the dependency
comment in `pyproject.toml`; a rename there should go red in `tests/tui/` first.

## See also

- [Architecture](../architecture.md) — where `openx/tui/` sits in the module tree
- [Modes & permissions](../user/guide/modes-permissions.md) — auto mode, which makes `--tui` usable today
- [Subagents](subagents.md) — the fleet deck that a full-screen surface will render as a real panel
- [Plugins](plugins.md) — `ui.panel` slots, currently inline-only
