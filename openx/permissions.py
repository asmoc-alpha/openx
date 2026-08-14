"""Permission system for OpenX tools.

Inspired by Claude Code's permission model:
- ALLOW: always allowed
- ASK: ask user before executing
- DENY: always blocked (dangerous operations)

Permission rules can be persisted to ``~/.openx/settings.json`` under
``permissions.allow`` and ``permissions.deny`` lists.  Rules support
wildcard matching against ``ToolName(args)`` strings.
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

import fnmatch
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class PermissionLevel(Enum):
    """Permission levels for tool execution."""

    ALLOW = "allow"  # always allowed
    ASK = "ask"  # ask user before executing
    DENY = "deny"  # always blocked


@dataclass
class Permission:
    """A permission check for a tool execution."""

    level: PermissionLevel
    reason: str = ""

    @classmethod
    def allow(cls) -> "Permission":
        return cls(level=PermissionLevel.ALLOW)

    @classmethod
    def ask(cls, reason: str = "") -> "Permission":
        return cls(level=PermissionLevel.ASK, reason=reason)

    @classmethod
    def deny(cls, reason: str) -> "Permission":
        return cls(level=PermissionLevel.DENY, reason=reason)


# ── persisted rules ─────────────────────────────────────────────


@dataclass
class PermissionRules:
    """Manage stored allow/deny rules with wildcard matching.

    Rules are persisted in ``~/.openx/settings.json``::

        {
          "permissions": {
            "allow": ["shell(npm test)", "edit_file(*.py)"],
            "deny": ["shell(rm *)", "shell(sudo *)"]
          }
        }

    Patterns use shell-style globbing (``*`` matches anything,
    ``?`` matches a single character).
    """

    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)

    # ── persistence ──────────────────────────────────────────

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "PermissionRules":
        """Load rules from settings.json.  Returns empty rules if none exist."""
        path = path or Path.home() / ".openx" / "settings.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, FileNotFoundError):
            return cls()
        perms = data.get("permissions", {})
        return cls(
            allow=list(perms.get("allow", [])),
            deny=list(perms.get("deny", [])),
        )

    def save(self, path: Optional[Path] = None) -> None:
        """Persist current rules to settings.json, merging with existing data."""
        path = path or Path.home() / ".openx" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Read-modify-write to preserve other keys
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, FileNotFoundError):
            data = {}
        data["permissions"] = {
            "allow": self.allow,
            "deny": self.deny,
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    # ── matching ─────────────────────────────────────────────

    def check(
        self, tool_name: str, args_summary: str = ""
    ) -> Optional[PermissionLevel]:
        """Check stored rules for a match.

        Returns:
            ``PermissionLevel.ALLOW`` if an allow rule matches,
            ``PermissionLevel.DENY`` if a deny rule matches,
            ``None`` if no rule matches (fall through to interactive prompt).
        """
        target = f"{tool_name}({args_summary})" if args_summary else tool_name

        # Deny rules checked first (they take precedence)
        for pattern in self.deny:
            if fnmatch.fnmatch(target, pattern):
                return PermissionLevel.DENY

        # Allow rules
        for pattern in self.allow:
            if fnmatch.fnmatch(target, pattern):
                return PermissionLevel.ALLOW

        return None  # no stored rule → ask interactively

    def add_allow(self, pattern: str) -> None:
        """Add an allow pattern and persist."""
        if pattern not in self.allow:
            self.allow.append(pattern)
            self.save()

    def add_deny(self, pattern: str) -> None:
        """Add a deny pattern and persist."""
        if pattern not in self.deny:
            self.deny.append(pattern)
            self.save()

    def remove(self, pattern: str) -> bool:
        """Remove a pattern from whichever list it's in.  Returns True if found."""
        for lst in (self.allow, self.deny):
            if pattern in lst:
                lst.remove(pattern)
                self.save()
                return True
        return False

    def format_rules(self) -> str:
        """Return a human-readable summary of current rules."""
        lines = ["[bold]Permission Rules[/bold]\n"]
        if self.allow:
            lines.append("[green]Allow:[/green]")
            for p in self.allow:
                lines.append(f"  ✓ {p}")
        if self.deny:
            lines.append("[red]Deny:[/red]")
            for p in self.deny:
                lines.append(f"  ✗ {p}")
        if not self.allow and not self.deny:
            lines.append("[dim](no stored rules — all ASK tools prompt interactively)[/dim]")
        return "\n".join(lines)


def check_command_danger(command: str, dangerous_patterns: list[str]) -> tuple[bool, str]:
    """Check if a shell command matches any dangerous patterns.

    Returns (is_dangerous, matched_pattern).
    """
    # Normalize: strip leading/trailing whitespace, collapse spaces
    normalized = " ".join(command.split())

    for pattern in dangerous_patterns:
        if pattern in normalized:
            return True, pattern
    return False, ""


if __name__ == "__main__":
    import tempfile

    # Permission 工厂
    assert Permission.allow().level is PermissionLevel.ALLOW
    assert Permission.ask("confirm?").level is PermissionLevel.ASK
    assert Permission.deny("danger").level is PermissionLevel.DENY

    # PermissionRules.check 通配符匹配（load/save 用临时 settings，不碰真实 home）
    with tempfile.TemporaryDirectory() as _td:
        _settings = Path(_td) / "settings.json"
        _rules = PermissionRules(
            allow=["shell(npm test)", "read_file(*)"],
            deny=["shell(rm *)"],
        )
        _rules.save(path=_settings)
        _loaded = PermissionRules.load(path=_settings)
        assert _loaded.check("shell", "npm test") is PermissionLevel.ALLOW
        assert _loaded.check("shell", "rm -rf /") is PermissionLevel.DENY
        assert _loaded.check("read_file", "any/file.py") is PermissionLevel.ALLOW
        assert _loaded.check("shell", "ls -la") is None  # 无规则 → 交互式询问

    # check_command_danger
    _dangerous, _pat = check_command_danger("  sudo  rm -rf  / ", ["mkfs.", "rm -rf"])
    assert _dangerous and _pat == "rm -rf"
    assert check_command_danger("ls -la", ["rm -rf"]) == (False, "")

    print("openx/permissions.py OK ✓")
