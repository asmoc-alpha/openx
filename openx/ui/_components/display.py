from __future__ import annotations

"""Output display: assistant text, code, tool calls, streaming status."""

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

from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.text import Text

from .._helpers import done_line, trunc
from .._style import DIM, ERROR_STYLE, SPIN_FRAMES


class DisplayMixin:
    """Assistant output, code blocks, tool call/result display, streaming."""

    config: object
    _console: object

    # ── streaming status ────────────────────────────────────────

    # 与流式路径同一份星形家族（定义在 _style.py）。--no-stream 无动画，
    # 故本路径只取家族首帧作静态指示，不逐帧推进。
    _SPINNER_FRAMES = list(SPIN_FRAMES)

    def print_streaming_start(self) -> None:
        """Print the *Thinking…* spinner before generation begins."""
        self._console.print()
        self._console.print(f"[dim]{SPIN_FRAMES[0]} Thinking…[/dim]")

    def print_streaming_done(
        self, elapsed: float, tokens: int, tool_calls: int = 0
    ) -> None:
        """回合结束行 ``✻ Cooked for 42s``（对标 Claude Code）。

        与流式路径同款收尾行，唯一差别是这里附上用量摘要——流式路径的
        输入框状态行已常驻展示 in/out 用量，本路径没有，不在这里给就无处可查。
        """
        tok_s = f"{tokens / 1000:.1f}k" if tokens >= 1000 else str(tokens)
        suffix = f"{tok_s} tokens"
        if tool_calls > 0:
            suffix += f"  ·  {tool_calls} tool call{'s' if tool_calls > 1 else ''}"
        self._console.print(done_line(elapsed, suffix=suffix))

    # ── tool calls ──────────────────────────────────────────────

    def print_tool_call(self, tool_name: str, args: dict | None = None) -> None:
        """Print a tool call being executed."""
        t = Text()
        t.append("● ", style=DIM)
        t.append(tool_name, style="bold")
        if args:
            args_str = ", ".join(
                f"{k}={trunc(str(v), 40)}" for k, v in args.items()
            )
            t.append(f"  [dim]({args_str})[/dim]")
        self._console.print(t)

    def print_tool_result(self, result: str, error: bool = False) -> None:
        """Print tool result (truncated for display)."""
        style = ERROR_STYLE if error else DIM
        preview = trunc(result, 300).replace("\n", " ")
        self._console.print(f"   {preview}", style=style)

    # ── assistant output ────────────────────────────────────────

    def print_assistant(self, text: str) -> None:
        """Print assistant response as rendered Markdown."""
        if not text.strip():
            return
        self._console.print()
        md = Markdown(text, code_theme=self.config.syntax_theme)
        self._console.print(md)
        # 与流式分支同款留白：正文之后恒隔 1 行（= _BODY_FRAME_GAP，对标
        # Claude Code）。紧随其后是 print_streaming_done 的结束行。
        self._console.print()

    def print_code(self, code: str, language: str = "python") -> None:
        """Print syntax-highlighted code."""
        self._console.print(
            Syntax(code, language, theme=self.config.syntax_theme)
        )


if __name__ == "__main__":
    import io
    from rich.console import Console
    from openx.config import OpenXConfig
    _buf = io.StringIO()
    _m = DisplayMixin()
    _m.config = OpenXConfig()
    _m._console = Console(file=_buf, width=100)
    _m.print_streaming_start()
    _m.print_tool_call("read_file", {"path": "/tmp/demo.py"})
    _m.print_tool_result("file contents here")
    _m.print_assistant("Hello **world**\n\n```python\nprint(1)\n```")
    _m.print_code("x = 41 + 1")
    _m.print_streaming_done(1.25, 42, tool_calls=1)
    print(f"captured {len(_buf.getvalue())} chars: {_buf.getvalue().strip()[:60]!r}")
    print("openx/ui/_components/display.py OK ✓")
