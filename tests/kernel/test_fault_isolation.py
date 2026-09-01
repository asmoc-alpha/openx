"""P-B Manifest + P-C 故障隔离测试。

P-B：manifest 解析/校验/暴露（形状错拒载、未知 type 只警告、目录暴露）。
P-C：ProtectPluginTool 调用防护（timeout/输出上限/熔断/结构化错误/委托）+
装配时只包插件工具不包内置。

环境：kernel_env fixture（临时 workspace + SETTINGS_PATH + 新内核）。
"""

from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path

from openx.kernel import get_kernel
from openx.kernel.sandbox.protect import ProtectPluginTool, structured_error
from openx.permissions import PermissionLevel
from openx.tools.base import Tool, ToolResult

PLUGIN_FULL = textwrap.dedent('''\
    """P-B 测试：完整 manifest。"""
    __openx_meta__ = {
        "type": "capability.tool", "mount": "loop.tool-call", "trust": "user",
        "summary": "画图", "permissions": ["fs:read", "network"],
        "cost": {"schemaTokens": 400}, "timeout": 12,
    }

    from openx.tools.base import Tool, ToolResult

    class VizTool(Tool):
        name = "viz"
        description = "画图"
        async def execute(self, **kw):
            return ToolResult(output="viz")

    def factory(host):
        return [VizTool()]

    def apply(ctx):
        ctx.register_tool_factory("viz", factory)
''')


def _write_plugin(ws: Path, name: str, body: str) -> Path:
    d = ws / ".openx" / "plugins"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.py"
    p.write_text(body, encoding="utf-8")
    return p


# ── P-B：manifest 解析 / 校验 / 暴露 ────────────────────────────


def test_manifest_parsed_and_exposed(kernel_env):
    ws, _ = kernel_env
    _write_plugin(ws, "vizplugin", PLUGIN_FULL)
    k = get_kernel()
    k.ensure_loaded(str(ws))

    entry = next(c for c in k.list_plugins() if c["id"] == "vizplugin")
    assert entry["type"] == "capability.tool"
    assert entry["mount"] == "loop.tool-call"
    assert entry["trust"] == "user"

    info = k.plugin_help("vizplugin")
    assert info["manifest"]["timeout"] == 12
    assert info["manifest"]["permissions"] == ["fs:read", "network"]
    assert info["manifest_warnings"] == []
    # summary/cost 仍由 P-A 字段承载
    assert info["summary"] == "画图"


def test_manifest_shape_error_rejects(kernel_env):
    ws, _ = kernel_env
    _write_plugin(ws, "badplugin", PLUGIN_FULL.replace(
        '"timeout": 12,', '"timeout": "12s",'  # timeout 非 number → 拒载
    ))
    k = get_kernel()
    k.ensure_loaded(str(ws))
    info = k.plugin_help("badplugin")
    assert info["phase"] == "failed"
    assert "manifest" in info["error"]


def test_manifest_unknown_type_warns_not_rejects(kernel_env):
    ws, _ = kernel_env
    _write_plugin(ws, "weirdplugin", PLUGIN_FULL.replace(
        '"type": "capability.tool"', '"type": "future.stuff"'
    ))
    k = get_kernel()
    k.ensure_loaded(str(ws))
    info = k.plugin_help("weirdplugin")
    assert info["phase"] == "active"          # 不拒
    assert any("type" in w for w in info["manifest_warnings"])  # 只警告


# ── P-C：ProtectPluginTool 调用防护 ─────────────────────────────


class _OkTool(Tool):
    name = "ok"
    description = "ok"
    async def execute(self, **kw):
        return ToolResult(output="fine")


class _CrashTool(Tool):
    name = "crash"
    description = "crash"
    async def execute(self, **kw):
        raise RuntimeError("boom")


class _SlowTool(Tool):
    name = "slow"
    description = "slow"
    async def execute(self, **kw):
        await asyncio.sleep(5)
        return ToolResult(output="late")


class _BizErrTool(Tool):
    name = "bizerr"
    description = "bizerr"
    async def execute(self, **kw):
        return ToolResult(error="file not found")  # 业务错，不计熔断


async def test_protect_timeout_structured_error():
    r = await ProtectPluginTool(_SlowTool(), timeout=0.1).execute()
    assert "[status: timeout]" in r.error
    assert "[tool: slow]" in r.error
    assert "suggestion" in r.error


async def test_protect_business_error_not_counted():
    w = ProtectPluginTool(_BizErrTool(), max_failures=3)
    for _ in range(5):
        r = await w.execute()
        assert r.error == "file not found"
    assert not w._tripped  # 业务错不触发熔断


async def test_protect_circuit_breaker():
    trips = []
    w = ProtectPluginTool(_CrashTool(), max_failures=3,
                          on_trip=lambda t: trips.append(t.name))
    for _ in range(2):
        r = await w.execute()
        assert "[status: plugin_error]" in r.error and not w._tripped
    r = await w.execute()  # 触发熔断
    assert w._tripped and trips == ["crash"]
    r = await w.execute()  # 短路
    assert "[status: circuit_open]" in r.error


async def test_protect_output_cap():
    class _Big(Tool):
        name = "big"
        description = "big"
        async def execute(self, **kw):
            return ToolResult(output="x" * 1000)
    r = await ProtectPluginTool(_Big(), max_output=100).execute()
    assert len(r.output) == 100 and r.truncated


def test_protect_delegates_tool_surface():
    w = ProtectPluginTool(_OkTool(), timeout=5)
    assert w.name == "ok" and w.description == "ok"
    assert w.permission.level == PermissionLevel.ALLOW
    assert w.validate_args() is None
    assert w.auto_allowed({}) is False
    assert w.is_high_risk({}) is False
    assert w.preview_diff({}) is None
    schema = w.to_openai_schema()
    assert schema["function"]["name"] == "ok"


def test_structured_error_text():
    err = structured_error("x", "timeout", "took too long", "retry")
    assert err.count("[") == 4 and "retry" in err


# ── P-C：装配时只包插件工具 ─────────────────────────────────────


async def test_instantiate_wraps_plugin_not_builtin(kernel_env):
    ws, _ = kernel_env
    _write_plugin(ws, "vizplugin", PLUGIN_FULL)
    k = get_kernel()
    k.ensure_loaded(str(ws))

    from openx.kernel.sandbox.host import ToolHost
    from openx.services.assembly import instantiate_tools

    tools = instantiate_tools(k, ToolHost(workspace=str(ws)))
    assert isinstance(tools["viz"], ProtectPluginTool)
    assert tools["viz"]._timeout == 12.0  # manifest timeout 生效
    assert not isinstance(tools["read_file"], ProtectPluginTool)  # 内置不包
