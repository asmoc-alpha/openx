"""E4 退场评测门 · **策略面**——"带脚手架 vs 摘除"的成功率对比 → 退场裁决。

设计（`docs/design/openx-self-evolution-design.md` §3.2）：脚手架退场三步的
第①步是**评测门**——在 ``scaffold.eval_set`` 上对比"带脚手架 vs 摘除"的任务
成功率：**不掉点 → 准许退场；掉点 → 保持**。评测是回归测试，能力棘轮的双向性
靠它维持。

**边界（收窄版）**：本模块只做**裁决与证据**——给定两侧的成功率度量，算出
退场是否准许并产出证据 payload。真实**跑评测任务集**（把 ``eval_set`` 的每个
任务在带/不带脚手架两种配置下各跑一遍、判定成败）需要模型调用与一个评测执行
器，属独立切片，不在此模块。输入度量由调用方（人工评测 / 未来的 runner）供给，
本模块据此出**可审计的证据**——"声明本身不是证据，跑出来的才是"。

`compare_success` 的输出直接作为 ``kernel.retire_scaffold(evidence=...)`` 的
证据落全局账本（``scaffold_retired`` payload 的 ``evidence`` 字段）。
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

from typing import Any

#: 证据 schema 版本（消费方据此判断字段形状）。
SCHEMA = "openx-retirement/v1"

#: 退场容差：摘除后成功率允许的最大跌幅（0.0 = 严格不掉点）。
DEFAULT_TOLERANCE = 0.0


def success_rate(metrics: dict[str, Any]) -> float:
    """一次评测的度量 -> 成功率（0.0–1.0）。

    接受两种形状：``{"success": int, "total": int}``（计数）或
    ``{"success_rate": float}``（已算好的比率，原样返回）。无度量 -> 0.0。
    """
    if not isinstance(metrics, dict):
        return 0.0
    if "success_rate" in metrics:
        try:
            return float(metrics["success_rate"])
        except (TypeError, ValueError):
            return 0.0
    total = metrics.get("total")
    try:
        total = int(total)
    except (TypeError, ValueError):
        return 0.0
    if total <= 0:
        return 0.0
    try:
        success = int(metrics.get("success", 0))
    except (TypeError, ValueError):
        success = 0
    return max(0.0, min(1.0, success / total))


def compare_success(
    with_scaffold: dict[str, Any],
    without_scaffold: dict[str, Any],
    tolerance: float = DEFAULT_TOLERANCE,
) -> dict[str, Any]:
    """对比"带脚手架 vs 摘除"的成功率 → 退场裁决 + 证据 payload。

    退场**准许**当且仅当摘除后成功率**不掉点**：
    ``rate(without) >= rate(with) - tolerance``。掉点则"保持"（脚手架留住），
    证据照记——本次评测的结论本身就是决策依据。

    返回 ``{schema, verdict, delta, tolerance, with_scaffold, without_scaffold}``，
    ``verdict`` ∈ ``{"retire", "keep"}``；``delta = rate(without) - rate(with)``
    （正 = 摘除后更好/持平，负 = 掉点）。直接作为 ``retire_scaffold`` 的证据。
    """
    with_rate = success_rate(with_scaffold)
    without_rate = success_rate(without_scaffold)
    delta = without_rate - with_rate
    verdict = "retire" if delta >= -abs(float(tolerance)) else "keep"
    return {
        "schema": SCHEMA,
        "verdict": verdict,
        "delta": round(delta, 6),
        "tolerance": float(tolerance),
        "with_scaffold": {
            "success_rate": round(with_rate, 6),
            "metrics": dict(with_scaffold) if isinstance(with_scaffold, dict) else {},
        },
        "without_scaffold": {
            "success_rate": round(without_rate, 6),
            "metrics": dict(without_scaffold) if isinstance(without_scaffold, dict) else {},
        },
    }


if __name__ == "__main__":
    import json as _json

    # 摘除不掉点 -> 准许退场
    _v = compare_success({"success": 8, "total": 10}, {"success": 8, "total": 10})
    assert _v["verdict"] == "retire" and _v["delta"] == 0.0
    assert _v["schema"] == SCHEMA

    # 摘除后更好 -> 更该退
    _v = compare_success({"success": 6, "total": 10}, {"success": 9, "total": 10})
    assert _v["verdict"] == "retire" and _v["delta"] > 0

    # 掉点 -> 保持
    _v = compare_success({"success": 9, "total": 10}, {"success": 5, "total": 10})
    assert _v["verdict"] == "keep" and _v["delta"] < 0

    # 容差：小幅掉点可容忍
    _v = compare_success({"success": 10, "total": 10},
                         {"success": 9, "total": 10}, tolerance=0.15)
    assert _v["verdict"] == "retire"

    # 已算好的比率形状
    assert success_rate({"success_rate": 0.75}) == 0.75
    assert success_rate({"success": 3, "total": 4}) == 0.75
    assert success_rate({"total": 0}) == 0.0 and success_rate("nope") == 0.0

    # 可 JSON 序列化（要落账本证据）
    _json.loads(_json.dumps(_v))

    print("openx/services/retirement_gate.py OK ✓")
