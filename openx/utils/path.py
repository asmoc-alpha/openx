"""Path utilities shared across tools and UI."""

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


def resolve_path(workspace: Path, file_path: str) -> Path:
    """Resolve *file_path* against *workspace*.

    Absolute paths are kept as-is; relative paths are joined with the
    workspace directory.  The result is always resolved (no ``..`` or
    symlink components).
    """
    p = Path(file_path)
    if not p.is_absolute():
        p = workspace / p
    return p.resolve()


def shorten_path(path: Path, max_len: int = 40) -> str:
    """Return a display-friendly shortened version of *path*.

    Short paths are returned as-is.  Longer paths are shown as
    ``…/<last-two-segments>``.  The home directory is replaced with ``~``.
    """
    s = str(path)
    home = str(Path.home())
    if s.startswith(home):
        s = "~" + s[len(home):]

    if len(s) <= max_len:
        return s

    parts = path.parts
    if len(parts) >= 2:
        s = "…/" + "/".join(parts[-2:])
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s


if __name__ == "__main__":
    import tempfile

    workspace = Path(tempfile.gettempdir())  # 只用系统临时目录，不写真实 home
    rel = resolve_path(workspace, "sub/dir/file.txt")
    print(f"resolve_path(relative): {rel}")
    assert rel == (workspace / "sub/dir/file.txt").resolve()

    abs_p = resolve_path(workspace, "/etc/hosts")
    print(f"resolve_path(absolute kept as-is): {abs_p}")
    assert abs_p == Path("/etc/hosts").resolve()

    long = Path.home() / "Documents/very/deeply/nested/project/src/module/file.py"
    print(f"shorten_path: {long} -> {shorten_path(long)}")
    print(f"shorten_path(short unchanged): {Path('/tmp/x.py')} -> {shorten_path(Path('/tmp/x.py'))}")
    print("openx/utils/path.py OK ✓")
