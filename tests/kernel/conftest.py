"""kernel 测试环境：隔离 settings（用户插件目录随之隔离）+ 新鲜内核。"""

from __future__ import annotations

import pytest

import openx.config as config_mod
import openx.kernel.global_ledger as global_ledger_mod
from openx.kernel import reset_kernel


@pytest.fixture
def kernel_env(tmp_path, monkeypatch):
    """(workspace, settings_path)；SETTINGS_PATH 指向 tmp，kernel 全新。"""
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(config_mod, "SETTINGS_PATH", settings)
    # 全局账本（K5）落点也隔离：决策事件绝不在测试中写真实 ~/.openx。
    monkeypatch.setattr(
        global_ledger_mod, "GLOBAL_LEDGER_PATH", tmp_path / "ledger.jsonl"
    )
    reset_kernel()
    ws = tmp_path / "ws"
    (ws / ".openx" / "plugins").mkdir(parents=True)
    yield ws, settings
    reset_kernel()
