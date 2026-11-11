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

> **Status: experimental, streaming.** Answers stream, thinking and tool blocks are
> collapsible, and `Esc` interrupts a turn. The inline REPL is still the default and
> still the source of truth for dialogs, input editing and the fleet/plugin panels
> (see [Limits](#limits)).

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

## Streaming

The inline implementation renders a turn into a Rich `Live` region and pays for
it with a viewport budget, a line-commit watermark and cursor arithmetic. The
full-screen pipeline keeps the **same event stream** and none of that machinery:
`TuiStream` (`openx/tui/stream.py`) has the same surface as `StreamingService`
(`start` / `feed` / `done` / `cancel` / `pause` / `set_cancel_target` /
`esc_interrupted`), so `interactive.py` picks a backend in one place and the
wiring below it is unchanged.

`feed()` only classifies and enqueues; `StreamView` (`openx/tui/view.py`) applies
the events to widgets, and it is driven exclusively by the app's 30 Hz drain —
the same single-consumer rule as the output sink.

| Chunk from `agent.stream_run` | Widget result |
|---|---|
| `str` | appended to the current answer entry as Rich `Markdown` (math pre-rendered), re-rendered at most 10×/s |
| `StreamReasoning(text)` | a `ThinkingBlock`, mounted on the first token; frozen to `● Thought for Ns` by the first non-reasoning chunk |
| `ToolStartEvent(name, arguments, call_id)` | a `ToolBlock`, title = state dot + name + argument summary |
| `ToolResultEvent(name, output, is_error, call_id)` | the matching block flips to done/error and shows the output |

A tool block **closes the current answer segment**: text after a tool call starts
a new entry, mirroring the inline text/tool alternation — otherwise new text would
be spliced into the old Markdown around the block.

Tool results pair by `call_id` when the provider supplies one, falling back to
"first running block with that name" (the inline rule). Parallel calls to the same
tool therefore land in the right block, which the inline implementation cannot do.

Interrupting: `Esc` is an app binding (priority, so it works while typing). It
calls `TuiStream.interrupt`, which is the inline `_interrupt` semantics point for
point — a per-turn latch, cancelling **only** the registered stream-consumer
subtask, `loop.call_soon_threadsafe` for the actual cancel, and `esc_interrupted`
so `interactive.py` can decide between swallowing the cancellation (back to the
prompt) and re-raising. The handler is unbound on `done()`/`cancel()`, so a stray
`Esc` after a turn cannot touch the next one.

Enter during a turn **queues** the message instead of sending it: it goes into
`console._input_queue`, the channel the inline REPL already drains between turns,
and the transcript echoes `↳ queued: …` so the user sees what is waiting.

## Keys

| Key | Action |
|---|---|
| `Enter` | submit the input line (during a turn: queue it for the next turn) |
| `Ctrl+Q` | quit (wakes a pending input with EOF) |
| `Ctrl+R` | toggle every thinking block |
| `Ctrl+T` | expand all blocks, or collapse all if any is collapsed |
| `Tab` / `Shift+Tab` | move focus between block titles and the input line |
| `Esc` | interrupt the running turn (no-op when idle) |

Clicking a block's title bar toggles it; the vertical scrollbar and mouse wheel
scroll the transcript, and the view stops following the bottom while you are
reading history.

## Limits

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
- Plugin UI panels (`ctx.register_ui_slot`), the subagent fleet deck and the
  plan/queue deck are inline-only for now: their renderers write through
  `Console`, so their output shows up in the transcript as text rather than as a
  pinned panel. `--tui` carries the to-do/fleet/panel arguments in the
  `TuiStream` constructor and ignores them.
- Long answers re-render as Markdown at 10 Hz while streaming; a very long answer
  on a slow terminal is correspondingly heavier than the inline line-commit path.

## Testing

`tests/tui/test_spike.py` pins the load-bearing behaviour: title click and
Enter-toggle, `Ctrl+T`, thinking content staying plain text, tool result states,
Rich/Markdown/math fidelity inside `Static`, sink delivery, attach/detach, and
the `ask()` contract (submitted text, or `None` on quit).

`tests/tui/test_streaming.py` drives the pipeline with **real event objects**
(`StreamReasoning`, `ToolStartEvent`, `ToolResultEvent`): segment splitting,
parallel same-name pairing by `call_id`, orphan results not being dropped,
`task` start suppression plus result echo, the done/interrupted lines, the
`Esc` latch and its unbinding, queued input routing, and one end-to-end run of
`_stream_response` that asserts widgets are fed **and** that no inline
`InputCapture` is started (cbreak would fight Textual for stdin).

Textual's `App.run_test()` + `Pilot` assert **widget state** directly, which is
what the inline implementation cannot do — there, stream rendering is verified
through `pyte` screen snapshots. Both files are `pytest.importorskip("textual")`-
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
