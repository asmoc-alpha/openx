"""模型驱动装配的元工具（P-A）——内核驻留编排核心。

四个元工具把内核插件管理 API（``kernel.list_plugins`` / ``load_plugin`` /
``unload_plugin`` / ``plugin_help``）暴露给模型：

- ``list_plugins``——插件目录（轻量：id/phase/summary/cost，模型的第一认知入口）；
- ``load_plugin``——装配（会话内动态装载，复用五阶段校验）；
- ``unload_plugin``——卸载（仅限 session 增量，回滚 = 卸载）；
- ``plugin_help``——详情按需展开。

结构性工具，由消费方（agent）直接装配、恒先占位（同 task/workflow/
exit_plan_mode）——**插件在任何阶段都拿不到 agent 本体**，元工具是内核/消费方
一侧的能力。子代理不继承（``_build_tools`` 的 ``_parent is None`` 块内构造）。

安全：list/help 只读放行；load/unload 为 **ASK**——P-C 故障隔离（沙箱）到位前，
装载一个声明了 auto-approve 工具的插件等于绕过弹窗，故逐项确认（只紧不松）。
load/unload 成功后触发 ``agent._rebuild_tools()``（/workspace 同款重建），
新装配的工具下一轮生效。
"""

from __future__ import annotations

# ── 独立调试支持：允许直接运行本文件（python openx/.../xxx.py）──────
if __name__ == "__main__" and not __package__:
    import sys as _sys
    from pathlib import Path as _Path
    _file = _Path(__file__).resolve()
    _root = _file.parent
    while _root != _root.parent and not (_root / "pyproject.toml").exists():
        _root = _root.parent
    _sys.path.insert(0, str(_root))
    __package__ = ".".join(_file.relative_to(_root).parts[:-1])

import asyncio
from typing import Any

from ..permissions import Permission, PermissionLevel
from .base import Tool, ToolResult


class ListPluginsTool(Tool):
    """查询插件目录（只读）：装配决策的第一认知入口。"""

    name = "list_plugins"
    description = (
        "List available plugins (id, phase, summary, token cost). "
        "Use this before load_plugin to decide what to assemble."
    )
    parameters = {
        "type": "object",
        "properties": {
            "filter": {
                "type": "string",
                "description": "Optional substring; keep only plugins whose id "
                               "or summary contains it.",
            },
        },
    }

    def __init__(self, kernel: Any) -> None:
        self._kernel = kernel

    async def execute(self, filter: str = "") -> ToolResult:
        rows = self._kernel.list_plugins()
        if filter:
            f = str(filter).lower()
            rows = [
                r for r in rows
                if f in r["id"].lower() or f in r["summary"].lower()
            ]
        if not rows:
            return ToolResult(output="No plugins found.")
        lines = []
        for r in rows:
            status = r["phase"]
            if r["scope"] == "session":
                status += " (session)"
            summary = r["summary"] or "-"
            cost = ""
            tok = r["cost"].get("schemaTokens") if r["cost"] else None
            if tok:
                cost = f" · {tok} tok"
            group = f" [{r.get('type')}]" if r.get("type") else ""
            lines.append(f"• {r['id']}{group} [{status}] {summary}{cost}")
        return ToolResult(output="\n".join(lines))


class PluginHelpTool(Tool):
    """查看插件详情（只读）：注册的工具/命令、警告、来源。"""

    name = "plugin_help"
    description = (
        "Show detailed usage of a plugin: registered tools, commands, "
        "warnings, source. Call before load_plugin to inspect a candidate."
    )
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }

    def __init__(self, kernel: Any) -> None:
        self._kernel = kernel

    async def execute(self, name: str) -> ToolResult:
        info = self._kernel.plugin_help(name)
        if info is None:
            return ToolResult(output=f"Plugin not found: {name}")
        lines = [
            f"plugin: {info['id']}",
            f"phase: {info['phase']} (scope={info['scope']})",
            f"source: {info['source']}",
        ]
        if info.get("summary"):
            lines.append(f"summary: {info['summary']}")
        if info.get("tools"):
            lines.append(f"tools: {', '.join(info['tools'])}")
        if info.get("commands"):
            lines.append(f"commands: {', '.join(info['commands'])}")
        if info.get("providers"):
            lines.append(f"providers: {', '.join(info['providers'])}")
        if info.get("contexts"):
            lines.append(f"contexts: {', '.join(info['contexts'])}")
        if info.get("lifecycle"):
            lines.append(f"lifecycle: {', '.join(info['lifecycle'])}")
        if info.get("ui_slots"):
            lines.append(f"ui_slots: {', '.join(info['ui_slots'])}")
        if info.get("warnings"):
            lines.append(f"warnings: {', '.join(info['warnings'])}")
        if info.get("error"):
            lines.append(f"error: {info['error']}")
        return ToolResult(output="\n".join(lines))


class LoadPluginTool(Tool):
    """装配一个插件到当前会话（ASK，逐项确认）。"""

    name = "load_plugin"
    description = (
        "Load a plugin into the current session (requires user approval). "
        "Its tools become available next turn."
    )
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }

    def __init__(self, kernel: Any, agent: Any) -> None:
        self._kernel = kernel
        self._agent = agent  # 仅用于装配成功后重建工具集（结构性工具持有）

    @property
    def permission(self) -> Permission:
        return Permission(
            level=PermissionLevel.ASK,
            reason="load a plugin into this session",
        )

    async def execute(self, name: str) -> ToolResult:
        ok, message = self._kernel.load_plugin(name)
        if ok and self._agent is not None:
            self._agent._rebuild_tools()
        return ToolResult(output=message)


class UnloadPluginTool(Tool):
    """卸载一个 session 插件的工具（ASK，逐项确认）；回滚 = 卸载。"""

    name = "unload_plugin"
    description = (
        "Unload a session-loaded plugin (requires user approval). Its tools "
        "are removed next turn."
    )
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }

    def __init__(self, kernel: Any, agent: Any) -> None:
        self._kernel = kernel
        self._agent = agent

    @property
    def permission(self) -> Permission:
        return Permission(
            level=PermissionLevel.ASK,
            reason="unload a plugin from this session",
        )

    async def execute(self, name: str) -> ToolResult:
        ok, message = self._kernel.unload_plugin(name)
        if ok and self._agent is not None:
            self._agent._rebuild_tools()
        return ToolResult(output=message)


if __name__ == "__main__":
    # 自检：假 kernel（鸭子类型）+ 假 agent（记录 rebuild 调用）
    class _FakeKernel:
        def __init__(self):
            self.catalog = [
                {"id": "builtin-tools", "phase": "active", "scope": "boot",
                 "source": "base-bundle", "summary": "核心工具集", "cost": {},
                 "tools": ["read_file"], "commands": []},
                {"id": "dataviz", "phase": "active", "scope": "session",
                 "source": "test-dir", "summary": "画图", "cost": {"schemaTokens": 400},
                 "tools": ["viz"], "commands": []},
            ]
            self.loaded = False
            self.unloaded = False

        def list_plugins(self):
            return [dict(r) for r in self.catalog]

        def plugin_help(self, name):
            return next((dict(r) for r in self.catalog if r["id"] == name), None)

        def load_plugin(self, name):
            self.loaded = True
            return (True, f"plugin loaded: {name} (session)")

        def unload_plugin(self, name):
            self.unloaded = True
            return (True, f"plugin unloaded: {name}")

    class _FakeAgent:
        def __init__(self):
            self.rebuilds = 0

        def _rebuild_tools(self):
            self.rebuilds += 1

    async def _check() -> None:
        kernel = _FakeKernel()
        agent = _FakeAgent()

        r = await ListPluginsTool(kernel).execute()
        assert "builtin-tools" in r.output and "dataviz" in r.output
        assert "400 tok" in r.output and "(session)" in r.output
        r = await ListPluginsTool(kernel).execute(filter="viz")
        assert "dataviz" in r.output and "builtin-tools" not in r.output

        r = await PluginHelpTool(kernel).execute("dataviz")
        assert "tools: viz" in r.output and "summary: 画图" in r.output
        r = await PluginHelpTool(kernel).execute("nope")
        assert "not found" in r.output

        # load/unload：ASK 权限 + 成功后触发重建
        assert LoadPluginTool(kernel, agent).permission.level == PermissionLevel.ASK
        r = await LoadPluginTool(kernel, agent).execute("dataviz")
        assert kernel.loaded and agent.rebuilds == 1 and "loaded" in r.output
        r = await UnloadPluginTool(kernel, agent).execute("dataviz")
        assert kernel.unloaded and agent.rebuilds == 2 and "unloaded" in r.output

        # 只读工具默认 ALLOW
        assert ListPluginsTool(kernel).permission.level == PermissionLevel.ALLOW

    asyncio.run(_check())
    print("openx/tools/plugin_tools.py OK ✓")
