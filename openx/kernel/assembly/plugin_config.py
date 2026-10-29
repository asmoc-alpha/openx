"""插件配置校验（S5a）——manifest ``config``（JSON Schema 子集）对用户配置。

对标 Cordis 的 ``export const Config`` + 行级 ``config``：插件用 JSON
Schema 声明可配置面，装载激活时对照校验用户的 ``plugins.config.<id>``。

**子集声明**：本仓库零依赖（无 jsonschema），这里实现的校验词有——
``type``（object 顶层 + 属性级 string/number/integer/boolean/array/object）、
``required``、``enum``、``properties``、``items``、``minimum``/``maximum``、
``default``（归并默认值）。schema 之外的键被忽略；schema 自身畸形只记
problem 不抛。足够表达插件的配置面，覆盖不了的部分如实是"未校验"。
"""

from __future__ import annotations

from typing import Any

__all__ = ["validate_config", "merge_defaults"]

_TYPES: dict[str, tuple] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def _type_ok(value: Any, expected: str) -> bool:
    types = _TYPES.get(expected)
    if types is None:
        return True  # 未知类型词不校验（宽松；不假装认识）
    if expected in ("number", "integer") and isinstance(value, bool):
        return False  # bool 是 int 子类，JSON 语义上不是数
    return isinstance(value, types)


def validate_config(
    config: Any, schema: dict, *, plugin: str = ""
) -> list[str]:
    """用户配置对照 schema → problems（非空 = 拒载，对标 Cordis 激活校验）。"""
    problems: list[str] = []
    if not isinstance(schema, dict):
        return [f"{plugin}: config schema is not a dict"] if plugin else \
            ["config schema is not a dict"]
    if config is None:
        config = {}
    if not isinstance(config, dict):
        return [f"{plugin}: config must be a JSON object"]
    prefix = f"{plugin}: " if plugin else ""

    declared = schema.get("properties")
    if not isinstance(declared, dict):
        declared = {}
    for key in schema.get("required", []) or []:
        if not isinstance(key, str):
            continue
        if key not in config:
            problems.append(f"{prefix}config missing required key {key!r}")
    for key, value in config.items():
        prop = declared.get(key)
        if not isinstance(prop, dict):
            if declared:  # 声明了 properties 却出现未声明键 → 提示
                problems.append(
                    f"{prefix}config.{key} is not declared in the schema"
                )
            continue
        expected = prop.get("type")
        if isinstance(expected, str) and not _type_ok(value, expected):
            problems.append(
                f"{prefix}config.{key} must be {expected}, "
                f"got {type(value).__name__}"
            )
            continue
        if "enum" in prop and isinstance(prop["enum"], list) and value not in prop["enum"]:
            problems.append(
                f"{prefix}config.{key}={value!r} not in enum {prop['enum']!r}"
            )
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in prop and isinstance(prop["minimum"], (int, float)) \
                    and value < prop["minimum"]:
                problems.append(f"{prefix}config.{key} < minimum {prop['minimum']}")
            if "maximum" in prop and isinstance(prop["maximum"], (int, float)) \
                    and value > prop["maximum"]:
                problems.append(f"{prefix}config.{key} > maximum {prop['maximum']}")
        if expected == "array" and isinstance(value, list) \
                and isinstance(prop.get("items"), dict):
            item_type = prop["items"].get("type")
            if isinstance(item_type, str):
                for i, item in enumerate(value):
                    if not _type_ok(item, item_type):
                        problems.append(
                            f"{prefix}config.{key}[{i}] must be {item_type}"
                        )
    return problems


def merge_defaults(schema: dict, config: dict) -> dict:
    """把 schema 里声明的 ``default`` 归并进用户配置（用户值优先）。

    只归并顶层 ``properties`` 的标量/对象 default——不做深递归（插件的
    配置面就该浅）。schema 非 dict / properties 非 dict → 原样返回。
    """
    if not isinstance(schema, dict) or not isinstance(config, dict):
        return dict(config) if isinstance(config, dict) else {}
    out = dict(config)
    props = schema.get("properties")
    if not isinstance(props, dict):
        return out
    for key, prop in props.items():
        if key in out or not isinstance(prop, dict) or "default" not in prop:
            continue
        out[key] = prop["default"]
    return out


if __name__ == "__main__":
    schema = {
        "type": "object",
        "properties": {
            "endpoint": {"type": "string"},
            "retries": {"type": "integer", "minimum": 0, "maximum": 10,
                        "default": 3},
            "mode": {"type": "string", "enum": ["fast", "safe"]},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["endpoint"],
    }
    # 合法
    assert validate_config({"endpoint": "http://x", "retries": 2,
                            "mode": "fast", "tags": ["a"]}, schema) == []
    # 缺 required / 类型错 / 枚举外 / 越界 / 数组元素类型
    assert any("endpoint" in p for p in validate_config({}, schema))
    assert any("must be integer" in p for p in validate_config(
        {"endpoint": "x", "retries": "many"}, schema))
    assert any("not in enum" in p for p in validate_config(
        {"endpoint": "x", "mode": "zzz"}, schema))
    assert any("minimum" in p for p in validate_config(
        {"endpoint": "x", "retries": -1}, schema))
    assert any("[0]" in p for p in validate_config(
        {"endpoint": "x", "tags": [1]}, schema))
    # bool 不是 integer
    assert validate_config({"endpoint": "x", "retries": True}, schema)
    # 默认归并：用户值优先
    merged = merge_defaults(schema, {"endpoint": "x"})
    assert merged == {"endpoint": "x", "retries": 3}
    assert merge_defaults(schema, {"endpoint": "x", "retries": 7})["retries"] == 7
    print("openx/kernel/assembly/plugin_config.py OK ✓")
