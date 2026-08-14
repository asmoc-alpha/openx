"""Tests for the instruction loading system (OPENX.md)."""

import pytest
from pathlib import Path

from openx.instructions import (
    InstructionLevel,
    InstructionSet,
    InstructionRegistry,
    load_instructions,
    build_system_prompt,
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
