"""Instruction file management for OpenX.

Inspired by Claude Code's CLAUDE.md mechanism:
- OPENX.md files are Markdown files that inject custom instructions into the system prompt.
- Hierarchical loading: global (~/.openx/OPENX.md) → project (<workspace>/OPENX.md).
- Subdirectory-level OPENX.md scoping is reserved for a future iteration.

Usage:
    from openx.instructions import load_instructions, build_system_prompt

    instructions = load_instructions("/path/to/workspace")
    prompt = build_system_prompt("/path/to/workspace", instructions)
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

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


# ── Constants ───────────────────────────────────────────────────

GLOBAL_OPENX_DIR = Path.home() / ".openx"
GLOBAL_OPENX_MD = GLOBAL_OPENX_DIR / "OPENX.md"
PROJECT_OPENX_MD_NAME = "OPENX.md"

BASE_SYSTEM_PROMPT = """You are OpenX, an agentic coding assistant that helps developers write,
understand, and maintain code. You operate in a workspace — a directory on the user's
filesystem that serves as the project root.

## Your capabilities

You have access to tools that let you:
- **Read files** — Read any file in the workspace (or outside, if permitted)
- **Write files** — Create or overwrite files
- **Edit files** — Find-and-replace text in existing files (unique match by default)
- **Search code** — grep for patterns across the codebase
- **List files** — glob patterns and directory listings
- **Run shell commands** — Execute CLI commands (tests, builds, git, etc.)
- **Git operations** — status, diff, log, branch listing
- **Track tasks** — Use todo_write to plan and track multi-step work
- **Search the web** — web_search (DuckDuckGo, Bing fallback) and web_fetch for up-to-date info
- **Ask the user** — ask_user to clarify requirements or choose between approaches

## How you work

1. Understand the user's request
2. Explore the codebase as needed (read files, search, list directories)
3. Plan your approach
4. Execute — write/edit files, run commands
5. Verify your work

## Important rules

- **Be thorough.** Read relevant files before making changes. Don't guess.
- **Be precise.** When editing, match text exactly. Small edits > rewriting whole files.
- **Be safe.** Don't run destructive commands. Ask before deleting things.
- **Be transparent.** Explain what you're doing and why.
- **Be efficient.** Make multiple independent tool calls in parallel when possible.
- **Prefer tools over shell commands.** Use read_file, not `cat`. Use grep, not `grep`.
- **Respect the workspace.** Don't write outside the workspace without permission.

## Communication style

- Be concise but complete. Get to the point.
- Use code blocks for code, file paths as `monospace`.
- When you've finished a task, summarize what you did.
- If you're unsure about something, ask — don't assume.

## Current workspace

The workspace is the project root. All relative paths are relative to the workspace.
Use the tools to explore — don't ask the user to tell you what files exist."""


PLAN_MODE_INSTRUCTIONS = """

## Plan mode is active (计划模式已激活)

- Write tools (write_file, edit_file, shell) are DISABLED until the plan is
  approved — do not attempt to call them.
  写入类工具在计划获批前被禁用，不要尝试调用。
- Explore the codebase with read-only tools only: read_file, grep, glob,
  list_directory, and the git tools (git_status, git_diff, git_log, git_branch).
  仅使用只读工具探索代码库。
- When exploration is complete, produce a COMPLETE implementation plan (files
  to change, step-by-step approach, verification) and call exit_plan_mode
  EXACTLY ONCE with that plan as its `plan` argument.
  探索完成后产出完整实现计划，并恰好调用一次 exit_plan_mode。
- Do NOT ask the user to "approve" in plain text — the exit_plan_mode tool IS
  the approval flow; the user approves or rejects through it.
  不要用纯文本请求批准——exit_plan_mode 工具本身就是审批流程。
- If the user rejects, revise the plan from their feedback and call
  exit_plan_mode again. 若被拒绝，根据反馈修订计划后再次调用。
"""


MANUAL_MODE_INSTRUCTIONS = """

## Manual mode is active (手动模式已激活)

- Read-only tools (read_file, grep, glob, list_directory, the git_* tools,
  web_search / web_fetch, todo_write, ask_user) run WITHOUT confirmation.
  只读工具免确认直接执行。
- Tools that change anything (write_file, edit_file, shell, workflow, MCP
  tools) ALWAYS prompt the user per call — stored allow rules, the shell
  whitelist and auto-approve/-y do NOT apply in manual mode.
  写入类工具每次都弹窗确认，已存规则、白名单与 -y 一律不生效。
- If the user's task requires file changes or running commands, call the
  choose_mode tool ONCE — before any write tool — so the user can pick
  Auto / Plan / stay Manual. 需要修改文件或执行命令的任务，先调用一次
  choose_mode 让用户选择模式，再开始动手。
- Pure questions or analysis need no mode change — answer directly.
  纯问答/分析任务无需切换模式，直接作答。
- If the user stays in manual mode, proceed anyway: each write will be
  confirmed individually. NEVER call choose_mode again this session.
  用户选择保持手动则继续工作（逐项确认），切勿再次调用 choose_mode。
"""


SUBAGENT_INSTRUCTIONS = """

## Sub-agent mode (子代理模式)

You are a sub-agent spawned by the main agent to complete ONE delegated task.
你是主代理为完成一项委派任务而派生的子代理。

- Work autonomously — you CANNOT ask the user questions (there is no ask_user
  tool); make reasonable assumptions and proceed.
  自主工作——你无法向用户提问（没有 ask_user 工具），做合理假设继续推进。
- Your FINAL text response is the RETURN VALUE handed back to the main agent,
  NOT a human-facing message. 你的最终文本回复是交回主代理的**返回值**，
  不是面向人类的消息。
- Make it SELF-CONTAINED: state conclusions, exact file paths with line
  numbers, key code excerpts — everything the main agent needs to act on
  your result without re-reading the codebase.
  结果必须自包含：结论、精确文件路径与行号、关键代码摘录——让主代理
  无需重读代码即可据此行动。
- No pleasantries, no meta-commentary — report facts and results only.
  不要寒暄、不要元评论——只报告事实与结果。
"""

# 结构化输出契约：仅注入带 schema 的子代理（{schema} 由 agent 填充）。
# 关键语义：纯文本最终回复被丢弃，结果只认 structured_output 一次调用。
STRUCTURED_OUTPUT_INSTRUCTIONS = """

## Structured output (REQUIRED)

Your final result MUST be delivered by calling the `structured_output` tool
exactly once, with `data` conforming to this JSON Schema:

```json
{schema}
```

- Finish ALL your work first (reads, searches, edits, commands) — then call
  `structured_output` as your LAST action. The call ends your run.
- Do NOT present the final result as plain text — text responses are
  DISCARDED when structured output is required.
- If a call is rejected by validation, read the error, fix the reported
  fields, and call `structured_output` again.
"""


OPENX_MD_TEMPLATE = """# OPENX.md

Instructions for OpenX — the AI agent reads this file to understand your project
and follow your conventions. This file is loaded automatically when you run OpenX
in this directory.

## Project Overview

[Brief description of what this project does]

## Build & Test Commands

- Build:
- Test:
- Lint:

## Code Style & Conventions

-

## Architecture Notes

-

## Important Caveats

-
"""


# ── Data types ───────────────────────────────────────────────────

class InstructionLevel(Enum):
    """The level at which an instruction file was loaded."""
    GLOBAL = "global"      # ~/.openx/OPENX.md
    PROJECT = "project"    # <workspace>/OPENX.md
    SUBDIR = "subdir"      # <workspace>/<subdir>/OPENX.md (reserved)


@dataclass
class InstructionSet:
    """A set of loaded instructions from a single OPENX.md file."""

    level: InstructionLevel
    path: Path
    content: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.content.strip()


@dataclass
class InstructionRegistry:
    """Registry of all loaded instruction sets."""

    global_instructions: Optional[InstructionSet] = None
    project_instructions: Optional[InstructionSet] = None
    subdir_instructions: list[InstructionSet] = field(default_factory=list)

    @property
    def all_instructions(self) -> list[InstructionSet]:
        """All non-empty instructions in priority order (lowest first)."""
        result: list[InstructionSet] = []
        if self.global_instructions and not self.global_instructions.is_empty:
            result.append(self.global_instructions)
        if self.project_instructions and not self.project_instructions.is_empty:
            result.append(self.project_instructions)
        for s in self.subdir_instructions:
            if not s.is_empty:
                result.append(s)
        return result

    @property
    def has_any(self) -> bool:
        return len(self.all_instructions) > 0


@dataclass
class ProjectInfo:
    """Structured information about a workspace project.

    Built by agent.explore_project() for display in the CLI header/project overview.
    """

    project_type: str = "Unknown"               # e.g. "Python", "Node.js", "Rust"
    project_type_file: str = ""                 # the config file that identified the type
    config_files: list[str] = field(default_factory=list)  # key config files found
    file_counts: dict[str, int] = field(default_factory=dict)  # {".py": 15, ".md": 3}
    total_files: int = 0
    top_dirs: list[str] = field(default_factory=list)   # top-level subdirectories
    top_files: list[str] = field(default_factory=list)   # top-level files (config, README, etc.)
    git_branch: str = ""
    git_status_summary: str = ""                # "clean" or "+2/-1 uncommitted"
    git_recent: list[str] = field(default_factory=list)  # last 3 commits (oneline)
    openx_md_loaded: bool = False
    openx_md_sections: int = 0                  # count of ## headings in OPENX.md


# ── Project type detection ──────────────────────────────────────

# Mapping: config file name → (project_type_label,)
_PROJECT_TYPE_MAP: dict[str, str] = {
    "pyproject.toml": "Python",
    "setup.py": "Python",
    "setup.cfg": "Python",
    "requirements.txt": "Python",
    "Pipfile": "Python",
    "package.json": "Node.js",
    "tsconfig.json": "TypeScript",
    "next.config.js": "Next.js",
    "next.config.ts": "Next.js",
    "Cargo.toml": "Rust",
    "go.mod": "Go",
    "go.sum": "Go",
    "Makefile": "C/C++ (Make)",
    "CMakeLists.txt": "C/C++ (CMake)",
    "pom.xml": "Java (Maven)",
    "build.gradle": "Java (Gradle)",
    "build.gradle.kts": "Java (Gradle Kotlin)",
    "Gemfile": "Ruby",
    "mix.exs": "Elixir",
    "Cargo.lock": "Rust",
    "composer.json": "PHP",
    "pubspec.yaml": "Dart/Flutter",
    "build.sbt": "Scala",
}

_CONFIG_FILES = frozenset({
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile",
    "package.json", "tsconfig.json", "next.config.js", "next.config.ts",
    "Cargo.toml", "Cargo.lock", "go.mod", "go.sum",
    "Makefile", "CMakeLists.txt", "pom.xml", "build.gradle", "build.gradle.kts",
    "Gemfile", "mix.exs", "composer.json", "pubspec.yaml", "build.sbt",
    ".gitignore", ".env.example", ".env.template",
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "README.md", "LICENSE", "CHANGELOG.md", "CONTRIBUTING.md",
})


def detect_project_type(workspace: Path) -> tuple[str, str]:
    """Detect the project type by scanning for known config files.

    Returns (project_type_label, matched_config_file).
    If no known config file is found, returns ("Unknown", "").
    """
    for config_name, label in _PROJECT_TYPE_MAP.items():
        if (workspace / config_name).exists():
            return label, config_name
    return "Unknown", ""

def load_instructions(
    workspace: str | Path,
    *,
    global_path: Optional[Path] = None,
    project_filename: str = PROJECT_OPENX_MD_NAME,
) -> InstructionRegistry:
    """Load instruction files from all levels.

    Loading order (lower priority first):
    1. Global: ~/.openx/OPENX.md (always loaded if present)
    2. Project: <workspace>/OPENX.md (loaded if present)

    Missing files are silently skipped — they are not errors.

    Args:
        workspace: The workspace root directory.
        global_path: Override the global OPENX.md path (for testing).
        project_filename: Override the project-level filename (for testing).

    Returns:
        An InstructionRegistry with all loaded instruction sets.
    """
    workspace_path = Path(workspace).resolve()
    registry = InstructionRegistry()

    # 1. Global instructions
    global_md = global_path or GLOBAL_OPENX_MD
    if global_md.exists() and global_md.is_file():
        content = _read_file_safe(global_md)
        if content is not None:
            registry.global_instructions = InstructionSet(
                level=InstructionLevel.GLOBAL,
                path=global_md,
                content=content,
            )

    # 2. Project instructions
    project_md = workspace_path / project_filename
    if project_md.exists() and project_md.is_file():
        content = _read_file_safe(project_md)
        if content is not None:
            registry.project_instructions = InstructionSet(
                level=InstructionLevel.PROJECT,
                path=project_md,
                content=content,
            )

    # 3. Subdirectory instructions (future iteration)
    # Walk workspace_path for nested OPENX.md files, each scoped to its parent dir.

    return registry


def build_system_prompt(
    workspace: str | Path,
    registry: Optional[InstructionRegistry] = None,
    *,
    base_prompt: str = BASE_SYSTEM_PROMPT,
) -> str:
    """Build the full system prompt by appending any loaded instructions.

    The prompt structure is:

        [Base system prompt]

        ## Project Instructions

        [Global OPENX.md content, if any]

        [Project OPENX.md content, if any]

    If no instructions are loaded, returns the base prompt unchanged.

    Args:
        workspace: The workspace root directory.
        registry: Pre-loaded InstructionRegistry. If None, calls load_instructions().
        base_prompt: Override the base system prompt (for testing).

    Returns:
        The complete system prompt string.
    """
    workspace_path = Path(workspace).resolve()

    if registry is None:
        registry = load_instructions(workspace_path)

    # Inject workspace path into the base prompt
    prompt = base_prompt.rstrip()

    # Append instructions if any
    instructions = registry.all_instructions
    if instructions:
        prompt += "\n\n## Project Instructions\n\n"
        prompt += "The following instructions come from OPENX.md files and "
        prompt += "provide additional context about this project.\n\n"

        for iset in instructions:
            level_label = _level_label(iset.level)
            prompt += f"<!-- BEGIN {level_label}: {iset.path} -->\n"
            prompt += iset.content.strip() + "\n"
            prompt += f"<!-- END {level_label} -->\n\n"

    return prompt


def scaffold_openx_md(
    workspace: str | Path,
    *,
    filename: str = PROJECT_OPENX_MD_NAME,
    template: str = OPENX_MD_TEMPLATE,
    force: bool = False,
) -> tuple[Path, bool]:
    """Create an OPENX.md template in the workspace root.

    Args:
        workspace: The workspace root directory.
        filename: The filename to create (default: OPENX.md).
        template: Template content to write.
        force: If True, overwrite an existing file.

    Returns:
        Tuple of (path_to_created_file, already_existed).

    Raises:
        FileExistsError: If the file already exists and force=False.
    """
    workspace_path = Path(workspace).resolve()
    target = workspace_path / filename

    if target.exists() and not force:
        raise FileExistsError(f"Instruction file already exists: {target}")

    if target.exists():
        existed = True
    else:
        existed = False

    workspace_path.mkdir(parents=True, exist_ok=True)
    target.write_text(template.lstrip(), encoding="utf-8")
    return target, existed


def reload_instructions(
    workspace: str | Path,
    *,
    global_path: Optional[Path] = None,
) -> InstructionRegistry:
    """Reload instructions from disk. A convenience wrapper around load_instructions().

    Use this when OPENX.md files have been modified at runtime (e.g., after /init).
    """
    return load_instructions(workspace, global_path=global_path)


# ── Internal helpers ─────────────────────────────────────────────

def _read_file_safe(path: Path) -> Optional[str]:
    """Read a file's content, returning None on any error."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        return content
    except (OSError, PermissionError):
        return None


def _level_label(level: InstructionLevel) -> str:
    """Human-readable label for an instruction level."""
    labels = {
        InstructionLevel.GLOBAL: "GLOBAL",
        InstructionLevel.PROJECT: "PROJECT",
        InstructionLevel.SUBDIR: "SUBDIR",
    }
    return labels.get(level, "UNKNOWN")


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as _td:
        _ws = Path(_td)
        (_ws / "OPENX.md").write_text("# Test Project\n\nAlways use type hints.\n", encoding="utf-8")

        # load_instructions：global_path 指向不存在的临时路径，保证不读真实 home
        _reg = load_instructions(_ws, global_path=_ws / "no-global.md")
        assert _reg.has_any and _reg.project_instructions is not None

        # build_system_prompt：项目指令应被注入
        _prompt = build_system_prompt(_ws, _reg)
        assert "Always use type hints." in _prompt and "## Project Instructions" in _prompt
        print(f"system prompt: {len(_prompt)} chars, project OPENX.md injected ✓")

        # detect_project_type：无配置文件 → Unknown；加 pyproject.toml → Python
        assert detect_project_type(_ws) == ("Unknown", "")
        (_ws / "pyproject.toml").write_text("[project]\nname = 't'\n", encoding="utf-8")
        assert detect_project_type(_ws) == ("Python", "pyproject.toml")
        print(f"detect_project_type → {detect_project_type(_ws)}")

    print("openx/instructions.py OK ✓")
