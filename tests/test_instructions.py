"""Tests for the instruction loading system (OPENX.md / AGENT.md / AGENTS.md)."""

import pytest
from pathlib import Path

from openx.instructions import (
    InstructionLevel,
    InstructionSet,
    InstructionRegistry,
    load_instructions,
    build_system_prompt,
    format_instructions,
    scaffold_openx_md,
    reload_instructions,
    BASE_SYSTEM_PROMPT,
    OPENX_MD_TEMPLATE,
)


class TestInstructionSet:
    """InstructionSet dataclass tests."""

    def test_empty_when_no_content(self):
        s = InstructionSet(level=InstructionLevel.PROJECT, path=Path("/tmp"))
        assert s.is_empty

    def test_not_empty_with_content(self):
        s = InstructionSet(
            level=InstructionLevel.PROJECT,
            path=Path("/tmp"),
            content="Hello world",
        )
        assert not s.is_empty


class TestInstructionRegistry:
    """InstructionRegistry tests."""

    def test_empty_registry(self):
        reg = InstructionRegistry()
        assert not reg.has_any
        assert reg.all_instructions == []

    def test_with_global_only(self):
        reg = InstructionRegistry()
        reg.global_instructions = InstructionSet(
            level=InstructionLevel.GLOBAL,
            path=Path("/home/.openx/OPENX.md"),
            content="Global instructions",
        )
        assert reg.has_any
        assert len(reg.all_instructions) == 1
        assert reg.all_instructions[0].level == InstructionLevel.GLOBAL

    def test_with_all_levels(self):
        reg = InstructionRegistry(
            global_instructions=InstructionSet(
                level=InstructionLevel.GLOBAL,
                path=Path("/home/.openx/OPENX.md"),
                content="Global",
            ),
            project_instructions=InstructionSet(
                level=InstructionLevel.PROJECT,
                path=Path("/ws/OPENX.md"),
                content="Project",
            ),
        )
        assert reg.has_any
        assert len(reg.all_instructions) == 2
        # Global comes before project
        assert reg.all_instructions[0].level == InstructionLevel.GLOBAL
        assert reg.all_instructions[1].level == InstructionLevel.PROJECT

    def test_empty_instructions_skipped(self):
        """Instructions with empty content should be skipped."""
        reg = InstructionRegistry(
            global_instructions=InstructionSet(
                level=InstructionLevel.GLOBAL,
                path=Path("/home/.openx/OPENX.md"),
                content="",  # empty
            ),
            project_instructions=InstructionSet(
                level=InstructionLevel.PROJECT,
                path=Path("/ws/OPENX.md"),
                content="Project instructions",
            ),
        )
        assert reg.has_any
        # Empty global should be skipped
        assert len(reg.all_instructions) == 1
        assert reg.all_instructions[0].level == InstructionLevel.PROJECT


class TestLoadInstructions:
    """Tests for load_instructions() with real files."""

    def test_no_files(self, tmp_path):
        """When no OPENX.md files exist, registry should be empty."""
        reg = load_instructions(str(tmp_path), global_path=tmp_path / "nonexistent.md")
        assert not reg.has_any
        assert reg.global_instructions is None
        assert reg.project_instructions is None

    def test_project_only(self, tmp_path):
        """Load only a project-level OPENX.md."""
        project_md = tmp_path / "OPENX.md"
        project_md.write_text("# My Project\n\nBuild with: make")

        reg = load_instructions(
            str(tmp_path),
            global_path=tmp_path / "nonexistent.md",
        )
        assert reg.has_any
        assert reg.project_instructions is not None
        assert "# My Project" in reg.project_instructions.content
        assert reg.project_instructions.level == InstructionLevel.PROJECT
        assert reg.global_instructions is None

    def test_global_and_project(self, tmp_path):
        """Load both global and project-level OPENX.md."""
        global_md = tmp_path / "global_OPENX.md"
        global_md.write_text("Global: always use type hints")

        project_md = tmp_path / "OPENX.md"
        project_md.write_text("Project: use pytest")

        reg = load_instructions(
            str(tmp_path),
            global_path=global_md,
        )
        assert reg.has_any
        assert reg.global_instructions is not None
        assert "type hints" in reg.global_instructions.content
        assert reg.project_instructions is not None
        assert "pytest" in reg.project_instructions.content

    def test_custom_project_filename(self, tmp_path):
        """Support custom project file names."""
        custom_md = tmp_path / ".myproject.md"
        custom_md.write_text("Custom instructions")

        reg = load_instructions(
            str(tmp_path),
            global_path=tmp_path / "nonexistent.md",
            project_filename=".myproject.md",
        )
        assert reg.has_any
        assert reg.project_instructions is not None
        assert "Custom instructions" in reg.project_instructions.content

    def test_empty_file_skipped(self, tmp_path):
        """An empty OPENX.md should not be treated as an instruction set."""
        (tmp_path / "OPENX.md").write_text("")

        reg = load_instructions(
            str(tmp_path),
            global_path=tmp_path / "nonexistent.md",
        )
        # Empty file still creates an InstructionSet, but it's filtered in has_any/all
        assert not reg.has_any
        assert len(reg.all_instructions) == 0

    def test_whitespace_only_skipped(self, tmp_path):
        """Whitespace-only content should be treated as empty."""
        (tmp_path / "OPENX.md").write_text("   \n\n  \n")

        reg = load_instructions(str(tmp_path))
        assert not reg.has_any


class TestAgentMd:
    """项目级 AGENT.md / AGENTS.md（跨工具标准名）加载。

    语义：与 OPENX.md **并存**（非替代），注入序 OPENX.md → AGENT.md →
    AGENTS.md；仅项目级识别，全局目录下的 AGENT.md 不加载。
    """

    def test_agent_md_alone_is_loaded(self, tmp_path):
        """Only AGENT.md present — still yields instructions."""
        (tmp_path / "AGENT.md").write_text("AGENT rule: run linters.")

        reg = load_instructions(
            str(tmp_path), global_path=tmp_path / "nonexistent.md"
        )
        assert reg.has_any
        assert reg.project_instructions is None          # 不是 OPENX.md
        assert len(reg.project_extra) == 1
        extra = reg.project_extra[0]
        assert extra.path.name == "AGENT.md"
        assert extra.level == InstructionLevel.PROJECT
        assert "run linters" in extra.content
        assert reg.all_instructions[0] is extra

    def test_agents_md_alone_is_loaded(self, tmp_path):
        """Only AGENTS.md present — still yields instructions."""
        (tmp_path / "AGENTS.md").write_text("AGENTS rule: keep it typed.")

        reg = load_instructions(
            str(tmp_path), global_path=tmp_path / "nonexistent.md"
        )
        assert reg.has_any
        assert len(reg.project_extra) == 1
        assert reg.project_extra[0].path.name == "AGENTS.md"

    def test_openx_first_then_agent_then_agents(self, tmp_path):
        """All three coexist — injection order is OPENX.md → AGENT.md → AGENTS.md."""
        (tmp_path / "OPENX.md").write_text("FROM-OPENX")
        (tmp_path / "AGENTS.md").write_text("FROM-AGENTS")
        (tmp_path / "AGENT.md").write_text("FROM-AGENT")

        reg = load_instructions(
            str(tmp_path), global_path=tmp_path / "nonexistent.md"
        )
        names = [s.path.name for s in reg.all_instructions]
        assert names == ["OPENX.md", "AGENT.md", "AGENTS.md"]
        # OPENX.md 仍占 project_instructions（向后兼容）
        assert reg.project_instructions.path.name == "OPENX.md"
        assert [s.path.name for s in reg.project_extra] == ["AGENT.md", "AGENTS.md"]

    def test_global_ordering_with_extras(self, tmp_path):
        """Global OPENX.md stays first, before any project-level extra."""
        global_md = tmp_path / "global_OPENX.md"
        global_md.write_text("FROM-GLOBAL")
        (tmp_path / "AGENT.md").write_text("FROM-AGENT")

        reg = load_instructions(str(tmp_path), global_path=global_md)
        names = [s.path.name for s in reg.all_instructions]
        assert names == ["global_OPENX.md", "AGENT.md"]
        levels = [s.level for s in reg.all_instructions]
        assert levels == [InstructionLevel.GLOBAL, InstructionLevel.PROJECT]

    def test_global_agent_md_not_loaded(self, tmp_path):
        """AGENT.md is project-level only — a global one is ignored."""
        global_dir = tmp_path / "home"
        global_dir.mkdir()
        (global_dir / "AGENT.md").write_text("GLOBAL-AGENT-RULE")
        (global_dir / "AGENTS.md").write_text("GLOBAL-AGENTS-RULE")
        # workspace 是 global_dir 的兄弟目录，本身无任何指令文件
        ws = tmp_path / "ws"
        ws.mkdir()

        reg = load_instructions(str(ws), global_path=global_dir / "OPENX.md")
        assert not reg.has_any
        assert reg.project_extra == []

    def test_empty_and_whitespace_extras_skipped(self, tmp_path):
        """Empty / whitespace-only AGENT*.md contribute nothing."""
        (tmp_path / "OPENX.md").write_text("FROM-OPENX")
        (tmp_path / "AGENT.md").write_text("")
        (tmp_path / "AGENTS.md").write_text("   \n\n  \n")

        reg = load_instructions(
            str(tmp_path), global_path=tmp_path / "nonexistent.md"
        )
        assert reg.has_any
        # 文件存在 → 建了 InstructionSet，但空内容被 all_instructions 过滤
        assert [s.path.name for s in reg.all_instructions] == ["OPENX.md"]

    def test_subdirectory_agent_md_not_loaded(self, tmp_path):
        """Only the workspace root counts (subdir scoping is reserved)."""
        sub = tmp_path / "packages" / "api"
        sub.mkdir(parents=True)
        (sub / "AGENT.md").write_text("NESTED-AGENT-RULE")

        reg = load_instructions(
            str(tmp_path), global_path=tmp_path / "nonexistent.md"
        )
        assert not reg.has_any

    def test_custom_project_filename_does_not_duplicate(self, tmp_path):
        """project_filename 覆写成 AGENT.md 时，同一文件只注入一次。"""
        (tmp_path / "AGENT.md").write_text("FROM-AGENT")

        reg = load_instructions(
            str(tmp_path),
            global_path=tmp_path / "nonexistent.md",
            project_filename="AGENT.md",
        )
        assert len(reg.all_instructions) == 1
        assert reg.project_instructions.path.name == "AGENT.md"
        assert reg.project_extra == []

    def test_format_instructions_lists_extras(self, tmp_path):
        """Each source is wrapped in its own BEGIN/END comment naming the path."""
        (tmp_path / "OPENX.md").write_text("FROM-OPENX")
        (tmp_path / "AGENT.md").write_text("FROM-AGENT")

        reg = load_instructions(
            str(tmp_path), global_path=tmp_path / "nonexistent.md"
        )
        section = format_instructions(reg)
        assert "FROM-OPENX" in section and "FROM-AGENT" in section
        assert section.count("<!-- BEGIN PROJECT:") == 2
        assert section.count("<!-- END PROJECT -->") == 2
        assert "AGENT.md" in section          # 来源路径可辨识
        # 注入顺序与 all_instructions 一致
        assert section.index("FROM-OPENX") < section.index("FROM-AGENT")

    def test_format_instructions_mentions_all_names(self, tmp_path):
        """描述句泛化：不再只提 OPENX.md。"""
        (tmp_path / "AGENTS.md").write_text("FROM-AGENTS")

        reg = load_instructions(
            str(tmp_path), global_path=tmp_path / "nonexistent.md"
        )
        section = format_instructions(reg)
        assert "OPENX.md" in section and "AGENT.md" in section
        assert "AGENTS.md" in section

    def test_build_system_prompt_includes_extras(self, tmp_path):
        """AGENT.md content reaches the assembled system prompt."""
        (tmp_path / "AGENT.md").write_text("AGENT rule: run linters.")

        reg = load_instructions(
            str(tmp_path), global_path=tmp_path / "nonexistent.md"
        )
        prompt = build_system_prompt(str(tmp_path), reg)
        assert "## Project Instructions" in prompt
        assert "AGENT rule: run linters." in prompt
        assert BASE_SYSTEM_PROMPT.strip() in prompt

    def test_reload_picks_up_new_agent_md(self, tmp_path):
        """Runtime-created AGENT.md is found by reload (as after /init)."""
        reg = load_instructions(
            str(tmp_path), global_path=tmp_path / "nonexistent.md"
        )
        assert not reg.has_any

        (tmp_path / "AGENT.md").write_text("Added later.")
        reg2 = reload_instructions(
            str(tmp_path), global_path=tmp_path / "nonexistent.md"
        )
        assert reg2.has_any
        assert "Added later." in reg2.project_extra[0].content


class TestBuildSystemPrompt:
    """Tests for build_system_prompt()."""

    def test_no_instructions(self, tmp_path):
        """Without any OPENX.md, prompt should be base prompt only."""
        reg = InstructionRegistry()
        prompt = build_system_prompt(str(tmp_path), reg)
        assert BASE_SYSTEM_PROMPT.strip() in prompt
        assert "## Project Instructions" not in prompt

    def test_with_project_instructions(self, tmp_path):
        """Project instructions should be appended to the prompt."""
        reg = InstructionRegistry(
            project_instructions=InstructionSet(
                level=InstructionLevel.PROJECT,
                path=Path(tmp_path) / "OPENX.md",
                content="Always use pytest.",
            ),
        )
        prompt = build_system_prompt(str(tmp_path), reg)
        assert "Always use pytest." in prompt
        assert "## Project Instructions" in prompt
        assert "<!-- BEGIN PROJECT:" in prompt
        assert "<!-- END PROJECT -->" in prompt

    def test_with_global_and_project(self, tmp_path):
        """Both global and project instructions should appear."""
        reg = InstructionRegistry(
            global_instructions=InstructionSet(
                level=InstructionLevel.GLOBAL,
                path=Path("/home/.openx/OPENX.md"),
                content="Use type hints everywhere.",
            ),
            project_instructions=InstructionSet(
                level=InstructionLevel.PROJECT,
                path=Path(tmp_path) / "OPENX.md",
                content="This project uses FastAPI.",
            ),
        )
        prompt = build_system_prompt(str(tmp_path), reg)
        assert "Use type hints everywhere." in prompt
        assert "This project uses FastAPI." in prompt
        # Global should come before project
        assert prompt.index("GLOBAL") < prompt.index("PROJECT")

    def test_auto_loads_when_registry_is_none(self, tmp_path):
        """build_system_prompt should auto-load if no registry provided."""
        (tmp_path / "OPENX.md").write_text("Auto-loaded instructions")
        prompt = build_system_prompt(str(tmp_path), None)
        assert "Auto-loaded instructions" in prompt


class TestFormatInstructions:
    """format_instructions()：指令块片段（K7 收口，context/v1 贡献契约）。"""

    def test_none_registry_is_empty(self):
        assert format_instructions(None) == ""

    def test_empty_registry_is_empty(self):
        assert format_instructions(InstructionRegistry()) == ""

    def test_bare_section_has_no_leading_separator(self, tmp_path):
        reg = InstructionRegistry(
            project_instructions=InstructionSet(
                level=InstructionLevel.PROJECT,
                path=Path(tmp_path) / "OPENX.md",
                content="Always use pytest.",
            ),
        )
        section = format_instructions(reg)
        assert section.startswith("## Project Instructions")  # 无前导空行
        assert "Always use pytest." in section
        assert "<!-- BEGIN PROJECT:" in section and "<!-- END PROJECT -->" in section

    def test_is_the_tail_of_build_system_prompt(self, tmp_path):
        reg = InstructionRegistry(
            project_instructions=InstructionSet(
                level=InstructionLevel.PROJECT,
                path=Path(tmp_path) / "OPENX.md",
                content="Shared body.",
            ),
        )
        section = format_instructions(reg)
        prompt = build_system_prompt(str(tmp_path), reg)
        assert prompt.endswith(section)          # 同一份块（逐字节）
        assert prompt == BASE_SYSTEM_PROMPT.rstrip() + "\n\n" + section


class TestScaffoldOpenxMd:
    """Tests for scaffold_openx_md()."""

    def test_creates_file(self, tmp_path):
        path, existed = scaffold_openx_md(str(tmp_path))
        assert path.exists()
        assert not existed
        content = path.read_text()
        assert "Project Overview" in content
        assert "Build & Test Commands" in content

    def test_detects_existing(self, tmp_path):
        (tmp_path / "OPENX.md").write_text("existing")
        path, existed = scaffold_openx_md(str(tmp_path), force=True)
        assert existed

    def test_raises_on_existing_without_force(self, tmp_path):
        (tmp_path / "OPENX.md").write_text("existing")
        with pytest.raises(FileExistsError):
            scaffold_openx_md(str(tmp_path), force=False)

    def test_custom_filename(self, tmp_path):
        path, existed = scaffold_openx_md(
            str(tmp_path),
            filename=".custom.md",
        )
        assert path.name == ".custom.md"
        assert path.exists()

    def test_custom_template(self, tmp_path):
        custom = "# Custom Template\n\nHello!"
        path, _ = scaffold_openx_md(str(tmp_path), template=custom)
        assert "Custom Template" in path.read_text()


class TestReloadInstructions:
    """Tests for reload_instructions()."""

    def test_reload_picks_up_new_file(self, tmp_path):
        """After creating a new OPENX.md, reload should find it."""
        # First load — no file
        reg = load_instructions(str(tmp_path), global_path=tmp_path / "nonexistent.md")
        assert not reg.has_any

        # Create the file
        (tmp_path / "OPENX.md").write_text("New instructions!")

        # Reload should find it
        reg2 = reload_instructions(str(tmp_path), global_path=tmp_path / "nonexistent.md")
        assert reg2.has_any
        assert "New instructions!" in reg2.project_instructions.content


class TestTemplateContent:
    """Ensure template has all required sections."""

    def test_template_sections(self):
        assert "## Project Overview" in OPENX_MD_TEMPLATE
        assert "## Build & Test Commands" in OPENX_MD_TEMPLATE
        assert "## Code Style & Conventions" in OPENX_MD_TEMPLATE
        assert "## Architecture Notes" in OPENX_MD_TEMPLATE
        assert "## Important Caveats" in OPENX_MD_TEMPLATE
