"""Web 插件（serve 数据层）——HTTP 集成：发现 / 启停 / 静态服务隔离。

hermetic：``SETTINGS_PATH`` monkeypatch 到 tmp（用户级插件目录随其推导 =
``~/.openx/web-plugins``）；workspace 指向 ``tmp/ws``（项目级插件目录 =
``ws/.openx/web-plugins``）。只挂 web 插件端点所需的最小 app——这些端点
不碰会话，无需 ServeSession / agent。

覆盖的安全咽喉：静态服务 resolve 后校验前缀——``..`` 穿越、符号链接逃逸
一律 404（镜像 ``api._resolve_in_workspace`` 同构语义）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import openx.app.serve.web_plugins as wp_mod
import openx.config as config_mod
from openx.app.serve.api import WORKSPACE_KEY, WorkspaceRef
from openx.app.serve.web_plugins import register_web_plugins


@pytest.fixture
def env(tmp_path, monkeypatch):
    """home（SETTINGS_PATH）+ workspace：两级插件目录都派生自它们。"""
    home = tmp_path / "home"
    settings = home / ".openx" / "settings.json"
    monkeypatch.setattr(config_mod, "SETTINGS_PATH", settings)
    monkeypatch.setattr(wp_mod, "SETTINGS_PATH", settings)
    return {
        "user": home / ".openx" / "web-plugins",
        "proj": tmp_path / "ws" / ".openx" / "web-plugins",
        "ws": tmp_path / "ws",
        "settings": settings,
    }


def _plugin(root: Path, name: str, html: str = "<p>ok</p>", manifest=None) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "index.html").write_text(html, encoding="utf-8")
    if manifest is not None:
        (d / "manifest.json").write_text(
            json.dumps(manifest) if not isinstance(manifest, str) else manifest,
            encoding="utf-8",
        )


@pytest.fixture
async def client(env):
    """预置两级插件目录 + 只含 web 插件端点的最小 app。"""
    # 用户级：clock（manifest）、notes（无 manifest → 全缺省）、tall（夹到上限）
    _plugin(env["user"], "clock", manifest={"title": "时钟", "height": 240})
    _plugin(env["user"], "notes")
    (env["user"] / "notes" / "theme").mkdir()
    (env["user"] / "notes" / "theme" / "main.js").write_text(
        "self._boot = true", encoding="utf-8"
    )
    _plugin(env["user"], "tall", manifest={"height": 9999})
    # 项目级：clock 同名（用户级先见者赢，被跳过）；dash manifest 损坏 → 全缺省
    _plugin(env["proj"], "clock", manifest={"title": "项目覆盖"})
    _plugin(env["proj"], "dash", html="<p>dash</p>", manifest="{broken json")
    (env["proj"] / "nohtml").mkdir()   # 无 index.html → 跳过

    app = web.Application()
    app[WORKSPACE_KEY] = WorkspaceRef(str(env["ws"]))
    register_web_plugins(app)
    async with TestClient(TestServer(app)) as c:
        yield c


# ── 清单：两级目录合并 + 同名先见者赢 + 缺省/夹取 ──────────────────


async def test_list_merges_levels_user_first_wins(client, env):
    resp = await client.get("/api/web-plugins")
    body = await resp.json()
    assert resp.status == 200 and body["ok"] is True
    data = body["data"]
    plugins = {p["name"]: p for p in data["plugins"]}

    # 用户级 clock 与项目级 clock 同名 → 用户级赢；nohtml 无入口被跳过
    assert set(plugins) == {"clock", "notes", "tall", "dash"}, set(plugins)
    assert plugins["clock"]["source"] == "user"
    assert plugins["clock"]["title"] == "时钟"     # manifest.title 生效
    assert plugins["clock"]["height"] == 240       # manifest.height 生效
    assert plugins["clock"]["enabled"] is True     # 无 settings → 默认全部启用

    # notes 无 manifest → title 回退目录名、height 回退默认 320
    assert plugins["notes"]["title"] == "notes"
    assert plugins["notes"]["height"] == 320
    assert plugins["notes"]["source"] == "user"

    # tall 超上限 → 夹到 1200（MIN/MAX 夹取在数据层）
    assert plugins["tall"]["height"] == 1200

    # dash：项目级、manifest 损坏静默缺省（不 500、不半改）
    assert plugins["dash"]["source"] == "project"
    assert plugins["dash"]["title"] == "dash"
    assert plugins["dash"]["height"] == 320

    # 端点回带用户级目录，供前端空态指引用
    assert data["userDir"] == str(env["user"])


# ── 启停：写 settings.json 的 webPlugins.disabled，即时生效 ─────────


async def test_toggle_writes_settings_single_source(client, env):
    # 停用 clock
    r = await client.post("/api/web-plugins/clock/toggle", json={"disabled": True})
    body = await r.json()
    assert r.status == 200 and body["ok"] is True
    resp = await client.get("/api/web-plugins")
    listed = (await resp.json())["data"]["plugins"]
    assert {p["name"]: p["enabled"] for p in listed}["clock"] is False
    # settings.json 是唯一真源（与内核插件 plugins.disabled 同模式）
    settings = json.loads(env["settings"].read_text(encoding="utf-8"))
    assert settings["webPlugins"]["disabled"] == ["clock"]

    # 再启用 → 写回空表；对其它插件无副作用
    r = await client.post("/api/web-plugins/clock/toggle", json={"disabled": False})
    body = await r.json()
    assert r.status == 200 and body["ok"] is True
    settings = json.loads(env["settings"].read_text(encoding="utf-8"))
    assert settings["webPlugins"]["disabled"] == []


async def test_toggle_rejects_unknown_and_bad_body(client):
    assert (await client.post(
        "/api/web-plugins/nope/toggle", json={"disabled": True})).status == 404
    assert (await client.post(
        "/api/web-plugins/clock/toggle", json={"disabled": "yes"})).status == 400
    assert (await client.post(
        "/api/web-plugins/../toggle", json={"disabled": True})).status == 404


# ── 静态服务：只读 + 前缀校验挡 ../ 与软链逃逸 ─────────────────────


async def test_static_serves_entry_and_nested_assets(client):
    r = await client.get("/web-plugins/clock/index.html")
    assert r.status == 200
    assert "ok" in await r.text()
    assert r.headers.get("Cache-Control") == "no-store"   # 与主前端一致
    # 任意深度的静态资源（{file:.+}）
    r = await client.get("/web-plugins/notes/theme/main.js")
    assert r.status == 200
    assert "_boot" in await r.text()


async def test_static_blocks_traversal_and_symlink_escape(client, env):
    # ``..`` 穿越：百分号编码绕开 URL 规范化；无论被哪层挡下都不得 200
    r = await client.get("/web-plugins/clock/%2e%2e/%2e%2e/%2e%2e/etc/passwd")
    assert r.status == 404

    # 符号链接逃逸：插件内链接指向插件目录外 → 404（resolve 后校验前缀）
    secret = env["user"].parent / "secret.txt"   # home/.openx/secret.txt
    secret.write_text("TOP-SECRET", encoding="utf-8")
    os.symlink(secret, env["user"] / "notes" / "leak")
    r = await client.get("/web-plugins/notes/leak")
    assert r.status == 404

    # 插件名白名单（同 discover）也挡穿越分量
    assert (await client.get("/web-plugins/no.such/index.html")).status == 404
    assert (await client.get("/web-plugins/notes/missing.js")).status == 404
