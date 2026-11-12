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

> **Status: experimental, streaming, dialogs migrated.** Answers stream, thinking
> and tool blocks are collapsible, `Esc` interrupts a turn, and permission / plan
> approval / `ask_user` are real modal screens. The inline REPL is still the
> default, and it still owns input editing, the slash-command menus and the
> fleet/plugin panels (see [Limits](#limits)).

```bash
openx --tui                     # full-screen interface
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
| Dialogs | raw-mode menus on the tty (`ui/_components/dialogs.py`) | `ModalScreen`s over the transcript (`openx/tui/modals.py`) |

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

## Dialogs

The inline prompts are the part of the old surface that a widget tree cannot
share: `ui/_components/dialogs.py` puts the tty into raw mode, writes escape
sequences and positions the cursor by counting lines. With Textual owning both
stdin and the screen that is a guaranteed mess, so the three questions the agent
actually blocks on became `ModalScreen`s (`openx/tui/modals.py`).

| Question | Screen | Cancel (`Esc`) |
|---|---|---|
| permission ASK (`Console.ask_permission`) | `ChoiceScreen` | `(False, False)` — do not run |
| plan approval (`confirm_plan`) | `ChoiceScreen` (Markdown body) | `False` — do not execute |
| `ask_user` (`ask_user_question`) | `ChoiceScreen` (multi-select) / `TextScreen` ("Other") | `""` / `[]` — unanswered |

Two widgets cover all three: one choice screen and one text screen. What differs
is data (title, body renderable, option table) — a fourth dialog should not need a
third screen.

**Wiring is unchanged on the tool side.** `Console.ask_permission` checks
`_tui` first and hands over to `app.ask_permission`; `ask_user` / `confirm_plan`
route through the async-first entries (`ask_user_question_async` /
`confirm_plan_async`) that `tools/console_dialog.py` already prefers when they
exist — the same pair the browser (`--serve`) surface uses. Tool code needed no
edits, and the same two methods fall back to the synchronous inline implementations
verbatim when there is no app attached.

**Cancellation is fail-closed**, and it is the screen's value, not the caller's
guess: `Esc`, app shutdown or a lost screen all resolve to the conservative
answer (deny / do not execute / unanswered). A dismissed dialog can never become
an implicit authorization. Permission ordering also matches inline: *allow once /
don't ask again / reject*, so one extra `↓` lands on the narrower grant rather
than turning a rejection into a permanent allow.

**A dialog never leaks a dangling `await`.** Each modal resolves a `Future` the
caller awaits; `App._close()` resolves every pending one with its default before
exiting, and a dialog requested *after* shutdown returns its default immediately.
Without that, a tool blocked in `ask_permission` would outlive the interface
(a deadlock, not an exit).

While a dialog is up the status line says `Waiting for you…` instead of
`Answering…`: the turn is paused on a tool result, and "answering" would read as
"already answered".

`Esc` is deliberately **not** an app-level priority binding: the input line has no
`Esc` binding so the key still bubbles up and interrupts a turn, while a modal
screen's own binding takes precedence over the app's — which is exactly the
layering needed for both meanings to coexist.

## Keys

| Key | Action |
|---|---|
| `Enter` | submit the input line (during a turn: queue it for the next turn) |
| `Ctrl+Q` | quit (wakes a pending input with EOF) |
| `Ctrl+R` | toggle every thinking block |
| `Ctrl+T` | expand all blocks, or collapse all if any is collapsed |
| `Tab` / `Shift+Tab` | move focus between block titles and the input line |
| `Esc` | interrupt the running turn (no-op when idle); cancel the open dialog |

In a dialog: `↑`/`↓` move, `space` toggles (multi-select only), `Enter` confirms,
`Esc` cancels with the conservative answer.

Clicking a block's title bar toggles it; the vertical scrollbar and mouse wheel
scroll the transcript, and the view stops following the bottom while you are
reading history.

## Limits

- **Slash-command menus are not migrated.** `/config`, `/model`, `/mcp add`,
  `/hooks`, `/skill create`, `/resume` and friends still drive the inline
  selectors and free-text prompts, which is raw-mode tty work the app cannot
  share. In `--tui` those paths raise `TuiInputNeeded`, and
  `handle_slash_command` / `_stream_response` turn it into one warning: the
  command or turn is dropped, the session continues, the screen is untouched.
  Pass the value on the command line instead (e.g. `/model <name>`).
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

`tests/tui/test_dialogs.py` covers the migrated dialogs: widget shape (two
screens, no third for a fourth question), fail-closed cancellation, the
permission option order, the diff preview in the permission body, the multi-select
toggle, the "Other" branch into `TextScreen`, the three seams (`Console.ask_permission`,
`tools.console_dialog`'s async-first entries), detach falling back to inline, and
two shutdown contracts — a pending dialog resolves to its default, and closing the
app **actually ends it** (bounded wait, so a regression fails instead of hanging).
The guard tests are here too: the unmigrated entries raise `TuiInputNeeded`, and
the slash-command / stream-response paths turn that into a warning rather than an
error.

Textual's `App.run_test()` + `Pilot` assert **widget state** directly, which is
what the inline implementation cannot do — there, stream rendering is verified
through `pyte` screen snapshots. All three files are
`pytest.importorskip("textual")`-guarded, so the default path never goes red for a
missing optional dependency.

Dependency: `textual>=8,<9` (it requires `rich>=14.2`, compatible with this
repo's `rich>=14,<16`). The API surface relied on — `Collapsible` with reactive
`title`/`collapsed`, `CollapsibleTitle`'s click and Enter handlers, `run_async`,
`run_test`, `Static.update`/`visual`, `OptionList` with
`replace_option_prompt_at_index`, `ModalScreen`/`push_screen(screen, callback)`,
`VerticalScroll` — is listed in the dependency comment in `pyproject.toml`; a
rename there should go red in `tests/tui/` first.

> One naming landmine worth knowing: `MessagePump._closed` is Textual's own
> "message queue is closed" switch, and `post_message` **silently drops** anything
> posted while it is set. The app's own teardown flag is therefore `_tui_closed`;
> reusing `_closed` made `_close()` swallow the `ExitApp` that follows it, and the
> app then sat in `_process_messages` forever (tests hung instead of failing).

## See also

- [Architecture](../architecture.md) — where `openx/tui/` sits in the module tree
- [Modes & permissions](../user/guide/modes-permissions.md) — auto mode, which makes `--tui` usable today
- [Subagents](subagents.md) — the fleet deck that a full-screen surface will render as a real panel
- [Plugins](plugins.md) — `ui.panel` slots, currently inline-only
