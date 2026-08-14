"""StructuredOutputTool — 结构化输出的唯一出口。

子代理被要求以符合 JSON Schema 的对象（而非自由文本）交付最终结果时，
本工具注入其工具集：模型调用 ``structured_output(data=...)``，校验通过
即把结果写入所属 agent 并结束该轮运行；校验失败返回错误消息，模型在
同一轮循环内自行修正重试（与 Claude Code 的 StructuredOutput 同款契约）。

设计要点
========
- ``parameters`` 在实例级动态生成：``data`` 的 schema 即调用方传入的
  JSON Schema——函数调用的参数层校验由模型提供商完成第一道，本工具的
  :func:`~openx.utils.jsonschema.validate` 完成第二道（确定性、可测试）。
- 权限 ``ALLOW``：纯结构性出口，无副作用，绝不弹窗。
- 仅注入**带 schema 的子代理**：顶层 agent 与无 schema 子代理的工具集
  里永远看不到它（见 ``agent._build_tools``）。
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

from typing import Any, Optional

from .base import Tool, ToolResult
from ..permissions import Permission
from ..utils.jsonschema import validate


class StructuredOutputTool(Tool):
    """以符合 JSON Schema 的 ``data`` 交付子代理的最终结果。"""

    name = "structured_output"
    description = (
        "Deliver your final result as structured data. Call this exactly "
        "once when your task is complete, with 'data' conforming to the "
        "required JSON Schema. This call ends your run — do all your work "
        "(reading, searching, editing) before calling it. Plain-text final "
        "answers are discarded when this tool is present."
    )

    def __init__(self, agent: Any, schema: dict) -> None:
        # agent = 所属子代理（惰性持有）：校验通过后写入其
        # _structured_result 并由此终止运行循环。
        self._agent = agent
        self._schema = schema
        # 实例级参数表：data 的 schema 即调用方约束。实例属性遮蔽类属性，
        # to_openai_schema 读 self.parameters 自然拿到动态版本。
        self.parameters = {
            "type": "object",
            "properties": {
                "data": {
                    **schema,
                    "description": (
                        schema.get("description", "")
                        + " The final result, conforming to the JSON Schema."
                    ).strip(),
                },
            },
            "required": ["data"],
        }

    @property
    def permission(self) -> Permission:
        # 结构性出口，无副作用：免询问。
        return Permission.allow()

    async def execute(self, data: Any = None, **_: Any) -> ToolResult:
        if data is None:
            return ToolResult(
                error="Missing required field 'data'. Call structured_output "
                      "with your final result as 'data'."
            )
        error: Optional[str] = validate(data, self._schema)
        if error:
            # 错误消息即修正指令：模型在下一轮重试，无需外部循环
            return ToolResult(
                error=f"Schema validation failed: {error}. Fix the reported "
                      f"fields and call structured_output again."
            )
        self._agent._structured_result = data
        return ToolResult(
            output="Structured output accepted. Your task is complete."
        )


if __name__ == "__main__":
    import asyncio

    class _FakeAgent:
        _structured_result = None

    _schema = {
        "type": "object",
        "required": ["answer"],
        "properties": {"answer": {"type": "string"}},
    }
    _agent = _FakeAgent()
    _tool = StructuredOutputTool(_agent, _schema)
    assert _tool.name == "structured_output"
    # 动态参数表：data 携带调用方 schema
    _props = _tool.to_openai_schema()["function"]["parameters"]["properties"]
    assert _props["data"]["required"] == ["answer"]

    async def _self_check():
        bad = await _tool.execute(data={"answer": 42})
        assert not bad.success and "expected string" in bad.error
        assert _agent._structured_result is None  # 校验失败绝不写入

        missing = await _tool.execute()
        assert not missing.success and "Missing required field" in missing.error

        ok = await _tool.execute(data={"answer": "hi"})
        assert ok.success and _agent._structured_result == {"answer": "hi"}

    asyncio.run(_self_check())
    print("openx/tools/structured_output.py OK ✓")
