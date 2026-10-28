"""S2a 目录插件：``<dir>/<name>/plugin.py`` 发现 + ``ctx.plugin_dir`` 资产解析。

覆盖：
- 目录插件被发现/装载，`ctx.plugin_dir` 指向插件根（数据资产相对它读）；
- 同 id 单文件优先（先见者赢）；
- 单文件插件的 plugin_dir 为 None（行为不变）；
- 信任门对目录插件同样生效（未信任整目录跳过，含目录插件）。

运行：``python -m pytest tests/kernel/test_directory_plugins.py -q``
"""

from __future__ import annotations

import json

import pytest

import openx.config as config_mod
import openx.kernel.global_ledger as global_ledger_mod
from openx.kernel import get_kernel, reset_kernel


PLUGIN_SRC = '''
import json
from pathlib import Path

__openx_meta__ = {"summary": "目录插件", "trust": "user"}

_DATA = Path(__file__).parent / "assets" / "greeting.json"

class HiTool:
    name = "dirhi"
    description = "hi from dir plugin"
    parameters = {"type": "object", "properties": {}}

    @property
    def permission(self):
        from openx.permissions import Permission, PermissionLevel
        return Permission(level=PermissionLevel.ASK)

    async def execute(self, **kw):
        from openx.tools.base import ToolResult
        return ToolResult(output=_DATA.read_text(encoding="utf-8"))

def apply(ctx):
    assert ctx.plugin_dir is not None, "dir plugin must see its root"
    ctx.register_tool(HiTool())

def self_test():
    assert (_DATA.parent / "greeting.json").is_file()
'''


@pytest.fixture
def dir_env(tmp_path, monkeypatch):
    """kernel_env 同款（已信任 + admission 关）。"""
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(config_mod, "SETTINGS_PATH", settings)
    monkeypatch.setattr(
        global_ledger_mod, "GLOBAL_LEDGER_PATH", tmp_path / "ledger.jsonl"
    )
    monkeypatch.setenv("OPENX_PLUGIN_ADMISSION", "0")
    reset_kernel()
    ws = tmp_path / "ws"
    (ws / ".openx" / "plugins").mkdir(parents=True)
    config_mod.OpenXConfig.add_trusted_dir(str(ws))
    yield ws
    reset_kernel()


def test_directory_plugin_discovers_and_loads(dir_env):
    ws = dir_env
    root = ws / ".openx" / "plugins" / "dirplug"
    (root / "assets").mkdir(parents=True)
    (root / "plugin.py").write_text(PLUGIN_SRC, encoding="utf-8")
    (root / "assets" / "greeting.json").write_text(
        json.dumps({"hello": "world"}), encoding="utf-8"
    )

    k = get_kernel()
    k.ensure_loaded(str(ws))
    entry = next(c for c in k.list_plugins() if c["id"] == "dirplug")
    assert entry["phase"] == "active"
    assert "dirhi" in {e.name for e in k.registry("tools").entries()}


def test_single_file_wins_over_directory(dir_env):
    ws = dir_env
    (ws / ".openx" / "plugins" / "dup.py").write_text(
        '__openx_meta__ = {"summary": "file wins"}\n'
        "def apply(ctx):\n    pass\n",
        encoding="utf-8",
    )
    dupdir = ws / ".openx" / "plugins" / "dup"
    dupdir.mkdir()
    (dupdir / "plugin.py").write_text(
        '__openx_meta__ = {"summary": "dir loses"}\n'
        "def apply(ctx):\n    pass\n",
        encoding="utf-8",
    )
    k = get_kernel()
    k.ensure_loaded(str(ws))
    info = k.plugin_help("dup")
    assert info["manifest"]["summary"] == "file wins"


def test_single_file_plugin_dir_is_none(dir_env):
    ws = dir_env
    (ws / ".openx" / "plugins" / "plain.py").write_text(
        '__openx_meta__ = {"summary": "plain"}\n'
        "SEEN = {}\n"
        "def apply(ctx):\n    SEEN['dir'] = ctx.plugin_dir\n",
        encoding="utf-8",
    )
    k = get_kernel()
    k.ensure_loaded(str(ws))
    import sys

    assert sys.modules["_openx_plugin_plain"].SEEN["dir"] is None


def test_trust_gate_covers_directory_plugins(dir_env, monkeypatch):
    ws = dir_env
    root = ws / ".openx" / "plugins" / "dirplug"
    root.mkdir()
    (root / "plugin.py").write_text(PLUGIN_SRC, encoding="utf-8")

    from openx.kernel.assembly import loader

    monkeypatch.setattr(config_mod, "SETTINGS_PATH",
                        ws.parent / "other-settings.json")
    # 新 settings 无 trusted_dirs → 未信任
    assert loader.project_plugins_untrusted(str(ws)) is True
    assert all(s.id != "dirplug" for s in loader.discover(str(ws)))
