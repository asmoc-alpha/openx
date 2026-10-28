"""S1a boot 信任门：项目插件目录未信任 → 整目录跳过 + 记账；信任后自动装载。

覆盖：
- 未信任：项目插件一个都不装（loader 零 spec）、`plugin_skipped` 事件带
  目录与插件清单；**用户目录插件不受影响**（决策 #5：默认可信）；
- 信任持久化（trusted_dirs 写进 settings.json）后：组合键变化 → 下次
  `ensure_loaded` 自动重组装载；
- `/trust` 命令：未信任工作区一键信任 + 装载；已信任时幂等提示。

环境：本地 fixture 复刻 kernel_env 但**不预信任**（信任门是本文件主题）。
运行：``python -m pytest tests/kernel/test_plugin_trust.py -q``
"""

from __future__ import annotations

import json

import pytest

import openx.config as config_mod
import openx.kernel.global_ledger as global_ledger_mod
from openx.kernel import get_kernel, reset_kernel
from openx.kernel.assembly import loader

from ._helpers import write_plugin

HELLO = '''
from openx.tools.base import Tool, ToolResult

class HelloTool(Tool):
    name = "hello"
    description = "say hello"
    parameters = {"type": "object", "properties": {}}
    @property
    def permission(self):
        from openx.permissions import Permission, PermissionLevel
        return Permission(level=PermissionLevel.ASK)
    async def execute(self, **kw):
        return ToolResult(output="hi")

def apply(ctx):
    ctx.register_tool(HelloTool())
'''


@pytest.fixture
def untrusted_env(tmp_path, monkeypatch):
    """kernel_env 同款隔离，但工作区**不预信任**（信任门主题件）。"""
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(config_mod, "SETTINGS_PATH", settings)
    monkeypatch.setattr(
        global_ledger_mod, "GLOBAL_LEDGER_PATH", tmp_path / "ledger.jsonl"
    )
    monkeypatch.setenv("OPENX_PLUGIN_ADMISSION", "0")
    reset_kernel()
    ws = tmp_path / "ws"
    (ws / ".openx" / "plugins").mkdir(parents=True)
    yield ws, settings
    reset_kernel()


def _tool_names(k) -> set[str]:
    return {e.name for e in k.registry("tools").entries()}


def test_untrusted_project_plugins_skipped(untrusted_env):
    ws, _settings = untrusted_env
    write_plugin(ws, "projplugin", HELLO)
    k = get_kernel()
    k.ensure_loaded(str(ws))

    # 一个 spec 都不产出：工具不在册、phase 是 loading（未进 _plugins）
    assert "hello" not in _tool_names(k)
    assert all(p["id"] != "projplugin" for p in k.list_plugins())

    # 发现面同样沉默（boot 与 session load 同源）
    assert all(s.id != "projplugin" for s in loader.discover(str(ws)))
    assert loader.project_plugins_untrusted(str(ws)) is True
    assert loader.untrusted_project_plugin_ids(str(ws)) == ["projplugin"]


async def test_plugin_skipped_event_and_trust_flow(untrusted_env):
    ws, settings = untrusted_env
    write_plugin(ws, "projplugin", HELLO)
    k = get_kernel()
    sink = []
    k.attach_ledger(lambda e: sink.append(e), session="s1")
    k.ensure_loaded(str(ws))
    skipped = [e for e in sink if e.type == "plugin_skipped"]
    assert skipped, [e.type for e in sink]
    payload = skipped[0].payload
    assert payload["trust"] == "untrusted"
    assert payload["plugins"] == ["projplugin"]
    assert payload["directory"].endswith(".openx/plugins")

    # 信任持久化（与 CLI 信任屏同一条 add_trusted_dir 路径）→ 组合键变化
    # → 下次 ensure_loaded 自动装载
    config_mod.OpenXConfig.add_trusted_dir(str(ws))
    k.ensure_loaded(str(ws))
    assert "hello" in _tool_names(k)
    assert loader.project_plugins_untrusted(str(ws)) is False


def test_user_dir_unaffected_by_trust(untrusted_env, tmp_path):
    ws, _settings = untrusted_env
    settings_path = config_mod.SETTINGS_PATH
    user_dir = settings_path.parent / "plugins"
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "userplugin.py").write_text(HELLO, encoding="utf-8")

    k = get_kernel()
    k.ensure_loaded(str(ws))
    # 项目插件被跳过，用户插件照常装载（决策 #5：用户目录默认可信）
    assert "hello" in _tool_names(k)
    entry = next(c for c in k.list_plugins() if c["id"] == "userplugin")
    assert entry["phase"] == "active"


async def test_trust_command_trusts_and_loads(untrusted_env, monkeypatch):
    from openx.app.cli import commands

    ws, _settings = untrusted_env
    write_plugin(ws, "projplugin", HELLO)
    k = get_kernel()
    k.ensure_loaded(str(ws))
    assert "hello" not in _tool_names(k)

    class _CapConsole:
        infos: list = []
        successes: list = []
        warnings: list = []
        raw = None

        def print_info(self, m):
            self.infos.append(m)

        def print_success(self, m):
            self.successes.append(m)

        def print_warning(self, m):
            self.warnings.append(m)

    class _FakeAgent:
        workspace = ws
        rebuilds = 0

        def _rebuild_tools(self):
            self.rebuilds += 1

    agent = _FakeAgent()
    stub = _CapConsole()
    assert await commands.handle_slash_command("trust", agent, stub, []) is True
    assert stub.successes and agent.rebuilds == 1
    assert "hello" in _tool_names(k)

    # 幂等：已信任 → 提示，不重复动作
    stub2 = _CapConsole()
    assert await commands.handle_slash_command("trust", agent, stub2, []) is True
    assert stub2.infos and "already trusted" in stub2.infos[-1]
