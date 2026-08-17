# Contributing to OpenX

Thanks for considering a contribution! Issues, docs improvements, and bug fixes are all
welcome. ([中文](CONTRIBUTING.zh.md))

## Development setup

Requires Python ≥ 3.10.

```bash
git clone https://github.com/asmoc-alpha/openx.git   # or your fork
cd openx
pip install -e ".[dev]"

python -m pytest tests -q     # run the test suite
```

## Tests

- `pytest-asyncio` runs in `auto` mode — write async tests directly, no decorators needed.
- Prefer hand-written fakes (`FakeLLM`, `FakeConsole` in `tests/test_bugfixes.py`) over `unittest.mock`.
- Terminal UI behavior is tested with [pyte](https://pypi.org/project/pyte/) screen
  simulation and real-pty end-to-end harnesses — see `tests/services/test_terminal_interaction.py`
  and `tests/services/test_esc_interrupt.py` for the established patterns.
- Settings/sessions/tasks paths are monkeypatched via module constants; never touch real
  user state in tests.

Any user-visible change should come with a regression test.

## Code conventions

- Match the surrounding code: comment density, naming, error-handling idiom.
- UI text uses the geometric marker family and color constants in `openx/ui/_style.py`
  (no emoji, single accent color).
- Docs are bilingual — update the `.zh.md` twin whenever you change a doc page.
- Release bookkeeping (maintainers): version lives in **two places**
  (`pyproject.toml` and `openx/__init__.__version__`) and release notes go in
  `openx/CHANGELOG.md` (data source of the startup panel and `/release-notes`).

## Pull requests

1. For anything non-trivial, open or claim an issue first so work doesn't collide.
2. Keep PRs focused — one change per PR.
3. Make sure `python -m pytest tests -q` is green; CI runs it on 3.10 and 3.12.
4. Fill in the PR template; include terminal screenshots for UI changes.

## License

By contributing you agree your work is released under the project's MIT license.
