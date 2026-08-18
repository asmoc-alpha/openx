"""贡献注册表与加载期校验器单测。

运行：``python -m pytest tests/kernel/test_registry.py -q``
"""

from __future__ import annotations

import pytest

from openx.kernel.context import CommandContribution
from openx.kernel.registry import ContributionRegistry
from openx.kernel.validate import validate_command, validate_tool
from openx.permissions import Permission, PermissionLevel
from openx.tools.base import Tool, ToolResult


class GoodTool(Tool):
    name = "gt"
    description = "a good tool"
    parameters = {"type": "object", "properties": {}}

    @property
    def permission(self) -> Permission:
        return Permission(level=PermissionLevel.ALLOW)

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(output="ok")


async def _handler(agent, console, args):
    return True


class TestRegistry:
    def test_register_and_items(self):
        reg = ContributionRegistry("tools", validate_tool)
        assert reg.register("gt", GoodTool(), "p1") == []
        assert set(reg.items()) == {"gt"}

    def test_first_wins_between_plugins(self):
        reg = ContributionRegistry("tools", validate_tool)
        reg.register("gt", GoodTool(), "p1")
        problems = reg.register("gt", GoodTool(), "p2")
        assert problems and "first wins" in problems[0]
        assert reg.get("gt").plugin == "p1"

    def test_validator_rejects(self):
        reg = ContributionRegistry("tools", validate_tool)
        problems = reg.register("x", object(), "p1")
        assert problems  # 无 name/permission/execute
        assert reg.items() == {}

    def test_note_conflict_dedup(self):
        reg = ContributionRegistry("tools", validate_tool)
        reg.register("gt", GoodTool(), "p1")
        reg.note_conflict("gt", "gt")
        reg.note_conflict("gt", "gt")
        assert len(reg.get("gt").warnings) == 1

    def test_empty_name_rejected(self):
        reg = ContributionRegistry("tools")
        assert reg.register("", 1, "p1")


class TestValidators:
    def test_tool_ok(self):
        assert validate_tool("gt", GoodTool()) == []

    def test_tool_name_mismatch(self):
        problems = validate_tool("other", GoodTool())
        assert any("!= registered name" in p for p in problems)

    def test_tool_missing_permission(self):
        class NoPerm:
            name = "np"
            description = "d"
            parameters = {}

            async def execute(self, **kw):
                return ToolResult()

        problems = validate_tool("np", NoPerm())
        assert any("permission" in p for p in problems)

    def test_command_ok(self):
        contrib = CommandContribution(_handler, "d", ["al"])
        assert validate_command("my-cmd", contrib) == []

    def test_command_non_async_handler(self):
        def sync(agent, console, args):
            return True

        assert validate_command("c", CommandContribution(sync))

    def test_command_bad_name(self):
        assert validate_command("Bad Name", CommandContribution(_handler))

    def test_command_bad_aliases(self):
        contrib = CommandContribution(_handler, "", ["UPPER"])
        assert validate_command("c", contrib)
