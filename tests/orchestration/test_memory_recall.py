"""E6 召回回账 · ``memory_recall`` 事件（agent → 会话账本）。

覆盖：
- 构建系统提示并入了 coding memory 时，账本记一条 ``memory_recall``（携带被召回
  的记忆 id / 分类 / 字符数）；
- **同提示版本去重**：召回 id 序列不变时不再重复记；
- **子代理守卫**：无 ``session_store`` 的 agent 不 emit（共享内核，否则串写父账本）。

conftest 的 autouse fixture 已把 coding memory 指向 tmp；SESSIONS_DIR 与 hooks
SETTINGS_PATH 亦 monkeypatch 到 tmp。运行：``python -m pytest
tests/orchestration/test_memory_recall.py -q``
"""

from __future__ import annotations

import pytest

from openx.config import OpenXConfig


@pytest.fixture
def sessions_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "openx.orchestration.sessions.SESSIONS_DIR", tmp_path / "sessions"
    )
    monkeypatch.setattr(
        "openx.kernel.audit.hooks.SETTINGS_PATH", tmp_path / "no-such-settings.json"
    )
    return tmp_path / "sessions"


def _make_agent(tmp_path, store=None, sid=None):
    from openx.agent import OpenXAgent

    config = OpenXConfig()
    config.workspace = str(tmp_path)
    config.model = "test-model"
    return OpenXAgent(config, session_store=store, session_id=sid)


def _recalls(store) -> list[dict]:
    from openx.orchestration.sessions import SessionStore

    store.flush()  # 账本事件在首条消息前先缓冲；读盘前先刷出
    return [e for e in SessionStore.iter_events(store.path)
            if e.get("type") == "memory_recall"]


def test_build_prompt_emits_memory_recall(tmp_path, sessions_tmp):
    from openx.orchestration.sessions import SessionStore

    ws = str(tmp_path)
    store = SessionStore.create(ws, "test-model", session_id="mr-1")
    agent = _make_agent(tmp_path, store=store, sid="mr-1")
    agent.coding_memory.remember("use pytest not unittest", category="code_convention",
                                 keywords=["pytest"], related_paths=["tests/**"])

    agent._build_system_prompt()

    recalls = _recalls(store)
    assert len(recalls) == 1
    r = recalls[0]
    assert len(r["ids"]) == 1 and len(r["ids"][0]) == 12  # memory id = 12-hex
    assert r["categories"] == ["code_convention"]
    assert r["chars"] > 0


def test_same_prompt_version_not_rerecorded(tmp_path, sessions_tmp):
    from openx.orchestration.sessions import SessionStore

    ws = str(tmp_path)
    store = SessionStore.create(ws, "test-model", session_id="mr-2")
    agent = _make_agent(tmp_path, store=store, sid="mr-2")
    agent.coding_memory.remember("fact one", category="project_fact")

    agent._build_system_prompt()
    agent._build_system_prompt()   # 同一召回集 → 去重，不新增

    assert len(_recalls(store)) == 1


def test_new_memory_triggers_new_recall(tmp_path, sessions_tmp):
    from openx.orchestration.sessions import SessionStore

    ws = str(tmp_path)
    store = SessionStore.create(ws, "test-model", session_id="mr-3")
    agent = _make_agent(tmp_path, store=store, sid="mr-3")
    agent.coding_memory.remember("fact one", category="project_fact")
    agent._build_system_prompt()

    agent.coding_memory.remember("fact two", category="project_fact")
    agent._build_system_prompt()   # 召回集变化 → 再记一条

    assert len(_recalls(store)) == 2


def test_no_memories_no_recall_event(tmp_path, sessions_tmp):
    from openx.orchestration.sessions import SessionStore

    store = SessionStore.create(str(tmp_path), "test-model", session_id="mr-4")
    agent = _make_agent(tmp_path, store=store, sid="mr-4")
    agent._build_system_prompt()   # 无记忆 → 无片段 → 无事件

    assert _recalls(store) == []


@pytest.mark.asyncio
async def test_subagent_guard_no_emit(tmp_path, monkeypatch, sessions_tmp):
    """session_store=None（子代理语义）时不 emit memory_recall。"""
    from openx.kernel import get_kernel, reset_kernel

    reset_kernel()
    seen: list = []
    get_kernel().attach_ledger(lambda e: seen.append(e.type), session="parent")

    agent = _make_agent(tmp_path)  # store=None
    agent.coding_memory.remember("child fact", category="project_fact")
    agent._build_system_prompt()

    assert "memory_recall" not in seen
    reset_kernel()
