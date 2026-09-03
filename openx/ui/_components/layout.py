from __future__ import annotations

"""Layout components: header, startup panel, project overview, status line."""

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

from rich.columns import Columns
from rich.console import Group
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ...changelog import load_releases
from ...instructions import ProjectInfo
from .._helpers import (
    box_rounded,
    cell_to_ansi,
    cell_vis_width,
    get_version,
    shorten_path,
)
from .._style import (
    ACCENT, ACCENT_BOLD, CHROME, DIM, MARK_BULLET, MARK_OK,
    SUCCESS_STYLE, WARNING_STYLE,
)


def _model_label(config: object) -> str:
    """头部/启动面板的模型标签：激活组非空显示 ``组 · 模型``，否则纯模型。"""
    model = getattr(config, "model", "") or "(not set)"
    group = getattr(config, "active_group", "") or ""
    return f"{group} · {model}" if group else model


class LayoutMixin:
    """Header bar, startup screen, project overview, and status hints."""

    # These are set by Console.__post_init__
    config: object
    _console: object
    _terminal_width: int

    # ── top-level entry points ──────────────────────────────────

    def show_startup(
        self, info: ProjectInfo, instructions_loaded: bool = False
    ) -> None:
        """Print the startup screen: mascot + key info in a bordered panel."""
        self._console.print()
        self._console.print(self._build_startup_panel(info, instructions_loaded))
        self._console.print()

    def show_startup_single_shot(self, prompt: str) -> None:
        """Minimal header for single-shot mode."""
        self.print_header(instructions_loaded=False)
        self._console.print()
        self.print_info(
            f"Processing: {prompt[:80]}{'...' if len(prompt) > 80 else ''}"
        )

    # ── header bar ──────────────────────────────────────────────

    def print_header(self, instructions_loaded: bool = False) -> None:
        """Compact single-line header: brand · model · workspace."""
        ws = shorten_path(Path(self.config.workspace), max_len=40)
        model = _model_label(self.config)

        bar = Text()
        bar.append("  openx", style=ACCENT_BOLD)
        bar.append("  ·  ", style=DIM)
        bar.append(model, style="white")
        bar.append("  ·  ", style=DIM)
        bar.append(ws, style="white")
        if instructions_loaded:
            bar.append("  ·  OPENX.md ", style=DIM)
            bar.append(MARK_OK, style=SUCCESS_STYLE)
        self._console.print(bar)

    # ── project overview ────────────────────────────────────────

    def print_project_overview(self, info: ProjectInfo) -> None:
        """Structured project overview panel（克制配色：标签 dim、值默认、
        仅 git 状态等语义点用色）。"""
        table = Table(show_header=False, box=None, padding=(0, 1), expand=False)
        table.add_column("label", style=DIM, width=12)
        table.add_column("value")

        type_text = info.project_type
        if info.project_type_file:
            type_text += f" [dim]({info.project_type_file})[/dim]"
        table.add_row("type", type_text)

        if info.file_counts:
            top_exts = list(info.file_counts.items())[:5]
            parts = [f"{c} [dim]*{e}[/dim]" for e, c in top_exts]
            if len(info.file_counts) > 5:
                parts.append(f"[dim]+{len(info.file_counts) - 5} more[/dim]")
            table.add_row(
                "files", f"{info.total_files} total — " + ", ".join(parts)
            )
        else:
            table.add_row("files", "[dim](empty)[/dim]")

        if info.top_dirs:
            dirs = "  ".join(info.top_dirs[:6])
            if len(info.top_dirs) > 6:
                dirs += f"  [dim]+{len(info.top_dirs) - 6} more[/dim]"
            table.add_row("structure", dirs)
        elif info.top_files:
            table.add_row("top files", "  ".join(info.top_files[:6]))

        if info.config_files:
            table.add_row("config", "  ".join(info.config_files[:6]))

        if info.git_branch or info.git_status_summary:
            git = ""
            if info.git_branch:
                git += f"[{ACCENT}]{info.git_branch}[/{ACCENT}]"
            if info.git_status_summary:
                if git:
                    git += "  "
                s = info.git_status_summary
                st = SUCCESS_STYLE if s == "clean" else WARNING_STYLE
                git += f"[{st}]({s})[/{st}]"
            table.add_row("git", git)

        if info.git_recent:
            table.add_row("recent", f"[dim]{info.git_recent[0]}[/dim]")

        if info.openx_md_loaded:
            md = (
                f"[{SUCCESS_STYLE}]{MARK_OK}[/{SUCCESS_STYLE}]"
                f"  [dim]loaded ({info.openx_md_sections} sections)[/dim]"
            )
        else:
            md = "[dim]not found — /init to create[/dim]"
        table.add_row("OPENX.md", md)

        self._console.print(
            Panel(
                table,
                title="Project overview",
                title_align="left",
                border_style=CHROME,
                box=box_rounded(),
                padding=(0, 1),
            )
        )

    # ── status line ─────────────────────────────────────────────

    def print_status_line(self) -> None:
        """Compact hint line of available commands."""
        hints = [
            ("/help", DIM), ("/quit", DIM), ("/init", DIM),
            ("/image", DIM), ("/clipboard", DIM), ("/todos", DIM),
            ("/cost", DIM), ("/compact", DIM), ("/config", DIM),
        ]
        parts = []
        for label, style in hints:
            t = Text()
            t.append(label, style=style)
            parts.append(t)
        self._console.print(
            Padding(Columns(parts, equal=False, expand=False), (0, 0, 0, 2))
        )

    # ── startup panel (internal) ────────────────────────────────

    # 吉祥物（v0.5.0 定稿）：线条形圆角小机器人——纯制表线字符
    # （╭╮╰╯ ─ │ ┴ ┬）勾勒：圆头、天线（○ 球 + ┴ 座）、点眼（●）、
    # 外撇的小脚（╰ ╯）。整机品牌亮青（bold bright_cyan）——与启动
    # 面板右侧 "openx" 字样同色：机器人即品牌标记，活泼且统一（设计
    # 系统里强调色只此一处用途增量：品牌字样 + 吉祥物）。造型取舍
    # （用户反馈演进）：块状版被评"太丑"，线条版轻量耐看。每行由
    # (字符, 样式) 段组成，**恒 9 宽 × 5 行**（启动面板并排的前提；
    # 修改时务必保持每行等宽，layout 自检与 pytest 钉死该约束）。
    _BODY = "bold bright_cyan"  # 唯一颜色：整机同色 = 品牌强调色

    @classmethod
    def _mascot_lines(cls) -> list:
        B = cls._BODY
        # fmt: off
        rows = [
            [("    ", None), ("○", B), ("    ", None)],   # 天线球
            [(" ", None), ("╭──┴──╮", B), (" ", None)],   # 圆头（顶）
            [(" ", None), ("│ ● ● │", B), (" ", None)],   # 点眼
            [(" ", None), ("╰──┬──╯", B), (" ", None)],   # 圆头（底）+ 颈
            [("   ", None), ("╰ ╯", B), ("   ", None)],   # 外撇小脚
        ]
        # fmt: on
        out = []
        for segs in rows:
            t = Text()
            for chars, style in segs:
                t.append(chars, style=style or None)
            out.append(t)
        return out

    # 结构化发布说明：数据与渲染分离——数据源是 openx/CHANGELOG.md
    # （Claude Code 风格：每个版本一节 ``## <version> — <title>``，最新版
    # 在文件顶部），由 openx/changelog.py 在导入时解析为
    # (版本, 标题, 要点列表)。启动面板取 short 摘要，/release-notes
    # （别名 /release）按版本列表选择查看。发新版只需在 CHANGELOG.md
    # 顶部追加一节并升版本号，不改代码。
    RELEASES: list = load_releases()

    @classmethod
    def release_notes_markup(cls, entries=None) -> str:
        """把版本条目渲染为 markup 文本（默认全部版本）。"""
        if entries is None:
            entries = cls.RELEASES
        blocks = []
        for version, title, bullets in entries:
            head = f"[bold]v{version}[/bold]"
            if title:
                head += f" — {title}"
            lines = [head]
            lines += [f"[dim]{MARK_BULLET}[/dim] {b}" for b in bullets]
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    @classmethod
    def _get_release_notes(cls, *, short: bool = True) -> str:
        if not short:
            return cls.release_notes_markup()
        # 启动面板摘要：最新版本标题 + 首条要点，次新版本仅首条要点。
        # 要点截短防折行（面板内换行会打散 ▸ 列表的视觉节奏）。
        if not cls.RELEASES:
            return ""
        def _clip(b: str, n: int = 62) -> str:
            return b if len(b) <= n else b[: n - 1].rstrip() + "…"
        head = cls.RELEASES[0]
        head_line = f"[bold]v{head[0]}[/bold]"
        if head[1]:
            head_line += f" — {head[1]}"
        lines = [head_line,
                 f"[dim]{MARK_BULLET}[/dim] {_clip(head[2][0])}"]
        if len(cls.RELEASES) > 1 and cls.RELEASES[1][2]:
            prev = cls.RELEASES[1]
            lines.append(f"[dim]{MARK_BULLET}[/dim] {_clip(prev[2][0])} (v{prev[0]})")
        return "\n".join(lines)

    def _build_startup_panel(
        self, info: ProjectInfo, instructions_loaded: bool
    ) -> Panel:
        """启动面板（v0.5.0）：像素吉祥物 + 品牌/元信息并排，细灰边框。

        层次靠字重：品牌字样强调色 bold，元信息白/dim 混排，提示与
        更新日志 dim 退居背景——第一眼只抓住"我在哪、用什么模型"，
        吉祥物提供辨识度而不争夺注意力（主体灰、仅眼/手着色）。
        """
        ws = shorten_path(Path(self.config.workspace), max_len=50)
        model = _model_label(self.config)
        version = get_version()

        brand = Text()
        brand.append("openx", style=ACCENT_BOLD)
        brand.append(f"  v{version}", style=DIM)

        meta = Text()
        meta.append(model, style="white")
        meta.append("  ·  ", style=DIM)
        meta.append(ws, style="white")
        if instructions_loaded:
            meta.append("  ·  OPENX.md ", style=DIM)
            meta.append(MARK_OK, style=SUCCESS_STYLE)

        hints = [
            "type anything to chat — /help lists commands",
            "Esc interrupts · / for commands · Ctrl-O agent views",
            "type while answering — Enter queues, Esc sends now",
        ]
        hint_lines = []
        for h in hints:
            t = Text()
            t.append(f"{MARK_BULLET} ", style=ACCENT)
            t.append(h, style=DIM)
            hint_lines.append(t)

        # ── 左吉祥物 / 右信息 并排 ──
        mascot = self._mascot_lines()
        mascot_w = max((cell_vis_width(m) for m in mascot), default=0)
        right = [brand, meta, Text("")] + hint_lines
        n = max(len(mascot), len(right))
        lines: list = []
        for i in range(n):
            row = Text()
            if i < len(mascot):
                left = mascot[i]
                row.append_text(left)
                pad = mascot_w - cell_vis_width(left)
                row.append(" " * (pad + 3))  # 栏间距
            else:
                row.append(" " * (mascot_w + 3))
            if i < len(right):
                row.append_text(right[i])
            lines.append(row)

        # ── 更新日志（通栏）──
        notes = [
            line for line in self._get_release_notes().split("\n")
            if line.strip()
        ]
        lines.append(Text(""))
        whatsnew = Text()
        whatsnew.append(f"What's new in v{version}", style="white")
        whatsnew.append("  —  /release-notes for all", style=DIM)
        lines.append(whatsnew)
        for nl in notes:
            lines.append(Text.from_markup(f"  {nl}"))

        return Panel(
            Group(*lines),
            box=box_rounded(),
            border_style=CHROME,
            padding=(0, 1),
        )


if __name__ == "__main__":
    import io
    from rich.console import Console
    from openx.config import OpenXConfig
    from openx.instructions import ProjectInfo
    _buf = io.StringIO()
    _m = LayoutMixin()
    _m.config = OpenXConfig(workspace="/tmp/openx-selfcheck")
    _m._console = Console(file=_buf, width=100)
    _m._terminal_width = 100
    _info = ProjectInfo(project_type="Python", project_type_file="pyproject.toml",
                        total_files=3, file_counts={".py": 3}, top_dirs=["src"],
                        git_branch="main", git_status_summary="clean")
    _m.print_header(instructions_loaded=True)
    _m.show_startup(_info, instructions_loaded=True)
    # 吉祥物：5 行、等宽（并排排版的前提）、整机单色
    _mascot = LayoutMixin._mascot_lines()
    assert len(_mascot) == 5
    _widths = {cell_vis_width(m) for m in _mascot}
    assert len(_widths) == 1, f"mascot rows not equal width: {_widths}"
    assert "╭──┴──╮" in _buf.getvalue()  # 启动面板含吉祥物
    print(f"captured {len(_buf.getvalue())} chars of layout rendering")
    print("openx/ui/_components/layout.py OK ✓")
