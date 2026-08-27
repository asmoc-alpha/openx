"""加载器行为：阶段语义、失败隔离、禁用、内置优先、重载键。

运行：``python -m pytest tests/kernel/test_loader.py -q``
"""

from __future__ import annotations

import json

from openx.kernel import get_kernel
from openx.services.assembly import instantiate_tools

from ._helpers import (
    BAD_SRC,
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
        reg = instantiate_tools(k, None, include_builtin=False)
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
        assert instantiate_tools(k, None, include_builtin=False) == {}

    def test_disabled_via_settings(self, kernel_env):
        ws, settings = kernel_env
        write_plugin(ws, "hello", HELLO_SRC)
        settings.write_text(json.dumps({"plugins": {"disabled": ["hello"]}}))
        k = get_kernel()
        k.ensure_loaded(str(ws))
        info = next(i for i in k.inventory() if i.id == "hello")
        assert info.phase == "disabled"
        assert instantiate_tools(k, None, include_builtin=False) == {}

    def test_user_dir_follows_settings_path(self, kernel_env):
        ws, settings = kernel_env
        write_user_plugin(settings, "hello", HELLO_SRC)
        k = get_kernel()
        k.ensure_loaded(str(ws))  # 项目目录为空，用户目录命中
        user_ids = [
            i.id for i in k.inventory() if i.source != "base-bundle"
        ]
        assert user_ids == ["hello"]


class TestReload:
    def test_reload_when_key_changes(self, kernel_env):
        ws, settings = kernel_env
        write_plugin(ws, "hello", HELLO_SRC)
        k = get_kernel()
        k.ensure_loaded(str(ws))
        assert next(i for i in k.inventory() if i.id == "hello").phase == "active"
        settings.write_text(json.dumps({"plugins": {"disabled": ["hello"]}}))
        k.ensure_loaded(str(ws))  # 禁用表变 -> 重载
        assert next(i for i in k.inventory() if i.id == "hello").phase == "disabled"

    def test_no_user_plugins_only_builtin(self, kernel_env):
        ws, _ = kernel_env
        k = get_kernel()
        k.ensure_loaded(str(ws))
        # 无用户插件时仅剩 base bundle 内置插件（builtin-tools/builtin-providers）
        assert [i.source for i in k.inventory()] == ["base-bundle", "base-bundle"]
        assert len(k.registry("tools")) == 1       # core-tools 工厂
        # providers：openai-compat 恒在；anthropic 视 SDK 可选注册（M4）
        providers = k.registry("providers")
        assert providers.get("openai-compat") is not None
        try:
            import anthropic  # noqa: F401
        except ImportError:
            assert len(providers) == 1
        else:
            assert len(providers) == 2
        assert instantiate_tools(k, None, include_builtin=False) == {}

    def test_half_loaded_state_retries(self, kernel_env, monkeypatch):
        """B2 回归：中途致命异常不提交加载键，下次完整重试。"""
        ws, _ = kernel_env
        write_plugin(ws, "hello", HELLO_SRC)
        k = get_kernel()

        def boom(ctx):
            raise RuntimeError("builtin broken")

        monkeypatch.setattr("openx.builtin.tools.apply", boom)
        try:
            k.ensure_loaded(str(ws))
        except RuntimeError:
            pass
        assert k._load_key is None  # 键未提交
        monkeypatch.undo()
        k.ensure_loaded(str(ws))  # 同一 key 重试成功
        assert next(i for i in k.inventory() if i.id == "hello").phase == "active"
