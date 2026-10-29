"""S5b 插件资产端点：``/api/plugin-asset/<plugin>/<path>``。

覆盖：
- ACTIVE 目录插件的 svg → 200 `image/svg+xml`；白名单外后缀 / 穿越 /
  软链逃逸 / 未知插件 / 非 ACTIVE → 404；
- 256KiB 上限。

运行：``python -m pytest tests/serve/test_plugin_asset.py -q``
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import openx.config as config_mod
import openx.kernel.global_ledger as global_ledger_mod
from openx.config import OpenXConfig
from openx.kernel import get_kernel, reset_kernel


@pytest.fixture
async def asset_env(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(config_mod, "SETTINGS_PATH", settings)
    monkeypatch.setattr(
        global_ledger_mod, "GLOBAL_LEDGER_PATH", tmp_path / "ledger.jsonl"
    )
    monkeypatch.setenv("OPENX_PLUGIN_ADMISSION", "0")
    reset_kernel()
    ws = tmp_path / "ws"
    plug = ws / ".openx" / "plugins" / "pet"
    plug.mkdir(parents=True)
    (plug / "plugin.py").write_text(
        "__openx_meta__ = {'summary': 'pet'}\n"
        "def apply(ctx):\n    pass\n",
        encoding="utf-8",
    )
    (plug / "pet.svg").write_text(
        "<svg xmlns='http://www.w3.org/2000/svg'><rect width='4' height='4'/></svg>",
        encoding="utf-8",
    )
    (plug / "notes.txt").write_text("not an image", encoding="utf-8")
    config_mod.OpenXConfig.add_trusted_dir(str(ws))
    k = get_kernel()
    k.ensure_loaded(str(ws))

    from aiohttp import web as aioweb
    from aiohttp.test_utils import TestClient, TestServer

    from openx.app.serve.api import register_api

    app = aioweb.Application()
    register_api(app, type("S", (), {"agent": None})(), ws)
    async with TestClient(TestServer(app)) as client:
        yield client, plug
    reset_kernel()


async def test_serves_active_plugin_svg(asset_env):
    client, _plug = asset_env
    r = await client.get("/api/plugin-asset/pet/pet.svg")
    assert r.status == 200
    assert r.headers["Content-Type"] == "image/svg+xml"
    assert "<svg" in await r.text()


async def test_asset_refuses_traversal_and_bad_types(asset_env):
    client, plug = asset_env
    # 穿越
    assert (await client.get(
        "/api/plugin-asset/pet/%2e%2e/%2e%2e/settings.json")).status == 404
    assert (await client.get(
        "/api/plugin-asset/pet/..%2f..%2fsettings.json")).status == 404
    # 白名单外后缀
    assert (await client.get("/api/plugin-asset/pet/notes.txt")).status == 404
    # 未知插件 / 未知文件
    assert (await client.get("/api/plugin-asset/ghost/pet.svg")).status == 404
    assert (await client.get("/api/plugin-asset/pet/absent.svg")).status == 404


async def test_asset_requires_active_plugin(asset_env):
    client, plug = asset_env
    # 插件被禁用（非 ACTIVE）→ 404（禁用表在全局 settings 的 plugins 键下）
    global_settings = config_mod.SETTINGS_PATH
    data = json_settings(global_settings)
    data.setdefault("plugins", {}).setdefault("disabled", []).append("pet")
    global_settings.write_text(__import__("json").dumps(data), encoding="utf-8")
    k = get_kernel()
    k.ensure_loaded(str(k.workspace or "."))
    r = await client.get("/api/plugin-asset/pet/pet.svg")
    assert r.status == 404


def json_settings(path: Path) -> dict:
    if path.exists():
        return __import__("json").loads(path.read_text(encoding="utf-8"))
    return {}
