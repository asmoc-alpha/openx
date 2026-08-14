"""Phase 6 会话持久化回归测试。

覆盖：create/append/load 往返 / meta_update 前向合并 / workspace_hash
确定性 / list_for_workspace 倒序与工作区隔离 / 图片 base64 绝不落盘 /
孤立 tool 消息与损坏行清洗 / resolve_latest 与 resolve_by_id /
agent 集成（真实 SessionStore 落盘 + load_session 恢复）/ CLI 参数解析 /
hooks payload tool_input 截断（ride-along）。

SESSIONS_DIR 与 hooks SETTINGS_PATH 均 monkeypatch 到 tmp_path，
绝不触碰真实 ~/.openx。

运行：``python -m pytest tests/test_sessions.py -q``
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openx.config import OpenXConfig
from openx.core.sessions import (
    IMAGE_PLACEHOLDER_TEXT,
    SessionMeta,
    SessionStore,
    resolve_by_id,
    resolve_latest,
)


# ── fixtures ────────────────────────────────────────────────────


@pytest.fixture
def sessions_tmp(tmp_path, monkeypatch):
    """隔离会话目录与全局 settings.json（agent 构造会读后者）。"""
    monkeypatch.setattr(
        "openx.core.sessions.SESSIONS_DIR", tmp_path / "sessions"
    )
    monkeypatch.setattr(
        "openx.core.hooks.SETTINGS_PATH", tmp_path / "no-such-settings.json"
    )
    return tmp_path / "sessions"


def _write_meta_line(
    sessions_dir: Path, ws_hash: str, session_id: str, updated_at: str,
    workspace: str = "/ws", model: str = "m",
) -> Path:
    """手写一个只含 meta 行的会话文件（受控时间戳，排序测试用）。"""
    directory = sessions_dir / ws_hash
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text(json.dumps({
        "type": "meta", "version": 1, "session_id": session_id,
        "workspace": workspace, "model": model,
        "created_at": "2026-07-01T00:00:00+00:00", "updated_at": updated_at,
    }) + "\n")
    return path


def _make_agent(tmp_path, responses, session_store=None, session_id=None):
    """构造挂载 FakeLLM 的 OpenXAgent（绕过真实 API 与 settings.json）。"""
    from openx.agent import OpenXAgent
    from openx.permissions import PermissionRules
    from .test_bugfixes import FakeLLM

    config = OpenXConfig()
    config.workspace = str(tmp_path)
    config.api_key = "sk-test"
    config.api_base = "https://example.com/v1"
    config.model = "test-model"
    agent = OpenXAgent(
        config, session_store=session_store, session_id=session_id,
    )
    agent.llm = FakeLLM(responses)
    agent.tool_executor._rules = PermissionRules()  # 忽略真实 settings.json
    return agent


# ── 1. 往返 ─────────────────────────────────────────────────────


class TestRoundtrip:
    """create → append → update_meta → load：消息与 meta 字段无损。"""

    def test_create_append_load_roundtrip(self, sessions_tmp):
        store = SessionStore.create("/tmp/ws-a", "gpt-4o", session_id="abc123")
        assert store.path.is_file()

        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "user", "content": "bye"},
        ]
        store.append_messages(msgs)
        store.update_meta(total_input_tokens=42, total_output_tokens=7)

        meta, loaded = SessionStore.load(store.path)
        assert loaded == msgs
        assert meta.session_id == "abc123"
        assert meta.workspace == str(Path("/tmp/ws-a").resolve())
        assert meta.model == "gpt-4o"
        assert meta.total_input_tokens == 42
        assert meta.total_output_tokens == 7
        assert meta.created_at and meta.updated_at >= meta.created_at
        assert meta.path == store.path


# ── 2. meta_update 前向合并 ─────────────────────────────────────


class TestMetaUpdateMerge:
    """多条 meta_update 顺序回放：后写覆盖先写。"""

    def test_forward_merge_tokens_todos_updated_at(self, sessions_tmp):
        store = SessionStore.create("/ws", "m", session_id="s1")
        store.update_meta(
            total_input_tokens=10, total_output_tokens=1,
            todos=[{"content": "a", "activeForm": "a", "status": "pending"}],
        )
        store.update_meta(
            total_input_tokens=25, total_output_tokens=3,
            todos=[{"content": "a", "activeForm": "a", "status": "completed"}],
        )

        meta, _ = SessionStore.load(store.path)
        assert meta.total_input_tokens == 25
        assert meta.total_output_tokens == 3
        assert meta.todos == [
            {"content": "a", "activeForm": "a", "status": "completed"}
        ]
        # updated_at 取第二条 meta_update
        lines = [json.loads(x) for x in store.path.read_text().splitlines()]
        updates = [x for x in lines if x.get("type") == "meta_update"]
        assert len(updates) == 2
        assert meta.updated_at == updates[-1]["updated_at"]
        # meta 行本身未被重写（append-only）
        assert lines[0]["type"] == "meta"
        assert "total_input_tokens" not in lines[0]


# ── 3. workspace_hash ───────────────────────────────────────────


class TestWorkspaceHash:
    """确定性：同路径恒等、异路径不同、16 位十六进制。"""

    def test_deterministic_and_distinct(self):
        a1 = SessionStore.workspace_hash("/some/ws")
        a2 = SessionStore.workspace_hash("/some/ws")
        b = SessionStore.workspace_hash("/other/ws")
        assert a1 == a2
        assert a1 != b
        assert len(a1) == 16
        int(a1, 16)  # 合法十六进制


# ── 4. 列表排序与工作区隔离 ─────────────────────────────────────


class TestListForWorkspace:
    """按 updated_at 倒序；其他工作区的会话不可见。"""

    def test_ordering_newest_first_and_workspace_isolation(
        self, sessions_tmp
    ):
        ws = "/ws/listing"
        ws_hash = SessionStore.workspace_hash(ws)
        _write_meta_line(sessions_tmp, ws_hash, "s_old", "2026-07-01T00:00:00+00:00")
        _write_meta_line(sessions_tmp, ws_hash, "s_mid", "2026-07-02T00:00:00+00:00")
        _write_meta_line(sessions_tmp, ws_hash, "s_new", "2026-07-03T00:00:00+00:00")
        # 其他工作区的会话 —— 必须被排除
        other_hash = SessionStore.workspace_hash("/other/ws")
        _write_meta_line(sessions_tmp, other_hash, "s_other", "2026-07-04T00:00:00+00:00")

        metas = SessionStore.list_for_workspace(ws)
        assert [m.session_id for m in metas] == ["s_new", "s_mid", "s_old"]
        assert all(m.path is not None for m in metas)

    def test_empty_when_no_directory(self, sessions_tmp):
        assert SessionStore.list_for_workspace("/never/created") == []


# ── 5. 图片省略（base64 绝不落盘）────────────────────────────────


class TestImageElision:
    """image_url part 写盘前替换为占位文本；原消息不被改动。"""

    def test_image_parts_never_hit_disk(self, sessions_tmp):
        store = SessionStore.create("/ws", "m")
        b64 = "A" * 800
        content = [
            {"type": "text", "text": "look at this"},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]
        original = {"role": "user", "content": content}
        store.append_messages([original])

        raw = store.path.read_bytes()
        assert b"[image omitted" in raw
        assert b64.encode() not in raw

        # 原消息（agent 历史里的对象）绝不被清洗改动
        assert content[1]["type"] == "image_url"

        _, loaded = SessionStore.load(store.path)
        assert loaded[0]["content"][0] == {"type": "text", "text": "look at this"}
        assert loaded[0]["content"][1] == {
            "type": "text", "text": IMAGE_PLACEHOLDER_TEXT,
        }


# ── 6. 孤立 tool 消息与损坏行 ───────────────────────────────────


class TestLoadSanitization:
    """孤立 tool 消息被丢弃（validate 语义）；损坏行跳过；永不抛异常。"""

    def test_orphan_tool_message_dropped_and_corrupt_line_skipped(
        self, sessions_tmp, capsys
    ):
        ws_hash = SessionStore.workspace_hash("/ws")
        path = sessions_tmp / ws_hash / "sani.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps({"type": "meta", "version": 1, "session_id": "sani",
                        "workspace": "/ws", "model": "m",
                        "created_at": "2026-07-01T00:00:00+00:00",
                        "updated_at": "2026-07-01T00:00:00+00:00"}),
            json.dumps({"type": "message", "ts": "t",
                        "message": {"role": "user", "content": "hi"}}),
            json.dumps({"type": "message", "ts": "t",
                        "message": {"role": "assistant", "content": None,
                                    "tool_calls": [{
                                        "id": "tc1",
                                        "function": {"name": "shell",
                                                     "arguments": "{}"}}]}}),
            json.dumps({"type": "message", "ts": "t",
                        "message": {"role": "tool", "tool_call_id": "tc1",
                                    "content": "ok"}}),
            # 孤立 tool 消息：无匹配 tc999 的 assistant tool_call
            json.dumps({"type": "message", "ts": "t",
                        "message": {"role": "tool", "tool_call_id": "tc999",
                                    "content": "orphan"}}),
            "this line is not json {{{",  # 损坏行：跳过并告警
        ]
        path.write_text("\n".join(lines) + "\n")

        meta, messages = SessionStore.load(path)  # 绝不抛异常
        assert meta.session_id == "sani"
        assert [m["role"] for m in messages] == ["user", "assistant", "tool"]
        assert messages[2]["tool_call_id"] == "tc1"
        assert all(m.get("tool_call_id") != "tc999" for m in messages)

        out = capsys.readouterr().out
        assert "orphan tool message" in out
        assert "corrupt session line" in out


# ── 7. resolvers ────────────────────────────────────────────────


class TestResolvers:
    """resolve_latest 取最新；resolve_by_id 精确命中；未命中 → None。"""

    def test_hit_and_miss(self, sessions_tmp):
        ws = "/ws/resolvers"
        store1 = SessionStore.create(ws, "m", session_id="sess1")
        store2 = SessionStore.create(ws, "m", session_id="sess2")
        # 明确拔高 sess2 的 updated_at，保证排序确定性
        store2.update_meta(first_user_message="second session")
        del store1

        latest = resolve_latest(ws)
        assert latest is not None and latest.session_id == "sess2"
        assert latest.first_user_message == "second session"

        hit = resolve_by_id(ws, "sess1")
        assert hit is not None and hit.session_id == "sess1"

        assert resolve_latest("/no/such/ws") is None
        assert resolve_by_id(ws, "no-such-id") is None


# ── 8. agent 集成 ───────────────────────────────────────────────


class TestAgentIntegration:
    """真实 SessionStore 落盘；load_session 恢复历史/tokens/todos/id。"""

    @pytest.mark.asyncio
    async def test_run_persists_and_load_session_restores(
        self, sessions_tmp, tmp_path
    ):
        ws = str(tmp_path)
        store = SessionStore.create(ws, "test-model", session_id="agent-sess")
        agent = _make_agent(
            tmp_path, [("done here", None)],
            session_store=store, session_id="agent-sess",
        )
        agent.todos[:] = [
            {"content": "task", "activeForm": "tasking", "status": "in_progress"}
        ]

        out = await agent.run("hello world")
        assert out == "done here"

        # JSONL 存在且含 meta + message 行
        raw_lines = [json.loads(x) for x in store.path.read_text().splitlines()]
        kinds = [x.get("type") for x in raw_lines]
        assert kinds[0] == "meta"
        assert kinds.count("message") == 2  # user + assistant

        meta, messages = SessionStore.load(store.path)
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[0]["content"] == "hello world"
        assert messages[1]["content"] == "done here"
        assert meta.first_user_message == "hello world"
        assert meta.total_output_tokens == agent.total_output_tokens
        assert meta.total_input_tokens == agent.total_input_tokens
        assert meta.todos == agent.todos

        # 第二个 agent 从文件恢复
        agent2 = _make_agent(tmp_path, [])
        assert agent2.session_id != "agent-sess"
        agent2.load_session(meta, messages)
        assert [m["role"] for m in agent2.history.messages] == [
            "user", "assistant",
        ]
        assert agent2.history.messages[0]["content"] == "hello world"
        assert agent2.history.validate()
        assert agent2.session_id == "agent-sess"
        assert agent2.hooks.session_id == "agent-sess"
        assert agent2.total_output_tokens == agent.total_output_tokens
        # todos 原地恢复：TodoWriteTool 共享的仍是同一 list 对象
        assert agent2.todos == [
            {"content": "task", "activeForm": "tasking", "status": "in_progress"}
        ]
        assert agent2.tools["todo_write"]._store is agent2.todos

    @pytest.mark.asyncio
    async def test_stream_run_persists(self, sessions_tmp, tmp_path):
        ws = str(tmp_path)
        store = SessionStore.create(ws, "test-model")
        # 与 main.py 一致：agent 的 session_id 取自 store.meta
        agent = _make_agent(
            tmp_path, [("streamed answer", None)],
            session_store=store, session_id=store.meta.session_id,
        )
        chunks = [c async for c in agent.stream_run("stream me")]
        assert "".join(chunks).startswith("streamed")

        meta, messages = SessionStore.load(store.path)
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[0]["content"] == "stream me"
        assert meta.first_user_message == "stream me"
        assert meta.session_id == agent.session_id

    @pytest.mark.asyncio
    async def test_no_store_is_a_noop(self, sessions_tmp, tmp_path):
        """session_store=None（默认）→ 不落盘、不报错。"""
        agent = _make_agent(tmp_path, [("fine", None)])
        assert agent.session_store is None
        out = await agent.run("nothing persisted")
        assert out == "fine"
        assert not sessions_tmp.exists() or not any(sessions_tmp.rglob("*.jsonl"))


# ── 9. CLI 参数解析 ─────────────────────────────────────────────


class TestArgParsing:
    """--continue / --resume / --resume <id> 的 dest 值。"""

    def test_session_flags(self):
        from openx.main import _PICK_SENTINEL, parse_args

        a = parse_args(["--continue"])
        assert a.continue_session is True and a.resume is None

        b = parse_args(["--resume"])
        assert b.resume == _PICK_SENTINEL

        c = parse_args(["--resume", "abc123"])
        assert c.resume == "abc123"

        d = parse_args([])
        assert d.continue_session is False and d.resume is None


# ── 10. ride-along：hooks payload tool_input 截断 ───────────────


class TestToolInputTruncation:
    """超长 tool_input 字符串值截断并加标记；小值与非字符串值不变。"""

    def test_posttooluse_truncates_tool_input_strings(self):
        from openx.core.hooks import TOOL_INPUT_LIMIT, build_posttooluse_payload

        big = "x" * (TOOL_INPUT_LIMIT + 500)
        payload = build_posttooluse_payload(
            "write_file",
            {"file_path": "a.py", "content": big, "lines": 42},
            "ok",
        )
        truncated = payload["tool_input"]["content"]
        assert len(truncated) == TOOL_INPUT_LIMIT + len("...[truncated]")
        assert truncated.endswith("...[truncated]")
        assert payload["tool_input"]["file_path"] == "a.py"  # 小值不变
        assert payload["tool_input"]["lines"] == 42  # 非字符串值不变

    def test_pretooluse_truncates_tool_input_strings(self):
        from openx.core.hooks import TOOL_INPUT_LIMIT, build_pretooluse_payload

        big = "z" * (TOOL_INPUT_LIMIT + 10)
        payload = build_pretooluse_payload("write_file", {"content": big})
        assert payload["tool_input"]["content"].endswith("...[truncated]")
