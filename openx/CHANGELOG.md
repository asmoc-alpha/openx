# Changelog

All notable changes to OpenX. One `## <version> — <title>` section per release,
newest first; parsed at runtime by `openx/changelog.py` into the startup panel
and `/release-notes`.

## 0.1.26 — `plugin_manager` / `plugin_inspect`: the model gets the console

### Cordis `plugin_manager` / `cordis_inspect` parity, plus MCP bundles

- **`plugin_manager` (ASK)** — the model-facing packaging tool: `install`
  (absolute dir with `plugin.py`, or a single `.py` → admission sandbox →
  user plugins dir → session load, refuses over existing, cleans up on load
  failure), `set_enabled` (writes the settings disabled table + recomposes —
  builtins refuse), `list` (installed with phases + discoverable-but-unloaded).
  Every mutating action asks, matching DSH's "each `plugin_manager` action
  needs approval"
- **`plugin_inspect` (read-only)** — the model reads the contract before
  writing code: `list` registered entries per kind with their owning plugin;
  `query` one entry (owner, summary, trust, permissions, config schema,
  warnings); `config` a plugin's schema plus current user values. This is
  the OpenX answer to `cordis_inspect_list`/`query` — registration is
  introspectable instead of guessed
- **MCP bundles**: a directory plugin may ship `mcp.json` (same shape as
  settings `mcpServers`, stdio). At startup the agent ensures the plugin set
  is loaded, collects bundles from ACTIVE plugins only (trust gate and
  sandbox semantics already gate that set), and merges them — settings-declared
  same-name servers win. Failures degrade to warnings, as MCP always does
- Docs updated in both languages (meta-tools + the `mcp.json` convention);
  4 new tests incl. a real end-to-end dir-plugin install through the sandbox

## 0.1.25 — Plugins get a config system (Cordis `Config` parity)

### A plugin can declare what it accepts; the user configures it; it's validated at load

- **`manifest.config` (JSON Schema subset)**: a plugin declares its
  configurable surface in `__openx_meta__.config` — `type`/`properties`/
  `required`/`enum`/`items`/`minimum`/`maximum`/`default`. Zero new
  dependencies: the validator (`kernel/assembly/plugin_config.py`) implements
  this subset itself and says so — anything outside it is "not validated",
  never silently trusted
- **Storage and injection**: user values live in settings.json under
  `plugins.config.<plugin_id>` (`OpenXConfig.load/save_plugin_config`).
  At load the values are validated against the schema — a violation **refuses
  load** with the offending keys named (Cordis validates at activation; so do
  we) — then `default`s are merged and the result is injected as
  **`ctx.config`**. Plugins without a schema get the raw dict; builtins get
  `{}`
- **`/plugin config <name>`** shows the schema plus current values;
  `/plugin config <name> <json>` validates and saves (bad JSON or schema
  violations name the problem and change nothing); `reset` clears. The
  config section is part of the composition key, so a save rebuilds the
  bundle on the next turn — the command also force-reloads to make it live
  immediately
- Docs updated (manifest reference + `ctx.config` in both languages);
  6 tests (gate, validate/merge, injection-with-defaults, fail-load,
  no-schema passthrough, command view/set/reset)

## 0.1.24 — The task panel stops guessing

### Pairing by id, measured time, and human gates as observable events

- **Pairing by id, not by name**: `tool_use` / `tool_result` now carry the
  model's `call_id` and the `round` (which model request produced the batch)
  end to end — `ToolStartEvent` / `ToolResultEvent` → protocol → panel. Parallel
  calls of the same tool used to be matched "same name, last in first out", so
  two `read_file`s in one batch swapped results and durations. The chat
  transcript pairs on the id too
- **Durations are server-measured** (`tool_result.duration_ms`: tool_use →
  tool_result on the server, per turn): a client that attaches late would
  otherwise time every replayed step at 0ms. Unknown start → the key is absent
  and no duration is shown — never `0ms`, which reads as "instant"
- **No fabricated progress**: the hardcoded bar widths (35% / 60% / 85%) are
  gone. Total steps are unknown until the turn ends, so the bar is
  indeterminate while running and settles at 100% only when the turn does. The
  subline reports facts — step number, round number, elapsed, "no new events
  for Ns" — never an ETA
- **Human gates become events**: an `interaction` (open → close + outcome)
  follows every `permission_request` / `ask_user` / `plan_request`, so "waiting
  for you" stops looking identical to "a tool is running". Outcomes separate
  `approved` / `denied` / `timeout` / `answered` / `skipped` / `rejected` /
  `cancelled` (Esc took the turn down) / `disconnected` (last client left,
  fail-closed) — system behaviour never masquerades as the user's decision. The
  request event still comes first: old clients only know that one
- **`interrupted` says who** (`kind`), in the panel too
- **The transcript survives a refresh**: `GET /api/flow` projects the same
  `_trace_rounds` source as `/api/trace`, with outputs merged into steps by
  `call_id`; the panel loads the last turn from it on boot, on replay and when
  the tab opens — no more blank rail after F5
- **Honest history**: session messages don't persist `is_error` or per-step
  timing, so archived turns show no duration and leave the status unknown
  rather than claiming success; calls with no result are marked "not returned"
- Also: `shell` summaries carry the whole command (the first three words of
  `git commit -m "…"` are `git commit -m`, which tells you nothing); step rows
  expand to the raw output (failures open by default with the first error line
  inline); `result.usage` adds cumulative `cached_tokens` / `plugin_tokens`
- 18 tests: pairing/rounding, duration-unknown honesty, interaction ordering
  and outcomes incl. disconnect attribution, flow merge/pending/404, and the
  panel's three honesty rules

## 0.1.23 — `--auto-approve` / `/auto-approve` actually stop the prompts

### The flag toggled the switch but not the mode the switch depends on

- **The bug**: `agent._mode` defaults to **manual**, and in manual mode the
  Guard's force-prompt station overrides `auto_approve` by design (manual's
  contract is per-call confirmation). `--auto-approve`/`-y` and the
  `/auto-approve` toggle only set `executor.auto_approve` — never the mode —
  so every ASK-level call (which, since the S1c permission ceiling, includes
  every plugin tool whose manifest declares no `permissions`) kept prompting.
  Serve made this the default experience: it starts in manual mode
- **The fix**: `--auto-approve`/`-y` now lands all three entry paths
  (interactive, single-shot, serve) in **auto mode** (`agent.set_mode`), and
  `/auto-approve` ON flips manual → auto too (with an explanatory line);
  OFF leaves the mode alone — in auto, ASK-level calls return to the normal
  asking flow. Dangerous-command / high-risk tools still always ask (their
  contract is unchanged and documented)
- **Trust screen aligned with its own docstring**: the code promised single-shot
  `-y` skips the trust prompt but never did. Now `-y` in an untrusted
  workspace asks nothing **and grants nothing** — project plugins are
  skipped fail-closed (user dir unaffected); run interactively once or use
  `/trust` to trust
- 3 regression tests through the real Guard stack (manual+toggle → auto,
  toggle off resumes prompting, toggle in auto mode is quiet)

## 0.1.22 — Protocol pinning becomes part of the plugin contract

### `find()` gets its consumer; the Skills line is drawn in writing

- **`manifest.protocol` is enforced**: a plugin can pin the contract it
  targets (`"protocol": "tool/v1"` alongside `type`). The declaration is now
  a refuse-load check in `validate_manifest` — an unknown protocol, or a
  protocol whose routed type differs from `manifest.type`, is rejected with
  both type names in the message. This wires `protocols.find()`, which
  existed since P-D with zero callers and a docstring promising exactly this
  consumer. No `protocol` key → the type-table default route, zero impact on
  existing plugins. `strategy.planning` / `orchestration` stay reserved with
  no protocol row (documented: they cannot be pinned yet)
- **`api` and `protocol` are visible**: `/plugins` and `plugin_help`
  projections now include the kernel's supported plugin API version
  (`manifest_api_version()`) and the plugin's pin — the model and the user
  can see the contract, not just the outcome
- **The Skills↔plugins boundary, in writing**: skills are instruction packs
  (prompt-level, one builtin context fragment, no registration surface);
  plugins are code (the four protocols). They don't merge — a skill teaches
  the model *when* to call a plugin's tool. Cross-linked from both docs;
  the speculative `plugin:` frontmatter bridge is explicitly not built
  (no consumer)
- 6 new tests (pin/match, unknown, mismatch, non-string, no-pin regression,
  direct validator)

## 0.1.21 — Plugin author experience: scaffold, reload, sandboxed self-test, a real guide

### The write-test-load loop stops requiring restarts and guesswork

- **`/plugin new <name>`**: writes a loadable minimal tool plugin into the
  project plugins dir — manifest with the S1c permission semantics as
  comments, an example tool, `apply(ctx)`, and a meaningful `self_test`
  (the admission prerequisite). The generated file is the documentation:
  pass admission as-is, edit the TODOs. Refuses to overwrite. (`auto-`
  prefix stays reserved for model-produced plugins.)
- **`/plugin reload <name>`**: explicit dev reload. Session/persistent
  plugins unload and reload fresh; **boot-scoped plugins refresh in place**
  (lifecycle `on_unload` → provenance purge → module drop → re-apply, scope
  preserved) — previously a boot plugin could only refresh by restarting.
  `OPENX_PLUGIN_AUTORELOAD=1` folds plugin-file signatures into the
  composition key so saving a file rebuilds the bundle on the next turn
  (off by default: it stats every plugin file per key check)
- **`/plugin test <name>`**: re-runs `self_test` in the admission sandbox
  (same subprocess as S1b) and shows PASS/FAIL plus the test's own output —
  the model-facing `test_plugin` tool uses the same path. Reloading in
  general hit a subtle trap that's now fixed centrally: the bytecode cache
  validates on **second-granularity mtime + size**, so an equal-length edit
  within the same second made a "reloaded" plugin run its old bytecode —
  `load_module` now drops the cache pyc before every import
- **A real plugin guide**: `docs/subsystems/plugins.md` (+ 中文) — discovery
  order, full manifest reference (including the new `api`/`dependencies`/
  permission-ceiling semantics), the whole `apply(ctx)` surface, security
  model with honest limits, lifecycle, testing/debugging, and the Skills
  boundary. The cookbook's "add a custom tool" no longer teaches editing
  core files first; the plugin path is the default and core edits are framed
  as contributions. (User-guide coverage lives in the same page to avoid
  drift; en + zh.)

## 0.1.20 — Plugins get packaging: directories, dependencies, versions, git install

### A plugin stops being a single .py file; the dead manifest fields grow teeth

- **Directory plugins (S2a)**: `<plugins>/<name>/plugin.py` is now discovered
  alongside single files (same trust gate, same loading pipeline; same-id
  single file wins). The plugin root is exposed as **`ctx.plugin_dir`** —
  data assets live next to `plugin.py` and resolve relatively. Single-file
  plugins see `plugin_dir = None`; nothing about them changed
- **`dependencies` finally mean something (S2b)**: the field was shape-checked
  and never read. Now each declaration must be an ACTIVE plugin at load time —
  a missing or failed dependency fails the plugin **loudly** (error names the
  missing one; the skip shows in `composition_resolved`). Cycles fail both
  sides naturally. Honest limitation: load order is discovery order (user
  dir → project dir, alphabetical), so put depended-on plugins in the user
  dir or pin them via overlay enable — no import-free topological sort at
  boot
- **`api` version + unknown-key warning (S2c)**: `__openx_meta__` may declare
  `api: 1`; a mismatch with the kernel's supported version is a **refuse-load**
  with the supported version in the message (previously a kernel upgrade
  silently broke plugins into PHASE_FAILED mystery). Unknown **top-level**
  manifest keys now warn instead of being silently ignored (a typo'd key used
  to no-op invisibly). Plugins without `api` load unchanged
- **`/plugin install <git-url>` (S2d)**: ASK confirm (serve/headless has no
  approver → fail-closed) → `git clone --depth 1` to a temp dir → locate the
  entry (`plugin.py` at repo root, one level down, or a top-level .py) →
  **admission sandbox gate** (S1b; `file://` works for air-gapped installs,
  the sandbox still runs) → copy into the user plugins dir (`.git` stripped,
  existing target refuses) → session load + tool rebuild. No marketplace, no
  dependency resolution — that's later
- New tests: directory plugins (+ trust-gate interplay), dependency
  satisfied/missing/cycle/session-reject, api gate + unknown-key warning,
  and a full `file://` git-install flow with real admission children

## 0.1.19 — Plugin trust gate, admission sandbox, and a permission ceiling

### The five holes from the plugin audit close; two designed-but-unbuilt items land

- **Boot trust gate (S1a, per openx-kernel-design.md §1.4)**: a hostile repo's
  `.openx/plugins/*.py` no longer executes just because you opened a session
  in it. `discover()` consults the existing `OpenXConfig.is_trusted()` —
  an untrusted project plugin directory yields **zero** specs (user dir and
  entry-points stay trusted by design), the skip is recorded as a
  `plugin_skipped` event, and the trust decision is part of the composition
  key so trusting the workspace reloads plugins automatically. Interactive
  CLI already asks via the trust screen; serve/headless fail closed, and
  `/trust` is the explicit opt-in there. `write_plugin`'s own output bypasses
  the discovery gate (`load_plugin(name, path=...)`) — that code was just
  approved via ASK and admitted through the sandbox; explicit
  `/plugin load` of an untrusted project file gets a hint instead
- **Admission sandbox (S1b, D9 loading-phase L2)**: the first execution of
  plugin code (import + `self_test`) moved out of the main process. New
  `kernel/sandbox/admission.py` runs `python -m openx.kernel.sandbox.admission`
  with the file's sha256 verified on both sides (TOCTOU closed), under
  macOS `sandbox-exec` (deny network, deny file-write outside /tmp; degrades
  to a plain subprocess with rlimits, honestly documented) plus
  `resource.setrlimit`. `subprocess.run` timeout kills the child — the old
  daemon-thread leak (a timed-out thread with full privileges) is gone.
  Every file plugin passes this gate before `load_module` at boot and at
  session load; builtins and pip entry-points are exempt (explicit install).
  `OPENX_PLUGIN_ADMISSION=0` is the documented debug escape hatch
- **Manifest permissions become a capability ceiling (S1c)**: the field was
  decorative — nothing consumed it. Now a plugin tool whose manifest declares
  **no** `permissions` gets its self-declared permission level capped to ASK
  and `auto_allowed` disabled (the wrapper notes the reason; `/plugins`
  shows a warning). Declaring permissions keeps the tool's own levels; Guard's
  high-risk force-ASK still applies on top. **Migration**: existing plugins
  that self-declared ALLOW without declaring permissions will start asking —
  add a `permissions: [...]` list to keep the old behavior
- **contribute/render timeouts (S1d)**: context `contribute()` (5s) and UI
  `render()` (1s) now run bounded in a shared thread pool — a hanging plugin
  can no longer stall system-prompt assembly or the 5Hz deck frame. Repeated
  timeouts trip the same circuit-breaker semantics as crashes (3 → the
  contribution is skipped for the session / the panel is unregistered)
- **Transactional apply + real module unload (S1e)**: a plugin whose
  `apply(ctx)` throws mid-registration no longer leaves half its tools
  registered under PHASE_FAILED — entries are purged by provenance, the
  contribution lists cleared, and the module object removed from
  `sys.modules` (unload used to be nominal: classes and state persisted
  forever)
- New tests: trust gate, admission subprocess (real child processes:
  pass/raise/hang/hash-mismatch/network-deny), timeouts + permission cap,
  transactional failure; two stale test contracts updated to the new
  guarantees. `OPENX_PLUGIN_ADMISSION=0` and pre-trusted workspace defaults
  are wired into the kernel test fixture so the regular suite stays fast

## 0.1.18 — The settings overlay gets a fixed frame

### One box size for every tab; content differences live in the scroll region

- **The settings modal no longer resizes itself per tab**: `.modal.wide` had
  only a `max-height`, so the frame was content-sized — the 通用 tab rendered
  a short box, the 模型 tab a tall one, and the frame jumped on every tab
  switch. It now pins both dimensions (880 × min(660px, 86vh)): head, tab
  strip and body stay put; only `.modal-body` scrolls (`min-height: 0` so
  the flex child actually scrolls inside the fixed column)
- **Tab strip redrawn as a segmented control**: the accent-underline tabs
  become raised pills (hover fill + bordered active) — the strip reads as a
  constant part of the frame skeleton rather than content chrome
- **One horizontal rhythm for all modal parts**: head / tool / reason /
  details / actions all align to the body's 20px padding (was a mix of
  16/18px), so confirm dialogs and the settings shell share the same inset
- CSS only; DOM, JS and tests untouched

## 0.1.17 — The web UI speaks slash: "/" opens the command menu, commands actually run

### Typing `/` in `openx serve` now does what it does in the terminal

- **The "/" menu**: typing `/` as the first character of the web input opens a
  filterable command menu — name, description, aliases — backed by a new
  `GET /api/commands` endpoint that serves the **same source** as the
  terminal's completion menu (`commands.menu_entries`: built-ins, dynamic
  `/<skill-name>` commands, plugin commands). Prefix matches rank first,
  then substring matches on name/description; ↑/↓ move, Enter/Tab select
  (inserts `/name ` into the input), Esc closes, click selects, and the
  menu only appears while the caret is inside the command word
- **Slash commands actually execute on serve**: leading-`/` messages route
  through the same registry the REPL uses (`handle_slash_command`) instead
  of being fed to the model as literal text. Command output is captured by
  a buffered console (a real `Console` with its Rich backend swapped for a
  StringIO — every mixin panel, `/help`'s table included, renders to plain
  text) and broadcast as one `user_message → text_delta → result` turn, so
  the web transcript shape is identical to a model answer. `/clear` clears
  the same shared history/todos; state commands act on the live agent
- **Interactive input degrades loudly, never hangs**: commands that read the
  terminal (`paste_aware_input`, `_interactive_select`) get clean
  "需要交互式输入，请在终端 REPL 使用" responses in web — the session swaps
  the input helper during dispatch and catches the guard; the server's stdin
  is never read and the event loop is never blocked. A dispatch crash still
  broadcasts a `result` (belt and suspenders around the worker), so a broken
  command can't wedge the session into a stuck-busy state
- 7 new tests (endpoint, dispatch/broadcast shape, unknown-command hint,
  both interactive guards, frontend wiring)

## 0.1.16 — Web search grows up: relevance extraction, real search APIs, query fan-out

### The scraping-grade ceiling lifts; the head-truncation waste goes away

- **`web_fetch` returns what you asked for, not the top of the page**: fetched
  pages are no longer head-truncated (modern pages spend their first 400 lines
  on navigation, headers and cookie banners while the answer sits mid-page).
  A zero-cost heuristic (`_extract_relevant`) scores text blocks by query-term
  hits (latin words + CJK runs, stopwords stripped, CJK function chars trimmed
  off the edges), selects the scoring blocks with one line of neighbor context
  and returns them in original order under a `[relevant passages extracted]`
  note. No prompt or zero hits → the old head truncation. The 15-minute cache
  now stores the **cleaned raw page** — extraction runs per call, so re-fetching
  the same URL with a different prompt doesn't get the previous answer's residue
- **Optional small-model distillation** (opt-in, `OPENX_WEB_EXTRACT=on` or
  `settings.json` `web_fetch_extract`): with it on, the heuristic pre-selects
  24K chars and hands them to the model group's **mini role** (falls back to
  the main binding) for a target-aware distillation — the claude-code WebFetch
  behavior this tool's own docstring used to decline for cost reasons. The
  call goes straight through `OpenAICompatProvider` with the role settings
  snapshotted at tool instantiation (a read-only `ToolHost` field — no agent
  loopback); any failure, timeout (60s) or `NO_RELEVANT_CONTENT` silently
  falls back to the heuristic. `web_fetch` never becomes unavailable because
  of it
- **Real search APIs land**: Tavily and Brave backends (keys via
  `OPENX_TAVILY_API_KEY` / `OPENX_BRAVE_API_KEY`) — API-grade ranking,
  content-quality snippets, no anti-bot fights. The `auto` chain admits
  key-holding backends first, then the scraper chain (DDG → Bing) as the
  keyless fallback; `OPENX_WEB_SEARCH=tavily|brave|ddg|bing` still pins one.
  Missing keys degrade, never crash. All searches now run in
  `asyncio.to_thread` — a search can no longer freeze the TUI or serialize
  the executor's parallel `gather` behind a blocking HTTP call
- **Query fan-out**: `web_search` accepts `queries` — up to 3 additional
  variants the model phrases itself. All variants run in parallel (per-query
  backend chains intact), merge round-robin by rank (query diversity without
  privileging the first phrasing), dedupe by normalized URL (host lowercased,
  fragments and trailing slashes dropped), then domain-diversify and cap.
  All backends failing vs. genuinely-no-results stays a strict distinction
- **grep learns `output_mode`**: `content` (default, unchanged), plus
  `files_with_matches` (unique paths) and `count` (matches per file) — both
  derived from the shared match list, so ripgrep and pure-Python backends
  stay output-identical. (`context_lines` already existed.)
- 15 new tests; zero new Python dependencies

## 0.1.15 — Math rendering: real typesetting on the web, dignified text in the terminal

### `$$…$$` stops being literal punctuation soup in both output surfaces

- **The web gets real math (KaTeX, vendored)**: `renderMarkdown` now extracts
  closed math spans — `$$…$$` and `\[…\]` as display blocks, `\(…\)` and
  `$…$` inline — and typesets them through **KaTeX 0.16.11**, vendored into
  `web/vendor/katex/` (js + css + the 20 woff2 fonts, ~630KB) so the local
  server stays self-contained and offline-capable; index.html pulls it with
  `defer`. The extraction runs *after* fenced blocks are pulled aside and
  *before* escaping, with inline code spans pulled out too — a `$…$` inside
  backticks is code, not a formula. Same streaming discipline as fences:
  only **closed** spans render, an unclosed `$$` stays literal until its
  closing pair arrives, then the block appears. Inline `$…$` carries a
  currency guard (closing `$` preceded by whitespace → not math), so
  `cost $100 and $200` is untouched. KaTeX output is safe by construction
  (`trust` stays `false` — the model text can never produce chosen HTML);
  if the script fails to load, the span falls back to escaped text
  (`math-fallback`), never a blank hole
- **The terminal gets a Unicode transcriber** (`ui/_math_text.py`, new): there
  is no TeX engine in the Rich ecosystem and a matplotlib-render-to-image
  dependency would be absurd, so terminal math is transcribed into the
  closest Unicode: Greek letters, operators and relations, `∑ ∫ √` with
  script limits (`∫₀^∞ e⁻ˣ dx`), `1⁄2` fractions (`(n+1)/2` when a side is
  compound), super/subscripts (`E=mc²`, `a₁`) with a **lossless fallback** —
  a character without a variant (`x^q`) keeps its original notation instead
  of half a glyph, accents (`v⃗`), blackboard (`ℝ`), operator names
  (`sin² x`), unknown commands keep their name minus the backslash. Same
  closed-span / fence / inline-code / currency rules as the web, one shared
  discipline. Wired into all three render sites: the streaming frame
  (`_markdown_safe`, so it survives fail-open), the streaming table-height
  measurement, and the non-streaming `print_assistant`
- **`/static` serves subpaths**: the route was flat-only (`{name}` rejected
  any `/`), which would 404 every vendored font. Now `{tail:.+}` with the
  same `resolve()` + prefix guard — traversal and symlink escape stay 404
- **`test_no_build_artifacts_in_static` still holds**: it inspects top-level
  files only; a third-party minified bundle under `vendor/` is a vendored
  dependency, not a build artifact of this repo — the distinction is the
  point
- Zero new Python dependencies; the wheel grows by the ~630KB vendor dir,
  all of it tracked so hatchling ships it exactly once

## 0.1.14 — The retirement eval gate actually runs

### E4 step ① closes: scaffold retirement is now evidence-backed end to end

- **`/scaffolds eval <name>` runs the eval_set** (`services/eval_runner`, new):
  until now the gate could only *judge* success rates handed to it
  (`retirement_gate.compare_success`) — nothing executed the tasks. The runner
  loads the scaffold's declared `eval_set` (JSONL, one task per line, resolved
  relative to the workspace) and runs every task **twice — scaffold present vs
  probed-away** — through headless child agents (`build_child_agent`,
  `role="main"`, so the eval runs on the exact model the scaffold serves). A
  per-task timeout marks the task failed; an agent crash is a failure too.
  Invoking the command is the authorization for the real model spend (same
  discipline as `/distill save`); the task count is printed up front
- **Deterministic judges first** (`openx-eval-run/v1`): a task is
  `{"task": …, "expect": {"contains" / "not_contains" / "regex"}}` —
  case-insensitive substring checks plus a literal `re.search`; `contains`
  must fully hit, `not_contains` fails on the first hit, any failed check
  names the failure in the report. A malformed line rejects the whole set
  (a regression gate that silently drops tasks is a gate with a hole).
  Model-graded judging is left for a later slice — offline first
- **`kernel.scaffold_probe`**: the without-side runs under an *in-memory*
  retirement — the probe entry goes into the same folded retired map
  `retire_scaffold` uses, so the composition skips the plugin through the
  identical `PHASE_RETIRED` path (not imported, no contributions), then the
  `finally` restores the previous set and reloads. It **never emits a decision
  event**: a probe is a measurement, not a verdict — the global ledger stays
  untouched, and the two `composition_resolved` entries the reloads write are
  the honest trace of what happened. The plugin's scaffold declaration rides
  along in the probe entry so `/plugins` keeps showing it via the usual
  ledger-backfill path
- **`--retire` closes the loop**: the eval prints a report (both sides'
  success rates, delta, verdict, per-task failures) and stops there by
  default; with `--retire`, a `retire` verdict retires the scaffold in the
  same step and the `scaffold_retired` event carries this eval's payload as
  its `evidence` — the first time retirement decisions land with real
  measured proof instead of the bare declaration. A `keep` verdict retires
  nothing. `--tolerance <n>` widens the allowed drop (default 0.0), `--json`
  prints the raw evidence payload. Guards: unknown plugin, non-scaffold,
  missing `eval_set`, or an already-retired scaffold (no contrast to run —
  restore first) all refuse before any model call
- **Design doc §3.2 / §8 updated**: the E4 slice's "runner 待" is now landed;
  the only open piece left in the ablation line is model-graded judging

## 0.1.13 — Prompt fragments unified behind the context protocol

### The loop stops importing prompt sources; everything enters by priority

- **Prompt sources unified (K7)**: the system prompt's hard-wired sources —
  OPENX.md instructions, the skills catalog and the memory-usage instructions —
  are now **`context/v1` registrations** from the new built-in plugin
  `builtin-context`, instead of direct imports inside `agent.py`. The loop no
  longer imports concrete prompt sources (`MEMORY_INSTRUCTIONS` and
  `build_skills_prompt` are gone from `agent.py`; the constant moved into the
  plugin). Memory *content* (`memory` / `coding_memory`) stays in the agent — it
  belongs to `memory_backends`, which is not yet registry-backed
- **Priority ordering**: `collect_context_fragments` now collects in **ascending
  `priority`** (ties keep registration order, so built-ins stay first). The
  built-in fragments use 10 (instructions) / 40 (memory hints) / 50 (skills); a
  user context plugin keeps the default 100 and therefore always lands *after*
  them. The character budget is consumed in the same order, so an exhausted
  budget starves the lowest-priority fragments first
- **Payload by arity**: a contribution may declare **one parameter** to receive
  a read-only `PromptHost` (`workspace` / `instructions` / `skills` / `budget`).
  Zero-argument contributions are untouched — the payload is passed **only when
  the callable accepts it**, the same rule lifecycle hooks already used. The
  arity check now lives in one place (`assembly.context.call_with_payload`) and
  is shared by both
- `format_instructions()` extracts the OPENX.md block so `build_system_prompt`
  and the plugin emit it byte-for-byte identically. The assembled prompt is
  content-identical to before; note the instruction block now follows the memory
  blocks (priority-driven) while the remaining fragments keep their relative order
- **`/clear` now really starts over**: `agent.clear_history()` clears the plan
  (`todos`, in place — the list is shared with `TodoWriteTool`/deck) and rebuilds
  the cached system prompt, so the stale `## Current Task List` block no longer
  survives into the fresh context. Serve's new-session/workspace-switch paths get
  the same behavior for free (they already call `clear_history`)
- **Code in an answer no longer lands in scrollback as fragments**: the commit
  watermark kept only the *last rendered line* volatile, assuming it is the line
  still growing. A fenced code block is rendered by Rich's `CodeBlock` →
  `Syntax(padding=1)`, which appends a blank padding line — so the growing code
  line was second-to-last, judged stable, and its half-typed state was frozen
  into scrollback on every frame (5 Hz tick + forced feed refresh). Transcripts
  showed `acc_0`, `acc_`, `ac`… instead of the code. The watermark now skips
  trailing rendered blank lines before choosing the volatile line, so each code
  line commits once, complete. Plain-text answers were unaffected (their last
  line really is the growing one), which is why it only ever bit code
- **The input line is now prompt_toolkit**: the waiting-for-input REPL prompt
  moved from the hand-rolled cbreak editor (~600 lines of termios reading,
  hand-drawn slash menu and manual CJK width/cursor arithmetic in
  `ui/_components/prompt.py`) to a `PromptSession` (new
  `ui/_components/editor.py`). Users get **persistent input history**
  (`~/.openx/history`, ↑/↓ recall), **Ctrl+R incremental reverse search**
  (one Enter submits the hit), full emacs line editing (Home/End, Ctrl+A/E/K/W,
  forward delete) and slash-command completion — the streaming layer
  (`InputCapture`, `Live`) is untouched. Key remap: the idle-state thinking
  expand/collapse moves from Ctrl+R (now history search) to **Alt+R**;
  Ctrl+D on an empty line still re-prompts (never exits), Ctrl+C still exits.
  Alt+Enter and kitty-protocol Shift+Enter still insert literal newlines (ptk's
  ANSI table is extended with `\x1b[13;2u` at import). The frame's bottom rule
  and status line now render inside ptk as the `bottom_toolbar` — a static
  print there would be clobbered by the CompletionsMenu overlay without
  restore (ptk only erases its own region; verified empirically). Non-TTY
  (piped) input keeps the readline fallback byte-for-byte, dialogs/wizard
  readers are untouched
- **`curl … | bash` installs again**: `pyproject.toml` carried a redundant
  `force-include` for `openx/app/serve/web`. `packages = ["openx"]` already
  selects the whole package tree (skipping only VCS-ignored files), so the
  tracked frontend assets were added to the wheel **twice**. Older hatchling
  only warned about the duplicate path and still produced a wheel — which is
  why the entry survived — but current hatchling fails the build outright
  (`A second file is being added to the wheel archive at the same path`), so
  `pip install git+…` died in metadata generation and took the whole installer
  with it (pipx, `uv` and venv paths alike). The entry is gone; the wheel still
  ships all 15 frontend assets, exactly once
- **Version numbers agree again**: `openx.__version__` (0.1.11), the
  distribution version in `pyproject.toml` (0.1.12) and this changelog's newest
  section (0.1.13) had drifted apart, so `openx --version` — and the install
  script's own verification line — reported a version no distribution ever had.
  All three now read 0.1.13
- **The installer no longer defaults to a stale tag**: `OPENX_REF` defaulted to
  `v0.1.2` — the newest tag that exists, ten versions behind — and that tag's
  own `pyproject.toml` carries the `force-include` bug above, so the documented
  install command could never have worked. The default is now `main`
  (install.sh and both READMEs); pin a release tag with `OPENX_REF=vX.Y.Z` once
  one is cut

## 0.1.12 — Kernel gates: resource gate & promotion gate

### Runtime limits owned by the kernel, and dynamic insertion through one gate

- **Resource gate (K4)**: turn/stop/budget limits moved out of the loop invariant
  into `kernel/audit/resource_gate.py`. The kernel holds the gate
  (`resource_gate()` / `set_resource_limits`); each loop pushes the round limit at
  the top of a turn, reads `.limit`, and records `resource_gate_tripped` via
  `.trip()`. New limits (tokens/time/cost) plug in with `register_check` — **swap
  the loop, not the gate**
- **Promotion gate (K6)**: `kernel/audit/admission.py` (`PromotionGate`) is the
  single admission path for dynamic insertion. Read-only (`observe`) contributions
  hot-plug after validation; change (`act`) contributions need an approver; **no
  approver = fail-closed**. `insert_contribution` reuses the existing `register_*`
  surface (same gate as boot plugins, provenance intact) and `remove_contribution`
  rolls back (fires `on_unload`)
- **MCP as the pilot**: each connected server's tools are registered at startup as
  a **session-scoped** contribution (`plugin_id = mcp:<server>`, one factory for
  all of its tools) and removed on shutdown; a composition reload replays them, so
  they never silently vanish mid-session. Every remote call still asks (ASK)
- **Safety ratchet wired**: a hook that tries to *loosen* a decision
  (`permissionDecision: allow`/`approve`, top-level or nested) is discarded and
  recorded once as `ratchet_tightened` in the global ledger (tighten-only)
- The interactive CLI attaches an approver at startup; `serve` wires it through the
  web permission bridge (`fail-closed` until a client attaches)

## 0.1.11 — Skills as SKILL.md packs

### Instruction packs that load only when they are used

- Skills are now **directories** — `<name>/SKILL.md` (personal `~/.openx/skills/`, project
  `<workspace>/.openx/skills/`) — following the open **Agent Skills** standard, with optional
  supporting files (scripts, templates, references). Skills written in the `SKILL.md` format
  load as-is from the Claude skill directories (read-only interop)
- **Progressive disclosure**: the system prompt receives only a *catalog* (name, description
  and pre-approved tools); a skill's **body is loaded on demand** — the user types
  `/<skill-name>`, or the model calls the new **`skill` tool**. Long reference material costs
  no context until it is actually needed
- Frontmatter gains `allowed-tools`, `license` and `argument-hint`. Activating a skill adds
  **session-scoped** allow rules for its `allowed-tools` — held in memory only and **never
  written to `settings.json`**, so installing a skill can never permanently widen
  permissions; `deny` rules still win and high-risk tools still always confirm
- A body may request **dynamic context injection** with `` !`cmd` ``; the command runs through
  the normal tool gate (Guard / permission / hooks / ledger) when the skill activates — never
  bare at prompt-assembly time — and its output replaces the placeholder
- `/skill` gains **`show`** (print a body *without* running injections); `/skill add` prompts
  for pre-approved tools; `/skill install` now accepts a skill directory, a `SKILL.md`, or a
  legacy flat `.md` (migrated to the directory layout, supporting files copied)
- Loading precedence is six-way (Claude interop < legacy flat < native directory, personal <
  project); a same-name conflict resolves to the higher source. Legacy flat `<name>.md` skills
  are still read (tagged `legacy`) and migrated when reinstalled
- The Web UI (Settings → Skills) shows level, pre-approved tools and supporting files, and
  accepts pre-approved tools on create

## 0.1.10 — Composition & promotion persistence

### Resolve the load bundle from a model profile × overlays, and make promotion real

- Added **composition inputs** (`kernel/assembly/composition.py`): the loader no
  longer loads "everything it can find" but the **computed bundle** — the result
  of `model_profile × user overlay × project overlay`. Same-key conflicts resolve
  **user over project**; `settings.json`'s `plugins.disabled` becomes the legacy
  form of a user-level `disable` (both read, writes only to the overlay)
- Overlays are **JSON** (`~/.openx/openx.json`, `<ws>/.openx/openx.json`) to keep
  OpenX dependency-free; a **model profile** is a named sheet under
  `~/.openx/profiles/<name>.json` (`{"retire": [...], "require": [...]}`),
  selected by `plugins.profile`. An empty overlay reproduces today's behaviour
  exactly
- **`auto-*` plugins are excluded from the boot bundle by default** — model-produced
  plugins only survive a restart once enabled. This is what makes *"session first,
  then persistent"* real
- **Promotion persistence (E7)**: `promote_plugin` now **writes the plugin back
  into the composition** (user overlay `enable`) and marks it `scope=persistent`,
  so it is in the boot bundle next time; `unload_plugin` rolls a persistent plugin
  back by removing it from the overlay
- **Profile-driven retirement** (auto-remount): `profile.retire` skips scaffolds
  the model no longer needs (a derived state — changing the profile recomputes the
  bundle), `require` restores them; neither overrides a retirement the user
  *decided* via the ledger
- Every recomposition appends a `composition_resolved` event now carrying the
  profile name, overlay operations and skip reasons; `/composition [json]` shows
  the bundle and why each plugin was skipped

## 0.1.9 — Experience distillation

### Turn sessions into durable memory, and account for recall

- Added `/distill`: mines recent sessions for **candidate experiences** —
  `workflow` (a shell command that succeeded repeatedly), `debug_pattern` (a tool
  that first errored then succeeded) and `project_fact` (a repeatedly edited
  file). Commands run only once are ignored; candidates are deduplicated and
  ordered by kind then frequency
- `/distill save` writes the candidates into coding memory with
  `source="distill"` (distinguishable from model- and user-written memories, so
  they can be reviewed or batch-removed); `/distill json` prints the structured
  report (`openx-distill-report/v1`)
- **Recall accounting**: when the agent assembles a system prompt, every coding
  memory it includes is now recorded as a `memory_recall` event in the session
  ledger; `/distill recall` aggregates them into a recall-frequency ranking — the
  data source for the "memory quality" question
- Offline and read-only over the session ledger; writing happens only on the
  explicit `/distill save`. Writes go to `~/.openx/coding-memory/`, never into a
  project `.openx`

## 0.1.8 — Assembly report

### Align the loaded composition with actual usage

- Added `/assembly`: an offline report that pairs the current composition
  (`/plugins`) against recent usage (the tool calls in the session ledger). It
  ranks active plugins by how often their tools were invoked, lists
  **assembled-but-never-used** plugins (active, non-builtin, zero calls — stop
  loading them, or batch-roll-back `auto-*`), and shows the top tools
- `/assembly suggestions` prints tuning suggestions as text — e.g. *roll back
  auto plugin 'x'* — for a human to act on; `/assembly json` prints the
  structured report (`openx-assembly-report/v1`)
- Plugins that contribute no tools (`context.memory` / `lifecycle` / `ui.panel`)
  are marked non-measurable rather than falsely reported as unused
- Same discipline as `/gaps`: offline, read-only, **evidence not action** — it
  never writes rules, plugins or config. Writing suggestions back into the
  composition (overlay) is a later slice

## 0.1.7 — Gap report

### Turn the session ledger into a human-readable gap report

- Added `/gaps`: an offline analyzer over recent sessions that clusters four
  kinds of recurring, actionable failure signal — `tool_failure` (a tool that
  keeps erroring), `permission_friction` (the same tool asked-and-approved
  repeatedly), `denied_calls` (the same tool blocked repeatedly) and `detour`
  (the same call repeated in one session). Incidental signals are filtered by
  per-kind thresholds and entries are ordered by severity
- `/gaps context` prints the report as a compact **context fragment** you can
  paste into `OPENX.md` (or contribute from a `context.memory` plugin) to feed
  the gaps back into the next session's system prompt; `/gaps json` prints the
  structured report (`openx-gap-report/v1`)
- The report is **evidence, not action**: it only reads the current workspace's
  session ledger (offline, read-only) and never writes rules, plugins or config.
  Tool-failure detection is a heuristic (a leading `Error:` line in the
  tool-result text)

## 0.1.6 — Scaffold retirement

### Scaffolds can now retire, and the decision is recorded

- Added scaffold **retirement**: `/scaffolds retire <name>` records a
  `scaffold_retired` decision on the global ledger (with the declaration and the
  eval evidence) and the next composition **skips** the scaffold — it is not
  imported, so it no longer contributes, but its code and registration remain.
  Removal is not deletion: `/scaffolds restore <name>` records
  `scaffold_restored` and puts it back
- `/scaffolds` lists every declared scaffold (compensates / exit_when / eval_set)
  with its retirement status. Only plugins that declare a `scaffold` block (E1)
  can be retired, and retirement is **user-confirmed** — it is never taken
  silently by the model
- The retired set is **derived from the global ledger** (single source of truth),
  so a retirement survives a restart — a new process folds the same set from the
  ledger. `/plugins` shows retired scaffolds as `retired`; their declaration stays
  visible (backfilled from the ledger decision)
- Added the retirement-gate **policy** (`services/retirement_gate.py`): a
  with-vs-without success comparison yields a `retire` / `keep` verdict and the
  evidence payload the decision carries. This release wires the decision, the
  retired-set bookkeeping and the gate policy; **running** the tasks in an
  `eval_set` (and the profile-linked auto-restore) is a separate slice

## 0.1.5 — Scaffold retirement declarations

### Every scaffold declares why it exists and when it should retire

- Added an optional `scaffold` block to the plugin manifest (`__openx_meta__`):
  `compensates` (which model shortcoming the module offsets) and `exit_when`
  (when it should leave), plus optional `eval_set` (the retirement eval task set)
  and `fallback` (how it comes back when the model regresses). `compensates` and
  `exit_when` are **mandatory** — per v4.1 standard three, a module that cannot
  answer both has no business existing as a scaffold: either it belongs in the
  kernel or it is an ordinary capability plugin
- The kernel validates the block's **shape** only: a missing required field
  rejects the plugin; an unknown `fallback` value or extra key is a warning
  (the same discipline as `type` / `mount` / `permissions`)
- Surfaced read-only through `/plugins`, `list_plugins` (a `⚑scaffold` marker on
  the directory row) and `plugin_help` (the full declaration). This is the input
  the retirement eval gate consumes — declaring a scaffold is not evidence by
  itself

## 0.1.4 — Trajectory cost fields & eval export

### Every turn records its cost

- Added a per-turn `turn_usage` ledger event carrying the turn's **token delta**
  (input / output / cached / plugin) and its wall-clock duration. It is written by the
  top-level session agent at turn end — including when the turn is interrupted or
  raises — so a turn that went wrong still leaves its cost in the trajectory
- Subagents don't emit it: they share the process-level kernel, so without that guard
  their usage would be attributed to the parent session's ledger
- This is the quantitative baseline the self-evolution tuning line needs (context
  budgeting, compaction payoff). Token cost only — **money cost is deferred** until a
  pricing source exists

### Export trajectories as an eval set

- Added `/export-eval [path]` — exports every session of the current workspace to a
  JSONL eval set (one session per line): each turn's user input, assistant output,
  tools called and cost fields, plus per-session totals. This is the input a
  retirement eval gate needs, drawn from the ledger that already exists
- Default output is `~/.openx/eval-export.jsonl`; sessions with no recorded turns are
  skipped

## 0.1.3 — Global decision ledger

### Cross-session decisions have one home

- Added a **global decision ledger** at `~/.openx/ledger.jsonl`. Promotions, rollbacks,
  scaffold retirement and ratchet tightening are *cross-session* facts — filing them
  under whichever session happened to be running misattributes them. They now land in
  one authoritative place, and the session ledger keeps a `decision_ref` pointing at
  the global entry instead of copying its content
- Added `/ledger` — the read surface for that ledger: the most recent decisions with
  their attribution (what, which session, when), plus an integrity check
- The global ledger reuses the session ledger's `seq` + hash chain, and the chain
  **continues across processes**: a restart resumes both `seq` and `digest` from the
  existing file, so a tampered middle entry is reported rather than silently
  re-anchored from empty
- Decisions wired today: `plugin_promoted` (promotion is now recorded globally) and
  `plugin_rolled_back` (unloading a previously promoted plugin). The `scaffold_*` and
  `ratchet_tightened` emitters arrive with the retirement-eval slice

## 0.1.2 — Recovery checkpoints, interrupt memory & auto plan mode

### Recovery: turn-level checkpoints

- Added turn-level checkpoints — after **every tool round** OpenX persists the
  in-flight turn, so a crash, power loss, Ctrl-C or `kill` no longer throws away
  the work already done. Previously a session was only saved at turn boundaries
- Added `--recover` (with `--continue` / `--resume`) to resume an interrupted turn,
  and `--recover-mode {auto,strict,drop}` to choose what happens when the
  checkpoint is unusable
- Completed tool calls are **never replayed**. A checkpoint is committed only once
  every tool result for the round is in the message log, so the log itself is the
  idempotency unit — no skip-list, no retry heuristics
- Calls that were still in flight when the process died are reported to the model
  as `[status: interrupted]` instead of being re-run, since their side effects are
  genuinely unknown
- Added the `on_checkpoint` / `on_resume` plugin lifecycle hooks — declared since
  the lifecycle protocol landed, now actually triggered
- Added the `checkpoint`, `checkpoint_discarded`, `turn_started` and `resume`
  ledger events, and implemented the `interrupt` / `resource_gate_tripped` events
  that were already reserved in the kernel design
- Interrupts are additive to the existing exit paths: Ctrl-C still raises
  `KeyboardInterrupt` and `SIGTERM` still terminates normally — OpenX only flushes
  the in-flight turn before the process unwinds. Esc and the web client interrupt
  reuse the existing cancellation path
- Fixed the ledger hash chain restarting whenever a session was resumed — `seq`
  continued but `digest` started over from empty, which broke the chain exactly
  when it was needed to prove the history was unmodified

### Interrupts keep their context

- Esc, Ctrl-C and the web client interrupt no longer erase the turn they cut short.
  The interrupted turn — **including the message you typed** — is truncated at the
  last legal point and folded into the conversation and the session file, so the next
  message still has something to refer to. Previously the whole turn was dropped, and
  a follow-up like "继续" had nothing to continue from
- Tool calls that were still in flight are recorded as `[status: interrupted]` — the
  same wording the `--recover` path uses — so the model knows the outcome is unknown
  and does not replay them
- A still-running tool now shows a spinner and a ticking elapsed time
  (`⎿ ✢ Running… · 12s`) instead of a frozen `Running…`, so a long command reads as
  "still working" rather than "stuck". The scrollback form stays static — a frozen
  frame there would read as a broken glyph

### Plan mode

- Added the `enter_plan_mode` tool: for a **complex** task — coordinated changes across
  several files, a new feature, a refactor, a migration, or an approach that needs your
  sign-off — the model now switches straight into plan mode as its first action instead
  of asking which mode you want. It announces the switch in one line, explores
  read-only, and submits the plan through `exit_plan_mode`, so the approval point is the
  plan itself. Small, obvious single-file changes never take this path
- Available in manual and auto mode; disabled in single-shot / headless runs, where
  nobody can approve a plan — the tool is not even offered to the model there
- Fixed the system prompt going stale mid-turn: mode switches, plugin load/unload and
  instruction reloads now reach the message sequence on the next round. Until now a
  turn that switched into plan mode lost the write tools but kept the old prompt, so
  the model never learned it was supposed to explore and submit a plan

### Web UI

- Added file uploads and image attachments to the web chat
- Added graph rendering and artifact/path panels
- Added the three-pane layout, with collapsible sidebars
- Added hook activity to the web surface
- Unified elapsed-time formatting with the CLI (`12ms` → `1.5s` → `1m 2s` → `1h 2m`) —
  the web previously had no sub-second or hour tier

### CLI

- Fixed thinking output not being shown for resumed sessions
- Added the current tool (`name(summary)`) to the fleet view so the deck shows
  what each agent is doing right now
- Tool calls are now grouped: a run of consecutive read-only exploration calls collapses
  into one summary line (`Read 2 files, ran 1 command`) instead of scrolling the whole
  transcript away, while writes, `task`, `todo` and `workflow` calls stay individually
  visible — anything that changes something must be readable line by line. A single call
  is never collapsed, and `Ctrl+T` expands a group until it is committed to scrollback
- Fixed the plan/queue deck crowding the line below it — the spinner now sits one blank
  line further down, and that gap is charged to the viewport budget

### Docs

- Repositioned the README and pyproject summary around the microkernel story: a minimal
  trusted core with the agent loop, providers, context and tools as replaceable plugins

### Search

- `web_search` now routes CJK queries to the Chinese-language region (affects
  ranking only) and detects rate-limit / anomaly challenge pages as backend
  failures, so they fall through the degradation chain instead of being reported
  as "no results"

### Install

- Added `install.sh` — install with
  `curl -fsSL https://raw.githubusercontent.com/asmoc-alpha/openx/main/install.sh | bash`;
  prefers pipx, then uv, then a venv at `~/.openx/venv`, and honours `OPENX_REF`
  (git ref) and `OPENX_EXTRAS` (e.g. `web`)

## 0.1.1 — Model groups & Anthropic-compatible protocol

### Configuration: model groups

- Added model groups as the single model/provider config — `modelGroups` (+ `activeGroup`) in `~/.openx/settings.json`, replacing the legacy single model, flat `api_key`/`api_base`, providers and profiles forms
- Added per-group, per-role models — `main`/`exec`/`mini`/`modal` bindings (`openx-main-model` & friends), each able to override kind / endpoint / credentials; roles that are absent fall back to the group's main
- Added `/model <group>` and `/model <group>:<role>` to switch groups or set a role's model; `/config` views and edits the active group
- Added `env:VAR` indirection for `apiKey`/`apiBase` inside groups — the only external credential channel
- Added the first-run setup wizard, which writes a `default` model group

### Providers & routing

- Added `anthropic-compat` — the anthropic kind now points at any Anthropic-format endpoint via `apiBase` (e.g. DeepSeek) and defaults to Anthropic's official API when blank; the legacy `anthropic` kind remains as an alias
- Added project-scoped group selection — a project's `.openx/settings.json` `activeGroup` picks which group that workspace starts on
- Added multi-modal routing — image-bearing turns use the group's `modal` model when declared, falling back to `main` otherwise

### CLI & Web

- Removed the `--model`/`-m`, `--api-key`, `--api-base`, `--max-rounds` and `--temperature` launch flags — models and tuning live in model groups or project settings; `--image`/`-i` is retained for one-shot image analysis
- Added the web UI — `openx serve` exposes a browser interface to the same agent

## 0.1.0 — First public release

OpenX is an agentic coding CLI in Python — chat with your codebase using any
OpenAI-compatible LLM. This release consolidates the full pre-release history.

### Core loop

- Added the agentic loop — autonomous tool calling, up to 30 rounds per query
- Added parallel tool execution — independent calls run concurrently; permission checks stay serial
- Added retry with exponential backoff for transient API failures (429 / 5xx / connection errors / mid-stream disconnects); Retry-After headers honored, tunable via max_retries / retry_base_delay
- Added auto-compaction at 80% of the history token budget, or manually via /compact

### Tools

- Added file tools — read_file, write_file, edit_file (find-and-replace semantics), glob, list_directory
- Added shell execution, grep code search with regex, and git status/diff/log/branch
- Added web tools — web_fetch, web_search
- Added image analysis — files, clipboard screenshots (/image, /clipboard, --image)
- Added persistent memory — /remember, /memory, /forget
- Added colored unified diffs in write/edit approval dialogs

### Modes & permissions

- Added three permission modes — manual (startup default: reads run free, every write confirms), auto, and plan (read-only exploration, then approve the plan); /mode [manual|auto|plan]
- Added the choose_mode flow — manual mode offers Auto / Plan / Stay in manual on the first task that needs changes
- Dangerous shell commands always prompt — never skipped by rules, whitelist, or -y
- Added a three-tier permission system (allow / ask / deny) with stored rules via /permissions
- Added workspace scoping — no writes outside project boundaries by default

### Subagents & orchestration

- Added subagents — task tool, builtin types (general-purpose, explore) plus custom .openx/agents/*.md; children cannot nest
- Added structured output — task and workflow agent() accept a JSON Schema and return the validated object via structured_output
- Added the live status deck — todos checklist plus one status row per parallel subagent (5 Hz); Ctrl-O cycles into sub-agent detail views
- Added workflows — deterministic multi-agent orchestration in Python scripts (.openx/workflows/), with agent / parallel / pipeline / phase / log hooks
- Added background tasks — shell run_in_background with task_output / task_stop; cleanup on exit

### Extensions

- Added hooks — Claude-Code-compatible shell hooks on PreToolUse / PostToolUse / UserPromptSubmit / Stop
- Added MCP — stdio servers via mcpServers in settings.json, zero extra dependencies (newline-delimited JSON-RPC)
- Added sessions — append-only JSONL under ~/.openx/sessions; resume with --continue / --resume

### Terminal & UI

- Added interrupt & queued input — Esc interrupts while the agent thinks or answers; Enter queues a message during streaming, Esc sends it
- Added slash command completion — type / to browse and filter, ↑↓ to navigate, Tab to complete
- Added terminal-resize handling — streaming re-anchors on window resize; CJK-aware cursor reposition
- Added the visual design — restrained chrome-grey palette with one accent colour, pixel-art mascot, cleaner panels and dialogs
- Added /release-notes (/release) — browse release notes by version; /config edits model, API key, and base URL

### Headless & CI

- Added headless JSON output — --output-format json (one result object) or stream-json (NDJSON events) with CI exit codes
- Added multi-provider support — any OpenAI-compatible API (OpenAI, Anthropic via proxy, DeepSeek, …)
