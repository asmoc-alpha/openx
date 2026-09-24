# Composition

English | [中文](composition.zh.md)

The kernel does not load "every plugin it can find". It loads the **computed
bundle** — the result of resolving the *composition inputs* of the current
workspace. That keeps every session's plugin set reproducible and auditable.

```
model_profile (per-model-version capability face)
  × user overlay    (~/.openx/openx.json)     patch primitives
  × project overlay (<ws>/.openx/openx.json)  same primitives
  = bundle (computed) — the loader only loads plugins inside it
```

```bash
/composition        # human-readable: bundle + skipped items + reasons
/composition json   # the structured summary (kernel.composition_summary)
```

## The three inputs

**Model profile** — a named JSON sheet under `~/.openx/profiles/<name>.json`,
selected by `plugins.profile` in `settings.json`:

```json
{ "name": "gpt-5", "retire": ["histcompact"], "require": [] }
```

`retire` marks scaffolds this model no longer needs (they are skipped, code and
registration intact); `require` re-includes them. Because the profile is a
*derived* input, changing it recomputes the bundle — the "auto re-mount on
downgrade" of the retirement line, for free. A profile never overrides a
retirement that a user *decided* via the ledger (that needs `/scaffolds
restore`).

**User / project overlays** — patch primitives acting on the profile result:

```json
{ "plugins": { "enable": ["auto-greet"], "disable": ["noisy"] } }
```

- Same-key conflict: **user overlay wins over project overlay.**
- `plugins.disabled` in `settings.json` is the legacy form of a user-level
  `disable`; both are read (union), but writes only go to the overlay — no two
  competing write sources.
- `add` / `remove` / `replace` are parsed and recorded but reserved for now.

## Defaults

- **Empty overlay ≡ today.** With no overlay and no profile, the bundle is the
  builtin plugins plus every discovered plugin.
- **`auto-*` is excluded by default.** Model-produced plugins (`auto-*`) do not
  enter the boot bundle unless enabled — this is what makes *"session first, then
  persistent"* real: `promote_plugin` writes the plugin into the user overlay's
  `enable`, and only then does it survive a restart. See [plugins & promotion]
  below.

## Promotion & rollback (E7)

`promote_plugin` on an `auto-*` plugin:

1. records the `plugin_promoted` decision on the global ledger (cross-session
   fact);
2. sets `trust=user` and `scope=persistent`;
3. **writes the plugin into the user overlay's `enable`** — so it is in the boot
   bundle next time.

Rollback is unload: `unload_plugin` on a persistent plugin removes it from the
overlay `enable` and records `plugin_rolled_back`; next boot it is back to the
factory default (not in the bundle).

## Ledger

Every actual (re)composition appends a `composition_resolved` event carrying the
profile name, the overlay operations applied, the final load list, and the skip
reasons — any session's bundle can be reproduced after the fact.

## Limits

- **JSON, not YAML.** The overlay is JSON to keep OpenX dependency-free; the
  original design sketches said `.yml`.
- **`add` / `remove` / `replace` reserved.** Only `enable` / `disable` are
  consumed today (they cover plugin-level bundling).
- **Profiles are declarative.** OpenX does not probe a model's capabilities; you
  author the profile.

## See also

- [Scaffolds](scaffolds.md) — retirement declarations and the eval gate (E1/E4)
- [Self-evolution design](../design/openx-self-evolution-design.md) — §1.1 ring ⑤
