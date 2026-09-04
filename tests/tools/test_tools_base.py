"""Tests for OpenX."""

import pytest
from pathlib import Path
from openx.config import OpenXConfig
from openx.permissions import Permission, PermissionLevel, check_command_danger
from openx.tools.base import Tool, ToolResult, truncate_output
from openx.tools.file_tools import ReadFileTool, WriteFileTool, EditFileTool, GlobTool
from openx.tools.search_tools import GrepTool


class TestConfig:
    """Configuration tests."""

    def test_default_config(self):
        config = OpenXConfig()
        # 模型为解析后 echo，默认为空；凭据/端点字段已不在 config 上
        assert config.model == ""
        assert config.active_group == ""
        assert config.temperature == 0.0
        assert config.max_tool_rounds == 30
        assert not config.auto_approve
        assert not hasattr(config, "api_key")
        assert not hasattr(config, "api_base")

    def test_merge_config(self):
        config = OpenXConfig()
        config._merge({"model": "gpt-4-turbo", "temperature": 0.5})
        assert config.model == "gpt-4-turbo"
        assert config.temperature == 0.5

    def test_merge_list_extends(self):
        config = OpenXConfig()
        original = len(config.allowed_commands)
        config._merge({"allowed_commands": ["custom-cmd"]})
        assert len(config.allowed_commands) == original + 1
        assert "custom-cmd" in config.allowed_commands


class TestPermissions:
    """Permission system tests."""

    def test_allow(self):
        perm = Permission.allow()
        assert perm.level == PermissionLevel.ALLOW

    def test_ask(self):
        perm = Permission.ask("Need confirmation")
        assert perm.level == PermissionLevel.ASK
        assert "confirmation" in perm.reason

    def test_deny(self):
        perm = Permission.deny("Too dangerous")
        assert perm.level == PermissionLevel.DENY

    def test_check_dangerous(self):
        dangerous = ["rm -rf", "sudo rm", "fork bomb"]
        is_dangerous, matched = check_command_danger("rm -rf /", dangerous)
        assert is_dangerous
        assert "rm -rf" in matched

    def test_check_safe(self):
        dangerous = ["rm -rf", "sudo rm"]
        is_dangerous, matched = check_command_danger("ls -la", dangerous)
        assert not is_dangerous


class TestToolResult:
    """ToolResult tests."""

    def test_success(self):
        result = ToolResult(output="Hello world")
        assert result.success
        assert "Hello world" in result.to_message()

    def test_error(self):
        result = ToolResult(error="Something went wrong")
        assert not result.success
        assert "Error" in result.to_message()

    def test_truncated(self):
        result = ToolResult(output="x", truncated=True, truncated_notice="...truncated")
        assert "...truncated" in result.to_message()


class TestTruncateOutput:
    """Output truncation tests."""

    def test_no_truncation(self):
        text = "short text"
        result, was_truncated, notice = truncate_output(text, max_lines=100, max_chars=10000)
        assert result == text
        assert not was_truncated

    def test_line_truncation(self):
        lines = [f"line {i}" for i in range(100)]
        text = "\n".join(lines)
        result, was_truncated, notice = truncate_output(text, max_lines=50)
        assert was_truncated
        assert result.count("\n") <= 49

    def test_char_truncation(self):
        text = "x" * 5000
        result, was_truncated, notice = truncate_output(text, max_chars=100)
        assert was_truncated
        assert len(result) <= 100


class TestReadFileTool:
    """ReadFileTool tests."""

    @pytest.mark.asyncio
    async def test_read_file(self, tmp_path):
        file_path = tmp_path / "test.txt"
        file_path.write_text("line 1\nline 2\nline 3\n")

        tool = ReadFileTool(str(tmp_path))
        result = await tool.execute(str(file_path))
        assert result.success
        assert "line 1" in result.output
        assert "line 2" in result.output
        assert "line 3" in result.output

    @pytest.mark.asyncio
    async def test_read_file_range(self, tmp_path):
        file_path = tmp_path / "test.txt"
        file_path.write_text("line 1\nline 2\nline 3\nline 4\nline 5\n")

        tool = ReadFileTool(str(tmp_path))
        result = await tool.execute(str(file_path), start_line=2, end_line=4)
        assert result.success
        assert "line 1" not in result.output
        assert "line 2" in result.output
        assert "line 3" in result.output
        assert "line 4" in result.output
        assert "line 5" not in result.output

    @pytest.mark.asyncio
    async def test_read_missing_file(self, tmp_path):
        tool = ReadFileTool(str(tmp_path))
        result = await tool.execute(str(tmp_path / "nonexistent.txt"))
        assert not result.success
        assert "not found" in result.error.lower()


class TestWriteFileTool:
    """WriteFileTool tests."""

    @pytest.mark.asyncio
    async def test_write_file(self, tmp_path):
        tool = WriteFileTool(str(tmp_path))
        result = await tool.execute(str(tmp_path / "output.txt"), "hello world")
        assert result.success
        assert (tmp_path / "output.txt").read_text() == "hello world"

    @pytest.mark.asyncio
    async def test_write_outside_workspace(self, tmp_path):
        tool = WriteFileTool(str(tmp_path), allow_outside_workspace=False)
        outside = tmp_path.parent / "outside.txt"
        result = await tool.execute(str(outside), "bad")
        assert not result.success
        assert "outside workspace" in result.error.lower()

    @pytest.mark.asyncio
    async def test_write_creates_parent_dirs(self, tmp_path):
        tool = WriteFileTool(str(tmp_path))
        path = tmp_path / "deep" / "nested" / "file.txt"
        result = await tool.execute(str(path), "deep content")
        assert result.success
        assert path.read_text() == "deep content"


class TestEditFileTool:
    """EditFileTool tests."""

    @pytest.mark.asyncio
    async def test_edit_file(self, tmp_path):
        file_path = tmp_path / "test.py"
        file_path.write_text("hello world\nfoo bar\n")

        tool = EditFileTool(str(tmp_path))
        result = await tool.execute(str(file_path), "world", "universe")
        assert result.success
        assert "universe" in file_path.read_text()
        assert "world" not in file_path.read_text()

    @pytest.mark.asyncio
    async def test_edit_not_found(self, tmp_path):
        file_path = tmp_path / "test.py"
        file_path.write_text("hello world\n")

        tool = EditFileTool(str(tmp_path))
        result = await tool.execute(str(file_path), "nonexistent", "replacement")
        assert not result.success
        assert "not found" in result.error.lower()


class TestGlobTool:
    """GlobTool tests."""

    @pytest.mark.asyncio
    async def test_glob(self, tmp_path):
        (tmp_path / "a.py").write_text("")
        (tmp_path / "b.py").write_text("")
        (tmp_path / "c.txt").write_text("")

        tool = GlobTool(str(tmp_path))
        result = await tool.execute("*.py")
        assert result.success
        assert "a.py" in result.output
        assert "b.py" in result.output
        assert "c.txt" not in result.output


class TestGrepTool:
    """GrepTool tests."""

    @pytest.mark.asyncio
    async def test_grep(self, tmp_path):
        (tmp_path / "a.py").write_text("def foo():\n    pass\n")
        (tmp_path / "b.py").write_text("def bar():\n    return foo()\n")

        tool = GrepTool(str(tmp_path))
        result = await tool.execute("foo", path=str(tmp_path))
        assert result.success
        assert "foo" in result.output

    @pytest.mark.asyncio
    async def test_grep_regex(self, tmp_path):
        (tmp_path / "a.py").write_text("def foo_bar():\n    pass\n")

        tool = GrepTool(str(tmp_path))
        result = await tool.execute(r"def \w+", path=str(tmp_path), is_regex=True)
        assert result.success
        assert "foo_bar" in result.output

    @pytest.mark.asyncio
    async def test_grep_no_match(self, tmp_path):
        (tmp_path / "a.py").write_text("hello world\n")

        tool = GrepTool(str(tmp_path))
        result = await tool.execute("nonexistent", path=str(tmp_path))
        assert "No matches" in result.output


class TestToolSchema:
    """Tool schema generation tests."""

    def test_tool_to_openai_schema(self):
        """Test that a tool generates a valid OpenAI function schema."""
        tool = ReadFileTool("/tmp")
        schema = tool.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "read_file"
        assert "file_path" in schema["function"]["parameters"]["properties"]
        assert "file_path" in schema["function"]["parameters"]["required"]
