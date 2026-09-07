"""Web 端到端：aiohttp TestServer 上验证路由 + WS 往返 + 权限 + 复盘。

hermetic：SESSIONS_DIR monkeypatch 到 tmp、假 agent、零 LLM / 零真实内核。
TestServer 与测试同进程同事件循环（pytest-asyncio auto）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from openx.agent import ToolResultEvent, ToolStartEvent
from openx.app.serve.api import WORKSPACE_KEY as API_WORKSPACE_KEY
from openx.app.serve.bridge import ServeConsole
from openx.app.serve.server import SESSION_KEY, WORKSPACE_KEY, create_app
from openx.app.serve.session import ServeSession
from openx.kernel.protocol import Event
from openx.orchestration.sessions import SessionStore


class FakeHistory:
    """带 clear() 的消息盒——切区后清空历史、attach 快照随之变空。"""

    def __init__(self):
        self.messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello!"},
        ]

    def clear(self):
        self.messages = []


class FakeAgent:
    """鸭子实现真实 OpenXAgent 在 serve 路径上被触碰的面（每实例状态）。

    切区 handler 无分支地调用 ``_build_tools`` / ``_compute_tool_schemas`` /
    ``reload_instructions`` / ``clear_history``——真实 agent 全具备，fake 只须
    补齐最小面；每实例 ``__init__`` 避免跨测试共享类级可变状态。
    """

    class _Cfg:
        def __init__(self):
            self.model = "fake-model"
            self.active_group = "fake-group"
            self.workspace = ""

    class _Hooks:
        def __init__(self):
            self.session_id = "sess-live"

    def __init__(self):
        self.session_id = "sess-live"
        self.config = self._Cfg()
        self.hooks = self._Hooks()
        self.history = FakeHistory()
        self.todos = []
        self.session_store = None
        self.workspace = None
        self.total_input_tokens = 5
        self.total_output_tokens = 2
        self.total_cached_tokens = 0
        self.total_plugin_tokens = 0
        self.last_tool_rounds = 1
        self.tools = {"read_file": 1, "write_file": 1}

    def clear_history(self):
        self.history.clear()

    def _build_tools(self):
        return dict(self.tools)

    def _compute_tool_schemas(self):
        return []

    def reload_instructions(self):
        return None

    async def startup(self):
        pass

    async def shutdown(self):
        pass

    async def stream_run(self, text):
        yield "rep"
        yield "ly"
        yield ToolStartEvent(name="read_file", arguments="{}")
        yield ToolResultEvent(name="read_file", output="file contents", is_error=False)


@pytest.fixture
def agent():
    return FakeAgent()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """真实会话文件：monkeypatch SESSIONS_DIR + 预置一条历史会话供复盘。"""
    import openx.orchestration.sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "SESSIONS_DIR", tmp_path / "sessions")
    ws = tmp_path / "ws"
    store = SessionStore.create(str(ws), "test-model", session_id="sess-old")
    store.append_messages([
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ])
    # 镜像 agent._persist_turn：首条用户消息回填 meta.first_user_message
    store.update_meta(first_user_message="old question")
    store.append_event(Event(
        seq=1, ts=1.0, session="sess-old", type="permission_decision",
        payload={"type": "permission_decision", "tool": "shell",
                 "approved": True, "verdict": "ALLOW"},
        origin="kernel", digest="d1",
    ))
    return str(ws)


@pytest.fixture
async def server(agent, workspace):
    """TestServer + TestClient（yield client；teardown 停 session）。"""
    console = ServeConsole()
    session = ServeSession(agent, console)
    session.start()
    app = create_app(session, workspace=workspace)
    async with TestClient(TestServer(app)) as client:
        yield client
    session.stop()


# ── HTTP 路由 ───────────────────────────────────────────────────


async def test_index_serves_frontend(server):
    resp = await server.get("/")
    assert resp.status == 200
    body = await resp.text()
    # P4.5 重构：品牌保留 OpenX，但页面标题改为「OpenX · 智能助手工作台」
    assert "OpenX" in body
    assert 'src="/static/app.js"' in body


async def test_static_assets(server):
    for path in ("/static/app.js", "/static/style.css"):
        resp = await server.get(path)
        assert resp.status == 200, path


async def test_sessions_list(server, workspace):
    resp = await server.get("/api/sessions")
    assert resp.status == 200
    data = await resp.json()
    assert data[0]["session_id"] == "sess-old"
    assert data[0]["model"] == "test-model"
    assert data[0]["first_user_message"] == "old question"


async def test_session_events_replay(server):
    resp = await server.get("/api/sessions/sess-old/events")
    assert resp.status == 200
    data = await resp.json()
    types = [e["type"] for e in data["events"]]
    # 消息行 + 账本行投影同现，且按文件序
    assert "message" in types and "permission_decision" in types
    assert types.index("message") < types.index("permission_decision")
    perm = next(e for e in data["events"] if e["type"] == "permission_decision")
    assert perm["seq"] == 1 and perm["tool"] == "shell"


async def test_session_events_404(server):
    resp = await server.get("/api/sessions/no-such-id/events")
    assert resp.status == 404


# ── WebSocket 往返 ──────────────────────────────────────────────


async def test_ws_attach_and_message_roundtrip(server):
    ws = await server.ws_connect("/ws")
    init = await ws.receive_json(timeout=5)
    assert init["type"] == "system" and init["subtype"] == "init"
    hist = await ws.receive_json(timeout=5)
    assert hist["type"] == "history"

    await ws.send_json({"type": "message", "text": "hi"})
    events = []
    for _ in range(20):
        ev = await ws.receive_json(timeout=5)
        events.append(ev)
        if ev["type"] == "result":
            break
    types = [e["type"] for e in events]
    assert types[0] == "user_message"
    assert "text_delta" in types and "tool_use" in types and "tool_result" in types
    assert types[-1] == "result"
    await ws.close()


async def test_ws_permission_roundtrip(server):
    """真实权限流：经 WS 广播 permission_request，客户端应答后放行。"""
    ws = await server.ws_connect("/ws")
    await ws.receive_json(timeout=5)  # init
    await ws.receive_json(timeout=5)  # history

    # 经 session.bridge 发起一次请求（模拟 executor 的 ask_permission）
    async def _ask():
        return await server.app[SESSION_KEY].bridge.ask_permission(
            "shell", "run some command"
        )

    fut = asyncio.ensure_future(_ask())
    req = await ws.receive_json(timeout=5)
    assert req["type"] == "permission_request"
    assert req["tool"] == "shell"
    assert req["request_id"]

    await ws.send_json({
        "type": "permission_response",
        "request_id": req["request_id"],
        "allowed": True,
        "remember": False,
    })
    approved, _remember = await fut
    assert approved is True
    await ws.close()


async def test_ws_interrupt_uplink(server):
    ws = await server.ws_connect("/ws")
    await ws.receive_json(timeout=5)  # init
    await ws.receive_json(timeout=5)  # history
    # 无回合时 interrupt 安全 no-op，连接不炸
    await ws.send_json({"type": "interrupt"})
    await asyncio.sleep(0.05)
    await ws.close()


# ── P4.1 交互弹窗：ask_user / plan_request 经 WS 往返 ───────────


async def test_ws_ask_user_roundtrip(server):
    """ask_user 广播 → 客户端应答 → bridge 唤醒返回所选 label。"""
    ws = await server.ws_connect("/ws")
    await ws.receive_json(timeout=5)  # init
    await ws.receive_json(timeout=5)  # history

    async def _ask():
        return await server.app[SESSION_KEY].bridge.ask_user(
            "Pick a color", [{"label": "red"}, {"label": "blue"}]
        )

    fut = asyncio.ensure_future(_ask())
    req = await ws.receive_json(timeout=5)
    assert req["type"] == "ask_user"
    assert req["question"] == "Pick a color"
    assert req["request_id"]
    assert req["options"] == [
        {"label": "red", "description": ""},
        {"label": "blue", "description": ""},
    ]

    await ws.send_json({
        "type": "ask_user_response",
        "request_id": req["request_id"],
        "answers": ["blue"],
    })
    assert await fut == "blue"
    await ws.close()


async def test_ws_ask_user_empty_answers_conservative(server):
    """空答（前端 Skip）→ 立即落保守默认，不等超时。"""
    ws = await server.ws_connect("/ws")
    await ws.receive_json(timeout=5)
    await ws.receive_json(timeout=5)

    async def _ask():
        return await server.app[SESSION_KEY].bridge.ask_user(
            "Mode?", [{"label": "Auto"}, {"label": "Stay in manual"}]
        )

    fut = asyncio.ensure_future(_ask())
    req = await ws.receive_json(timeout=5)
    await ws.send_json({
        "type": "ask_user_response",
        "request_id": req["request_id"],
        "answers": [],
    })
    assert await fut == "Stay in manual"  # 保守默认：绝不切成 Auto
    await ws.close()


async def test_ws_plan_request_roundtrip(server):
    """plan_request 广播 → 客户端批准 → bridge 返回 True。"""
    ws = await server.ws_connect("/ws")
    await ws.receive_json(timeout=5)
    await ws.receive_json(timeout=5)

    async def _ask():
        return await server.app[SESSION_KEY].bridge.confirm_plan("# Plan")

    fut = asyncio.ensure_future(_ask())
    req = await ws.receive_json(timeout=5)
    assert req["type"] == "plan_request"
    assert req["plan"] == "# Plan"
    await ws.send_json({
        "type": "plan_response",
        "request_id": req["request_id"],
        "approved": True,
    })
    assert await fut is True
    await ws.close()


async def test_ws_plan_request_reject(server):
    ws = await server.ws_connect("/ws")
    await ws.receive_json(timeout=5)
    await ws.receive_json(timeout=5)

    async def _ask():
        return await server.app[SESSION_KEY].bridge.confirm_plan("nope")

    fut = asyncio.ensure_future(_ask())
    req = await ws.receive_json(timeout=5)
    await ws.send_json({
        "type": "plan_response",
        "request_id": req["request_id"],
        "approved": False,
    })
    assert await fut is False
    await ws.close()


# ── 工作区树 / 跨区回放 / 切区（P 侧栏按工作区分类会话）──────────


def _seed_second_workspace(
    workspace: str, session_id: str = "sess-other", title: str = "other question"
) -> Path:
    """在隔离的 SESSIONS_DIR 里为另一工作区预置一条会话；返回其解析路径。"""
    ws2 = (Path(workspace).parent / "ws2").resolve()
    ws2.mkdir(parents=True, exist_ok=True)
    store = SessionStore.create(str(ws2), "test-model", session_id=session_id)
    store.append_messages([
        {"role": "user", "content": title},
        {"role": "assistant", "content": "other answer"},
    ])
    store.update_meta(first_user_message=title)
    return ws2


class TestWorkspaceTree:
    async def test_workspaces_lists_groups_active_first(self, server, workspace):
        ws2 = _seed_second_workspace(workspace)
        resp = await server.get("/api/workspaces")
        assert resp.status == 200
        groups = await resp.json()
        by_path = {g["workspace"]: g for g in groups}
        assert len(groups) == 2
        active_path = str(Path(workspace).resolve())
        # 活动组置首
        assert groups[0]["active"] is True
        assert groups[0]["workspace"] == active_path
        # 其它工作区组
        other = by_path[str(ws2)]
        assert other["active"] is False
        item = other["sessions"][0]
        assert item["session_id"] == "sess-other"
        for key in ("title", "workspace", "model", "group", "created_at",
                    "updated_at", "first_user_message", "total_input_tokens"):
            assert key in item
        # 活动工作区仍只见 boot 会话（列表隔离不受影响）
        assert {s["session_id"] for s in groups[0]["sessions"]} == {"sess-old"}

    async def test_replay_across_workspaces(self, server, workspace):
        ws2 = _seed_second_workspace(workspace)
        resp = await server.get("/api/sessions/sess-other/events")
        assert resp.status == 200
        data = await resp.json()
        assert data["workspace"] == str(ws2)
        types = [e["type"] for e in data["events"]]
        assert types.count("message") == 2
        # 活动工作区列表隔离：仍只有 sess-old
        lst = await (await server.get("/api/sessions")).json()
        assert [s["session_id"] for s in lst] == ["sess-old"]

    async def test_switch_reroots_agent_and_app_keys(self, server, workspace):
        agent = server.app[SESSION_KEY].agent
        ws2 = _seed_second_workspace(workspace)
        old_id = agent.session_id
        resp = await server.post(
            "/api/workspace/switch", json={"workspace": str(ws2)}
        )
        assert resp.status == 200, await resp.text()
        body = (await resp.json())["data"]
        assert body["workspace"] == str(ws2)
        assert body["session_id"] and body["session_id"] != old_id
        # agent 与新 store 重绑；新会话空白 → 惰性落盘（不发言不建文件）
        assert agent.session_store is not None
        assert not agent.session_store.path.is_file()
        assert agent.session_id == body["session_id"]
        assert agent.hooks.session_id == body["session_id"]
        assert Path(agent.workspace).resolve() == ws2
        assert agent.todos == []
        # 两处 app-key 同步（key 持 WorkspaceRef 盒子，改的是 .path）
        assert server.app[WORKSPACE_KEY].path == str(ws2)
        assert server.app[API_WORKSPACE_KEY].path == str(ws2)
        # /api/sessions 落到新工作区：只有 sess-other——新建空会话不入列
        lst = await (await server.get("/api/sessions")).json()
        ids = {s["session_id"] for s in lst}
        assert "sess-other" in ids and body["session_id"] not in ids
        info = await (await server.get("/api/info")).json()
        assert info["data"]["workspace"] == str(ws2)

    async def test_switch_rejects_unknown_dirs(self, server, workspace):
        """不存在的目录仍 404；真实目录（即便尚无会话）现在允许切过去开工。"""
        agent = server.app[SESSION_KEY].agent
        active = str(Path(workspace).resolve())
        bogus = (Path(workspace).parent / "never-existed").resolve()
        resp = await server.post(
            "/api/workspace/switch", json={"workspace": str(bogus)}
        )
        assert resp.status == 404
        # 真实目录但无会话：显式选择 = 信任 → 200，就地新建首条会话
        empty_dir = (Path(workspace).parent / "empty-ws").resolve()
        empty_dir.mkdir(parents=True, exist_ok=True)
        old_id = agent.session_id
        resp = await server.post(
            "/api/workspace/switch", json={"workspace": str(empty_dir)}
        )
        assert resp.status == 200, await resp.text()
        body = (await resp.json())["data"]
        assert body["session_id"] and body["session_id"] != old_id
        # 已切过去：agent 与新目录重根、app-key 同步、info 落到新目录
        assert agent.session_id == body["session_id"]
        assert Path(agent.workspace).resolve() == empty_dir
        assert server.app[WORKSPACE_KEY].path == str(empty_dir)
        assert server.app[API_WORKSPACE_KEY].path == str(empty_dir)
        info = await (await server.get("/api/info")).json()
        assert info["data"]["workspace"] == str(empty_dir)

    async def test_dirs_lists_children(self, server, workspace):
        """GET /api/dirs：列直接子目录供目录浏览器导航；坏路径回落活动工作区。"""
        ws = Path(workspace).resolve()
        sub = ws / "subdir"
        sub.mkdir(parents=True, exist_ok=True)
        resp = await server.get(f"/api/dirs?path={sub}")
        assert resp.status == 200
        body = (await resp.json())["data"]
        assert body["path"] == str(sub)
        assert body["parent"] == str(ws)
        # 不存在路径 → 回落活动工作区（不会 404）
        resp2 = await server.get("/api/dirs?path=/definitely/not/a/real/dir/xyz")
        assert resp2.status == 200
        assert (await resp2.json())["data"]["path"] == str(ws)

    # ── 会话删除（侧栏会话项的 ✕ 按钮 → DELETE /api/sessions/{sid}）──

    async def test_delete_historical_session(self, server, workspace):
        """删历史会话：文件移除、跨工作区定位、侧栏数据源（workspaces）同步。"""
        ws2 = _seed_second_workspace(workspace)
        resp = await server.delete("/api/sessions/sess-other")
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True
        data = body["data"]
        assert data["deleted"] == "sess-other"
        assert data["live"] is False and data["session_id"] == ""
        # 文件已删 → 复盘端点 404、二次删除 404、workspaces 不再含该会话
        assert SessionStore.resolve_anywhere("sess-other") is None
        assert (await server.get("/api/sessions/sess-other/events")).status == 404
        assert (await server.delete("/api/sessions/sess-other")).status == 404
        groups = await (await server.get("/api/workspaces")).json()
        ids = {s["session_id"] for g in groups for s in g["sessions"]}
        assert "sess-other" not in ids
        assert "sess-old" in ids  # 活动工作区会话不受影响

    async def test_delete_unknown_session_404(self, server, workspace):
        resp = await server.delete("/api/sessions/no-such-id")
        assert resp.status == 404
        # 既有会话原样保留
        assert SessionStore.resolve_anywhere("sess-old") is not None

    def _bind_live_session(self, server, workspace):
        """把当前 live 会话落成一条真实会话文件并绑定 agent（仿 agent 启动）。

        补一条消息让文件真正落盘（惰性创建）——删除当前会话按文件定位，
        空白 live 会话没有文件、也不该有。
        """
        store = SessionStore.create(workspace, "test-model", session_id="sess-live")
        store.append_messages([{"role": "user", "content": "live question"}])
        agent = server.app[SESSION_KEY].agent
        agent.session_store = store
        agent.session_id = store.meta.session_id
        agent.hooks.session_id = store.meta.session_id
        return store

    async def test_delete_live_session_rebinds_agent(self, server, workspace):
        """删当前活动会话：先重绑到同工作区新会话，再删旧文件（顺序见调用方契约）。"""
        old = self._bind_live_session(server, workspace)
        old_id = old.meta.session_id
        resp = await server.delete(f"/api/sessions/{old_id}")
        assert resp.status == 200, await resp.text()
        data = (await resp.json())["data"]
        assert data["deleted"] == old_id
        assert data["live"] is True
        new_id = data["session_id"]
        assert new_id and new_id != old_id
        # agent 已整体重绑到新会话；新会话空白 → 惰性落盘（不建文件）
        agent = server.app[SESSION_KEY].agent
        assert agent.session_id == new_id
        assert agent.hooks.session_id == new_id
        assert agent.session_store is not None
        assert not agent.session_store.path.is_file()
        assert agent.session_store.meta.session_id == new_id
        # 旧文件已删，复盘/workspaces 双双移除；新 live 会话空白 → 不入列
        assert SessionStore.resolve_anywhere(old_id) is None
        assert (await server.get(f"/api/sessions/{old_id}/events")).status == 404
        groups = await (await server.get("/api/workspaces")).json()
        ids = {s["session_id"] for g in groups for s in g["sessions"]}
        assert old_id not in ids and new_id not in ids
        assert "sess-old" in ids  # 无关历史会话不受影响

    async def test_delete_live_session_409_when_busy(self, server, workspace):
        """回合进行中删当前会话 → 409：删除会换 session_id，进行中的回合会写错 store。"""
        self._bind_live_session(server, workspace)
        session = server.app[SESSION_KEY]
        stall = asyncio.get_running_loop().create_task(asyncio.sleep(60))
        session._turn_task = stall
        try:
            resp = await server.delete("/api/sessions/sess-live")
            assert resp.status == 409
        finally:
            stall.cancel()
        # 未删成：live 会话仍在
        assert SessionStore.resolve_anywhere("sess-live") is not None

    # ── 新建对话（品牌钮 / 「+新建对话」→ POST /api/session/new）────

    async def test_session_new_rebinds_store_lazy(self, server, workspace):
        """新建对话：整体重绑到新会话（store/账本/计数），空白不落盘。"""
        agent = server.app[SESSION_KEY].agent
        old_store = self._bind_live_session(server, workspace)
        old_id = agent.session_id
        resp = await server.post("/api/session/new", json={})
        assert resp.status == 200, await resp.text()
        data = (await resp.json())["data"]
        assert data["session_id"] and data["session_id"] != old_id
        # store 一并换新——否则新对话的消息会错写进旧会话文件
        assert agent.session_store is not old_store
        assert agent.session_store.meta.session_id == data["session_id"]
        assert agent.session_id == data["session_id"]
        assert agent.hooks.session_id == data["session_id"]
        assert not agent.session_store.path.is_file()  # 空白会话不落盘
        assert agent.history.messages == []           # 上下文清空
        # 旧会话文件原样保留（新建 ≠ 删除）
        assert SessionStore.resolve_anywhere(old_id) is not None
