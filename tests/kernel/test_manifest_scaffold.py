"""E1 脚手架演进声明进 Manifest 测试。

覆盖：
- ``validate_manifest`` 对 ``scaffold`` 块的形状约束——必答项
  （compensates/exit_when）缺失或类型错 = 拒载；词汇外 fallback / 未知键
  只警告不拒（与 type/mount/permission 同纪律）；
- ``scaffold_of`` 读取入口；
- 端到端暴露：manifest 带声明的插件经五阶段加载后，
  ``list_plugins`` 带轻量标记、``plugin_help`` / ``inventory`` 带声明全量。

环境：kernel_env fixture（临时 workspace + 隔离 SETTINGS_PATH + 新内核）。
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from openx.kernel import get_kernel
from openx.kernel.assembly.manifest import scaffold_of, validate_manifest

# ── 纯校验层（无需内核）────────────────────────────────────────


def test_scaffold_valid_passes():
    meta = {
        "summary": "历史压缩",
        "scaffold": {
            "compensates": "模型上下文有限",
            "exit_when": "长会话免压缩评测通过",
            "eval_set": "evals/long-session.jsonl",
            "fallback": "reinstall-on-regression",
        },
    }
    assert validate_manifest(meta) == ([], [])
    assert scaffold_of(meta)["exit_when"] == "长会话免压缩评测通过"
    assert scaffold_of({}) == {}
    assert scaffold_of("not-a-dict") == {}


def test_scaffold_required_fields_reject():
    # 空块 / 缺 exit_when → 拒载（标准三：答不出讣告就没有脚手架资格）
    for block in ({}, {"compensates": "x"}, {"exit_when": "y"}):
        problems, _ = validate_manifest({"scaffold": block})
        assert problems
    problems, _ = validate_manifest({"scaffold": {"compensates": "x",
                                                  "exit_when": "  "}})
    assert any("exit_when" in p for p in problems)  # 空白串不算答


def test_scaffold_bad_shape_rejects():
    assert validate_manifest({"scaffold": "x"})[0] == \
        ["manifest.scaffold must be a dict"]
    problems, _ = validate_manifest(
        {"scaffold": {"compensates": "a", "exit_when": "b", "eval_set": 1}}
    )
    assert any("eval_set" in p for p in problems)


def test_scaffold_unknown_values_warn_only():
    problems, warnings = validate_manifest(
        {"scaffold": {"compensates": "a", "exit_when": "b",
                      "fallback": "nope", "frobnicate": 1}}
    )
    assert problems == []  # 不拒
    assert any("fallback" in w for w in warnings)
    assert any("frobnicate" in w for w in warnings)


# ── 端到端暴露（五阶段加载后）──────────────────────────────────

_SCAFFOLD_PLUGIN = textwrap.dedent('''\
    """E1：带脚手架声明的插件（补偿模型短板）。"""
    __openx_meta__ = {
        "type": "capability.tool", "mount": "loop.tool-call", "trust": "user",
        "summary": "历史压缩",
        "scaffold": {
            "compensates": "模型上下文有限",
            "exit_when": "长会话免压缩评测通过",
            "eval_set": "evals/long-session.jsonl",
            "fallback": "reinstall-on-regression",
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


def _write_plugin(ws: Path, name: str, body: str) -> None:
    d = ws / ".openx" / "plugins"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.py").write_text(body, encoding="utf-8")


def test_scaffold_surfaced_after_load(kernel_env):
    ws, _ = kernel_env
    _write_plugin(ws, "histcompact", _SCAFFOLD_PLUGIN)
    k = get_kernel()
    k.ensure_loaded(str(ws))

    entry = next(c for c in k.list_plugins() if c["id"] == "histcompact")
    assert entry["scaffold"] is True  # 轻量标记

    help_ = k.plugin_help("histcompact")
    assert help_["scaffold"]["compensates"] == "模型上下文有限"
    assert help_["scaffold"]["exit_when"] == "长会话免压缩评测通过"
    assert help_["manifest_warnings"] == []

    info = next(i for i in k.inventory() if i.id == "histcompact")
    assert info.scaffold["fallback"] == "reinstall-on-regression"


def test_plain_plugin_has_no_scaffold_marker(kernel_env):
    ws, _ = kernel_env
    _write_plugin(ws, "plain", _SCAFFOLD_PLUGIN.replace(
        '    "scaffold": {\n'
        '        "compensates": "模型上下文有限",\n'
        '        "exit_when": "长会话免压缩评测通过",\n'
        '        "eval_set": "evals/long-session.jsonl",\n'
        '        "fallback": "reinstall-on-regression",\n'
        '    },\n', ""
    ))
    k = get_kernel()
    k.ensure_loaded(str(ws))
    entry = next(c for c in k.list_plugins() if c["id"] == "plain")
    assert entry["scaffold"] is False
    assert k.plugin_help("plain")["scaffold"] == {}


def test_scaffold_incomplete_rejects_load(kernel_env):
    ws, _ = kernel_env
    _write_plugin(ws, "halfscaffold", _SCAFFOLD_PLUGIN.replace(
        '        "compensates": "模型上下文有限",\n', ""
    ))
    k = get_kernel()
    k.ensure_loaded(str(ws))
    info = k.plugin_help("halfscaffold")
    assert info["phase"] == "failed"          # 缺必答项 → 拒载
    assert "compensates" in info["error"]
