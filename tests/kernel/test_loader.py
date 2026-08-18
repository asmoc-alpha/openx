"""加载器行为：阶段语义、失败隔离、禁用、内置优先、重载键。

运行：``python -m pytest tests/kernel/test_loader.py -q``
"""

from __future__ import annotations

import json

from openx.kernel import get_kernel

from ._helpers import (
    BAD_SRC,
    CONFLICT_TOOL_SRC,
    HELLO_SRC,
    NOVALID_SRC,
    write_plugin,
    write_user_plugin,
)


class TestLoadPhases:
    def test_active_with_contributions(self, kernel_env):
        ws, _ = kernel_env
        write_plugin(ws, "hello", HELLO_SRC)
        k = get_kernel()
        k.ensure_loaded(str(ws))
        info = next(i for i in k.inventory() if i.id == "hello")
        assert info.phase == "active"
        assert info.tools == ["hello"] and info.commands == ["hi"]
        reg = {}
        k.merge_tools(reg)
        assert "hello" in reg

    def test_apply_failure_isolated(self, kernel_env):
        ws, _ = kernel_env
        write_plugin(ws, "bad", BAD_SRC)
        write_plugin(ws, "hello", HELLO_SRC)
        k = get_kernel()
        k.ensure_loaded(str(ws))
        by = {i.id: i for i in k.inventory()}
        assert by["bad"].phase == "failed" and "boom" in by["bad"].error
        assert by["hello"].phase == "active"  # 坏插件不连坐

    def test_shape_rejection_warns_not_crashes(self, kernel_env):
        ws, _ = kernel_env
        write_plugin(ws, "novalid", NOVALID_SRC)
        k = get_kernel()
        k.ensure_loaded(str(ws))
        info = next(i for i in k.inventory() if i.id == "novalid")
        assert info.phase == "active"  # 插件活着，贡献被拒
        assert any("permission" in w for w in info.warnings)
        reg = {}
        k.merge_tools(reg)
        assert reg == {}

    def test_disabled_via_settings(self, kernel_env):
        ws, settings = kernel_env
        write_plugin(ws, "hello", HELLO_SRC)
        settings.write_text(json.dumps({"plugins": {"disabled": ["hello"]}}))
        k = get_kernel()
        k.ensure_loaded(str(ws))
        info = next(i for i in k.inventory() if i.id == "hello")
        assert info.phase == "disabled"
        reg = {}
        k.merge_tools(reg)
        assert reg == {}

    def test_user_dir_follows_settings_path(self, kernel_env):
        ws, settings = kernel_env
        write_user_plugin(settings, "hello", HELLO_SRC)
        k = get_kernel()
        k.ensure_loaded(str(ws))  # 项目目录为空，用户目录命中
        assert [i.id for i in k.inventory()] == ["hello"]


class TestMergeAndReload:
    def test_builtin_priority_on_merge(self, kernel_env):
        ws, _ = kernel_env
        write_plugin(ws, "impostor", CONFLICT_TOOL_SRC)
        k = get_kernel()
        k.ensure_loaded(str(ws))
        sentinel = object()
        reg = {"grep": sentinel}
        k.merge_tools(reg)
        assert reg["grep"] is sentinel  # 内置不被覆盖
        info = next(i for i in k.inventory() if i.id == "impostor")
        assert any("builtin wins" in w for w in info.warnings)

    def test_reload_when_key_changes(self, kernel_env):
        ws, settings = kernel_env
        write_plugin(ws, "hello", HELLO_SRC)
        k = get_kernel()
        k.ensure_loaded(str(ws))
        assert next(i for i in k.inventory() if i.id == "hello").phase == "active"
        settings.write_text(json.dumps({"plugins": {"disabled": ["hello"]}}))
        k.ensure_loaded(str(ws))  # 禁用表变 → 重载
        assert next(i for i in k.inventory() if i.id == "hello").phase == "disabled"

    def test_no_plugins_no_state(self, kernel_env):
        ws, _ = kernel_env
        k = get_kernel()
        k.ensure_loaded(str(ws))
        assert k.inventory() == []
        reg = {}
        k.merge_tools(reg)
        assert reg == {}
