"""S4a 协议钉扎：manifest 可声明 `protocol` 并强校验（`find()` 由此获得消费方）。

覆盖：
- `protocol` 与 type 匹配 → 装载成功（投影带 api/protocol）；
- 未知协议 → 拒载（message 列出已知协议）；
- 协议与 type 错配 → 拒载（message 写明两边的 type）；
- 不声明 → 零回归（默认路由）；`protocol` 非串拒载；
- api / protocol 进 `list_plugins` 与 `plugin_help` 投影。

运行：``python -m pytest tests/kernel/test_protocol_pinning.py -q``
"""

from __future__ import annotations

import json

from openx.kernel import get_kernel, manifest_api_version
from openx.kernel.assembly.manifest import validate_manifest

from ._helpers import write_plugin


def _plugin(meta_extra: dict) -> str:
    return (
        "__openx_meta__ = " + json.dumps(meta_extra) + "\n"
        "def apply(ctx):\n    pass\n"
    )


def _phase(k, pid: str) -> str:
    return next(c for c in k.list_plugins() if c["id"] == pid)["phase"]


def test_matching_protocol_pins_and_loads(kernel_env):
    ws, _ = kernel_env
    write_plugin(ws, "pinned", _plugin(
        {"summary": "pinned", "type": "capability.tool", "protocol": "tool/v1"}))
    k = get_kernel()
    k.ensure_loaded(str(ws))
    assert _phase(k, "pinned") == "active"
    row = next(c for c in k.list_plugins() if c["id"] == "pinned")
    assert row["protocol"] == "tool/v1"
    assert row["api"] == manifest_api_version()
    help_ = k.plugin_help("pinned")
    assert help_["protocol"] == "tool/v1" and help_["api"] == manifest_api_version()


def test_unknown_protocol_refuses(kernel_env):
    ws, _ = kernel_env
    write_plugin(ws, "future", _plugin(
        {"summary": "x", "type": "capability.tool", "protocol": "tool/v9"}))
    k = get_kernel()
    k.ensure_loaded(str(ws))
    assert _phase(k, "future") == "failed"
    assert "tool/v9" in k.plugin_help("future")["error"]
    assert "tool/v1" in k.plugin_help("future")["error"]


def test_protocol_type_mismatch_refuses(kernel_env):
    ws, _ = kernel_env
    write_plugin(ws, "mismatch", _plugin(
        {"summary": "x", "type": "capability.tool", "protocol": "ui/v1"}))
    k = get_kernel()
    k.ensure_loaded(str(ws))
    assert _phase(k, "mismatch") == "failed"
    err = k.plugin_help("mismatch")["error"]
    assert "ui.panel" in err and "capability.tool" in err


def test_protocol_non_string_refuses(kernel_env):
    ws, _ = kernel_env
    write_plugin(ws, "nonstr", _plugin({"summary": "x", "protocol": 7}))
    k = get_kernel()
    k.ensure_loaded(str(ws))
    assert _phase(k, "nonstr") == "failed"


def test_no_protocol_declaration_zero_regression(kernel_env):
    ws, _ = kernel_env
    write_plugin(ws, "plain", _plugin({"summary": "plain"}))
    k = get_kernel()
    k.ensure_loaded(str(ws))
    assert _phase(k, "plain") == "active"
    row = next(c for c in k.list_plugins() if c["id"] == "plain")
    assert row["protocol"] == ""


def test_validate_manifest_protocol_directly():
    ok = {"summary": "x", "type": "lifecycle", "protocol": "lifecycle/v1"}
    assert validate_manifest(ok) == ([], [])
    bad = {"summary": "x", "protocol": "nope/v1"}
    problems, _ = validate_manifest(bad)
    assert problems and "unknown manifest.protocol" in problems[0]
