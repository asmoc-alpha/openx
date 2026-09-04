"""Changelog loading — single source of truth for release notes.

Release notes live in ``openx/CHANGELOG.md`` (Claude Code style: one
``## <version> — <title>`` section per release, newest first). This module
parses that file into the ``(version, title, bullets)`` tuples consumed by
the startup panel and ``/release-notes``. Cutting a release is a
CHANGELOG.md edit plus a version bump — no rendering code changes.

``###`` group headings and prose lines inside a section are allowed for
human readers; the parser only collects ``- `` bullets.
"""

from __future__ import annotations

import re
from importlib import resources

# ``## 0.1.0 — First public release`` (title optional; accepts — / – / -)
_HEADING = re.compile(
    r"^##\s+(?P<version>\d+\.\d+\.\d+)\s*(?:[—–-]\s*(?P<title>.+?))?\s*$"
)
_BULLET = re.compile(r"^[-*]\s+(?P<text>.+?)\s*$")

# Used only when the packaged CHANGELOG.md is missing/unreadable, so the
# startup panel and /release-notes never crash on a broken install.
_FALLBACK: list[tuple[str, str, list[str]]] = [
    ("0.1.1", "Model groups & Anthropic-compatible protocol", ["See openx/CHANGELOG.md"]),
]


def _changelog_text() -> str:
    return (resources.files("openx") / "CHANGELOG.md").read_text(encoding="utf-8")


def parse_releases(text: str) -> list[tuple[str, str, list[str]]]:
    """Parse changelog markdown into ``(version, title, bullets)`` entries.

    Entries keep file order, so the newest release must sit at the top of
    CHANGELOG.md. Sections without bullets are dropped.
    """
    entries: list[tuple[str, str, list[str]]] = []
    current: list | None = None  # [version, title, bullets]
    for raw in text.splitlines():
        line = raw.strip()
        m = _HEADING.match(line)
        if m:
            if current is not None:
                entries.append((current[0], current[1], current[2]))
            current = [m.group("version"), (m.group("title") or "").strip(), []]
            continue
        if current is None:
            continue
        b = _BULLET.match(line)
        if b:
            current[2].append(b.group("text"))
    if current is not None:
        entries.append((current[0], current[1], current[2]))
    return [(v, t, bs) for v, t, bs in entries if bs]


def load_releases() -> list[tuple[str, str, list[str]]]:
    """Load the packaged CHANGELOG.md; fall back to a stub, never raise."""
    try:
        entries = parse_releases(_changelog_text())
    except OSError:
        return list(_FALLBACK)
    return entries or list(_FALLBACK)


if __name__ == "__main__":
    for version, title, bullets in load_releases():
        print(f"v{version} — {title} ({len(bullets)} bullets)")
