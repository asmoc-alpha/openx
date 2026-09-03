"""coding-memory 项目级数据收敛到 home 的回归测试。

锁定：agent/项目记忆不再在 workspace 建 `.openx`；项目级 memories.jsonl
落在 home 的 ``PROJECTS_ROOT/<workspace_hash>/``；旧
``<workspace>/.openx/coding-memory`` 一次性迁移到新位置且不删旧文件。

conftest.py 已把 PROJECTS_ROOT / GLOBAL_MEMORY_DIR 指向测试 tmp，测试内
直接读取被 monkeypatch 后的模块常量即可定位。

运行：``python -m pytest tests/test_coding_memory_home.py -q``
"""

from __future__ import annotations

import json
from pathlib import Path

import openx.coding_memory as cm
from openx.coding_memory import CodingMemoryStore


class TestNoWorkspaceScatter:
    def test_remember_does_not_create_workspace_openx(self, tmp_path):
        ws = tmp_path / "proj"
        ws.mkdir()
        store = CodingMemoryStore(workspace=str(ws))
        store.remember(
            "本项目用 pytest 跑测试", category="project_fact",
            keywords=["pytest"], scope="project",
        )
        # 项目目录里绝无 .openx
        assert not (ws / ".openx").exists()
        # 数据落在 home projects/<hash>/
        target = cm.PROJECTS_ROOT / cm._project_hash(ws) / cm._MEMORY_FILE
        assert target.is_file()

    def test_project_isolated_from_other_workspace(self, tmp_path):
        ws1 = tmp_path / "a"
        ws2 = tmp_path / "b"
        ws1.mkdir()
        ws2.mkdir()
        CodingMemoryStore(workspace=str(ws1)).remember("facts about A", scope="project")
        CodingMemoryStore(workspace=str(ws2)).remember("facts about B", scope="project")

        hits1 = CodingMemoryStore(workspace=str(ws1)).list_all(scope="project")
        assert [m.content for m in hits1] == ["facts about A"]
        hits2 = CodingMemoryStore(workspace=str(ws2)).list_all(scope="project")
        assert [m.content for m in hits2] == ["facts about B"]

    def test_recall_and_dedup_smoke(self, tmp_path):
        ws = tmp_path / "proj"
        ws.mkdir()
        store = CodingMemoryStore(workspace=str(ws))
        store.remember("约定用 camelCase", category="code_convention",
                       keywords=["naming"], scope="project")
        # 去重：同内容不重复
        store.remember("约定用 camelCase", category="code_convention",
                       keywords=["naming"], scope="project")
        assert len(store.list_all(scope="project")) == 1
        found = store.recall(query="naming", limit=5)
        assert any("camelCase" in m.content for _, m in found)


class TestLegacyMigration:
    def test_legacy_project_file_migrated_once(self, tmp_path):
        ws = tmp_path / "proj"
        legacy = ws / ".openx" / "coding-memory" / cm._MEMORY_FILE
        legacy.parent.mkdir(parents=True)  # 模拟旧版本遗留
        line = json.dumps({
            "id": "legacy1", "category": "project_fact", "content": "old fact",
            "keywords": [], "related_paths": [], "scope": "project",
        }, ensure_ascii=False)
        legacy.write_text(line + "\n", encoding="utf-8")

        store = CodingMemoryStore(workspace=str(ws))

        # 旧内容已 copy 到新 home 位置
        target = cm.PROJECTS_ROOT / cm._project_hash(ws) / cm._MEMORY_FILE
        assert target.is_file()
        assert "old fact" in target.read_text(encoding="utf-8")
        # 旧文件保留作备份、不删除
        assert legacy.is_file()
        # 迁移后项目记忆可召回
        assert any(m.content == "old fact" for m in store.list_all(scope="project"))

    def test_no_legacy_no_migration(self, tmp_path):
        ws = tmp_path / "fresh"
        ws.mkdir()
        CodingMemoryStore(workspace=str(ws))
        # 无旧文件 → 只是干净的新 home 落点，workspace 无 .openx
        assert not (ws / ".openx").exists()
        assert (cm.PROJECTS_ROOT / cm._project_hash(ws)).is_dir()


def test_global_memory_still_under_home_tmp(tmp_path):
    # 全局记忆不受项目迁移影响（仍 GLOBAL_MEMORY_DIR/memories.jsonl）
    store = CodingMemoryStore(workspace=str(tmp_path / "w"))
    store.remember("跨项目规范", category="workflow", scope="global")
    assert (cm.GLOBAL_MEMORY_DIR / cm._MEMORY_FILE).is_file()
