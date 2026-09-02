# Development

English | [中文](development.zh.md)

Contributor setup and daily workflow.

## Setup

OpenX requires Python ≥ 3.10.

```bash
git clone https://github.com/asmoc-alpha/openx.git
cd openx
pip install -e ".[dev]"
```

The editable install puts the `openx` command on your PATH and pulls in the `dev`
extra: pytest, pytest-asyncio, and ruff.

## Running OpenX

```bash
openx                      # interactive REPL
openx "fix the failing test"   # single-shot mode
```

First run launches the setup wizard (API key, model); answers are saved to
`~/.openx/settings.json`. See [configuration](user/guide/configuration.md).

## Tests

```bash
pytest                                        # full suite
pytest tests/tools/test_tools_base.py         # one file
pytest tests/tools/test_tools_base.py -k edit # filter by name
```

Collection is restricted to `tests/` (`testpaths` in `pyproject.toml`), and async tests
run without decorators (`asyncio_mode = "auto"`). Test files are organized into
subdirectories mirroring the `openx/` package layout (`tests/orchestration/`,
`tests/kernel/`, `tests/serve/`, `tests/llm/`, `tests/services/`, `tests/tools/`,
`tests/ui/`, `tests/mcp/`); tests for top-level
modules (`agent`, `main`, `image`, `instructions`) live at the `tests/` root.

## Lint

```bash
ruff check openx tests
```

Line length is 100 and the target version is py310 (`[tool.ruff]` in `pyproject.toml`).

## Dependency constraint: rich

The streaming display relies on rich private interfaces (`Live._lock`,
`LiveRender._shape`, `Console._lock` — see `_ResizeAwareLive` in
`openx/services/streaming.py`). rich 13's `stop()` flush/early-return semantics do not
match the cursor arithmetic the done/cancel paths assume; versions 14 and 15 behave
identically (bytecode-level comparison). The pin `rich>=14,<16` encodes this. Before
upgrading to 16, re-check those private surfaces.

## Release notes

Release notes live in [`openx/CHANGELOG.md`](../openx/CHANGELOG.md) — one
`## <version> — <title>` section per release, newest first (Claude Code style).
`openx/changelog.py` parses the file at import time into the startup "What's new"
panel and `/release-notes` (alias `/release`); `###` group headings and prose lines
inside a section are fine, only `- ` bullets are collected. To cut a release:

1. Prepend the new section to `openx/CHANGELOG.md`.
2. Bump `version` in `pyproject.toml` and `__version__` in `openx/__init__.py`.

## Documentation

Docs live in `docs/` as bilingual pairs (`xxx.md` + `xxx.zh.md`); update both sides in
the same change. Structure and writing rules: [docs/AGENTS.md](AGENTS.md).
