from __future__ import annotations

"""Setup wizard: welcome screen, field prompts, review summary."""

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

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .._helpers import box_rounded, mask_key
from .._style import CHROME, DIM, PROMPT_STYLE
from .prompt import paste_aware_input


class SetupMixin:
    """First-run setup wizard components."""

    _console: object

    def print_setup_welcome(self) -> None:
        """Print the setup wizard welcome screen."""
        self._console.print()
        self._console.print(
            Panel(
                Text.from_markup(
                    "\n"
                    "  [bold white]Welcome to OpenX![/bold white]\n\n"
                    "  Let's configure your API connection. You'll need:\n\n"
                    "  [cyan]1.[/cyan] [dim]API Base URL[/dim] — the endpoint for your LLM provider\n"
                    "  [cyan]2.[/cyan] [dim]API Key[/dim] — your authentication key\n"
                    "  [cyan]3.[/cyan] [dim]Default Model[/dim] — which model to use\n\n"
                    "  [dim]Press Enter to accept the default value shown in brackets.[/dim]\n"
                    "  [dim]Your settings will be saved to ~/.openx/settings.json[/dim]\n"
                ),
                title="[bold cyan]openx setup[/bold cyan]",
                title_align="left",
                border_style=CHROME,
                box=box_rounded(),
                padding=(1, 2),
            )
        )

    def prompt_setup_field(
        self, step, total: int, label: str, default: str
    ) -> str:
        """Prompt for a single setup field. Returns the user's input or *default*."""
        self._console.print()
        t = Text()
        t.append(f"  Step {step}/{total}: ", style="bold cyan")
        t.append(label, style="bold white")
        self._console.print(t)
        disp = mask_key(default) if "KEY" in label.upper() and default else default
        self._console.print(f"  [dim][{disp}][/dim]")
        value = (
            paste_aware_input(
                self._console, f"  [{PROMPT_STYLE}]▸[/{PROMPT_STYLE}] "
            )
            .strip()
        )
        return value if value else default

    def print_setup_summary(self, env: dict) -> bool:
        """Show a summary and ask for confirmation. Returns True on accept."""
        self._console.print()
        table = Table(show_header=False, box=None, padding=(0, 2), expand=False)
        table.add_column("key", style=DIM, width=16)
        table.add_column("value", style="white")
        table.add_row("API Base URL:", env.get("OPENX_BASE_URL", ""))
        table.add_row("API Key:", mask_key(env.get("OPENX_API_KEY", "")))
        table.add_row("Model:", env.get("OPENX_DEFAULT_MODEL", ""))
        self._console.print(
            Panel(
                table,
                title="[bold]Review settings[/bold]",
                title_align="left",
                border_style=CHROME,
                box=box_rounded(),
                padding=(1, 2),
            )
        )
        response = (
            paste_aware_input(
                self._console,
                "\n  [yellow]Save these settings?[/yellow] [dim][Y/n][/dim] ",
            )
            .strip()
            .lower()
        )
        return response in ("", "y", "yes")


if __name__ == "__main__":
    import io, inspect
    from rich.console import Console
    _buf = io.StringIO()
    _m = SetupMixin()
    _m._console = Console(file=_buf, width=100)
    _m.print_setup_welcome()  # 纯渲染；其余两个方法会读 stdin，只打印签名
    print(f"welcome panel: {len(_buf.getvalue())} chars")
    print("prompt_setup_field", inspect.signature(SetupMixin.prompt_setup_field))
    print("print_setup_summary", inspect.signature(SetupMixin.print_setup_summary))
    print("openx/ui/_components/setup.py OK ✓")
