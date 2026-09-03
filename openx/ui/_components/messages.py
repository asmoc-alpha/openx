from __future__ import annotations

"""Message helpers: errors, warnings, info, help, tips, release notes."""

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

from .._helpers import box_rounded
from .._style import (
    ACCENT, ACCENT_BOLD, CHROME, DIM, ERROR_STYLE, INFO_STYLE,
    MARK_BULLET, MARK_FAIL, MARK_INFO, MARK_OK, MARK_WARN,
    SUCCESS_STYLE, WARNING_STYLE,
)


class MessagesMixin:
    """User-facing messages — errors, warnings, help, tips.

    标记体系（v0.5.0）：几何符号家族 ✓ ✕ ▲ ● 取代 emoji——语义靠
    符号形状 + 颜色双通道传达，字形跨平台稳定、与框线字符风格统一。
    """

    _console: object

    def print_error(self, message: str) -> None:
        self._console.print(f"\n{MARK_FAIL} {message}", style=ERROR_STYLE)

    def print_warning(self, message: str) -> None:
        self._console.print(f"{MARK_WARN}  {message}", style=WARNING_STYLE)

    def print_info(self, message: str) -> None:
        self._console.print(f"{MARK_INFO}  {message}", style=INFO_STYLE)

    def print_success(self, message: str) -> None:
        self._console.print(f"{MARK_OK} {message}", style=SUCCESS_STYLE)

    def print_goodbye(self, usage: dict | None = None) -> None:
        # Drop the pinned-frame scroll region so the terminal behaves
        # normally again before exiting.
        self.reset_scroll_region()
        # 交互退出时（/quit、EOF、Ctrl-C 汇聚到此）先展示本次会话的
        # token 用量四项，再告别。usage 缺失（非交互/服务端路径）不展示。
        if usage:
            self.print_session_usage(usage)
        self._console.print("\nGoodbye.", style=DIM)

    # ── help / tips / release notes ─────────────────────────────

    def print_help(self) -> None:
        """Well-formatted help panel with all available commands."""
        table = Table(show_header=False, box=None, padding=(0, 2), expand=False)
        table.add_column("cmd", style=ACCENT_BOLD, width=24)
        table.add_column("desc")
        for cmd, desc in [
            ("/quit, /exit, /q", "Exit OpenX"),
            ("/help", "Show this help"),
            ("/clear", "Clear screen and conversation history"),
            ("/model [group][:role]", "List model groups / switch active group or a role's model"),
            ("/workspace <path>", "Change workspace directory"),
            ("/auto-approve", "Toggle auto-approve mode"),
            ("/mode [mode]", "Show or switch permission mode (manual / auto / plan)"),
            ("/explore", "Show project overview"),
            ("/init", "Create an OPENX.md instruction file"),
            ("/instructions", "Show loaded OPENX.md instructions"),
            ("/image <path>", "Load and analyze an image file"),
            ("/clipboard", "Paste and analyze a clipboard screenshot"),
            ("/memory", "Show all stored memories"),
            ("/remember <fact>", "Save a fact to persistent memory"),
            ("/forget <name>", "Delete a memory"),
            ("/permissions", "Show and manage stored permission rules"),
            ("/hooks", "Show configured hooks"),
            ("/mcp", "Show MCP server status"),
            ("/workflow [name]", "List or run saved workflows"),
            ("/todos", "Show the agent's task list"),
            ("/cost", "Show cumulative token usage"),
            ("/compact", "Summarize history to free up context"),
            ("/tips", "Show usage tips"),
            ("/release-notes, /release", "Browse release notes by version"),
            ("/git", "Show git status"),
            ("/diff", "Show git diff"),
            ("/config", "Show current configuration"),
        ]:
            table.add_row(cmd, desc)
        tips = Text()
        tips.append("\nTips", style="white")
        tips.append("\n", style=DIM)
        for tip in [
            "Type anything to chat with the agent",
            "The agent can read/write files, run commands, search code",
            "Esc interrupts · Ctrl+C exits",
            "Place an OPENX.md file in your project for custom instructions",
            "Use /init to create a starter OPENX.md template",
        ]:
            tips.append(f"  {MARK_BULLET} {tip}\n", style=DIM)
        inner = Table.grid(padding=(0, 0))
        inner.add_row(table)
        inner.add_row(tips)
        self._console.print(
            Panel(
                inner,
                title="Commands",
                title_align="left",
                border_style=CHROME,
                box=box_rounded(),
                padding=(1, 2),
            )
        )

    def print_tips(self) -> None:
        """Full tips list in a panel."""
        tips_text = Text.from_markup(
            "[cyan]▸[/cyan] Type [bold]anything[/bold] to chat with the agent — "
            "it can read, write, edit, and search your code.\n"
            "[cyan]▸[/cyan] Use [bold]/help[/bold] to see all available commands.\n"
            "[cyan]▸[/cyan] Run [bold]/init[/bold] to create an OPENX.md with "
            "project instructions.\n"
            "[cyan]▸[/cyan] Press [bold]Esc[/bold] to interrupt the agent while "
            "it thinks or answers — you stay in the conversation "
            "([bold]Ctrl+C[/bold] exits).\n"
            "[cyan]▸[/cyan] Type while the model answers: [bold]Enter[/bold] "
            "queues your message (shown under the frame); [bold]Esc[/bold] "
            "interrupts and sends it.\n"
            "[cyan]▸[/cyan] While the agent works, the panel under the input "
            "frame shows its plan and running sub-agents — press "
            "[bold]Ctrl+O[/bold] to view a sub-agent's progress.\n"
            "[cyan]▸[/cyan] The agent works best with specific, concrete requests — "
            "tell it [italic]what[/italic] to do rather than [italic]how[/italic].\n"
            "[cyan]▸[/cyan] Place an [bold]OPENX.md[/bold] file in your project "
            "root for custom instructions that the agent follows automatically."
        )
        self._console.print()
        self._console.print(
            Panel(
                tips_text,
                title="Tips",
                title_align="left",
                border_style=CHROME,
                box=box_rounded(),
                padding=(1, 2),
            )
        )

    def print_release_notes(self) -> None:
        """Full release notes in a panel."""
        from .layout import LayoutMixin
        self._console.print()
        self._console.print(
            Panel(
                Text.from_markup(LayoutMixin.release_notes_markup()),
                title="Release notes",
                title_align="left",
                border_style=CHROME,
                box=box_rounded(),
                padding=(1, 2),
            )
        )

    def print_release_version(
        self, version: str, title: str, bullets: list
    ) -> None:
        """单个版本的发布说明面板（/release 选择查看用）。"""
        body = "\n".join(f"[dim]{MARK_BULLET}[/dim] {b}" for b in bullets)
        self._console.print()
        self._console.print(
            Panel(
                Text.from_markup(body),
                title=f"v{version} — {title}",
                title_align="left",
                border_style=CHROME,
                box=box_rounded(),
                padding=(0, 1),
            )
        )


if __name__ == "__main__":
    import io
    from rich.console import Console
    _buf = io.StringIO()
    _m = MessagesMixin()
    _m._console = Console(file=_buf, width=100)
    _m.print_error("boom")
    _m.print_warning("careful")
    _m.print_info("fyi")
    _m.print_success("yay")
    _m.print_help()
    _m.print_tips()
    _m.print_release_notes()
    print(f"captured {len(_buf.getvalue())} chars of messages rendering")
    print("openx/ui/_components/messages.py OK ✓")
