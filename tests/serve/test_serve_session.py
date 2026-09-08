"""ServeSession 单测：串行回合、广播、attach 快照、interrupt、权限桥。

假 agent（鸭子类型 stream_run）+ 假 WS 发送器（send_json 录到 list），
零 LLM / 零网络 / 零真实内核——只测会话编排逻辑本身。
"""

from __future__ import annotations

import asyncio

import pytest

from openx.agent import ToolResultEvent, ToolStartEvent
from openx.app.serve.session import ServeSession, Upload
from openx.kernel import protocol


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


# ── msg_id 至少一次投递（收到即回执 + 重连重发去重）──────────────────


async def test_submit_msg_id_acks_immediately_and_dedups(agent):
    """带 msg_id 的消息：收到即回执（先于回合）、重发同 id 只跑一轮但
    仍回执（客户端清 pending）。"""
    session = ServeSession(agent)
    session.start()
    ws = FakeWS()
    client = session.attach(ws)
    await _flush(client)
    ws.sent.clear()

    assert session.submit("hello", "m1") is True
    await asyncio.sleep(0.05)  # 回合跑完，ack 与回合事件都已在 ws.sent

    # 重发同 id：去重（不再次入队/跑轮）但再次回执
    assert session.submit("hello", "m1") is False
    await asyncio.sleep(0.02)

    acks = [e for e in ws.sent if e["type"] == "message_ack" and e["msg_id"] == "m1"]
    user = [e for e in ws.sent if e["type"] == "user_message" and e["text"] == "hello"]
    results = [e for e in ws.sent if e["type"] == "result"]
    assert len(acks) == 2, len(acks)      # 首回执 + 重发回执
    assert len(user) == 1, len(user)      # 回合只跑一遍 → 单 user_message
    assert len(results) == 1, len(results)
    # 首回执必须先于回合广播（收到即回，不经回合队列）
    assert ws.sent.index(acks[0]) < ws.sent.index(user[0])

    session.stop()


async def test_submit_without_msg_id_no_ack_no_dedup(agent):
    """存量客户端不带 msg_id：零回执、零去重，行为不变。"""
    session = ServeSession(agent)
    session.start()
    ws = FakeWS()
    client = session.attach(ws)
    await _flush(client)
    ws.sent.clear()

    assert session.submit("legacy") is True
    await asyncio.sleep(0.05)
    assert not any(e["type"] == "message_ack" for e in ws.sent)
    # 同样内容可再发一遍（无去重键）
    ws.sent.clear()
    assert session.submit("legacy") is True
    await asyncio.sleep(0.05)
    assert sum(1 for e in ws.sent if e["type"] == "user_message") == 1
    session.stop()


async def test_submit_blank_text_rejected(agent):
    """空 / 纯空白消息不入队、不回执、不记去重键。"""
    session = ServeSession(agent)
    session.start()
    ws = FakeWS()
    client = session.attach(ws)
    await _flush(client)
    ws.sent.clear()

    assert session.submit("", "m2") is False
    assert session.submit("   ", "m2") is False
    await asyncio.sleep(0.01)
    assert "m2" not in session._seen_msg_ids
    assert not any(e["type"] == "message_ack" for e in ws.sent)
    assert not any(e["type"] == "user_message" for e in ws.sent)
    session.stop()


def test_msg_id_dedup_cache_bounded_lru():
    """去重缓存有上限：溢出按 LRU 淘汰最旧，淘汰后重发当新消息。"""
    session = ServeSession(FakeAgent())  # 不起 worker：只入队+记缓存
    for i in range(300):
        session.submit(f"bulk {i}", f"b{i}")
    assert len(session._seen_msg_ids) == 256
    assert "b0" not in session._seen_msg_ids     # 最旧被淘汰
    assert session.submit("dup", "b299") is False  # 最新仍在 → 去重
    assert session.submit("dup", "b0") is True     # 淘汰过的 → 当新消息
    assert session.submit("dup", "b0") is False    # 现已入缓存 → 去重


# ── 上传附件：submit 合成 content parts + 会话清理 ────────────────


def _register_image(session: ServeSession, name: str = "pic.png") -> str:
    return session.register_upload(Upload(
        name=name, size=4, kind="image", mime="image/png",
        data_url="data:image/png;base64,AAAA",
    ))


def _register_file(session: ServeSession, name: str, rel: str) -> str:
    return session.register_upload(Upload(
        name=name, size=5, kind="file", mime="text/plain",
        rel_path=rel,
    ))


async def test_submit_with_attachments_builds_content_parts(agent):
    """带附件：submit 把上传 id 解析成 content parts 列表交给 stream_run，
    广播的 user_message 事件带 text + content（前端渲染缩略图/文件 chip）。"""
    session = ServeSession(agent)
    session.start()
    ws = FakeWS()
    client = session.attach(ws)
    await _flush(client)
    ws.sent.clear()

    captured = []
    original = agent.stream_run

    async def record(content):
        captured.append(content)
        async for ev in original(content):
            yield ev

    agent.stream_run = record

    img_id = _register_image(session)
    file_id = _register_file(session, "note.txt", ".openx/uploads/sess1/note.txt")
    assert session.submit("看看", "m-a", [img_id, file_id]) is True
    await asyncio.sleep(0.05)

    assert captured and isinstance(captured[0], list), captured
    types = [p.get("type") for p in captured[0]]
    assert types == ["text", "image_url", "openx_file"], types
    assert captured[0][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert captured[0][2]["relPath"] == ".openx/uploads/sess1/note.txt"

    um = [e for e in ws.sent if e["type"] == "user_message"]
    assert um and um[-1]["text"] == "看看"
    assert um[-1]["content"] == captured[0]
    session.stop()


async def test_submit_blank_text_with_attachment_allowed(agent):
    """空文本 + 有效附件 → 仍入队（附件即内容）；纯文本空消息仍拒绝。"""
    session = ServeSession(agent)
    session.start()
    ws = FakeWS()
    client = session.attach(ws)
    await _flush(client)
    ws.sent.clear()

    img_id = _register_image(session)
    assert session.submit("", "m-b", [img_id]) is True
    await asyncio.sleep(0.02)
    um = [e for e in ws.sent if e["type"] == "user_message"]
    assert um and um[-1]["text"] == ""
    assert isinstance(um[-1].get("content"), list)

    # 无附件空消息：仍拒绝、不回执（扩展前语义）
    ws.sent.clear()
    assert session.submit("   ") is False
    assert not any(e["type"] == "message_ack" for e in ws.sent)
    session.stop()


async def test_late_attach_replays_content(agent):
    """回合进行中迟到客户端 attach：live 快照里 user_message 带 content。"""
    session = ServeSession(agent)
    session.start()
    ws1 = FakeWS()
    client1 = session.attach(ws1)
    await _flush(client1)
    ws1.sent.clear()

    file_id = _register_file(session, "plan.md", ".openx/uploads/sess1/plan.md")
    agent.sleep = 0.3
    assert session.submit("看这份计划", "m-c", [file_id]) is True
    await asyncio.sleep(0.05)   # 回合已开跑（慢 agent 挂起中）

    ws2 = FakeWS()
    client2 = session.attach(ws2)
    await _flush(client2)
    um2 = [e for e in ws2.sent if e["type"] == "user_message"]
    assert um2 and um2[0]["content"]
    assert any(p.get("type") == "openx_file" for p in um2[0]["content"])
    await asyncio.sleep(0.4)   # 回合收尾
    agent.sleep = 0.0
    session.stop()


def test_discard_and_remove_uploads(tmp_path):
    """discard_uploads / remove_upload：删盘上文件并清注册表（幂等、safe）。"""
    session = ServeSession(FakeAgent())
    f1 = tmp_path / "u" / "a.txt"
    f1.parent.mkdir(parents=True)
    f1.write_bytes(b"x")
    id1 = _register_file(session, "a.txt", ".openx/uploads/s/a.txt")
    session._uploads[id1].path = str(f1)      # 模拟 api 写入
    img_id = _register_image(session)
    assert session.get_upload(id1) is not None

    session.discard_uploads()
    assert not f1.exists()
    assert session._uploads == {}

    f2 = tmp_path / "u2" / "b.txt"
    f2.parent.mkdir(parents=True)
    f2.write_bytes(b"y")
    id2 = _register_file(session, "b.txt", ".openx/uploads/s/b.txt")
    session._uploads[id2].path = str(f2)
    assert session.remove_upload(id2) is True
    assert not f2.exists()
    assert session.remove_upload("missing") is False


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


# ── 执行计划 / 子 agent 状态型广播（todos / fleet 下行）────────────


class FakeFleet:
    """假 FleetMonitor：register/reset/complete 的裸状态盒（鸭子最小面）。

    只承载 serve 投影用到的字段（id/label/subagent_type/status/
    tools_count/elapsed），不带行缓冲——serve 侧本来就不投影行。
    """

    def __init__(self, views=None):
        self.views = views if views is not None else []

    def snapshot(self):
        return [dict(v) for v in self.views]

    def reset(self):
        self.views = []

    def register(self, label, subagent_type="general-purpose"):
        view = {
            "id": len(self.views) + 1,
            "label": label,
            "subagent_type": subagent_type,
            "status": "running",
            "tools_count": 0,
            "elapsed": 1,
        }
        self.views.append(view)
        return view

    def complete(self, view, is_error=False):
        view["status"] = "error" if is_error else "done"


_PLAN = [
    {"content": "设计接口", "activeForm": "设计接口", "status": "in_progress"},
    {"content": "落地实现", "activeForm": "落地实现", "status": "pending"},
]


async def test_attach_snapshot_includes_todos_and_fleet():
    """attach 快照补发执行计划 / 子 agent：迟到客户端面板与实时一致。"""
    agent = FakeAgent()
    agent.todos = [dict(t) for t in _PLAN]
    agent.fleet = FakeFleet([
        {"id": 1, "label": "审阅改动", "subagent_type": "explore",
         "status": "running", "tools_count": 3, "elapsed": 5},
    ])
    session = ServeSession(agent)
    session.start()
    ws = FakeWS()
    client = session.attach(ws)
    await _flush(client)

    by_type = {e["type"]: e for e in ws.sent}
    assert by_type["todos"]["todos"] == _PLAN
    a = by_type["fleet"]["agents"][0]
    assert a["id"] == 1 and a["label"] == "审阅改动" and a["status"] == "running"
    assert a["tools_count"] == 3 and a["subagent_type"] == "explore"
    session.stop()


async def test_empty_snapshot_omitted_on_attach():
    """无执行计划 / 无子 agent → attach 不发空快照（端默认空态，避免噪声）。"""
    agent = FakeAgent()   # 无 todos / fleet 面
    session = ServeSession(agent)
    session.start()
    ws = FakeWS()
    client = session.attach(ws)
    await _flush(client)
    assert not any(e["type"] in ("todos", "fleet") for e in ws.sent)
    session.stop()


class _TodoWriteAgent(FakeAgent):
    """回合里先更新计划、再回一个 todo_write 结果的假 agent。"""

    async def stream_run(self, text):
        self.todos = [dict(t) for t in _PLAN]
        yield ToolResultEvent(name="todo_write", output="", is_error=False)
        yield "planned"


async def test_todo_write_result_broadcasts_plan():
    """todo_write 收尾 → 广播执行计划全量快照（todo_tools 侧同源）。"""
    agent = _TodoWriteAgent()
    session = ServeSession(agent)
    session.start()
    ws = FakeWS()
    client = session.attach(ws)
    await _flush(client)
    ws.sent.clear()

    session.submit("写个计划")
    await asyncio.sleep(0.05)
    todo = [e for e in ws.sent if e["type"] == "todos"]
    assert todo and todo[-1]["todos"] == _PLAN
    session.stop()


class _FleetAgent(FakeAgent):
    """回合行为由 delegate 开关控制：开 → 委派一个子代理跑几拍后收尾。

    复用于两处：委派轮验证运行中/终态广播；普通轮验证不残留、不发空帧。
    """

    def __init__(self):
        super().__init__()
        self.delegate = False

    async def stream_run(self, text):
        if not self.delegate:
            yield "plain turn"
            return
        fleet = self.fleet
        view = fleet.register("审阅改动", "explore")
        yield "delegating…"
        await asyncio.sleep(0.05)     # 给 ticker 几拍（_FLEET_TICK 已被调小）
        yield "waiting"
        fleet.complete(view)
        yield "done"


async def test_fleet_broadcasts_during_turn_and_finalizes(monkeypatch):
    """回合中 ticker 广播运行态（父等工具不 yield 事件也可见）；收尾定格终态。"""
    import openx.app.serve.session as session_mod

    monkeypatch.setattr(session_mod, "_FLEET_TICK", 0.01)
    agent = _FleetAgent()
    agent.delegate = True
    agent.fleet = FakeFleet()
    session = ServeSession(agent)
    session.start()
    ws = FakeWS()
    client = session.attach(ws)
    await _flush(client)
    ws.sent.clear()

    session.submit("派个子代理")
    await asyncio.sleep(0.2)

    fleet_evs = [e for e in ws.sent if e["type"] == "fleet"]
    assert fleet_evs, "没有 fleet 广播"
    # 回合进行中至少有一帧 running；终局定格为 done
    assert any(a["status"] == "running" for ev in fleet_evs for a in ev["agents"])
    assert fleet_evs[-1]["agents"][0]["status"] == "done"
    session.stop()


async def test_next_turn_resets_fleet_without_stale_broadcast(monkeypatch):
    """新一轮回合 fleet 归零：上轮委派不残留；本轮无子代理则不发空帧。"""
    import openx.app.serve.session as session_mod

    monkeypatch.setattr(session_mod, "_FLEET_TICK", 0.01)
    agent = _FleetAgent()
    agent.fleet = FakeFleet()
    session = ServeSession(agent)
    session.start()
    ws = FakeWS()
    client = session.attach(ws)
    await _flush(client)

    agent.delegate = True
    session.submit("有委派的一轮")
    await asyncio.sleep(0.15)
    assert any(e["type"] == "fleet" for e in ws.sent)  # 委派轮确有广播

    ws.sent.clear()
    agent.delegate = False
    session.submit("普通一轮")
    await asyncio.sleep(0.05)
    # 本轮无子代理 → 无任何 fleet 事件（reset 后空快照与指纹一致，不广播）
    assert not any(e["type"] == "fleet" for e in ws.sent)
    session.stop()
