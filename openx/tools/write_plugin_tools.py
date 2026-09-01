"""模型自产插件元工具（P-F）—— write / test / promote。

- ``write_plugin``（ASK）：admit 管线——manifest 校验（type/mount 由内核
  协议表派生）→ 语法/契约存在性 + 按 type 的注册面契约检查 → 进程内
  ``self_test`` → 绿 → 写 ``auto-<name>.py`` 到项目插件目录 →
  ``load_plugin``（会话热插）→ 重建工具集与系统提示。名字强制 ``auto-`` 前缀（批量回滚）。
- ``test_plugin``（ALLOW）：对**已加载**的文件插件重跑 ``self_test``（验证/调试）。
- ``promote_plugin``（ASK）：用户确认 → 决策事件 + ``manifest.trust="user"``
  （boot 持久化/进组合列后续）。

安全（D9 分层定价）：``self_test`` 在**进程内**跑——模型代码的隔离是 P-C
执行隔离（进程沙箱）的后续加固；P-F 的信任闸 = write/promote 的 ASK 弹窗 +
self_test 必须先绿 + 生成的工具调用仍走正常权限闸（且 P-C 已套 ProtectPluginTool）。
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

import ast
import json
import re
import threading
from typing import Any, Optional

from ..kernel.assembly.loader import project_plugins_dir
from ..kernel.assembly.manifest import validate_manifest
from ..kernel.assembly.protocols import ProtocolSpec, route
from ..permissions import Permission, PermissionLevel
from .base import Tool, ToolResult

# self_test 超时（秒）：模型生成的代码可能含死循环（while True），超时
# 即拒--宁留一个 daemon 死线程，不卡死 write_plugin/主进程。
SELF_TEST_TIMEOUT = 10.0


def _auto_id(name: str) -> str:
    """规范化插件 id：强制 ``auto-`` 前缀（可批量回滚的抓手）。"""
    base = re.sub(r"[^a-z0-9_-]", "_", str(name).strip().lower()).strip("_")
    return f"auto-{base or 'unnamed'}"


def _resolve_protocol(ptype: Any) -> tuple[Optional[ProtocolSpec], str]:
    """type -> 协议；未知 type 拒绝（write 侧强校验，不走默认路由）。"""
    proto = route(ptype)
    if not ptype or proto.ptype != str(ptype):
        return (
            None,
            f"未知插件类型 {ptype!r}（可用类型见系统提示的插件编写格式）",
        )
    return (proto, "")


def _build_manifest(
    summary: str,
    timeout: Optional[float],
    permissions: Optional[list],
    proto: ProtocolSpec,
) -> dict:
    """manifest 组装：type/mount 由协议表派生（唯一真源，不手填）。"""
    manifest: dict = {
        "type": proto.ptype,
        "mount": proto.mount,
        "trust": "auto",
        "summary": summary or "",
        "cost": {"schemaTokens": 400},
    }
    # timeout 只对工具协议有意义（插件工具的执行超时秒数）
    if timeout is not None and proto.registry_kind == "tools":
        manifest["timeout"] = timeout
    if permissions:
        manifest["permissions"] = list(permissions)
    return manifest


def _assemble(manifest: dict, code: str, test: str) -> str:
    """组装单文件：manifest 头 + 代码 + self_test。"""
    header = "__openx_meta__ = " + json.dumps(manifest, ensure_ascii=False)
    indent_test = "\n".join(
        ("    " + ln) if ln.strip() else ln for ln in str(test).splitlines()
    )
    return f"{header}\n\n{code}\n\ndef self_test():\n{indent_test}\n"


def _admit(source: str, proto: ProtocolSpec) -> tuple[bool, str]:
    """admit 管线：语法 → 契约存在性 → 进程内 self_test。返回 (ok, why)。

    进程内跑模型代码是 D9 进程隔离前的保守默认——write_plugin 的 ASK 闸
    是这道执行的信任锚点。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return (False, f"语法错误: {e}")
    fns = {
        n.name for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    # 必备函数按协议：factory 仅 tool/v1（context/lifecycle 无工厂）
    required = {"apply", "self_test"}
    if proto.registry_kind == "tools":
        required.add("factory")
    missing = required - fns
    if missing:
        return (False, f"缺少 {sorted(missing)}() 定义")
    # 注册面契约（P-D）：apply(ctx) 子树内须调用协议对应的 ctx.register_*
    apply_node = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "apply"
    )
    calls = {
        n.func.attr for n in ast.walk(apply_node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    if not calls & set(proto.register_calls):
        return (
            False,
            f"type {proto.ptype!r} 的 apply(ctx) 须调用 "
            f"{' 或 '.join(proto.register_calls)}",
        )
    ns: dict = {}
    return _run_in_thread(source, ns)


def _run_in_thread(
    source: str, ns: dict, timeout: Optional[float] = None
) -> tuple[bool, str]:
    """daemon 线程里执行模块代码 + self_test，join 超时即弃。

    模块级代码与 self_test 都可能死循环；Python 无线程终止原语，超时后
    线程遗留为 daemon（进程退出不受阻）--这是 D9 进程隔离落地前的
    保守兜底，write_plugin 的 ASK 闸仍是信任锚点。timeout 缺省取模块
    常量（调用期读，测试可 monkeypatch）。
    """
    if timeout is None:
        timeout = SELF_TEST_TIMEOUT
    result: dict = {}

    def _run() -> None:
        try:
            exec(compile(source, "<generated-plugin>", "exec"), ns)
        except Exception as e:
            result["error"] = f"self_test 失败: 模块代码异常 {type(e).__name__}: {e}"
            return
        try:
            ns["self_test"]()
            result["ok"] = True
        except Exception as e:
            result["error"] = f"self_test 失败: {type(e).__name__}: {e}"

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return (False, f"self_test 超时（>{timeout:.0f}s，疑似死循环）")
    if result.get("ok"):
        return (True, "")
    return (False, str(result.get("error") or "self_test 失败"))


class WritePluginTool(Tool):
    """生成并装载一个模型自产插件（ASK；admit 管线全过才落盘）。"""

    name = "write_plugin"
    description = (
        "Generate a new plugin from code: validate it and run its self_test, "
        "then load it into this session (requires user approval). Follow the "
        "plugin format in your instructions."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "插件名（自动加 auto- 前缀）"},
            "summary": {"type": "string", "description": "一句话描述（进插件目录）"},
            "code": {"type": "string", "description": "Python 代码：协议实现 + apply(ctx)"},
            "test": {"type": "string", "description": "self_test() 函数体（会缩进 4 空格）"},
            "type": {
                "type": "string",
                "description": "插件类型（可选，默认 capability.tool）："
                               "capability.tool（工具）/ context.memory（上下文）"
                               " / lifecycle（生命周期钩子）",
            },
            "timeout": {"type": "number", "description": "工具超时秒数（可选，默认 60；仅工具类生效）"},
            "permissions": {
                "type": "array", "items": {"type": "string"},
                "description": "权限声明（可选：fs:read / fs:write / network / shell / process）",
            },
        },
        "required": ["name", "summary", "code", "test"],
    }

    def __init__(self, kernel: Any, agent: Any) -> None:
        self._kernel = kernel
        self._agent = agent

    @property
    def permission(self) -> Permission:
        return Permission(
            level=PermissionLevel.ASK,
            reason="create and load a new auto-* plugin into this session",
        )

    async def execute(
        self,
        name: str,
        summary: str,
        code: str,
        test: str,
        type: str = "capability.tool",
        timeout: Optional[float] = None,
        permissions: Optional[list] = None,
    ) -> ToolResult:
        plugin_id = _auto_id(name)
        # type -> 协议（未知拒绝，不走默认路由）；mount 由协议表派生
        proto, why_not = _resolve_protocol(type)
        if proto is None:
            return ToolResult(error=why_not)
        manifest = _build_manifest(summary, timeout, permissions, proto)
        problems, _ = validate_manifest(manifest)
        if problems:
            return ToolResult(error=f"manifest 校验失败: {'; '.join(problems)}")
        source = _assemble(manifest, code, test)
        ok, why = _admit(source, proto)
        if not ok:
            return ToolResult(error=why)
        # 落盘 + 会话热插
        directory = project_plugins_dir(self._kernel.workspace)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{plugin_id}.py"
        path.write_text(source, encoding="utf-8")
        ok, msg = self._kernel.load_plugin(plugin_id)
        if not ok:
            try:
                path.unlink()  # 装载失败 → 回滚文件，不留垃圾
            except OSError:
                pass
            return ToolResult(error=f"加载失败: {msg}")
        if self._agent is not None:
            self._agent._rebuild_tools()
        return ToolResult(output=f"plugin created & loaded: {plugin_id} (session)\n{msg}")


class TestPluginTool(Tool):
    """对已加载的文件插件重跑 self_test（验证/调试）。"""

    name = "test_plugin"
    description = (
        "Re-run the self_test of a loaded file plugin; returns PASS/FAIL with "
        "output. Use to verify a plugin still works."
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
        if info is None or info["phase"] != "active":
            return ToolResult(error=f"plugin not loaded: {name}")
        path = project_plugins_dir(self._kernel.workspace) / f"{name}.py"
        if not path.is_file():
            return ToolResult(error=f"no testable file for plugin: {name}")
        try:
            source = path.read_text(encoding="utf-8")
            # 协议取自插件 manifest（write 侧已强校验；手写文件按 type 路由）
            proto = route(info["manifest"].get("type"))
            ok, why = _admit(source, proto)
        except OSError as e:
            return ToolResult(error=f"read failed: {e}")
        return ToolResult(output=f"self_test {'PASS' if ok else 'FAIL'}: {why or 'ok'}")


class PromotePluginTool(Tool):
    """用户确认晋升一个 auto-* 插件（trust=user + 决策记账）。"""

    name = "promote_plugin"
    description = (
        "Promote a session-loaded auto-* plugin to a trusted user plugin "
        "(requires user approval). Records a promotion decision in the ledger."
    )
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }

    def __init__(self, kernel: Any) -> None:
        self._kernel = kernel

    @property
    def permission(self) -> Permission:
        return Permission(
            level=PermissionLevel.ASK,
            reason="promote an auto-* plugin to user-trusted",
        )

    async def execute(self, name: str) -> ToolResult:
        ok, msg = self._kernel.promote_plugin(name)
        return ToolResult(output=msg if ok else f"Error: {msg}")


if __name__ == "__main__":
    import asyncio
    import tempfile
    from pathlib import Path

    import openx.config as config_mod
    from openx.kernel import get_kernel, reset_kernel

    CODE = """\
from openx.tools.base import Tool, ToolResult

class GreetTool(Tool):
    name = "greet"
    description = "打招呼"
    async def execute(self, **kw):
        return ToolResult(output="hi " + str(kw.get("who", "you")))

def factory(host):
    return [GreetTool()]

def apply(ctx):
    ctx.register_tool_factory("greet", factory)
"""
    TEST = """\
assert factory(None)[0].name == "greet"
"""

    async def _check() -> None:
        with tempfile.TemporaryDirectory() as td:
            config_mod.SETTINGS_PATH = Path(td) / "settings.json"
            reset_kernel()
            ws = Path(td) / "ws"
            (ws / ".openx" / "plugins").mkdir(parents=True)
            k = get_kernel()
            k.ensure_loaded(str(ws))

            class _Agent:
                def __init__(self):
                    self.rebuilds = 0
                def _rebuild_tools(self):
                    self.rebuilds += 1

            agent = _Agent()
            # write_plugin：admit 全过 → 落盘 + 加载 + 重建
            r = await WritePluginTool(k, agent).execute(
                "greet", "打招呼插件", CODE, TEST, timeout=10, permissions=["fs:read"])
            assert r.success and "auto-greet" in r.output, r.error or r.output
            assert agent.rebuilds == 1
            assert (ws / ".openx" / "plugins" / "auto-greet.py").is_file()
            info = k.plugin_help("auto-greet")
            assert info["phase"] == "active" and info["manifest"]["trust"] == "auto"
            assert info["manifest"]["timeout"] == 10
            # 生成的工具可用（经注册表）
            assert k.registry("tools").get("greet") is not None
            # 坏代码：缺 apply → 拒
            r = await WritePluginTool(k, agent).execute(
                "bad", "坏插件", "x = 1", TEST)
            assert not r.success and "apply" in r.error
            # self_test 失败 → 拒
            r = await WritePluginTool(k, agent).execute(
                "badtest", "坏测试", CODE, "assert False")
            assert not r.success and "self_test 失败" in r.error
            # test_plugin：已加载的通过
            r = await TestPluginTool(k).execute("auto-greet")
            assert "PASS" in r.output, r.output
            # promote：auto-* 才能晋升，记决策事件
            sink = []
            k.attach_ledger(lambda e: sink.append(e), session="s1")
            r = await PromotePluginTool(k).execute("auto-greet")
            assert r.success and k.plugin_help("auto-greet")["manifest"]["trust"] == "user"
            assert any(e.type == "plugin_promoted" for e in sink)
            r = await PromotePluginTool(k).execute("builtin-tools")
            assert "only auto-" in r.output
            reset_kernel()

    asyncio.run(_check())
    print("openx/tools/write_plugin_tools.py OK ✓")
