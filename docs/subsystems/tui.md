# Full-screen TUI (Textual)

English | [中文](tui.zh.md)

The default interface is the **full-screen TUI** (`openx/tui/`, built on Textual):
the screen is a widget tree, so folding a thinking block is a widget property
rather than a state machine, and the framework owns layout, focus and scrolling.

`openx --inline` runs the **inline REPL** instead — the pre-TUI surface where
output accumulates in the terminal's scrollback and the input frame is redrawn
in place. That model is cheap to get right and impossible to make rich: once a
line is in the scrollback it can never be rewritten, so "collapse this thinking
block", "re-render on resize" and "show a live subagent panel" all become cursor
arithmetic against a region the program does not own. It is kept as an escape
hatch (`--inline`), not as a peer surface.

> **Status: default interface.** Streaming, collapsible thinking/tool blocks,
> `Esc` interrupt, the multi-line input line (history + slash-command menu), the
> pinned decks (plan / queue / agents / plugin panels), the navigable Agents panel
> with a subagent detail view, and the permission / plan-approval / `ask_user`
> modal dialogs all run in the app. Remaining gaps are listed under
> [Limits](#limits).

```bash
openx                          # full-screen interface (default)
openx --inline                 # inline REPL (the pre-TUI surface)
openx --tui                    # no-op compat alias for the default
python -m openx.tui            # offline demo: no model, no network
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
│   Plan 1/3                                  ← #plan-deck│  plan + queue
│     ✓ read the streaming layer                          │
│     ✻ wire the decks                                    │
│   Queue (1)                                             │
│     ▸ also update the docs                              │
├─────────────────────────────────────────────────────────┤
│  ✻ Thinking…  (3.4s)  ·  esc to interrupt               │  status line
├─────────────────────────────────────────────────────────┤
│  Ask OpenX…   (/ pops the slash-command menu)           │  PromptEditor + SlashMenu
├─────────────────────────────────────────────────────────┤
│   Agents (2)                              ← #agents-deck│  agents + plugins
│  ❯1 explore streaming · 3 tools · 4s · ^O switch        │
│        ⎿  read_file(services/streaming.py)              │
│   2 ✓ review docs · 5 tools · 9s                        │
│   0 main · main answer                                  │
│  (^_^) demo-pet · frame 12                              │
└─────────────────────────────────────────────────────────┘

No footer: `Ctrl+Q` / `Ctrl+R` / `Ctrl+T` still work but are never displayed
on screen (the input placeholder and the dialogs' hint lines carry the hints).

`Ctrl+O` swaps the transcript block for a subagent's detail view (`#detail` —
the screen's other `1fr` child, so the two never compete for height):

```
┌ detail ──────────────────────────────── VerticalScroll ─┐
│  Agent 1: explore streaming · ✻ running · 3 tools · 4s  │  ← #detail-head
│  ● read_file (services/streaming.py)                    │  ← #detail-body
│    ⎿ 1 2200 lines / watermark                           │
│    正在核对 deck 预算与视口裁剪…                          │
└─────────────────────────────────────────────────────────┘
```

The status line, the input line and both decks stay where they are, so following
up on *this* subagent is still one keystroke away.

## The three seams

The full-screen interface does not reimplement the agent; it reroutes three
things the inline implementation already abstracted.

| Seam | Inline (`--inline`) | Default (TUI) |
|---|---|---|
| Output | `Console._console` is a Rich `Console` writing to stdout | `Console._console` is `TuiSink`: `print()` enqueues a renderable, the app mounts it as a widget |
| Input | `print_user_prompt` draws a frame and runs the prompt_toolkit editor | `print_user_prompt` delegates to `app.ask()` — the `PromptEditor` widget's next submission |
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
submitted text (possibly multi-line), or `None` when the interface is closed —
the same `None` the inline path returns on EOF, so `interactive.py` needs no
special case and `/quit` still exits cleanly. The widget behind it is
[`PromptEditor`](#the-input-line), not Textual's `Input`.

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
| `ToolBlock` | collapsed | same | result text, **diff-colored** for text operations: `+` green, `-` red, `@@`/`---`/`+++` dim. With a change preview (`edit_file` / `write_file`, probed before execution) the body becomes `⎿ +A −D` plus a syntax-highlighted diff instead of the raw output — the same decision `ui/_diff.py` makes for the inline transcript and for `openx serve`'s `diff` payload, so no surface can color a line the others leave white. The title carries the state dot (`○` running, `●` done, `✕` error) plus `name (summary)` |

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

## The input line

The input line is `PromptEditor` (`openx/tui/prompt.py`), a `TextArea` subclass,
not Textual's `Input`. The reason is a single hard fact: `Input`'s document model
has no `\n`, so a multi-line paste is flattened onto one line and the caret can
never reach a second one. `TextArea` has a real document — soft wrap, selection,
undo, bracketed paste — and the price is that every key meaning has to be
declared here.

| Key | Meaning |
|---|---|
| `Enter` | submit (during a turn: queue it); with the slash menu open, apply the candidate first and submit that |
| `Ctrl+J` | insert a literal newline |
| `Alt+Enter` / `Shift+Enter` | same (`ESC CR` is a distinguishable byte pair; `Shift+Enter` needs a terminal that reports it, e.g. the kitty keyboard protocol) |
| `↑` / `↓` | with the menu open: move the candidate cursor; otherwise recall history **at the first/last line only**, elsewhere they move the caret |
| `Tab` | with the menu open: apply the candidate + trailing space; otherwise the framework's focus chain |
| `Esc` | with the menu open: just close it; otherwise bubble up and interrupt the turn |

The inline editor is `multiline=False` with `("escape", "enter")` bound to insert
`\n`, so "Enter submits, `Alt+Enter` breaks the line" is the same contract on
both surfaces; the TUI only adds `Ctrl+J` (no terminal modifier support needed).

**The frame grows with the content.** `_sync_height` sets
`styles.height = rows + 2` (the two horizontal rules are one row each), taking
`rows` from `wrapped_document.height` — the height **after** soft wrap, so a long
single line and a multi-line paste are treated alike. It is capped at
`MAX_ROWS = 8` and the editor scrolls internally beyond that; without the cap a
paste would push the transcript off screen. The height is computed here rather
than left to the framework's auto-height semantics, which is a bet not worth
taking on a widget that also has borders.

**History is the inline REPL's file.** `tui/history.py` reads and appends
`~/.openx/history` in prompt_toolkit's `FileHistory` format — a `# <timestamp>`
header per entry, then each logical line prefixed with `+` — so a command typed
in `--inline` is on `↑` in the full-screen interface and vice versa. Writes are
append-only (two surfaces open at once cannot truncate each other), empty and
adjacent-duplicate entries are skipped exactly like the inline
`OpenXHistory.store_string`, and any IO failure degrades silently: history is a
convenience, never a reason for the input line to break. The cursor keeps a
draft — typing half a message, pressing `↑`, then `↓` past the newest entry
returns what you had typed.

**The slash menu keeps its two-path discipline.** Bindings for `↑`/`↓`/`Tab`/`Esc`
are gated by `check_action` (dead while the menu is closed) instead of calling
`stop()`, because Textual dispatches keys on two paths: the MRO `_on_key` (where
`TextArea` inserts characters and moves the caret) and, after bubbling, the
app's binding check. `prevent_default()` cuts only the first; `stop()` cuts both,
and the key then never reaches the binding action. So the editor calls
`prevent_default()` when overriding default behaviour and `stop()` only when it
has fully handled the key. The same gate is what lets `Esc` mean two things:
close the menu when one is open, interrupt the turn otherwise.

Because `Ctrl+R` / `Ctrl+T` are app-level actions and `TextArea` ships its own
editing bindings (`ctrl+z`, `ctrl+k`, …), those two are **priority** bindings —
a future `ctrl+r` in `TextArea` cannot silently shadow "fold all thinking".

`TextScreen` (the free-text modal) mounts the very same widget: same keys, same
look, and a growing frame. That is what makes `/skill add`'s multi-line
instructions fit in one dialog instead of one dialog per line. It passes no
history — a one-shot modal that recalls the transcript's commands is confusing —
and keeps the conservative contract: empty input means unanswered, `Esc` means
`None`.

## Decks

The four status panels the inline implementation renders inside its `Live` region
— **Plan** (the agent's to-do list), **Queue** (messages typed during a turn),
**Agents** (the subagent fleet) and **plugin `ui.panel` slots** — became widgets:
`#plan-deck` above the status line, `#agents-deck` below the input line. Nothing
about the *data* moved: `TuiStream.start()` hands the same three providers the
inline service gets from `_make_display` to `app.attach_decks`, and the app
renders them on a 5 Hz timer (the same cadence as the inline `Live`).

**One shared line format.** `openx/ui/_deck.py` builds the rows — Plan's three
states with `activeForm` for the running item, the fold-to-one-line summary when
everything is done, Queue's FIFO listing, Agents' leading main entry plus an
activity line (`⎿ tool(summary)`) under each *running* subagent, plugin panels
with `svg:` lines skipped and bad markup isolated per panel. The two surfaces
differ only in **budget** (how many rows they can spend) and **placement** (above
or below the frame); `StreamingService._deck_renderable` and friends are now thin
wrappers around the same functions, so "the panels look the same in both
interfaces" is a property of the code rather than a maintenance promise. The
spinner glyph moved to `ui/_style.py` for the same reason.

**Budget.** The inline side subtracts from the viewport because its `Live` region
is an absolute row count. The full-screen side takes the smaller of
`height // 4` and `(height - chrome - gaps - 3) // 2` — the ratio keeps the two
decks inside half the screen, the subtraction guarantees a transcript of at least
three rows (on a 12-row terminal the ratio alone would leave the transcript one
row). `DeckSource` also clamps each deck to its budget exactly: the shared plugin
renderer is allowed to exceed its row budget by one (that is pinned inline
behaviour), so the strictly-bounded invariant is enforced by the caller that
owns the viewport.

**Visibility differs from inline on purpose.** Inline hides everything when the
turn ends — a side effect of the `Live` region, not a requirement. Here:

| Deck | When it shows |
|---|---|
| Plan / Queue | whenever there is something to show — the to-do list outlives the turn |
| Agents | only while a turn is running (`fleet.reset()` empties it per turn) |
| Plugin panels | whenever they render — a `ui.panel` pet or monitor is meant to be resident |

**Geometry is explicit.** `DeckView` writes `styles.height = len(rows)` instead of
trusting auto-height (deck rows are `no_wrap`, so one row is one line), hides
itself via `display: none` when empty — which also drops its margin, exactly
equivalent to the inline rule that the `_DECK_SPINNER_GAP` row only exists while
the deck does — and never adds or removes children at runtime (the `SlashMenu` lesson:
a container that briefly renders an empty visual crashes `render_strips`). When a
deck changes height the app re-pins the transcript to the bottom, so growth does
not silently stop the follow-scroll.

### Agent detail view

`Ctrl+O` cycles the viewing target (`Alt+0…9` jumps): target `0` is the main
answer, `1..N` is a subagent. Picking one hides `#transcript` and shows `#detail`
in its place — the inline surface's "body slot switches to that subagent", with
the input line and both decks left alone so you can still queue a follow-up
while reading.

**Why not `↓` and `Enter`?** The inline deck uses `↓` to select and `Enter` to
confirm, which works there only because the inline input line stops taking edits
while a turn streams. Here the input line is always editable and `↑`/`↓` already
mean "recall history / move the caret" (`tests/tui/test_input.py` pins both), so
the keys are `Ctrl+O` (cycle) and `Alt+0…9` (jump). The Agents header says
`· ^O switch` — the shared renderer takes the hint text as a parameter
(`fleet_lines(hints=…)` accepts `True` / `False` / a string) because a header
must never advertise keys that do nothing, and "nothing" is not the only
alternative to the inline wording.

**Header and body are two `Static`s.** The header carries the spinner and the
elapsed time, so it is rewritten every 5 Hz tick; the body is the subagent's
captured stream, which only changes when new lines land (it is deduped by a
`plain` signature, same as `DeckView`). One widget would force the second to
follow the first's cadence — 200 lines × 5 Hz of pure layout churn. The body
rows are the one place in this file that **may wrap**: they are a subagent's raw
output, where truncation hurts more than wrapping, and they live in a scroll
container rather than in the deck's "one row ≡ one terminal row" geometry.

Rows come from `ui/_deck.py` (`detail_header` / `detail_body`), shared with the
inline `_detail_view`. That is also why a stray `[` in subagent output no longer
kills the frame: `format_stream_event` returns model text unescaped, so the
inline surface used to raise `MarkupError` on it; `detail_body` now degrades
that single line to plain text.

**Lifecycle.** The target resets to `0` on every turn (`attach_decks`), so a new
turn never opens on a view that no longer exists. If the fleet shrinks under the
current target (`reset()`, a finished run), the app clamps back to the main
answer on the next tick. A modal dialog suppresses the switch: the detail view
would replace the transcript the dialog is drawn over. Esc does not close the
detail view (Esc already means "interrupt" and "dismiss dialog") — `Alt+0` or
cycling back does.

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

**Visuals are the inline open format**: the modal screen paints no dim overlay
(transparent background — the transcript stays visible behind the dialog), and the
content is a full-width, flush-left column docked to the bottom — bold title line,
body, the bare `●` option list (selected row bright bold, others dim), and a
trailing `(↑/↓ to choose, …)` hint line. No card, no border, no box.

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

### Slash-command menus ride the same screens

The `/config`, `/model <group>:<role>`, `/mcp add|remove`, `/skill add|remove`
and `/release` flows used to drive the inline selectors, which under the app
raised `TuiInputNeeded`. They now go through two async-first entries on
`Console` — `select_async` (choice) and `ask_text_async` (free text) — that
hand over to `app.select` / `app.ask_text` when a TUI is attached and delegate
to the inline implementations verbatim otherwise. `app.select` is just another
`ChoiceScreen`: a fourth question still does not need a third screen.
Cancellation returns `None` / `""`, matching the menus' existing
"Cancel/Done" semantics. `/clear` removes the transcript widgets
(`app.clear_transcript`) instead of `raw.clear()`, which under the sink only
lands in the measuring `StringIO`.

`Esc` is deliberately **not** an app-level priority binding: the input line has no
`Esc` binding so the key still bubbles up and interrupts a turn, while a modal
screen's own binding takes precedence over the app's — which is exactly the
layering needed for both meanings to coexist.

## Keys

| Key | Action |
|---|---|
| `Enter` | submit the input line (during a turn: queue it for the next turn) |
| `Ctrl+J` / `Alt+Enter` | new line inside the input line (the input grows up to 8 rows) |
| `↑` / `↓` | slash menu open: move the candidate cursor; closed: recall history at the first/last line |
| `Ctrl+Q` | quit (wakes a pending input with EOF) |
| `Ctrl+R` | toggle every thinking block |
| `Ctrl+T` | expand all blocks, or collapse all if any is collapsed |
| `Ctrl+O` | cycle the viewing target: main answer → subagent 1 → … → back to main |
| `Alt+0` … `Alt+9` | jump straight to a target (`0` = main answer; out of range clamps to the last) |
| `Tab` / `Shift+Tab` | slash menu open: apply the candidate; closed: move focus between block titles and the input line |
| `Esc` | interrupt the running turn (no-op when idle); close the slash menu; cancel the open dialog |

In a dialog: `↑`/`↓` move, `space` toggles (multi-select only), `Enter` confirms,
`Esc` cancels with the conservative answer. A dialog's free-text input is the
same `PromptEditor`, so `Ctrl+J` breaks the line and the box grows — multi-line
answers and `/skill add` instructions are typed in one dialog.

Clicking a block's title bar toggles it; the vertical scrollbar and mouse wheel
scroll the transcript, and the view stops following the bottom while you are
reading history. Clicks never move focus off the input line: the transcript
container and dialog bodies are non-focusable, and a click anywhere on the
main screen hands focus back to the prompt — typing keeps working right after
a click (`Tab` still walks the focus chain; a focused title still toggles on
`Enter`).

## Limits

- **The input line is an editor, not the inline ptk one.** Multi-line editing,
  bracketed multi-line paste, soft wrap and `↑`/`↓` history (the same
  `~/.openx/history` file the inline REPL uses) are in. Still missing: the inline
  editor's `Ctrl+R` reverse search, and terminal mouse selection. `/skill add`'s
  multi-line instructions now fit in one modal.
- **The TUI needs a real terminal.** On a pipe or in CI the CLI prints a
  warning and falls back to the inline REPL; `python -m openx.tui` exits 2
  instead. If `textual` itself is missing, the default path likewise falls
  back with a warning, while an explicit `--tui` fails with exit 2 and an
  install hint.
- The TUI is mutually exclusive with `--serve` and with a one-shot prompt
  argument — those surfaces own stdout in ways a full-screen app cannot share
  (use `--inline` with a prompt if you need the inline surface).
- **The Agents panel is navigable but not clickable.** `Ctrl+O` / `Alt+0…9` reach
  the subagent detail view (see [Agent detail view](#agent-detail-view)); what is
  still missing is the inline surface's two-step `↓` select + `Enter` confirm, a
  click-to-open on a fleet row, and a way to dismiss the detail view with `Esc`
  (`Alt+0` returns instead). The header advertises only the keys that work.
- Long answers re-render as Markdown at 10 Hz while streaming; a very long answer
  on a slow terminal is correspondingly heavier than the inline line-commit path.

## Testing

`tests/tui/test_spike.py` pins the load-bearing behaviour: title click and
Enter-toggle, `Ctrl+T`, thinking content staying plain text, tool result states,
Rich/Markdown/math fidelity inside `Static`, sink delivery, attach/detach, and
the `ask()` contract (submitted text, or `None` on quit).

`tests/tui/test_deck.py` covers the status layer on both sides of the split. The
pure half (`ui/_deck.py`, no app) pins the row semantics: plan's three states and
`activeForm`, the fold-to-one-line summary, `+N more` overflows, queue FIFO and
its count, the shared plan/queue budget (and that the larger block shrinks
first), fleet rows with the activity line counted toward the budget, `hints`
turning the `↓ select · ↵ view` hint off (or replacing it per surface), plugin
panels skipping `svg:` lines and dropping only the panel with bad markup, and
every provider failing open. The detail-view row builders are pinned the same
way: header identity/state/elapsed plus the per-surface hint, the body window
(`budget`), the trailing `pending` fragment, `(no output yet)`, and the bad-markup
fallback. The widget half pins `DeckView` (hidden when empty without spending a
row, height equal to the row count, no wrapping, click never stealing the input
focus), the compose order (`transcript` → `detail` → `plan-deck` → status line
→ prompt → `agents-deck`), the per-turn lifecycle (`TuiStream.start()` fills all
four decks, `TurnEnd` retires Agents while Plan and plugin panels stay,
`todos[:] = …` shows up on the next tick, the fleet is reset per turn) and a
12-row terminal where both decks stay inside their budget and the transcript
keeps its three rows. `TestFleetNavigation` drives the detail view through real
key presses: `Ctrl+O` cycles and hides the transcript, `Alt+N` jumps and clamps,
the `❯` marker follows the target in the Agents deck, a new turn resets the
target, an emptied fleet clamps back to the main answer, the body skips rewriting
itself when nothing changed, and clicking the detail view keeps the input focus.

`tests/tui/test_streaming.py` drives the pipeline with **real event objects**
(`StreamReasoning`, `ToolStartEvent`, `ToolResultEvent`): segment splitting,
parallel same-name pairing by `call_id`, orphan results not being dropped,
`task` start suppression plus result echo, the done/interrupted lines, the
`Esc` latch and its unbinding, queued input routing, and one end-to-end run of
`_stream_response` that asserts widgets are fed **and** that no inline
`InputCapture` is started (cbreak would fight Textual for stdin).
`TestToolBlockDiff` pins the diff coloring: an `edit_file` result renders `-`
red / `+` green / headers dim, a probed preview replaces the raw output with
`⎿ +A −D` plus a highlighted diff (no duplicate echo), a failed operation never
shows the unapplied preview, other tools keep their output uncolored, and a
failing or absent `tool_lookup` degrades to the plain path.

`tests/tui/test_dialogs.py` covers the migrated dialogs: widget shape (two
screens, no third for a fourth question), fail-closed cancellation, the
permission option order, the diff preview in the permission body, the multi-select
toggle, the "Other" branch into `TextScreen`, the three seams (`Console.ask_permission`,
`tools.console_dialog`'s async-first entries), detach falling back to inline, and
two shutdown contracts — a pending dialog resolves to its default, and closing the
app **actually ends it** (bounded wait, so a regression fails instead of hanging).
The bridge tests cover `select_async` / `ask_text_async`: attached they open the
modal screens, detached they delegate to the inline selector verbatim. The
unmigrated-entry guard (`TuiInputNeeded` from a raw `_interactive_select` /
`paste_aware_input` call under an attached app) remains as the tripwire for any
future call site that bypasses the async entries.

`tests/tui/test_input.py` covers the input line end to end. `PromptHistory`
(no Textual needed) is pinned against the inline ptk format: `# <timestamp>` +
`+`-prefixed lines, multi-line round trip, garbage and broken bytes tolerated,
append-only writes, empty / adjacent-duplicate filtering, `max_entries` trimming
memory but not the file, and the `↑`/`↓` cursor with its draft. On the widget
side: `Enter` submits the whole multi-line text and clears (frame back to one
row), `Ctrl+J` / `Alt+Enter` / `Shift+Enter` insert newlines, a multi-line paste
arrives intact, height follows `wrapped_document` and caps at `MAX_ROWS`,
`↑`/`↓` recall history only at the first/last line (inside the text they move
the caret), a modal editor without history ignores them, `Tab` still walks the
focus chain and `Esc` still bubbles into the interrupt when no menu is open, and
submissions land in the history file — including that repeats and blank lines do
not. `tests/tui/conftest.py` points `HISTORY_PATH` at `tmp_path` for the whole
directory, so no test writes into the user's real `~/.openx/history`.

Textual's `App.run_test()` + `Pilot` assert **widget state** directly, which is
what the inline implementation cannot do — there, stream rendering is verified
through `pyte` screen snapshots. All five files are
`pytest.importorskip("textual")`-guarded, so the default path never goes red for a
missing optional dependency.

Dependency: `textual>=8,<9` (it requires `rich>=14.2`, compatible with this
repo's `rich>=14,<16`). The API surface relied on — `Collapsible` with reactive
`title`/`collapsed`, `CollapsibleTitle`'s click and Enter handlers, `run_async`,
`run_test`, `Static.update`/`visual`, `OptionList` with
`replace_option_prompt_at_index`, `TextArea` (`load_text`, `document`,
`wrapped_document`, `Changed`, `placeholder`, `styles.height`) with its
`prevent_default`-vs-`stop` key layering, `ModalScreen`/`push_screen(screen,
callback)`, `VerticalScroll` — is listed in the dependency comment in
`pyproject.toml`; a rename there should go red in `tests/tui/` first.

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
