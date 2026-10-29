"""插件管理元工具（S5b，对标 DSH ``plugin_manager`` / ``cordis_inspect``）。

- ``plugin_manager``（ASK）：模型可调的打包面——``install``（工作区目录 →
  admission 沙箱 → 用户插件目录 → 会话装载）、``set_enabled``（settings
  禁用表写回 + 重组）、``list``（已装 + 可发现）。每个变更动作都要用户
  批准——与 DSH "每个 action 都需审批" 同纪律；
- ``plugin_inspect``（ALLOW，只读）：注册项内省——按 kind 列注册、查单
  条目（归属插件 + schema + 权限级）、查插件配置 schema 与当前值。模型
  在写插件/调工具前先看合同，不再靠猜。

安装语义与 ``/plugin install`` 共享一途：admission 门（S1b）是唯一的
执行准入，拷贝前插件代码在主进程零执行。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

from ..config import OpenXConfig
from ..kernel.assembly.loader import (
    discover,
    project_plugins_dir,
    user_plugins_dir,
)
from ..kernel.sandbox.admission import run_admission
from .base import Tool, ToolResult

_KINDS = ("tools", "commands", "contexts", "lifecycle", "ui_slots", "providers")


def _locate_bundle_source(target: Path) -> tuple[Optional[Path], Optional[Path]]:
    """目标目录/文件 → (插件根, 入口文件)。root 为 None = 单文件。"""
    if target.is_file() and target.suffix == ".py":
        return None, target
    if target.is_dir():
        entry = target / "plugin.py"
        if entry.is_file():
            return target, entry
        py_files = [p for p in sorted(target.glob("*.py"))
                    if not p.name.startswith("_")]
        if py_files:
            return None, py_files[0]
    return None, None


class PluginManagerTool(Tool):
    """install / set_enabled / list——模型可调的插件管理面（ASK）。"""

    name = "plugin_manager"
    description = (
        "Manage plugins. Actions: "
        "install {target: absolute path to a plugin dir (with plugin.py) or "
        "a single .py file} — validates in the admission sandbox then installs "
        "into the user plugins dir and loads it this session; "
        "set_enabled {plugin_id, enabled} — toggle a plugin via the disabled "
        "table (builtin tools are exempt); "
        "list {} — installed and discoverable plugins. "
        "Every mutating action requires user approval."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["install", "set_enabled", "list"],
            },
            "target": {"type": "string",
                       "description": "install: absolute dir or .py path"},
            "plugin_id": {"type": "string", "description": "set_enabled: plugin id"},
            "enabled": {"type": "boolean", "description": "set_enabled: desired state"},
        },
        "required": ["action"],
    }

    def __init__(self, kernel: Any, agent: Any) -> None:
        self._kernel = kernel
        self._agent = agent

    @property
    def permission(self) -> Any:
        from ..permissions import Permission, PermissionLevel

        return Permission(
            level=PermissionLevel.ASK,
            reason="install, enable or disable plugins",
        )

    async def execute(
        self,
        action: str,
        target: str = "",
        plugin_id: str = "",
        enabled: bool = True,
    ) -> ToolResult:
        if action == "install":
            if not target:
                return ToolResult(error="install needs {target: absolute path}")
            return await self._install(Path(target))
        if action == "set_enabled":
            if not plugin_id:
                return ToolResult(error="set_enabled needs {plugin_id, enabled}")
            return self._set_enabled(plugin_id, bool(enabled))
        if action == "list":
            return self._list()
        return ToolResult(error=f"unknown action: {action}")

    # ── install ─────────────────────────────────────────────────

    async def _install(self, source: Path) -> ToolResult:
        source = Path(source).expanduser()
        if not source.exists():
            return ToolResult(error=f"not found: {source}")
        root, entry = _locate_bundle_source(source)
        if entry is None:
            return ToolResult(
                error="no plugin entry found (expect plugin.py or a .py file)"
            )
        plugin_id = root.name if root else entry.stem

        # admission 沙箱门：过不了就不落盘（拷贝前代码零执行）
        verdict = await asyncio.to_thread(run_admission, entry)
        if not verdict.ok:
            return ToolResult(error=f"admission failed: {verdict.error}")

        dest = user_plugins_dir() / plugin_id if root else \
            user_plugins_dir() / f"{plugin_id}.py"
        if dest.exists():
            return ToolResult(error=f"plugin already exists: {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if root is not None:
            shutil.copytree(source, dest,
                            ignore=shutil.ignore_patterns(".git", "__pycache__"))
            load_path = dest / "plugin.py"
        else:
            shutil.copy2(entry, dest)
            load_path = dest

        ok, msg = self._kernel.load_plugin(plugin_id, path=load_path)
        if not ok:
            try:
                if root is not None:
                    shutil.rmtree(dest, ignore_errors=True)
                else:
                    dest.unlink(missing_ok=True)
            except OSError:
                pass
            return ToolResult(error=f"installed but failed to load: {msg}")
        if self._agent is not None:
            self._agent._rebuild_tools()
        info = self._kernel.plugin_help(plugin_id) or {}
        summary = (info.get("manifest") or {}).get("summary", "")
        return ToolResult(
            output=f"plugin installed & loaded: {plugin_id}\n{summary}".strip()
        )

    # ── set_enabled ─────────────────────────────────────────────

    def _set_enabled(self, plugin_id: str, enabled: bool) -> ToolResult:
        row = next((c for c in self._kernel.list_plugins()
                    if c["id"] == plugin_id), None)
        if row is None:
            return ToolResult(error=f"plugin not loaded: {plugin_id}")
        if row.get("builtin", False) or plugin_id.startswith("builtin-"):
            return ToolResult(error="builtin plugins cannot be toggled")

        data = OpenXConfig._load_full_settings()
        plugins = data.get("plugins") or {}
        if not isinstance(plugins, dict):
            plugins = {}
        disabled = list(plugins.get("disabled") or [])
        changed = False
        if enabled and plugin_id in disabled:
            disabled.remove(plugin_id)
            changed = True
        elif not enabled and plugin_id not in disabled:
            disabled.append(plugin_id)
            changed = True
        if changed:
            plugins["disabled"] = disabled
            data["plugins"] = plugins
            OpenXConfig._save_full_settings(data)
            self._kernel._load_key = None  # 禁用表进组合键：显式触发重组
            self._kernel.ensure_loaded(str(self._agent.workspace)
                                       if self._agent else "")
            if self._agent is not None:
                self._agent._rebuild_tools()
        state = "enabled" if plugin_id not in disabled else "disabled"
        return ToolResult(output=f"plugin {plugin_id}: {state}")

    # ── list ────────────────────────────────────────────────────

    def _list(self) -> ToolResult:
        installed = {
            r["id"]: r.get("phase", "?")
            for r in self._kernel.list_plugins()
        }
        discoverable = sorted(s.id for s in discover(self._kernel.workspace))
        lines = ["installed:"]
        for pid in sorted(installed):
            lines.append(f"  {pid} ({installed[pid]})")
        pending = [p for p in discoverable if p not in installed]
        lines.append("discoverable but not loaded: "
                     + (", ".join(pending) if pending else "(none)"))
        return ToolResult(output="\n".join(lines))


class PluginInspectTool(Tool):
    """注册项内省（ALLOW，只读）——模型写插件/调工具前先读合同。"""

    name = "plugin_inspect"
    description = (
        "Inspect plugin registrations (read-only). Actions: "
        "list {kind?} — registered entries per kind (tools, commands, "
        "contexts, lifecycle, ui_slots, providers); "
        "query {kind, name} — one entry: owning plugin, its manifest "
        "(trust/permissions/config schema), kind-specific detail; "
        "config {plugin_id} — a plugin's config schema and current values."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "query", "config"]},
            "kind": {"type": "string",
                     "enum": list(_KINDS),
                     "description": "list/query: registry kind"},
            "name": {"type": "string", "description": "query: entry name"},
            "plugin_id": {"type": "string", "description": "config: plugin id"},
        },
        "required": ["action"],
    }

    def __init__(self, kernel: Any) -> None:
        self._kernel = kernel

    @property
    def permission(self) -> Any:
        from ..permissions import Permission, PermissionLevel

        return Permission.allow()

    async def execute(
        self,
        action: str,
        kind: str = "",
        name: str = "",
        plugin_id: str = "",
    ) -> ToolResult:
        if action == "config":
            return self._inspect_config(plugin_id)
        if kind not in _KINDS:
            return ToolResult(error=f"kind must be one of {list(_KINDS)}")
        reg = self._kernel.registry(kind)
        if reg is None:
            return ToolResult(error=f"unknown registry kind: {kind}")
        if action == "list":
            lines = [f"{kind} ({len(list(reg.entries()))} entries):"]
            for e in reg.entries():
                lines.append(f"  {e.name}  ← {e.plugin}")
            return ToolResult(output="\n".join(lines))
        # query
        entry = reg.get(name)
        if entry is None:
            return ToolResult(error=f"no {kind!r} entry named {name!r}")
        info = self._kernel.plugin_help(entry.plugin) or {}
        manifest = info.get("manifest") or {}
        payload = {
            "name": entry.name,
            "kind": kind,
            "plugin": entry.plugin,
            "plugin_summary": info.get("summary", ""),
            "trust": manifest.get("trust", ""),
            "permissions": manifest.get("permissions") or [],
            "config_schema": manifest.get("config") or {},
            "warnings": list(info.get("warnings") or []),
        }
        return ToolResult(output=json.dumps(payload, ensure_ascii=False, indent=2))

    def _inspect_config(self, plugin_id: str) -> ToolResult:
        info = self._kernel.plugin_help(plugin_id)
        if info is None:
            return ToolResult(error=f"plugin not loaded: {plugin_id}")
        payload = {
            "plugin": plugin_id,
            "config_schema": (info.get("manifest") or {}).get("config") or {},
            "current": OpenXConfig.load_plugin_config(plugin_id),
        }
        return ToolResult(output=json.dumps(payload, ensure_ascii=False, indent=2))
