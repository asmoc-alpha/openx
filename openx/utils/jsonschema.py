"""Mini JSON Schema validator for structured output (no dependencies).

为 ``structured_output`` 工具提供轻量校验：只覆盖实用子集——
``type``（含类型数组）/ ``enum`` / ``required`` / ``properties``（递归）/
``items``（单 schema 递归）。未识别的关键字一律忽略（宽容模式，
与 LLM 生态里"schema 是约束提示而非铁律"的实践一致）。

返回第一个错误的可读描述（含 JSON 路径），全部通过返回 ``None``。
绝不抛异常：schema 本身非法时降级为"不校验"。
"""

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

from typing import Any, Optional


def _type_name(value: Any) -> str:
    """JSON 风格类型名（报错信息用）。"""
    if value is None:
        return "null"
    if isinstance(value, bool):  # bool 必须先于 int 判断（bool ⊂ int）
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _matches_type(value: Any, type_name: str) -> bool:
    """单个 JSON Schema 类型判定。"""
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "null":
        return value is None
    return True  # 未知类型名 → 宽容放行


def validate(instance: Any, schema: Any, path: str = "$") -> Optional[str]:
    """校验 ``instance`` 是否符合 ``schema``（JSON Schema 实用子集）。

    返回 ``None`` 表示通过；否则返回第一个错误的描述（如
    ``"$.age: expected integer, got string"``）。``schema`` 非 dict 时
    视为无约束（宽容降级——坏 schema 绝不阻断结构化输出）。
    """
    if not isinstance(schema, dict):
        return None

    # type：字符串或字符串数组
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_matches_type(instance, t) for t in types):
            want = "/".join(str(t) for t in types)
            return f"{path}: expected {want}, got {_type_name(instance)}"

    # enum：精确成员判定
    if "enum" in schema and isinstance(schema["enum"], list):
        if instance not in schema["enum"]:
            return f"{path}: value {instance!r} not in enum {schema['enum']!r}"

    # object：required + properties 递归
    if isinstance(instance, dict):
        for key in schema.get("required") or []:
            if key not in instance:
                return f"{path}: missing required property {key!r}"
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for key, sub in properties.items():
                if key in instance:
                    err = validate(instance[key], sub, f"{path}.{key}")
                    if err:
                        return err

    # array：items 递归（单 schema 应用于每个元素）
    if isinstance(instance, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(instance):
                err = validate(item, items, f"{path}[{i}]")
                if err:
                    return err

    return None


if __name__ == "__main__":
    s = {
        "type": "object",
        "required": ["name", "tags"],
        "properties": {
            "name": {"type": "string"},
            "age": {"type": "integer"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "kind": {"enum": ["bug", "feature"]},
        },
    }
    good = {"name": "x", "age": 3, "tags": ["a"], "kind": "bug"}
    assert validate(good, s) is None
    assert "missing required property 'tags'" in validate({"name": "x"}, s)
    assert "expected string" in validate({"name": 1, "tags": []}, s)
    assert "expected integer, got boolean" in validate(
        {"name": "x", "tags": [], "age": True}, s)
    assert "tags[1]" in validate({"name": "x", "tags": ["a", 2]}, s)
    assert "not in enum" in validate({"name": "x", "tags": [], "kind": "y"}, s)
    assert validate({"anything": 1}, "not-a-schema") is None  # 宽容降级
    print("openx/utils/jsonschema.py OK ✓")
