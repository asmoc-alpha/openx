from __future__ import annotations

"""Miscellaneous display: todos, cost, images, diffs."""

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

from pathlib import Path

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from rich.markup import escape

from .._helpers import box_rounded
from .._style import (
    ACCENT, CHROME, DIM, MARK_FAIL, MARK_INFO, MARK_OK, MARK_PENDING,
    MARK_WARN, SUCCESS_STYLE,
)


class MiscMixin:
    """Todo list, cost summary, image metadata, and file diff display."""

    _console: object

    # ── plugins（微内核 inventory 面板）────────────────────────

    def print_plugins(self, infos: list) -> None:
        """Render the kernel inventory: phase, contributions, warnings.

        ``infos`` 为 ``openx.kernel.PluginInfo`` 列表（只读投影）。
        动态字段一律 escape——插件 id/警告可含方括号，防 MarkupError。
        """
        if not infos:
            self._console.print(
                "[dim]No plugins loaded. Drop .py modules into "
                "~/.openx/plugins/ or .openx/plugins/.[/dim]"
            )
            return
        for info in infos:
            if info.phase == "active":
                marker, style = f"{MARK_OK} active", SUCCESS_STYLE
            elif info.phase == "failed":
                marker, style = f"{MARK_FAIL} failed", "red"
            elif info.phase == "disabled":
                marker, style = f"{MARK_PENDING} disabled", DIM
            else:
                marker, style = f"{MARK_PENDING} {escape(info.phase)}", DIM
            parts = [f"[{style}]{marker}[/] {escape(info.id)} [dim]({escape(info.source)})[/dim]"]
            contrib = []
            if info.tools:
                contrib.append("tools: " + ", ".join(escape(t) for t in info.tools))
            if info.commands:
                contrib.append("commands: " + ", ".join(escape(c) for c in info.commands))
            if info.providers:
                contrib.append(
                    "providers: " + ", ".join(escape(p) for p in info.providers)
                )
            if contrib:
                parts.append("[dim]·[/] " + " [dim]·[/] ".join(contrib))
            self._console.print(" ".join(parts))
            if info.error:
                self._console.print(f"    [red]{MARK_FAIL} {escape(info.error)}[/red]")
            for w in info.warnings:
                self._console.print(f"    [dim]{MARK_WARN} {escape(w)}[/dim]")

    # ── todos ───────────────────────────────────────────────────

    def print_todos(self, todos: list[dict]) -> None:
        """Render the agent's task list with status coloring."""
        if not todos:
            self._console.print(
                "[dim]No tasks. The agent will create todos for multi-step work.[/dim]"
            )
            return
        table = Table(
            show_header=False,
            box=None,
            padding=(0, 1),
            expand=False,
        )
        table.add_column("status", width=12)
        table.add_column("task")
        for t in todos:
            status = t.get("status", "pending")
            content = t.get("content", "")
            if status == "completed":
                marker, style = f"{MARK_OK} done", SUCCESS_STYLE
            elif status == "in_progress":
                # 与状态层同款：进行中标记 + activeForm（"正在做什么"）
                marker, style = f"{MARK_INFO} working", ACCENT
                content = t.get("activeForm") or content
            else:
                marker, style = f"{MARK_PENDING} pending", DIM
            table.add_row(f"[{style}]{marker}[/{style}]", content)
        done = sum(1 for t in todos if t.get("status") == "completed")
        self._console.print(
            Panel(
                table,
                title=f"Tasks  {done}/{len(todos)}",
                title_align="left",
                border_style=CHROME,
                box=box_rounded(),
                padding=(0, 1),
            )
        )

    # ── cost ────────────────────────────────────────────────────

    def print_cost(self, input_tokens: int, output_tokens: int) -> None:
        """Cumulative token usage with rough cost estimate."""

        def _fmt(n: int) -> str:
            return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

        total = input_tokens + output_tokens
        body = (
            f"[dim]Input [/dim]{_fmt(input_tokens)} tokens   "
            f"[dim]Output[/dim] {_fmt(output_tokens)} tokens   "
            f"[dim]Total  [/dim]{_fmt(total)} tokens"
        )
        self._console.print(
            Panel(
                Text.from_markup(body),
                title="Usage",
                title_align="left",
                border_style=CHROME,
                box=box_rounded(),
                padding=(0, 1),
            )
        )

    # ── images ──────────────────────────────────────────────────

    def print_image_loaded(self, path: Path, metadata: dict) -> None:
        """Notice that an image has been loaded."""
        w, h = metadata.get("width", 0), metadata.get("height", 0)
        fmt = metadata.get("format", "?").upper()
        size = metadata.get("size_bytes", 0)
        size_str = self._format_bytes(size)
        t = Text()
        t.append("🖼  ", style="magenta")
        t.append(f"{path.name}", style="bold white")
        t.append(f"  {w}x{h}  {fmt}  {size_str}", style=DIM)
        self._console.print(t)

    def print_image_display_meta(self, path: Path, metadata: dict) -> None:
        """Show image metadata when terminal rendering is unavailable."""
        w, h = metadata.get("width", 0), metadata.get("height", 0)
        fmt = metadata.get("format", "?").upper()
        size = metadata.get("size_bytes", 0)
        size_str = self._format_bytes(size)
        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column(style=DIM, width=10)
        table.add_column(style="white")
        table.add_row("Size:", f"{w}x{h}")
        table.add_row("Format:", fmt)
        table.add_row("Bytes:", size_str)
        table.add_row("Path:", str(path))
        self._console.print(
            Panel(table, title="Image", border_style=CHROME)
        )

    @staticmethod
    def _format_bytes(size: int) -> str:
        for unit in ("B", "KB", "MB"):
            if size < 1024:
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} GB"

    # ── file diff ───────────────────────────────────────────────

    def print_file_diff(
        self, file_path: str, old: str, new: str, max_lines: int = 400
    ) -> None:
        """以彩色 unified diff 展示一次文件变更。

        权限审批弹窗（``ask_permission(diff=...)``）与任何需要展示"将
        要/已经发生的变更"的场景共用本方法。diff 文本由
        ``utils.text.unified_diff_text`` 单一生成，``rich.Syntax`` 的
        diff 词法器负责红绿着色；超长 diff 按 ``max_lines`` 截断。
        """
        from rich.syntax import Syntax
        from ...utils.text import unified_diff_text

        diff = unified_diff_text(file_path, old, new, max_lines=max_lines)
        if not diff:
            self._console.print(f"[{DIM}](no changes: {file_path})[/{DIM}]")
            return
        theme = getattr(getattr(self, "config", None), "syntax_theme", "monokai")
        self._console.print(
            Panel(
                Syntax(diff, "diff", theme=theme, word_wrap=False),
                title=file_path,
                border_style=CHROME,
                box=box_rounded(),
            )
        )


if __name__ == "__main__":
    import io
    from rich.console import Console
    _buf = io.StringIO()
    _m = MiscMixin()
    _m._console = Console(file=_buf, width=100)
    _m.print_todos([{"content": "write tests", "status": "completed"},
                    {"content": "ship it", "status": "in_progress"},
                    {"content": "docs", "status": "pending"}])
    _m.print_cost(12000, 3400)
    _m.print_image_loaded(Path("shot.png"), {"width": 800, "height": 600, "format": "png", "size_bytes": 20480})
    _m.print_file_diff("a.py", "old = 1\nsame", "new = 2\nsame")
    print(f"captured {len(_buf.getvalue())} chars | _format_bytes(2048)={_m._format_bytes(2048)}")
    print("openx/ui/_components/misc.py OK ✓")
