"""kernel 测试环境：隔离 settings（用户插件目录随之隔离）+ 新鲜内核。"""

from __future__ import annotations

import pytest

import openx.config as config_mod
from openx.kernel import reset_kernel


@pytest.fixture
def kernel_env(tmp_path, monkeypatch):
    """(workspace, settings_path)；SETTINGS_PATH 指向 tmp，kernel 全新。"""
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(config_mod, "SETTINGS_PATH", settings)
    reset_kernel()
    ws = tmp_path / "ws"
    (ws / ".openx" / "plugins").mkdir(parents=True)
    yield ws, settings
    reset_kernel()
