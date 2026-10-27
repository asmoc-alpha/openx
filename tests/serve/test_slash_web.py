"""web 端斜杠命令：``/api/commands`` 菜单端点 + ServeSession 分发广播。

覆盖：
- ``GET /api/commands``：与终端补全同源（menu_entries），含内置与别名；
- ``_run_slash_command``：``/help`` 输出捕获广播（user_message →
  text_delta → result 同一回合形状）；未知命令得到提示；
- 交互式输入守卫：``paste_aware_input`` / ``_interactive_select`` 在 web
  端以干净报错代答，不读服务进程 stdin、不阻塞事件循环。

假 agent（鸭子类型）+ 假 WS 发送器（test_serve_session 同款），零 LLM。
运行：``python -m pytest tests/serve/test_slash_web.py -q``
"""

from __future__ import annotations

import asyncio

import pytest

from openx.app.serve.session import ServeSession, _WebInputNeeded
from openx.config import OpenXConfig


class FakeWS:
    def __init__(self):
        self.sent: list = []

    async def send_json(self, obj):
        self.sent.append(obj)


class SlashAgent:
    """鸭子 agent：只带命令分发真正触到的面（config / 清历史）。"""

    session_id = "sess-slash"
    last_tool_rounds = 0
    total_input_tokens = 0
    total_output_tokens = 0
    cleared = 0

    def __init__(self, workspace):
        self.workspace = workspace
        self.config = OpenXConfig(workspace=str(workspace))

    def clear_history(self):
        self.cleared += 1


class FakeHistory:
    messages: list = []


@pytest.fixture
async def session(tmp_path):
    agent = SlashAgent(tmp_path)
    s = ServeSession(agent)
    s.start()
    yield s, agent
    s.stop()


async def _flush(client) -> None:
    # 先给 worker 一拍（命令回合在几个循环 tick 内跑完，纯 sleep(0)
    # 探测可能在 worker 出队前就看到空队列）
    await asyncio.sleep(0.05)
    for _ in range(200):
        await asyncio.sleep(0)
        if client.queue.empty():
            await asyncio.sleep(0)
            return
    raise AssertionError("client queue did not drain")


def _broadcasted(session, ws):
    return [e for e in ws.sent]


async def test_slash_help_broadcasts_output(session):
    s, _agent = session
    ws = FakeWS()
    client = s.attach(ws)
    await _flush(client)
    ws.sent.clear()

    s.submit("/help")
    await _flush(client)
    types = [e["type"] for e in ws.sent]
    # 回合形状与模型回合一致：user 回显 → 文本输出 → result 收尾
    assert types[0] == "user_message"
    assert "text_delta" in types and types[-1] == "result"
    text = "".join(e.get("text", "") for e in ws.sent if e["type"] == "text_delta")
    assert "/help" in text and "Show this help" in text
    assert not any(e["type"] == "result" and e.get("is_error") for e in ws.sent)


async def test_unknown_command_gets_hint(session):
    s, _agent = session
    ws = FakeWS()
    client = s.attach(ws)
    await _flush(client)
    ws.sent.clear()

    s.submit("/no-such-cmd")
    await _flush(client)
    text = "".join(e.get("text", "") for e in ws.sent if e["type"] == "text_delta")
    assert "Unknown command" in text and "/no-such-cmd" in text


async def test_clear_runs_agent_action(session):
    s, agent = session
    ws = FakeWS()
    client = s.attach(ws)
    await _flush(client)
    ws.sent.clear()

    s.submit("/clear")
    await _flush(client)
    assert agent.cleared == 1


async def test_interactive_input_guard(session, monkeypatch):
    """paste_aware_input 在 web 端换成交互不可用报错，命令中性收尾。"""
    from openx.app.cli import commands as commands_mod

    @commands_mod.register("web-slash-ask", description="test: asks for input")
    async def _ask(agent, console, args):
        commands_mod.paste_aware_input(console.raw, "say: ")
        return True  # 若守卫失效则走到这里

    try:
        s, _agent = session
        ws = FakeWS()
        client = s.attach(ws)
        await _flush(client)
        ws.sent.clear()

        s.submit("/web-slash-ask")
        await _flush(client)
        text = "".join(e.get("text", "") for e in ws.sent if e["type"] == "text_delta")
        assert "需要交互式输入" in text
        assert any(e["type"] == "result" for e in ws.sent)
        # 守卫卸载后原函数被还原（global 状态不留痕）
        assert commands_mod.paste_aware_input.__module__ != __name__
    finally:
        commands_mod._commands.pop("web-slash-ask", None)
        commands_mod._descriptions.pop("web-slash-ask", None)


async def test_interactive_select_guard(tmp_path):
    from openx.ui.console import Console

    con = Console(config=OpenXConfig(workspace=str(tmp_path)))
    # _SlashConsole 换缓冲 + 守卫；裸 Console 不受影响
    from openx.app.serve.session import _SlashConsole

    slash = _SlashConsole(config=OpenXConfig(workspace=str(tmp_path)))
    with pytest.raises(_WebInputNeeded):
        slash._interactive_select(["a", "b"])
    with pytest.raises(_WebInputNeeded):
        slash._interactive_select()
    # 捕获与取走
    slash.print_success("captured")
    assert "captured" in slash.take_output()
    assert slash.take_output() == ""


# ── /api/commands 端点 ───────────────────────────────────────────


async def test_commands_endpoint(tmp_path):
    from aiohttp import web as aioweb
    from aiohttp.test_utils import TestClient, TestServer

    from openx.app.serve.api import AGENT_KEY, register_api

    agent = SlashAgent(tmp_path)
    session = ServeSession(agent)
    app = aioweb.Application()
    app[AGENT_KEY] = agent
    register_api(app, session, tmp_path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/commands")
        assert resp.status == 200
        body = await resp.json()
        data = body["data"]  # 统一信封：前端 OX.get 解包后的形状
        names = [c["name"] for c in data["commands"]]
        assert "help" in names and "clear" in names and "scaffolds" in names
        entry = next(c for c in data["commands"] if c["name"] == "help")
        assert entry["description"]
        assert isinstance(entry["aliases"], list)
