"""顶层 pytest fixtures（autouse）：隔离 openx 运行时数据，绝不污染真实 ~/.openx。

agent 构造会创建 CodingMemoryStore（现在项目级记忆默认落 home 的
``PROJECTS_ROOT``/``GLOBAL_MEMORY_DIR``）。测试用真实模块常量构造，
因此这里把它们指向每个测试自己的 tmp 目录。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _hermetic_coding_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "openx.coding_memory.PROJECTS_ROOT", tmp_path / "cm-projects"
    )
    monkeypatch.setattr(
        "openx.coding_memory.GLOBAL_MEMORY_DIR", tmp_path / "cm-global"
    )
