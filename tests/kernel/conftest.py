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
    # S1a：测试工作区默认**已信任**（真实 trusted_dirs 路径；S1b admission
    # 默认关——专属测试 test_load_admission.py 走真实子进程）。既有测试
    # 改 settings 必须经 _helpers.merge_settings（合并而非覆盖），否则会把
    # 信任抹掉、项目插件被信任门跳过。
    config_mod.OpenXConfig.add_trusted_dir(str(ws))
    monkeypatch.setenv("OPENX_PLUGIN_ADMISSION", "0")
    yield ws, settings
    reset_kernel()
