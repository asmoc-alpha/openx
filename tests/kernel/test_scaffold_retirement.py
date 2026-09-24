"""E4 退场机制：``scaffold_retired`` / ``scaffold_restored`` 决策 + 退场登记表 + 组合跳过。

覆盖：
- 仅带 scaffold 声明的插件可退场（普通能力插件 / 未知名被拒）；
- 退场：``scaffold_retired`` 上全局账本（带声明与 evidence）+ 组合跳过
  （PHASE_RETIRED，注册条目撤销，工具消失）；重复退场被拒；
- 回挂：``scaffold_restored`` 上账本 + 重新装载（工具回来）；
- 读面：``list_plugins`` 退场标记、``plugin_help`` 退场态 + 声明（未被导入时
  由账本条目回填）；
- 跨进程：新内核从全局账本折出退场集合（摘除不是删除，重启后仍退场）。

环境：kernel_env 已把 SETTINGS_PATH 与 GLOBAL_LEDGER_PATH 隔离到 tmp，
绝不触碰真实 ~/.openx。运行：``python -m pytest tests/kernel/test_scaffold_retirement.py -q``
"""

from __future__ import annotations

import textwrap

from openx.kernel import get_kernel, reset_kernel

from ._helpers import write_plugin

SCAFFOLD_SRC = textwrap.dedent('''\
    """E4：带脚手架声明的工具插件（可退场）。"""
    __openx_meta__ = {
        "type": "capability.tool", "mount": "loop.tool-call", "trust": "user",
        "summary": "历史压缩",
        "scaffold": {
            "compensates": "模型上下文有限",
            "exit_when": "长会话免压缩评测通过",
            "eval_set": "evals/long-session.jsonl",
        },
    }

    from openx.tools.base import Tool, ToolResult

    class CompactTool(Tool):
        name = "compact"
        description = "compress history"
        async def execute(self, **kw):
            return ToolResult(output="ok")

    def factory(host):
        return [CompactTool()]

    def apply(ctx):
        ctx.register_tool_factory("compact", factory)
''')

PLAIN_SRC = textwrap.dedent('''\
    """E4：普通能力插件（无 scaffold 声明，无退场资格）。"""
    __openx_meta__ = {"type": "capability.tool", "trust": "user", "summary": "普通"}
    from openx.tools.base import Tool, ToolResult

    class PlainTool(Tool):
        name = "plain"
        description = "p"
        async def execute(self, **kw):
            return ToolResult(output="ok")

    def factory(host):
        return [PlainTool()]

    def apply(ctx):
        ctx.register_tool_factory("plain", factory)
''')


class Sink:
    """收集决策事件的全局账本 sink（test_global_ledger 同款）。"""

    def __init__(self):
        self.events: list = []

    def __call__(self, event) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]

    def of(self, type_: str) -> list:
        return [e for e in self.events if e.type == type_]


def _tool_names(k) -> set[str]:
    reg = k.registry("tools")
    return {e.name for e in reg.entries()}


def test_retire_requires_scaffold_declaration(kernel_env):
    ws, _ = kernel_env
    write_plugin(ws, "plain", PLAIN_SRC)
    k = get_kernel()
    k.ensure_loaded(str(ws))

    ok, msg = k.retire_scaffold("plain")
    assert not ok and "not a scaffold" in msg
    ok, msg = k.retire_scaffold("nope")
    assert not ok and "not active" in msg


def test_retire_emits_decision_and_skips_composition(kernel_env):
    ws, _ = kernel_env
    write_plugin(ws, "histcompact", SCAFFOLD_SRC)
    k = get_kernel()
    sink = Sink()
    k.attach_global_ledger(sink)
    k.ensure_loaded(str(ws))
    assert "compact" in _tool_names(k)  # 装载前在册

    evidence = {"schema": "openx-retirement/v1", "verdict": "retire", "delta": 0.0}
    ok, msg = k.retire_scaffold("histcompact", evidence=evidence)
    assert ok and "retired" in msg

    # 决策全文上全局账本：带声明 + evidence
    retired = sink.of("scaffold_retired")
    assert len(retired) == 1
    payload = retired[0].payload
    assert payload["plugin"] == "histcompact"
    assert payload["compensates"] == "模型上下文有限"
    assert payload["eval_set"] == "evals/long-session.jsonl"
    assert payload["evidence"]["verdict"] == "retire"

    # 组合跳过：工具被撤销，插件标 retired
    assert "compact" not in _tool_names(k)
    entry = next(c for c in k.list_plugins() if c["id"] == "histcompact")
    assert entry["retired"] is True and entry["phase"] == "retired"
    assert entry["scaffold"] is True  # 声明仍可见（由账本条目回填）

    # 声明经 plugin_help 仍可读（插件本体未被导入）
    info = k.plugin_help("histcompact")
    assert info["retired"] is True
    assert info["scaffold"]["compensates"] == "模型上下文有限"
    assert info["scaffold"]["exit_when"] == "长会话免压缩评测通过"

    # 重复退场被拒
    ok, msg = k.retire_scaffold("histcompact")
    assert not ok and "already retired" in msg


def test_restore_reincludes_scaffold(kernel_env):
    ws, _ = kernel_env
    write_plugin(ws, "histcompact", SCAFFOLD_SRC)
    k = get_kernel()
    sink = Sink()
    k.attach_global_ledger(sink)
    k.ensure_loaded(str(ws))
    k.retire_scaffold("histcompact")
    assert "compact" not in _tool_names(k)

    ok, msg = k.restore_scaffold("histcompact", reason="model regressed")
    assert ok and "restored" in msg
    restored = sink.of("scaffold_restored")
    assert restored and restored[0].payload["reason"] == "model regressed"

    # 回挂：重新装载，工具回来，退场标记消失
    assert "compact" in _tool_names(k)
    entry = next(c for c in k.list_plugins() if c["id"] == "histcompact")
    assert entry["retired"] is False and entry["phase"] == "active"

    # 未退场的不能回挂
    ok, msg = k.restore_scaffold("histcompact")
    assert not ok and "not retired" in msg


def test_retirement_persists_across_kernel_restart(kernel_env):
    ws, _ = kernel_env
    write_plugin(ws, "histcompact", SCAFFOLD_SRC)
    k = get_kernel()
    k.ensure_loaded(str(ws))
    assert k.retire_scaffold("histcompact")[0]  # 默认文件 sink → ledger.jsonl

    reset_kernel()  # "重启"：新内核从全局账本折出退场集合
    k2 = get_kernel()
    k2.ensure_loaded(str(ws))
    entry = next(c for c in k2.list_plugins() if c["id"] == "histcompact")
    assert entry["retired"] is True
    assert "compact" not in _tool_names(k2)


# ── CLI 命令面：/scaffolds ────────────────────────────────────────


class _CapConsole:
    """捕获 /scaffolds 输出的鸭子 console。"""

    def __init__(self):
        self.infos: list[str] = []
        self.successes: list[str] = []
        self.warnings: list[str] = []
        self.raw_lines: list[str] = []
        self.raw = self

    def print(self, *args, **kwargs):
        self.raw_lines.append(" ".join(str(a) for a in args))

    def print_info(self, message): self.infos.append(message)
    def print_success(self, message): self.successes.append(message)
    def print_warning(self, message): self.warnings.append(message)


class _FakeAgent:
    def __init__(self, ws):
        self.workspace = ws
        self.rebuilds = 0

    def _rebuild_tools(self):
        self.rebuilds += 1


async def test_scaffolds_command_lists_retires_restores(kernel_env):
    from openx.app.cli import commands

    ws, _ = kernel_env
    write_plugin(ws, "histcompact", SCAFFOLD_SRC)
    agent = _FakeAgent(ws)
    stub = _CapConsole()

    # 列表：声明可见
    assert await commands.handle_slash_command("scaffolds", agent, stub, []) is True
    blob = "\n".join(stub.raw_lines)
    assert "histcompact" in blob and "模型上下文有限" in blob

    # 退场：成功 + 触发工具重建
    assert await commands.handle_slash_command(
        "scaffolds", agent, stub, ["retire", "histcompact"]
    ) is True
    assert stub.successes and agent.rebuilds == 1
    k = get_kernel()
    assert next(c for c in k.list_plugins() if c["id"] == "histcompact")["retired"]

    # 回挂
    assert await commands.handle_slash_command(
        "scaffolds", agent, stub, ["restore", "histcompact"]
    ) is True
    assert next(c for c in k.list_plugins() if c["id"] == "histcompact")["retired"] is False

    # 非脚手架被拒（警告，不崩）
    stub2 = _CapConsole()
    assert await commands.handle_slash_command(
        "scaffolds", agent, stub2, ["retire", "ghost"]
    ) is True
    assert stub2.warnings

