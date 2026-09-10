"""Internal helpers shared across UI components."""

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

import random
from pathlib import Path
from typing import Optional

from rich.console import Console as RichConsole
from rich.text import Text

from ._style import ACCENT, DIM, MARK_WORKING


def shorten_path(path: Path, max_len: int = 40) -> str:
    """Shorten a path for display, keeping the tail visible."""
    s = str(path)
    if len(s) <= max_len:
        return s
    home = str(Path.home())
    if s.startswith(home):
        s = "~" + s[len(home):]
    if len(s) <= max_len:
        return s
    keep_end = max_len - 5
    return "..." + s[-keep_end:]


def trunc(text: str, max_len: int) -> str:
    """Truncate *text*, adding ellipsis if needed."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def box_rounded():
    """Return the ROUNDED box style (lazy import for clarity)."""
    from rich.box import ROUNDED
    return ROUNDED


def mask_key(key: str, visible: int = 7) -> str:
    """Mask an API key, showing only prefix + last 4 chars."""
    if len(key) <= visible + 4:
        return key[:3] + "..." if len(key) > 3 else key
    return key[:visible] + "..." + key[-4:]


def get_version() -> str:
    """Get the OpenX version string (lazy import to avoid circular deps)."""
    try:
        from openx import __version__
        return __version__
    except ImportError:
        return "0.1.1"


def cell_to_ansi(text: Text) -> str:
    """Render a Rich *Text* to an ANSI string (no trailing newline)."""
    import io as _io

    temp = RichConsole(
        file=_io.StringIO(),
        width=999,
        color_system="standard",
        force_terminal=True,
        highlight=False,
    )
    with temp.capture() as cap:
        temp.print(text, end="")
    return cap.get()


def cell_vis_width(text: Text) -> int:
    """Visual (cell) width of a Rich *Text* — emoji-aware."""
    return text.cell_len


# ── 回合结束行（对标 Claude Code ``✻ Cooked for 42s``）─────────────

# 动词表与 Claude Code 完全一致，随机取一，营造"刚忙完"的收尾观感。
# 放在 _helpers 而非 services/streaming.py：流式路径（streaming）与
# --no-stream 路径（ui/_components/display.py）都要用，而 streaming 反向
# 依赖 ui —— 两侧都能引此处且不成环（_style 无任何包内依赖）。
DONE_VERBS = ("Baked", "Brewed", "Churned", "Cogitated",
              "Cooked", "Crunched", "Sautéed", "Worked")


def fmt_duration(seconds: float) -> str:
    """耗时 → Claude Code 风格短串：``42s`` / ``1m 2s`` / ``1h 2m``。

    秒级不补零（``1m 2s`` 而非 ``1m 02s``），与 Claude Code 逐字一致。
    """
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60}s"
    return f"{total // 3600}h {(total % 3600) // 60}m"


def done_line(elapsed: float, verb: Optional[str] = None,
              suffix: str = "") -> Text:
    """回合结束行 ``✻ Cooked for 42s``（单行 Text）。

    ``✻`` 用唯一强调色、文案用 dim——遵守 _style.py 的单强调色纪律
    （只点亮"这事刚结束"这一个焦点，不引入第二种色相）。

    ``verb`` 显式传入则用之（测试确定性）；默认从 :data:`DONE_VERBS`
    随机取——随机性与 Claude Code 一致。

    ``suffix``：附加的 dim 用量摘要（``1.2k tokens · 2 tool calls``）。
    流式路径不传——输入框状态行已常驻展示 in/out 用量，再列一遍是冗余；
    --no-stream 路径没有状态行，故由它带上（见 display.print_streaming_done）。
    """
    word = verb if verb is not None else random.choice(DONE_VERBS)
    t = Text()
    t.append(f"{MARK_WORKING} ", style=ACCENT)
    t.append(f"{word} for {fmt_duration(elapsed)}", style=DIM)
    if suffix:
        t.append(f"  ·  {suffix}", style=DIM)
    # 硬不变量：1 行 ≡ 1 终端行（同 _deck_line / _replay_indicator）。
    # 完成行是重印几何的行数标尺的一部分，窄终端上折行会算错位置。
    t.no_wrap = True
    t.overflow = "ellipsis"
    return t


if __name__ == "__main__":
    p = Path("/Users/someone/very/deep/project/src/module/file.py")
    print("shorten_path:", shorten_path(p, 30))
    print("trunc:", trunc("abcdefghijklmnop", 10))
    print("mask_key:", mask_key("sk-1234567890abcdef"), "| version:", get_version())
    ansi = cell_to_ansi(Text("hello", style="bold"))
    print(f"cell_to_ansi: {len(ansi)} chars | cell_vis_width('hello ✓'):", cell_vis_width(Text("hello ✓")))
    print("box_rounded:", box_rounded().__class__.__name__)
    assert fmt_duration(42) == "42s"
    assert fmt_duration(62) == "1m 2s"
    assert fmt_duration(3720) == "1h 2m"
    assert done_line(42, verb="Cooked").plain == "✻ Cooked for 42s"
    assert len(DONE_VERBS) == 8
    print("done_line:", repr(done_line(62, verb="Baked").plain))
    print("openx/ui/_helpers.py OK ✓")
