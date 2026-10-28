"""S3 作者体验命令：``/plugin new``、``/plugin reload``、``/plugin test``、自动重载。

覆盖：
- new：生成的骨架 ast 可解析、过 admission 沙箱、可装载（闭环）；重名拒；
- reload：session 作用域卸了重装（新工具出现）；boot 作用域原地刷新
  （scope 保持 boot）；
- test：PASS / FAIL（含插件 print 原文）/ 源码缺失；
- OPENX_PLUGIN_AUTORELOAD=1：文件 mtime 变化 → ensure_loaded 重组；关 → 键不变。

运行：``python -m pytest tests/tools/test_plugin_commands.py -q``
"""

from __future__ import annotations

import asyncio
import ast
import os

import pytest

from openx.app.cli import commands
from openx.kernel import get_kernel, reset_kernel

import openx.config as config_mod
import openx.kernel.global_ledger as global_ledger_mod


class _CapConsole:
    def __init__(self):
        self.infos: list[str] = []
        self.successes: list[str] = []
        self.warnings: list[str] = []
        self.raw_lines: list[str] = []
        self.raw = self

    def print(self, *a, **k):
        self.raw_lines.append(" ".join(str(x) for x in a))

    def print_info(self, m):
        self.infos.append(m)

    def print_success(self, m):
        self.successes.append(m)

    def print_warning(self, m):
        self.warnings.append(m)


class _FakeAgent:
    def __init__(self, ws):
        self.workspace = ws
        self.rebuilds = 0

    def _rebuild_tools(self):
        self.rebuilds += 1


@pytest.fixture
def dev_env(tmp_path, monkeypatch):
    """kernel_env 同款：信任 + admission 关（new 闭环测试里单测开）。"""
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


async def test_plugin_new_scaffold_full_loop(dev_env, monkeypatch):
    """new → （过沙箱）test → 装载：闭环，生成物即是可发布插件。"""
    ws = dev_env
    agent = _FakeAgent(ws)
    console = _CapConsole()
    assert await commands.handle_slash_command(
        "plugin", agent, console, ["new", "greeter"]) is True
    src = ws / ".openx" / "plugins" / "greeter.py"
    assert src.is_file()
    ast.parse(src.read_text(encoding="utf-8"))  # 语法成立

    # 生成的骨架过 admission 沙箱（真实子进程：骨架自带合法 self_test）
    monkeypatch.setenv("OPENX_PLUGIN_ADMISSION", "1")
    console2 = _CapConsole()
    assert await commands.handle_slash_command(
        "plugin", agent, console2, ["test", "greeter"]) is True
    assert any("PASS" in s for s in console2.successes), console2.warnings
    monkeypatch.setenv("OPENX_PLUGIN_ADMISSION", "0")

    # 装载：信任工作区 boot 期已自动装载，工具在册
    k = get_kernel()
    k.ensure_loaded(str(ws))
    assert "greeter" in {e.name for e in k.registry("tools").entries()}

    # 重名拒
    console3 = _CapConsole()
    assert await commands.handle_slash_command(
        "plugin", agent, console3, ["new", "greeter"]) is True
    assert console3.warnings and "already exists" in console3.warnings[-1]


async def test_plugin_reload_session_and_boot(dev_env, monkeypatch):
    ws = dev_env
    agent = _FakeAgent(ws)
    plug = ws / ".openx" / "plugins" / "reloadme.py"
    plug.write_text(
        "__openx_meta__ = {'summary': 'v1'}\n"
        "from openx.tools.base import Tool, ToolResult\n"
        "class V1Tool(Tool):\n"
        "    name = 'v1'\n"
        "    description = 'v1'\n"
        "    parameters = {'type': 'object', 'properties': {}}\n"
        "    async def execute(self, **kw):\n"
        "        return ToolResult(output='v1')\n"
        "def apply(ctx):\n    ctx.register_tool(V1Tool())\n",
        encoding="utf-8",
    )
    k = get_kernel()
    k.ensure_loaded(str(ws))  # 信任工作区：boot 期即装载（session 流程下同）
    assert "v1" in {e.name for e in k.registry("tools").entries()}

    # 改内容 → reload → 新工具在、旧工具走（session 作用域卸了重装）
    plug.write_text(
        "__openx_meta__ = {'summary': 'v2'}\n"
        "from openx.tools.base import Tool, ToolResult\n"
        "class V2Tool(Tool):\n"
        "    name = 'v2'\n"
        "    description = 'v2'\n"
        "    parameters = {'type': 'object', 'properties': {}}\n"
        "    async def execute(self, **kw):\n"
        "        return ToolResult(output='v2')\n"
        "def apply(ctx):\n    ctx.register_tool(V2Tool())\n",
        encoding="utf-8",
    )
    console = _CapConsole()
    assert await commands.handle_slash_command(
        "plugin", agent, console, ["reload", "reloadme"]) is True
    assert console.successes and agent.rebuilds == 1
    names = {e.name for e in k.registry("tools").entries()}
    assert "v2" in names and "v1" not in names

    # boot 作用域：文件插件经 ensure_loaded 装载（scope=boot）→ 原地刷新
    plug2 = ws / ".openx" / "plugins" / "bootplug.py"
    plug2.write_text(
        "__openx_meta__ = {'summary': 'boot v1'}\n"
        "from openx.tools.base import Tool, ToolResult\n"
        "class BootTool(Tool):\n"
        "    name = 'boottool'\n"
        "    description = 'b'\n"
        "    parameters = {'type': 'object', 'properties': {}}\n"
        "    async def execute(self, **kw):\n"
        "        return ToolResult(output='b')\n"
        "def apply(ctx):\n    ctx.register_tool(BootTool())\n",
        encoding="utf-8",
    )
    k._load_key = None  # 新文件不进键（既有设计：fresh discover 归 load_plugin）
    k.ensure_loaded(str(ws))
    assert k._plugins["bootplug"].scope == "boot"
    plug2.write_text(
        "__openx_meta__ = {'summary': 'boot v2'}\n"
        "from openx.tools.base import Tool, ToolResult\n"
        "class BootTool2(Tool):\n"
        "    name = 'boottool2'\n"
        "    description = 'b2'\n"
        "    parameters = {'type': 'object', 'properties': {}}\n"
        "    async def execute(self, **kw):\n"
        "        return ToolResult(output='b2')\n"
        "def apply(ctx):\n    ctx.register_tool(BootTool2())\n",
        encoding="utf-8",
    )
    console2 = _CapConsole()
    assert await commands.handle_slash_command(
        "plugin", agent, console2, ["reload", "bootplug"]) is True
    assert any("boot" in s for s in console2.successes)
    assert k._plugins["bootplug"].scope == "boot"  # scope 保持
    assert "boottool2" in {e.name for e in k.registry("tools").entries()}

    # 未知名
    console3 = _CapConsole()
    assert await commands.handle_slash_command(
        "plugin", agent, console3, ["reload", "ghost"]) is True
    assert console3.warnings and "not found" in console3.warnings[-1]


async def test_plugin_test_reports_failures(dev_env, monkeypatch):
    monkeypatch.setenv("OPENX_PLUGIN_ADMISSION", "1")
    ws = dev_env
    (ws / ".openx" / "plugins" / "broken.py").write_text(
        "def apply(ctx):\n    pass\n"
        "def self_test():\n"
        "    print('probe-marker')\n"
        "    assert False, 'expected failure'\n",
        encoding="utf-8",
    )
    console = _CapConsole()
    assert await commands.handle_slash_command(
        "plugin", _FakeAgent(ws), console, ["test", "broken"]) is True
    # FAIL 走 warning 桶（PASS 才是 success）
    assert any("FAIL" in s for s in console.warnings), console.warnings
    blob = "\n".join(console.raw_lines)
    assert "probe-marker" in blob  # self_test 的 print 原文透传
    assert "expected failure" in "\n".join(console.warnings)

    # 源码缺失
    console2 = _CapConsole()
    assert await commands.handle_slash_command(
        "plugin", _FakeAgent(ws), console2, ["test", "ghost"]) is True
    assert console2.warnings and "not found" in console2.warnings[-1]


def test_autoreload_env_rebuilds_on_change(dev_env, monkeypatch):
    ws = dev_env
    plug = ws / ".openx" / "plugins" / "auto.py"
    plug.write_text('__openx_meta__ = {"summary": "v1"}\ndef apply(ctx):\n    pass\n')
    k = get_kernel()
    monkeypatch.setenv("OPENX_PLUGIN_AUTORELOAD", "1")
    k.ensure_loaded(str(ws))
    assert next(c for c in k.list_plugins() if c["id"] == "auto")["phase"] == "active"

    # 改内容 → 文件签名（mtime_ns）进键 → 键变 → 下一轮 ensure_loaded 重组
    plug.write_text(
        '__openx_meta__ = {"summary": "v2"}\ndef apply(ctx):\n    pass\n'
    )
    k.ensure_loaded(str(ws))
    assert k.plugin_help("auto")["manifest"]["summary"] == "v2"


def test_autoreload_off_by_default(dev_env, monkeypatch):
    monkeypatch.delenv("OPENX_PLUGIN_AUTORELOAD", raising=False)
    ws = dev_env
    plug = ws / ".openx" / "plugins" / "quiet.py"
    plug.write_text('__openx_meta__ = {"summary": "v1"}\ndef apply(ctx):\n    pass\n')
    k = get_kernel()
    k.ensure_loaded(str(ws))
    plug.write_text(
        '__openx_meta__ = {"summary": "changed"}\ndef apply(ctx):\n    pass\n'
    )
    k.ensure_loaded(str(ws))
    # 默认关：文件变了也不重组（summary 还是旧值）
    assert k.plugin_help("quiet")["manifest"]["summary"] == "v1"
