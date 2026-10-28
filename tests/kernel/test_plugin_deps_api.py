"""S2b/S2c：dependencies 装载期校验 + manifest `api` 版本与未知键警告。

覆盖：
- 依赖齐备 → 装载成功；缺失 → FAILED 且 error 写明缺谁（不再无声）；
- 依赖环（A↔B）→ 双双 FAILED；
- manifest `api` 匹配装载 / 不匹配拒载（message 说明支持版本）；
- 未知顶层 manifest 键 → 警告（此前静默忽略）；无 api 键零警告（兼容）。

运行：``python -m pytest tests/kernel/test_plugin_deps_api.py -q``
"""

from __future__ import annotations

import textwrap

from openx.kernel import get_kernel
from openx.kernel.assembly.manifest import PLUGIN_API_VERSION, validate_manifest

from ._helpers import write_plugin


def _plugin(summary: str, deps: list | None = None, api: int | None = None) -> str:
    meta = {"summary": summary, "trust": "user"}
    if deps is not None:
        meta["dependencies"] = deps
    if api is not None:
        meta["api"] = api
    import json

    return (
        "__openx_meta__ = " + json.dumps(meta, ensure_ascii=False) + "\n"
        "def apply(ctx):\n    pass\n"
    )


def _phase(k, pid: str) -> str:
    return next(c for c in k.list_plugins() if c["id"] == pid)["phase"]


def test_dependencies_satisfied_loads(kernel_env):
    ws, _ = kernel_env
    write_plugin(ws, "base", _plugin("base"))
    write_plugin(ws, "dependent", _plugin("needs base", deps=["base"]))
    k = get_kernel()
    k.ensure_loaded(str(ws))
    # 发现序（用户目录字典序）恰好 base 先于 dependent；语义上依赖已 ACTIVE
    assert _phase(k, "base") == "active"
    assert _phase(k, "dependent") == "active"


def test_missing_dependency_fails_loudly(kernel_env):
    ws, _ = kernel_env
    write_plugin(ws, "orphan", _plugin("wants ghost", deps=["ghost"]))
    k = get_kernel()
    k.ensure_loaded(str(ws))
    assert _phase(k, "orphan") == "failed"
    assert "ghost" in k.plugin_help("orphan")["error"]


def test_dependency_cycle_fails_both(kernel_env):
    ws, _ = kernel_env
    # 发现序保证 a 先试：a 依赖 b（未装载）→ a 失败；b 依赖 a（failed）→ b 失败
    write_plugin(ws, "a-cycle", _plugin("a", deps=["b-cycle"]))
    write_plugin(ws, "b-cycle", _plugin("b", deps=["a-cycle"]))
    k = get_kernel()
    k.ensure_loaded(str(ws))
    assert _phase(k, "a-cycle") == "failed"
    assert _phase(k, "b-cycle") == "failed"


def test_session_load_rejects_missing_dependency(kernel_env):
    ws, _ = kernel_env
    write_plugin(ws, "orphan", _plugin("wants ghost", deps=["ghost"]))
    k = get_kernel()
    k.ensure_loaded(str(ws))
    ok, msg = k.load_plugin("orphan")
    assert not ok and "ghost" in msg


def test_manifest_api_version_gate():
    # 匹配 → 干净通过
    assert validate_manifest({"summary": "x", "api": PLUGIN_API_VERSION}) == ([], [])
    # 不匹配 → 拒载 + message 说明支持版本
    problems, _ = validate_manifest({"summary": "x", "api": PLUGIN_API_VERSION + 9})
    assert problems and str(PLUGIN_API_VERSION) in problems[0]
    # 未声明 → 零警告（存量插件兼容）
    assert validate_manifest({"summary": "x"}) == ([], [])


def test_unknown_manifest_key_warns_not_rejects():
    problems, warnings = validate_manifest({"summary": "x", "permisions": ["fs:read"]})
    assert problems == []
    assert any("permisions" in w for w in warnings)  # 拼错的键不再静默失效


def test_plugin_with_api_field_loads(kernel_env):
    ws, _ = kernel_env
    write_plugin(ws, "pinned", _plugin("pinned", api=PLUGIN_API_VERSION))
    k = get_kernel()
    k.ensure_loaded(str(ws))
    assert _phase(k, "pinned") == "active"
