"""Tests for project exploration — instruction-file discovery."""

from pathlib import Path

from openx.services.exploration import _check_instruction_files
from openx.instructions import ProjectInfo


class TestCheckInstructionFiles:
    """_check_instruction_files：概览面板的指令文件发现。"""

    def _check(self, ws: Path) -> ProjectInfo:
        info = ProjectInfo()
        _check_instruction_files(ws, info)
        return info

    def test_no_instruction_files(self, tmp_path):
        info = self._check(tmp_path)
        assert not info.openx_md_loaded
        assert info.openx_md_sections == 0
        assert info.instruction_files == []

    def test_openx_md_only(self, tmp_path):
        (tmp_path / "OPENX.md").write_text("# T\n\n## Style\n\nUse types.\n")
        info = self._check(tmp_path)
        assert info.openx_md_loaded
        assert info.openx_md_sections == 1
        assert info.instruction_files == ["OPENX.md"]

    def test_agent_md_only(self, tmp_path):
        """AGENT.md-only project: extras recorded, OPENX.md stays unloaded."""
        (tmp_path / "AGENT.md").write_text("## Rules\n\nRun linters.\n")
        info = self._check(tmp_path)
        assert not info.openx_md_loaded
        assert info.openx_md_sections == 0
        assert info.instruction_files == ["AGENT.md"]

    def test_all_three_in_injection_order(self, tmp_path):
        # 故意乱序创建：结果必须是 OPENX.md → AGENT.md → AGENTS.md
        (tmp_path / "AGENTS.md").write_text("agents\n")
        (tmp_path / "OPENX.md").write_text("openx\n")
        (tmp_path / "AGENT.md").write_text("agent\n")
        info = self._check(tmp_path)
        assert info.instruction_files == ["OPENX.md", "AGENT.md", "AGENTS.md"]

    def test_empty_and_whitespace_files_skipped(self, tmp_path):
        (tmp_path / "AGENT.md").write_text("")
        (tmp_path / "AGENTS.md").write_text("   \n\n  \n")
        info = self._check(tmp_path)
        assert info.instruction_files == []
        assert not info.openx_md_loaded

    def test_sections_counted_for_leading_heading(self, tmp_path):
        (tmp_path / "OPENX.md").write_text("## First\n\nx\n\n## Second\n\ny\n")
        info = self._check(tmp_path)
        assert info.openx_md_sections == 2

    def test_directory_named_agent_md_ignored(self, tmp_path):
        (tmp_path / "AGENT.md").mkdir()
        info = self._check(tmp_path)
        assert info.instruction_files == []
