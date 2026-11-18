"""Web 端提示包端点：``GET`` / ``POST /api/prompts``。

覆盖：
- GET：目录 + ``active`` 标记（**正文不下发**——UI 不该替模型付一遍 token）；
- POST：激活（``{"name"}``）/ 摘除（``{"name", "active": false}``），
  走的是与 ``/prompt`` 命令同一个 ``agent.activate_prompt_pack``；
- 缺名 400、未知名 400 且列出可用 pack；
- 鸭子 agent 没有 prompt_packs（embeddable/子代理）→ 空目录而非 500。

运行：``python -m pytest tests/serve/test_prompt_packs_api.py -q``
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from openx.app.serve.api import AGENT_KEY, register_api
from openx.app.serve.session import ServeSession
from openx.prompt_packs import active_names, load_prompt_packs

PACK = "plugin-authoring"


class PromptAgent:
    """鸭子 agent：只带提示包端点真正触到的面。"""

    session_id = "sess-prompts"
    last_tool_rounds = 0
    total_input_tokens = 0
    total_output_tokens = 0

    def __init__(self, workspace, *, with_packs: bool = True):
        from openx.config import OpenXConfig

        self.workspace = workspace
        self.config = OpenXConfig(workspace=str(workspace))
        self.calls: list[tuple[str, str]] = []
        self._active_prompt_packs: set[str] = set()
        if with_packs:
            self.prompt_packs = load_prompt_packs()

    # ── 与 OpenXAgent 同名同语义的最小实现 ──────────────────────
    @property
    def active_prompt_packs(self):
        return active_names(self._active_prompt_packs, self.prompt_packs)

    def activate_prompt_pack(self, name, *, reason="manual"):
        pack = self.prompt_packs.get(name)
        if pack is None:
            raise KeyError(name)
        self.calls.append((pack.name, reason))
        self._active_prompt_packs.add(pack.name)
        return pack

    def deactivate_prompt_pack(self, name):
        if name not in self._active_prompt_packs:
            return False
        self._active_prompt_packs.discard(name)
        return True


@pytest.fixture
async def client(tmp_path):
    agent = PromptAgent(tmp_path)
    session = ServeSession(agent)
    app = web.Application()
    app[AGENT_KEY] = agent
    register_api(app, session, tmp_path)
    async with TestClient(TestServer(app)) as c:
        c.agent = agent  # 便于断言副作用
        yield c


async def test_get_lists_catalog_without_body(client):
    resp = await client.get("/api/prompts")
    assert resp.status == 200
    data = (await resp.json())["data"]
    names = [p["name"] for p in data["packs"]]
    assert names == [PACK]
    pack = data["packs"][0]
    assert pack["summary"] and pack["chars"] > 0
    assert pack["active"] is False
    assert isinstance(pack["triggers"], list) and "plugin" in pack["triggers"]
    assert data["active"] == []
    # 正文绝不下发
    assert "body" not in pack
    assert "__openx_meta__" not in str(data)


async def test_post_activates_and_deactivates(client):
    resp = await client.post("/api/prompts", json={"name": PACK})
    assert resp.status == 200
    data = (await resp.json())["data"]
    assert data["active"] is True and data["chars"] > 0
    # 走同一条 reason="command" 通道（与 /prompt 命令一字不差）
    assert client.agent.calls == [(PACK, "command")]

    listed = (await (await client.get("/api/prompts")).json())["data"]
    assert listed["active"] == [PACK]
    assert listed["packs"][0]["active"] is True

    resp = await client.post("/api/prompts", json={"name": PACK, "active": False})
    data = (await resp.json())["data"]
    assert data["active"] is False and data["removed"] is True
    assert client.agent.active_prompt_packs == []

    # 再摘一次：removed=False 而非报错（幂等）
    resp = await client.post("/api/prompts", json={"name": PACK, "active": False})
    assert (await resp.json())["data"]["removed"] is False


async def test_post_requires_name(client):
    resp = await client.post("/api/prompts", json={})
    assert resp.status == 400
    assert "name required" in (await resp.json())["reason"]


async def test_post_unknown_name_lists_available(client):
    resp = await client.post("/api/prompts", json={"name": "ghost"})
    assert resp.status == 400
    reason = (await resp.json())["reason"]
    assert "not found: ghost" in reason and PACK in reason
    assert client.agent.calls == []


async def test_agent_without_packs_yields_empty_catalog(tmp_path):
    agent = PromptAgent(tmp_path, with_packs=False)
    session = ServeSession(agent)
    app = web.Application()
    app[AGENT_KEY] = agent
    register_api(app, session, tmp_path)
    async with TestClient(TestServer(app)) as c:
        resp = await c.get("/api/prompts")
        assert resp.status == 200
        data = (await resp.json())["data"]
        assert data["packs"] == [] and data["active"] == []
