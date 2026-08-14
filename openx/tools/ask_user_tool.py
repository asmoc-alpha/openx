"""AskUser 工具 —— 参考 claude-code 的 AskUserQuestionTool。

让 agent 在执行过程中主动向用户提多选问题，用于：
1. 收集偏好或需求；
2. 澄清模糊指令；
3. 在实现方向上征求决策。

设计要点
========
- 工具持有 ``console`` 引用，``execute`` 调用 ``console.ask_user_question``
  阻塞读取用户选择，再把答案作为 ``ToolResult.output`` 返回给模型。
- 权限为 ``ALLOW``：提问本身无副作用。
- 单选/多选由 ``multi_select`` 控制；用户始终可选“Other”自定义输入
  （由 console 的交互选择器提供）。
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

import json
from typing import Any

from .base import Tool, ToolResult
from ..permissions import Permission


def _normalize_options(options: Any) -> list[dict[str, Any]] | None:
    """把模型给的 ``options`` 规范化成 ``[{"label": str, "description"?: str}]``。

    宽容对待真实的模型输出形状：
    - 对象数组（schema 约定）：``[{"label": ..., "description": ...}]``；
    - 字符串数组（常见简化输出）：``["A", "B"]``；
    - JSON 编码字符串（偶发双层序列化）：``'[{"label": "A"}]'``；
    - 其他标量：尽力字符串化。
    无法解析（非列表 / JSON 解析失败）→ ``None``；无可用 label 的项被丢弃。
    Returns normalized option dicts, or ``None`` when unusable.
    """
    if isinstance(options, str):
        try:
            options = json.loads(options)
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(options, (list, tuple)):
        return None

    normalized: list[dict[str, Any]] = []
    for item in options:
        if isinstance(item, dict):
            label = str(item.get("label") or "").strip()
            if not label:
                continue  # 无 label 的选项无法展示，直接丢弃
            entry: dict[str, Any] = {"label": label}
            description = item.get("description")
            if description:
                entry["description"] = str(description)
            normalized.append(entry)
        else:
            label = str(item).strip()
            if label:
                normalized.append({"label": label})
    return normalized


def _coerce_bool(value: Any) -> bool:
    """把 ``multi_select`` 强转成 bool：字符串 "true"/"false" 按字面解析。

    模型偶发给出 ``"true"`` / ``"false"`` 字符串——Python 里两者皆为真值，
    若不解析，``"false"`` 也会误开多选。
    Truthy strings are misleading ("false" is truthy) — parse literals.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


class AskUserTool(Tool):
    """向用户提出多选问题并等待回答。"""

    name = "ask_user"
    description = (
        "Ask the user a multiple-choice question to gather information, clarify "
        "ambiguity, or decide between approaches. The user can always pick "
        "'Other' to type a custom answer. Use multi_select=true to allow "
        "multiple selections."
    )
    parameters = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The complete question to ask.",
            },
            "options": {
                "type": "array",
                "description": "2-4 mutually exclusive choices (unless multi_select).",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Short option label."},
                        "description": {
                            "type": "string",
                            "description": "Explanation of this option.",
                        },
                    },
                    "required": ["label"],
                },
            },
            "multi_select": {
                "type": "boolean",
                "description": "Allow multiple selections. Default: false.",
            },
        },
        "required": ["question", "options"],
    }

    def __init__(self, console: Any):
        # 持有 ui.console.Console 引用，用于交互式提问
        self._console = console

    @property
    def permission(self) -> Permission:
        return Permission.allow()

    async def execute(
        self,
        question: Any = "",
        options: Any = None,
        multi_select: Any = False,
        **_extra: Any,
    ) -> ToolResult:
        # 防御性规范化：模型输出形状多变（字符串数组 / JSON 字符串 / 缺字段），
        # 任何畸形入参都不得抛出异常——ToolExecutor 会把异常转成错误结果，
        # 模型随即重试 ask_user，每次重试都刷一行 ● ask_user，屏幕疯狂打印
        # 而问题始终弹不出来。宁可返回清晰的错误结果引导模型自我纠正。
        # Never raise out of execute(): exceptions make the model retry in a
        # loop, flooding the screen while no question ever renders.
        opts = _normalize_options(options)
        if opts is None:
            return ToolResult(
                error="ask_user: 'options' must be a JSON array of 2-4 options "
                      "(objects with 'label'/'description' or plain strings). "
                      "Provide at least 2 options."
            )
        if len(opts) < 2:
            return ToolResult(
                error="ask_user requires at least 2 options — got "
                      f"{len(opts)} usable option(s). Provide 2-4 options."
            )
        # 防御性裁剪：选项数限制在 2~4（claude-code 同款约束）
        opts = opts[:4]

        q = str(question or "").strip()
        if not q:
            return ToolResult(
                error="ask_user requires a non-empty 'question' string."
            )

        # 调用 console 的交互选择器（阻塞读取用户输入）。弹窗异常（终端丢失
        # 等）同样兜底成错误结果——绝不外抛触发重试循环。
        try:
            selected = self._console.ask_user_question(
                question=q,
                options=opts,
                multi_select=_coerce_bool(multi_select),
            )
        except Exception as e:
            return ToolResult(
                error=f"ask_user dialog failed ({type(e).__name__}: {e}). "
                      "The user could not be asked; proceed with a "
                      "reasonable default instead of retrying."
            )

        # selected 为 list[str]（label 列表）；格式化为模型可读文本
        if isinstance(selected, (list, tuple)):
            if not selected:
                return ToolResult(output="User did not choose any option.")
            joined = "; ".join(selected)
            return ToolResult(output=f"User selected: {joined}")
        return ToolResult(output=f"User answered: {selected}")


if __name__ == "__main__":
    # 独立调试：绝不真的提问 —— 用非阻塞 mock console 模拟用户选择
    import asyncio

    class _MockConsole:
        def ask_user_question(self, question, options, multi_select=False):
            return [options[0]["label"]]  # 立即返回，不阻塞

    async def _self_check():
        tool = AskUserTool(console=_MockConsole())
        print(f"{tool.name}: {tool.description}")
        r = await tool.execute(
            question="Pick one", options=[{"label": "A"}, {"label": "B"}]
        )
        assert r.success and "A" in r.output, r.output
        print(r.output)

    asyncio.run(_self_check())
    print("openx/tools/ask_user_tool.py OK ✓")
