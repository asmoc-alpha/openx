"""Persistent memory system — Claude Code-aligned MEMORY.md files.

Stores facts, preferences, and project knowledge in ``~/.openx/memory/``
as individual markdown files with YAML frontmatter.  An index file
(``MEMORY.md``) lists all entries for fast scanning, and entries can
cross-reference each other with ``[[wikilink]]`` syntax.

Usage::

    store = MemoryStore()
    store.save("coding-style", "Prefer type hints", "Always use ...",
               metadata={"type": "user"})
    for entry in store.list_all():
        print(entry.name, entry.description)
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

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_MEMORY_DIR = Path.home() / ".openx" / "memory"
_INDEX_FILE = "MEMORY.md"

# ── data model ───────────────────────────────────────────────────


@dataclass
class MemoryEntry:
    """A single memory entry parsed from a markdown file."""

    name: str
    description: str = ""
    content: str = ""
    metadata: dict = field(default_factory=dict)
    path: Path = field(default_factory=Path)

    @staticmethod
    def _slug(text: str) -> str:
        """Convert arbitrary text to a safe kebab-case filename slug."""
        slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        return slug or "untitled"


# ── parser ───────────────────────────────────────────────────────


def parse_memory_file(filepath: Path) -> Optional[MemoryEntry]:
    """Parse a single memory ``.md`` file into a :class:`MemoryEntry`.

    Returns ``None`` if the file is unreadable or empty.
    """
    try:
        raw = filepath.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None

    meta: dict = {}
    body = raw

    # Parse YAML frontmatter if present (delimited by ---)
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            meta = _parse_frontmatter(parts[1])
            body = parts[2].strip()

    name = meta.pop("name", filepath.stem)
    desc = meta.pop("description", _extract_first_line(body))
    entry_meta = meta.pop("metadata", meta)  # remaining keys become metadata

    return MemoryEntry(
        name=name,
        description=desc,
        content=body,
        metadata=entry_meta if isinstance(entry_meta, dict) else {},
        path=filepath,
    )


def _parse_frontmatter(text: str) -> dict:
    """Minimal YAML frontmatter parser — handles simple ``key: value`` pairs
    and nested ``key:\n  sub: val`` blocks without pulling in a YAML library.
    """
    result: dict = {}
    current_key: Optional[str] = None
    current_map: dict = {}

    for line in text.strip().split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # Nested key under current map
        if line.startswith("  ") and current_key:
            if ":" in stripped:
                k, v = stripped.split(":", 1)
                current_map[k.strip()] = v.strip()
            continue

        # Top-level key: value
        if ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip()
            if value:
                result[key] = value
            else:
                current_key = key
                current_map = {}
                result[key] = current_map
        else:
            # Flush previous map
            current_key = None

    return result


def _extract_first_line(text: str) -> str:
    """Return the first non-empty, non-heading line of *text*."""
    for line in text.split("\n"):
        stripped = line.lstrip("#").strip()
        if stripped:
            return stripped[:120]
    return ""


# ── store ────────────────────────────────────────────────────────


class MemoryStore:
    """Manages the ``~/.openx/memory/`` directory of memory files."""

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self._base = base_dir or _MEMORY_DIR
        self._base.mkdir(parents=True, exist_ok=True)

    # ── public API ──────────────────────────────────────────────

    @property
    def has_any(self) -> bool:
        """True if any memory files exist (excluding the index)."""
        return any(self._iter_memory_files())

    def list_all(self) -> list[MemoryEntry]:
        """Return all stored memories sorted by name."""
        entries = []
        for fp in sorted(self._iter_memory_files()):
            entry = parse_memory_file(fp)
            if entry:
                entries.append(entry)
        return entries

    def get(self, name: str) -> Optional[MemoryEntry]:
        """Find a memory by its slug name."""
        fp = self._filepath(name)
        if fp.exists():
            return parse_memory_file(fp)
        return None

    def save(
        self,
        name: str,
        description: str,
        content: str,
        *,
        metadata: Optional[dict] = None,
    ) -> MemoryEntry:
        """Create or update a memory.  *name* is converted to a safe slug."""
        slug = MemoryEntry._slug(name)
        meta = metadata or {}
        lines = [
            "---",
            f"name: {slug}",
            f"description: {description}",
        ]
        for k, v in meta.items():
            lines.append(f"{k}: {v}")
        lines.append("---")
        lines.append("")
        lines.append(content)

        fp = self._filepath(slug)
        fp.write_text("\n".join(lines), encoding="utf-8")
        self._rebuild_index()
        return MemoryEntry(
            name=slug, description=description, content=content,
            metadata=meta or {}, path=fp,
        )

    def delete(self, name: str) -> bool:
        """Delete a memory by slug.  Returns ``True`` if it was removed."""
        fp = self._filepath(name)
        if fp.exists():
            fp.unlink()
            self._rebuild_index()
            return True
        return False

    def recall_relevant(self, query: str) -> list[MemoryEntry]:
        """Naively search memories whose name, description, or content
        mentions *query* (case-insensitive).  For production use this
        should be replaced with embeddings + vector search.
        """
        q = query.lower()
        results = []
        for entry in self.list_all():
            score = 0
            if q in entry.name.lower():
                score += 10
            if q in entry.description.lower():
                score += 5
            if q in entry.content.lower():
                score += 1
            if score > 0:
                results.append((score, entry))
        results.sort(key=lambda x: -x[0])
        return [e for _, e in results]

    def build_context_prompt(self) -> str:
        """Build a compact prompt fragment listing all stored memories.

        This is injected into the system prompt so the model is aware of
        persistent facts, preferences, and project knowledge.
        """
        entries = self.list_all()
        if not entries:
            return ""

        lines = ["", "## Persistent Memory", ""]
        lines.append(
            "The following facts are stored in your persistent memory "
            "(~/.openx/memory/).  Treat them as ground truth — do not "
            "contradict them unless the user explicitly corrects you."
        )
        lines.append("")
        for e in entries:
            type_tag = f" [{e.metadata.get('type', '')}]" if e.metadata.get("type") else ""
            lines.append(f"- **{e.name}**{type_tag}: {e.description}")
        lines.append("")
        return "\n".join(lines)

    # ── internals ───────────────────────────────────────────────

    def _filepath(self, name: str) -> Path:
        return self._base / f"{name}.md"

    def _iter_memory_files(self):
        """Yield ``.md`` files excluding the index."""
        return (
            p for p in self._base.glob("*.md")
            if p.name != _INDEX_FILE
        )

    def _rebuild_index(self) -> None:
        """Regenerate the MEMORY.md index file."""
        entries = self.list_all()
        lines = ["# Memory Index", ""]
        if entries:
            for e in entries:
                lines.append(f"- [{e.name}]({e.name}.md) — {e.description}")
        else:
            lines.append("_(empty — use `/remember <fact>` to add your first memory)_")
        (self._base / _INDEX_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    import tempfile

    # 所有写入都指向临时目录（base_dir 参数），绝不碰真实 ~/.openx/memory/
    with tempfile.TemporaryDirectory() as _td:
        _store = MemoryStore(base_dir=Path(_td))
        assert not _store.has_any
        assert _store.build_context_prompt() == ""  # 空记忆 → 空提示片段

        _store.save("Coding Style", "Prefer type hints", "Always annotate return types.",
                    metadata={"type": "user"})
        _entries = _store.list_all()
        assert len(_entries) == 1 and _entries[0].name == "coding-style"

        _prompt = _store.build_context_prompt()
        assert "## Persistent Memory" in _prompt and "coding-style" in _prompt
        print(f"memories: {len(_entries)}, context prompt {len(_prompt)} chars")
        print(_store.recall_relevant("type hints")[0].description)

    print("openx/memory.py OK ✓")
