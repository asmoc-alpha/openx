"""接线测试：agent 工具表 / 命令分发 / 菜单 / /plugins 命令。

运行：``python -m pytest tests/kernel/test_wiring.py -q``
"""

from __future__ import annotations

import pytest

from openx.app.cli import commands
from openx.config import OpenXConfig
from openx.kernel import get_kernel

from ._helpers import CONFLICT_CMD_SRC, CONFLICT_TOOL_SRC, HELLO_SRC, write_plugin


def _make_agent(ws):
    from openx.agent import OpenXAgent

    config = OpenXConfig()
    config.workspace = str(ws)
    config.api_key = "sk-test"
    config.api_base = "https://example.com/v1"
    config.model = "test-model"
    return OpenXAgent(config)


class StubConsole:
    """只捕获 print_plugins 的鸭子 console。"""

    def __init__(self):
        self.infos = None

    def print_plugins(self, infos):
        self.infos = list(infos)


class TestAgentWiring:
    def test_top_level_agent_gets_plugin_tools(self, kernel_env):
        ws, _ = kernel_env
        write_plugin(ws, "hello", HELLO_SRC)
        agent = _make_agent(ws)
        assert "hello" in agent.tools

    def test_child_agent_excludes_plugin_tools(self, kernel_env):
        ws, _ = kernel_env
        write_plugin(ws, "hello", HELLO_SRC)
        from openx.agent import OpenXAgent

        parent = _make_agent(ws)
        assert "hello" in parent.tools
        child = OpenXAgent(parent.config, parent=parent)
        assert "hello" not in child.tools  # 同结构性工具待遇

    def test_builtin_tool_priority_structural(self, kernel_env):
        """内置优先是结构性的：注册序即优先级，无需消费方仲裁。"""
        ws, _ = kernel_env
        write_plugin(ws, "impostor", CONFLICT_TOOL_SRC)
        agent = _make_agent(ws)
        assert agent.tools["grep"].description != "impostor"
        info = next(i for i in get_kernel().inventory() if i.id == "impostor")
        assert any("builtin wins" in w for w in info.warnings)


class TestCommandWiring:
    @pytest.fixture
    def loaded(self, kernel_env):
        ws, _ = kernel_env
        write_plugin(ws, "hello", HELLO_SRC)
        write_plugin(ws, "hijack", CONFLICT_CMD_SRC)
        get_kernel().ensure_loaded(str(ws))
        return ws

    def test_dispatch_plugin_command(self, loaded):
        handler = commands.find_handler("hi")
        assert handler is not None
        kernel_handler = get_kernel().lookup_command("hi")
        assert handler is kernel_handler

    def test_builtin_command_wins_dispatch(self, loaded):
        hijack = get_kernel().registry("commands").get("help").value.handler
        assert commands.find_handler("help") is not hijack

    def test_menu_entries_append_and_conflict(self, loaded):
        entries = commands.menu_entries()
        names = [name for name, _, _ in entries]
        assert "hi" in names
        help_descs = [d for n, d, _ in entries if n == "help"]
        assert help_descs == ["Show all available commands"]  # 内置描述，非 hijack
        info = next(i for i in get_kernel().inventory() if i.id == "hijack")
        assert any("builtin wins" in w for w in info.warnings)

    def test_all_descriptions_merges_plugins(self, loaded):
        desc = commands.all_descriptions()
        assert "hi" in desc
        assert desc["help"] == "Show all available commands"


class TestPluginsCommand:
    @pytest.mark.asyncio
    async def test_plugins_lists_inventory(self, kernel_env):
        ws, _ = kernel_env
        write_plugin(ws, "hello", HELLO_SRC)
        agent = _make_agent(ws)
        stub = StubConsole()
        result = await commands.handle_slash_command("plugins", agent, stub, [])
        assert result is True
        assert any(i.id == "hello" and i.phase == "active" for i in stub.infos)
