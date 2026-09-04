"""内置工具升格插件：base bundle 恒挂载、失败致命、禁用豁免、id 保护。

运行：``python -m pytest tests/kernel/test_builtin_plugin.py -q``
"""

from __future__ import annotations

import json

import pytest

from openx.kernel import BUILTIN_TOOLS_ID, get_kernel, reset_kernel

from ._helpers import write_plugin


class TestBuiltinMounted:
    def test_builtin_in_inventory(self, kernel_env):
        ws, _ = kernel_env
        get_kernel().ensure_loaded(str(ws))
        info = next(i for i in get_kernel().inventory() if i.id == BUILTIN_TOOLS_ID)
        assert info.phase == "active"
        assert info.source == "base-bundle"
        assert any("core-tools" in t for t in info.tools)

    def test_agent_tools_come_from_builtin(self, kernel_env):
        ws, _ = kernel_env
        from openx.agent import OpenXAgent
        from openx.config import OpenXConfig

        config = OpenXConfig()
        config.workspace = str(ws)
        config.model = "test-model"
        agent = OpenXAgent(config)
        for name in ("read_file", "shell", "grep", "task", "workflow"):
            assert name in agent.tools

    def test_builtin_immune_to_disable(self, kernel_env):
        ws, settings = kernel_env
        settings.write_text(
            json.dumps({"plugins": {"disabled": [BUILTIN_TOOLS_ID]}})
        )
        get_kernel().ensure_loaded(str(ws))
        info = next(i for i in get_kernel().inventory() if i.id == BUILTIN_TOOLS_ID)
        assert info.phase == "active"  # 禁用表对内置无效


class TestBuiltinSemantics:
    def test_builtin_failure_is_fatal(self, kernel_env, monkeypatch):
        ws, _ = kernel_env

        def boom(ctx):
            raise RuntimeError("builtin broken")

        monkeypatch.setattr("openx.builtin.tools.apply", boom)
        with pytest.raises(RuntimeError):
            get_kernel().ensure_loaded(str(ws))

    def test_user_plugin_cannot_claim_builtin_id(self, kernel_env):
        ws, _ = kernel_env
        write_plugin(ws, BUILTIN_TOOLS_ID, "def apply(ctx):\n    pass\n")
        get_kernel().ensure_loaded(str(ws))
        infos = [i for i in get_kernel().inventory() if i.id == BUILTIN_TOOLS_ID]
        assert len(infos) == 1
        assert infos[0].source == "base-bundle"  # 先见者赢，用户文件被跳
