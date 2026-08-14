"""Project exploration — scan a workspace and return structured metadata.

Extracted from ``OpenXAgent.explore_project()``.
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

from pathlib import Path
from typing import Any

from ..instructions import (
    ProjectInfo,
    detect_project_type,
    _CONFIG_FILES,
)


async def explore_project(
    workspace: Path,
    git_status_tool: Any,
    git_log_tool: Any,
) -> ProjectInfo:
    """Scan *workspace* and return structured project information.

    Detects project type, counts files by extension, lists top-level
    directories, queries git status/log, and reports OPENX.md presence.
    """
    info = ProjectInfo()

    # 1. Identify project type
    ptype, ptype_file = detect_project_type(workspace)
    info.project_type = ptype
    info.project_type_file = ptype_file

    # 2. Scan top level: config files, directories, file counts
    _scan_top_level(workspace, info)

    # 3. Git information
    await _collect_git_info(info, git_status_tool, git_log_tool)

    # 4. OPENX.md status
    _check_openx_md(workspace, info)

    return info


# ── internal helpers ─────────────────────────────────────────────


def _scan_top_level(workspace: Path, info: ProjectInfo) -> None:
    """Populate *info* with top-level filesystem metadata."""
    info.config_files = []
    info.top_dirs = []
    info.top_files = []
    file_counts: dict[str, int] = {}

    try:
        items = sorted(workspace.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except (OSError, PermissionError):
        return

    for item in items:
        if item.name.startswith("."):
            if item.name in (".gitignore", ".env.example", ".env.template"):
                info.config_files.append(item.name)
            continue

        if item.is_dir():
            if item.name not in (
                "node_modules", "__pycache__", ".git",
                ".venv", "venv", ".tox", "dist", "build",
            ):
                info.top_dirs.append(item.name + "/")
        else:
            info.top_files.append(item.name)
            ext = item.suffix or item.name
            file_counts[ext] = file_counts.get(ext, 0) + 1
            if item.name in _CONFIG_FILES:
                info.config_files.append(item.name)

    info.file_counts = dict(sorted(file_counts.items(), key=lambda x: -x[1]))
    info.total_files = sum(file_counts.values())


async def _collect_git_info(
    info: ProjectInfo,
    git_status_tool: Any,
    git_log_tool: Any,
) -> None:
    """Query git tools and populate git-related fields on *info*."""
    try:
        result = await git_status_tool.execute()
        output = result.output or ""
        if output.strip() and "fatal" not in output.lower():
            lines = output.strip().split("\n")
            info.git_status_summary = _summarize_git_status(lines)
            first = lines[0] if lines else ""
            if first.startswith("## "):
                info.git_branch = first[3:].split("...")[0].strip()
        else:
            info.git_status_summary = "clean"
            info.git_branch = ""

        log_result = await git_log_tool.execute(count=3, oneline=True)
        log_output = log_result.output or ""
        if log_output.strip():
            info.git_recent = [
                line.strip() for line in log_output.strip().split("\n")[:3]
                if line.strip()
            ]
    except Exception:
        pass  # git unavailable


def _check_openx_md(workspace: Path, info: ProjectInfo) -> None:
    """Check for OPENX.md and populate its metadata on *info*."""
    openx_md = workspace / "OPENX.md"
    if not openx_md.exists():
        return
    info.openx_md_loaded = True
    try:
        content = openx_md.read_text(encoding="utf-8", errors="replace")
        info.openx_md_sections = content.count("\n## ")
        if content.startswith("## "):
            info.openx_md_sections += 1
    except Exception:
        pass


def _summarize_git_status(lines: list[str]) -> str:
    """Summarise git status lines into a short label."""
    actual = [l for l in lines if not l.startswith("## ")]
    staged = sum(1 for l in actual if l.strip() and not l.strip().startswith("?"))
    untracked = sum(1 for l in actual if l.strip().startswith("?"))
    if staged == 0 and untracked == 0:
        return "clean"
    parts = []
    if staged:
        parts.append(f"{staged} modified")
    if untracked:
        parts.append(f"{untracked} untracked")
    return ", ".join(parts) if parts else "clean"


if __name__ == "__main__":
    import asyncio, tempfile

    class _NoGit:  # git 不可用时必须优雅降级（_collect_git_info 吞掉异常）
        async def execute(self, **_kw):
            raise RuntimeError("git unavailable")

    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
        (ws / "main.py").write_text("print('hi')\n")
        (ws / "src").mkdir()
        info = asyncio.run(explore_project(ws, _NoGit(), _NoGit()))
        assert info.project_type == "Python" and info.total_files == 2
        print(f"type={info.project_type} files={info.file_counts} dirs={info.top_dirs}")
    print("openx/services/exploration.py OK ✓")
