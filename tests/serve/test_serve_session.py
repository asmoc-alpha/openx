"""ServeSession 单测：串行回合、广播、attach 快照、interrupt、权限桥。

假 agent（鸭子类型 stream_run）+ 假 WS 发送器（send_json 录到 list），
零 LLM / 零网络 / 零真实内核——只测会话编排逻辑本身。
"""

from __future__ import annotations

import asyncio

import pytest

from openx.agent import ToolResultEvent, ToolStartEvent
from openx.app.serve.session import ServeSession
from openx.core import protocol


class FakeWS:
    """假 WebSocket：send_json 把事件录进 list（同步完成，无真实 IO）。"""

    def __init__(self):
        self.sent: list = []

    async def send_json(self, obj):
        self.sent.append(obj)


class FakeHistory:
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello!"},
    ]


class FakeAgent:
    session_id = "sess1"
    history = FakeHistory()
    tools = {"read_file": 1, "write_file": 1}
    last_tool_rounds = 2
    total_input_tokens = 10
    total_output_tokens = 4
    sleep = 0.0  # 秒；>0 模拟慢回合（供 live-attach / interrupt 测试）

    class _Cfg:
        model = "fake-model"

    config = _Cfg()

    async def stream_run(self, text):
        yield "Hel"
        if self.sleep:
            await asyncio.sleep(self.sleep)
        yield "lo"
        yield ToolStartEvent(name="read_file", arguments='{"path": "x"}')
        yield ToolResultEvent(name="read_file", output="contents", is_error=False)
        yield "\n\n[dim]● Compacting conversation…[/dim]\n"


@pytest.fixture
def agent():
    return FakeAgent()


async def _flush(client) -> None:
    """等客户端队列排空（downlink 任务把已入队事件发完）。"""
    for _ in range(200):
        await asyncio.sleep(0)
        if client.queue.empty():
            await asyncio.sleep(0)
            return
    raise AssertionError("client queue did not drain")


# ── attach 快照 ─────────────────────────────────────────────────


async def test_attach_sends_init_and_history(agent):
    session = ServeSession(agent)
    session.start()
    ws = FakeWS()
    client = session.attach(ws)
    await _flush(client)

    init = ws.sent[0]
    assert init["type"] == "system" and init["subtype"] == "init"
    assert init["session_id"] == "sess1"
    assert init["model"] == "fake-model"
    assert sorted(init["tools"]) == ["read_file", "write_file"]

    hist = ws.sent[1]
    assert hist["type"] == "history"
    assert len(hist["messages"]) == 2
    session.stop()


async def test_late_attach_receives_live_buffer(agent):
    """回合进行中 attach：快照应含 live user + 已广播的 live 事件。"""
    agent.sleep = 0.3
    session = ServeSession(agent)
    session.start()
    ws1 = FakeWS()
    session.attach(ws1)
    ws1.sent.clear()
    session.submit("slow turn")
    await asyncio.sleep(0.05)  # 首个 token 已广播，回合仍挂起

    ws2 = FakeWS()
    client2 = session.attach(ws2)
    await _flush(client2)
    types = [e["type"] for e in ws2.sent]
    assert "user_message" in types
    assert "text_delta" in types

    await asyncio.sleep(0.4)  # 回合跑完收尾
    session.stop()


# ── 回合流式 / 广播 ─────────────────────────────────────────────


async def test_turn_streams_events_and_result(agent):
    session = ServeSession(agent)
    session.start()
    ws = FakeWS()
    client = session.attach(ws)
    await _flush(client)  # 等 init/history 发完，避免残留快照干扰回合断言
    ws.sent.clear()

    session.submit("do it")
    await asyncio.sleep(0.05)

    types = [e["type"] for e in ws.sent]
    assert types[0] == "user_message"
    # 文本 token 合并、[dim] 标签剥净、压缩提示仍在（作为普通文本）
    text = "".join(e.get("text", "") for e in ws.sent if e["type"] == "text_delta")
    assert "Hello" in text
    assert "[dim]" not in text and "● Compacting" in text
    # 工具事件
    tool = next(e for e in ws.sent if e["type"] == "tool_use")
    assert tool["name"] == "read_file"
    result = next(e for e in ws.sent if e["type"] == "tool_result")
    assert result["output"] == "contents" and not result["is_error"]
    # 终局
    assert ws.sent[-1]["type"] == "result"
    assert ws.sent[-1]["subtype"] == "success"
    assert ws.sent[-1]["num_turns"] == 2
    session.stop()


async def test_broadcast_to_multiple_clients(agent):
    session = ServeSession(agent)
    session.start()
    ws1, ws2 = FakeWS(), FakeWS()
    c1 = session.attach(ws1)
    c2 = session.attach(ws2)
    await _flush(c1)
    await _flush(c2)
    ws1.sent.clear()
    ws2.sent.clear()

    session.submit("hi all")
    await asyncio.sleep(0.05)

    for ws in (ws1, ws2):
        types = [e["type"] for e in ws.sent]
        assert "user_message" in types
        assert "text_delta" in types
        assert ws.sent[-1]["type"] == "result"
    session.stop()


async def test_serialized_turns(agent):
    """回合串行：并发提交多条消息 → 按序逐个完成（REPL 语义）。"""
    session = ServeSession(agent)
    session.start()
    ws = FakeWS()
    session.attach(ws)

    session.submit("one")
    session.submit("two")
    await asyncio.sleep(0.1)

    results = [e for e in ws.sent if e["type"] == "result"]
    assert len(results) == 2
    users = [e for e in ws.sent if e["type"] == "user_message"]
    assert [u["text"] for u in users] == ["one", "two"]
    session.stop()


async def test_interrupt_broadcasts_and_worker_survives(agent):
    agent.sleep = 0.3
    session = ServeSession(agent)
    session.start()
    ws = FakeWS()
    session.attach(ws)
    ws.sent.clear()

    session.submit("interrupt me")
    await asyncio.sleep(0.05)
    session.interrupt()
    await asyncio.sleep(0.05)
    assert "interrupted" in [e["type"] for e in ws.sent]

    # worker 未毒死：下一条消息照常跑完
    agent.sleep = 0.0
    ws.sent.clear()
    session.submit("after interrupt")
    await asyncio.sleep(0.05)
    assert ws.sent[-1]["type"] == "result"
    session.stop()


# ── 上行分发 ────────────────────────────────────────────────────


async def test_uplink_dispatch(agent):
    session = ServeSession(agent)
    session.start()
    ws = FakeWS()
    session.attach(ws)
    ws.sent.clear()

    session._handle_uplink('{"type": "message", "text": "hello"}')
    await asyncio.sleep(0.05)
    assert any(e["type"] == "user_message" for e in ws.sent)

    # 无回合时 interrupt 安全 no-op；未匹配 permission_response 忽略
    session._handle_uplink('{"type": "interrupt"}')
    session._handle_uplink('{"type": "permission_response", "request_id": "nope", "allowed": true}')
    # 畸形行静默不断流
    session._handle_uplink("not json")
    session.stop()


# ── 权限桥（经 session.bridge）──────────────────────────────────


async def test_permission_bridge_no_clients_denies(agent):
    session = ServeSession(agent)
    session.start()
    assert await session.bridge.ask_permission("shell", "run") == (False, False)
    session.stop()


async def test_permission_bridge_roundtrip_via_response(agent):
    session = ServeSession(agent)
    session.start()
    ws = FakeWS()
    session.attach(ws)

    async def _respond():
        for _ in range(100):
            req = next((e for e in ws.sent if e["type"] == "permission_request"), None)
            if req is not None:
                break
            await asyncio.sleep(0.01)
        assert req is not None, "no permission_request broadcast"
        session.bridge.on_response(req["request_id"], True, remember=True)

    t = asyncio.ensure_future(_respond())
    approved, remember = await session.bridge.ask_permission("shell", "run")
    await t
    assert (approved, remember) == (True, True)
    session.stop()


async def test_permission_bridge_last_client_disconnect_denies(agent):
    """断流律：最后一个客户端断开 → 待决裁决全部按拒绝。"""
    session = ServeSession(agent)
    session.start()
    ws = FakeWS()
    client = session.attach(ws)

    async def _ask():
        return await session.bridge.ask_permission("shell", "run")

    fut = asyncio.ensure_future(_ask())
    await asyncio.sleep(0.01)  # 广播已发出、future 待决
    session.detach(client)     # 唯一客户端断开 → deny_all
    assert await fut == (False, False)
    session.stop()


# ── P4.1 交互弹窗桥：ask_user / confirm_plan 的 fail-closed ──────


async def test_ask_bridge_no_clients_conservative(agent):
    """无客户端 → 立即保守默认 / 拒绝，不发广播。"""
    session = ServeSession(agent)
    session.start()
    assert await session.bridge.ask_user(
        "Mode?", [{"label": "Auto"}, {"label": "Stay in manual"}]
    ) == "Stay in manual"          # 保守默认：绝不切成 Auto
    assert await session.bridge.ask_user(
        "q", [{"label": "A"}, {"label": "B"}], multi_select=True
    ) == ["B"]                     # 无保守项 → 末项
    assert await session.bridge.ask_user("q", []) == ""
    assert await session.bridge.confirm_plan("# plan") is False
    session.stop()


async def test_ask_bridge_timeout_conservative(agent):
    """超时 → 保守默认 / 拒绝（fail-closed），即使有客户端。"""
    session = ServeSession(agent)
    session.bridge._timeout = 0.05
    session.start()
    ws = FakeWS()
    session.attach(ws)

    assert await session.bridge.ask_user(
        "Mode?", [{"label": "Auto"}, {"label": "Stay in manual"}]
    ) == "Stay in manual"
    assert await session.bridge.confirm_plan("# plan") is False
    session.stop()


async def test_ask_plan_last_client_disconnect_conservative(agent):
    """断流律：最后客户端断开 → ask_user 落保守默认、plan 落拒绝。"""
    session = ServeSession(agent)
    session.start()
    ws = FakeWS()
    client = session.attach(ws)

    async def _ask():
        return await session.bridge.ask_user(
            "Mode?", [{"label": "Auto"}, {"label": "Stay in manual"}]
        )

    async def _plan():
        return await session.bridge.confirm_plan("# plan")

    ask_fut = asyncio.ensure_future(_ask())
    plan_fut = asyncio.ensure_future(_plan())
    await asyncio.sleep(0.01)
    session.detach(client)         # 断流 → deny_all
    assert await ask_fut == "Stay in manual"
    assert await plan_fut is False
    session.stop()


# ── 插件 UI 面板广播（ui/v1）────────────────────────────────────


class FakePanels:
    """假征集器：panels() 依次返回脚本帧（可含 rich 标签），录调用数。"""

    def __init__(self, frames):
        self.frames = frames
        self.calls = 0

    def panels(self):
        i = min(self.calls, len(self.frames) - 1)
        self.calls += 1
        return self.frames[i]


class BoomPanels:
    """坏征集器：panels() 崩溃（兜底路径：广播空面板，不炸 ticker）。"""

    def panels(self):
        raise RuntimeError("collector boom")


def _panel_agent(collector) -> FakeAgent:
    agent = FakeAgent()
    agent.ui_panels = collector
    return agent


async def test_attach_snapshot_includes_panels():
    """attach 快照含面板（行剥 rich 标签），宠物 attach 即可见。"""
    agent = _panel_agent(
        FakePanels([[("pet", ["[dim](=^··^=)  pet is happy[/dim]"])]])
    )
    session = ServeSession(agent)
    session.start()
    ws = FakeWS()
    client = session.attach(ws)
    await _flush(client)

    ev = [e for e in ws.sent if e["type"] == "panels"]
    assert ev and ev[0]["panels"] == [
        {"name": "pet", "lines": ["(=^··^=)  pet is happy"]}
    ]
    session.stop()


async def test_panel_ticker_broadcasts_on_change_only(monkeypatch):
    """ticker 变化才广播：帧变化发一帧、静止不重发（省带宽）。"""
    import openx.app.serve.session as session_mod

    monkeypatch.setattr(session_mod, "_PANEL_TICK", 0.02)
    agent = _panel_agent(FakePanels([
        [("pet", ["frame 0"])],
        [("pet", ["frame 1"])],
        [("pet", ["frame 1"])],
    ]))
    session = ServeSession(agent)
    session.start()
    ws = FakeWS()
    client = session.attach(ws)
    await _flush(client)
    ws.sent.clear()

    await asyncio.sleep(0.15)  # 数拍：frame0 -> frame1 变化一次后静止
    events = [e for e in ws.sent if e["type"] == "panels"]
    assert len(events) == 1, [e["panels"] for e in events]
    assert events[0]["panels"] == [{"name": "pet", "lines": ["frame 1"]}]
    session.stop()


async def test_panel_collector_crash_broadcasts_empty(monkeypatch):
    """征集器崩溃 → 兜底广播空面板（面板全消失语义），ticker 不死。"""
    import openx.app.serve.session as session_mod

    monkeypatch.setattr(session_mod, "_PANEL_TICK", 0.02)
    agent = _panel_agent(BoomPanels())
    session = ServeSession(agent)
    session.start()
    ws = FakeWS()
    client = session.attach(ws)
    await _flush(client)
    # attach 快照（崩溃兜底 → 空面板）后，ticker 继续跑、不再发新帧
    ws.sent.clear()

    await asyncio.sleep(0.1)
    assert ws.sent == []  # 空面板无变化 → 不广播；ticker 未炸
    session.stop()


async def test_panel_ticker_stops_with_last_client(monkeypatch):
    """最后客户端断开 → ticker 停止、指纹复位（重连时快照重发全量）。"""
    import openx.app.serve.session as session_mod

    monkeypatch.setattr(session_mod, "_PANEL_TICK", 0.02)
    agent = _panel_agent(FakePanels([[("pet", ["frame"])]]))
    session = ServeSession(agent)
    session.start()
    ws = FakeWS()
    client = session.attach(ws)
    await _flush(client)

    session.detach(client)
    assert session._panel_task is None
    assert session._panel_sig is None
    session.stop()
