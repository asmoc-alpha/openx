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

from pathlib import Path

from rich.console import Console as RichConsole
from rich.text import Text


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
        return "0.1.0"


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


if __name__ == "__main__":
    p = Path("/Users/someone/very/deep/project/src/module/file.py")
    print("shorten_path:", shorten_path(p, 30))
    print("trunc:", trunc("abcdefghijklmnop", 10))
    print("mask_key:", mask_key("sk-1234567890abcdef"), "| version:", get_version())
    ansi = cell_to_ansi(Text("hello", style="bold"))
    print(f"cell_to_ansi: {len(ansi)} chars | cell_vis_width('hello ✓'):", cell_vis_width(Text("hello ✓")))
    print("box_rounded:", box_rounded().__class__.__name__)
    print("openx/ui/_helpers.py OK ✓")
