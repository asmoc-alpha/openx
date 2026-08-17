"""First-run interactive setup wizard.

Prompts the user for API base URL, API key, and default model, then saves
to ``~/.openx/settings.json``.
"""

from __future__ import annotations

# ── 独立调试支持：允许直接运行本文件（python openx/.../xxx.py）──────
if __name__ == "__main__" and not __package__:
    import sys as _sys
    from pathlib import Path as _Path
    _file = _Path(__file__).resolve()
    _root = _file.parent
    while _root != _root.parent and not (_root / "pyproject.toml").exists():
        _root = _root.parent
    _sys.path.insert(0, str(_root))
    __package__ = ".".join(_file.relative_to(_root).parts[:-1])

from ...config import OpenXConfig
from ...ui.console import Console


async def run_setup_wizard() -> dict:
    """Run the interactive first-run setup wizard.

    Returns an env dict ready for ``OpenXConfig.save_settings()``.
    """
    # Use a bare-minimum console for setup (no config needed yet)
    console = Console(OpenXConfig())

    console.print_setup_welcome()

    env: dict[str, str] = {}

    # Step 1: API Base URL
    env["OPENX_BASE_URL"] = console.prompt_setup_field(
        step=1, total=3,
        label="API Base URL",
        default="https://api.openai.com/v1",
    )

    # Step 2: API Key
    env["OPENX_API_KEY"] = console.prompt_setup_field(
        step=2, total=3,
        label="API Key",
        default="",
    )

    # Step 3: Default Model
    env["OPENX_DEFAULT_MODEL"] = console.prompt_setup_field(
        step=3, total=3,
        label="Default Model",
        default="gpt-4o",
    )

    # Review and confirm
    while not console.print_setup_summary(env):
        console.raw.print("\n[dim]Let's try again...[/dim]")
        env["OPENX_BASE_URL"] = console.prompt_setup_field(
            step="*", total=3, label="API Base URL", default=env["OPENX_BASE_URL"],
        )
        env["OPENX_API_KEY"] = console.prompt_setup_field(
            step="*", total=3, label="API Key", default=env["OPENX_API_KEY"],
        )
        env["OPENX_DEFAULT_MODEL"] = console.prompt_setup_field(
            step="*", total=3, label="Default Model", default=env["OPENX_DEFAULT_MODEL"],
        )

    console.print_success("Settings saved to ~/.openx/settings.json")
    console._console.print()
    return env


if __name__ == "__main__":
    import inspect
    print(f"entry: run_setup_wizard{inspect.signature(run_setup_wizard)}")
    print("(wizard not launched — would prompt for input)")
    print("openx/cli/setup_wizard.py OK ✓")
