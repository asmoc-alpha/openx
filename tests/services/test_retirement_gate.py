"""E4 退场评测门 · ``retirement_gate``（带 vs 摘除 → 退场裁决 + 证据）。

覆盖：
- 摘除不掉点 → 准许退场（``retire``）；掉点 → 保持（``keep``）；
- 容差允许小幅掉点；
- 两种度量形状（计数 ``{success,total}`` / 已算好 ``{success_rate}``）；
- 证据 payload 可 JSON 序列化（要落账本）。

运行：``python -m pytest tests/services/test_retirement_gate.py -q``
"""

from __future__ import annotations

import json

from openx.services.retirement_gate import SCHEMA, compare_success, success_rate


def test_no_drop_allows_retirement():
    v = compare_success({"success": 8, "total": 10}, {"success": 8, "total": 10})
    assert v["verdict"] == "retire" and v["delta"] == 0.0
    assert v["schema"] == SCHEMA


def test_improvement_allows_retirement():
    v = compare_success({"success": 6, "total": 10}, {"success": 9, "total": 10})
    assert v["verdict"] == "retire" and v["delta"] > 0


def test_drop_keeps_scaffold():
    v = compare_success({"success": 9, "total": 10}, {"success": 5, "total": 10})
    assert v["verdict"] == "keep" and v["delta"] < 0


def test_tolerance_permits_small_drop():
    v = compare_success(
        {"success": 10, "total": 10}, {"success": 9, "total": 10}, tolerance=0.15
    )
    assert v["verdict"] == "retire"


def test_success_rate_shapes():
    assert success_rate({"success_rate": 0.75}) == 0.75
    assert success_rate({"success": 3, "total": 4}) == 0.75
    assert success_rate({"total": 0}) == 0.0
    assert success_rate("nope") == 0.0
    # delta 与两侧成功率都入证据
    v = compare_success({"success_rate": 0.5}, {"success_rate": 0.6})
    assert v["with_scaffold"]["success_rate"] == 0.5
    assert v["without_scaffold"]["success_rate"] == 0.6


def test_evidence_is_serializable():
    v = compare_success({"success": 8, "total": 10}, {"success": 7, "total": 10})
    json.loads(json.dumps(v))  # 落账本证据要可序列化
