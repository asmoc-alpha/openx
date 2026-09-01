"""Web 端到端：aiohttp TestServer 上验证路由 + WS 往返 + 权限 + 复盘。

hermetic：SESSIONS_DIR monkeypatch 到 tmp、假 agent、零 LLM / 零真实内核。
TestServer 与测试同进程同事件循环（pytest-asyncio auto）。
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from openx.agent import ToolResultEvent, ToolStartEvent
from openx.app.serve.bridge import ServeConsole
from openx.app.serve.server import SESSION_KEY, create_app
from openx.app.serve.session import ServeSession
from openx.core.protocol import Event
from openx.core.sessions import SessionStore


class FakeHistory:
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello!"},
    ]


class FakeAgent:
    session_id = "sess-live"
    history = FakeHistory()
    tools = {"read_file": 1, "write_file": 1}
    last_tool_rounds = 1
    total_input_tokens = 5
    total_output_tokens = 2

    class _Cfg:
        model = "fake-model"

    config = _Cfg()

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
    import openx.core.sessions as sessions_mod

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
    assert "OpenX Serve" in body
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
