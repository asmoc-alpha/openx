"""Skill 工具 —— 让模型按需加载 skill 正文（渐进披露的模型侧入口）。

对齐 Agent Skills / Claude Code 的技能模型：系统提示只注入 ``name + description``
的**目录**（见 :func:`openx.skills.build_skills_prompt`），正文在被用到时才加载。
用户可直接 ``/<skill-name>``；模型则调用本工具。

- 权限 ``ALLOW``：只读本地技能文件，无副作用；
- 副作用（把 skill 的 ``allowed-tools`` 挂为会话内存态 allow、执行
  ``!`cmd` `` 注入）全部由 ``agent.activate_skill`` 承担——本工具只做参数
  校验与错误语义化，保证「一处实现、两处入口」；
- 仅顶层 agent 持有（子代理不继承技能，与工具实例化同款口径）。
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

from ..permissions import Permission
from .base import Tool, ToolResult


class SkillTool(Tool):
    """按名加载一个已安装 skill 的正文指令。"""

    name = "skill"
    description = (
        "Load the full instructions of an installed skill by name. The system "
        "prompt lists available skills with their descriptions but NOT their "
        "bodies. When a task matches a skill's description, call this tool with "
        "that skill's name BEFORE doing the work, then follow the instructions "
        "it returns for the rest of the task. Pass user-supplied arguments "
        "through `arguments` (they substitute the skill's $ARGUMENTS)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Skill name exactly as listed in Available Skills.",
            },
            "arguments": {
                "type": "string",
                "description": (
                    "Optional free-form arguments substituted for the skill's "
                    "$ARGUMENTS placeholder."
                ),
            },
        },
        "required": ["name"],
    }

    def __init__(self, agent: Any):
        # 持有 agent：正文加载与副作用（allowed-tools 作用域、注入执行）
        # 统一走 agent.activate_skill，与 `/<skill-name>` 同一实现。
        self._agent = agent

    @property
    def permission(self) -> Permission:
        # 只读本地技能文件；真正的执行闸在注入命令（走 ToolExecutor）
        return Permission.allow()

    async def execute(self, name: str = "", arguments: str = "", **_: Any) -> ToolResult:
        name = (name or "").strip()
        if not name:
            return ToolResult(error="skill name required")
        try:
            text = await self._agent.activate_skill(name, arguments or "")
        except KeyError:
            available = ", ".join(sorted(getattr(self._agent, "skills", {}))) or "(none)"
            return ToolResult(error=f"skill not found: {name}. Available: {available}")
        return ToolResult(output=text or "(skill has no body)")


if __name__ == "__main__":
    import asyncio

    class _FakeAgent:
        """Duck-typed agent：只提供 skills 与 async activate_skill。"""

        def __init__(self, skills, fail=False):
            self.skills = skills
            self._fail = fail

        async def activate_skill(self, name, arguments=""):
            if self._fail or name not in self.skills:
                raise KeyError(name)
            return f"body of {name} / args={arguments}"

    async def _self_check():
        tool = SkillTool(_FakeAgent({"docker": object()}))
        assert tool.name == "skill"
        assert tool.permission.level.value == "allow"
        assert tool.parameters["required"] == ["name"]

        ok = await tool.execute(name="docker", arguments="a b")
        assert ok.success and ok.output == "body of docker / args=a b", ok
        assert tool.to_openai_schema()["function"]["name"] == "skill"

        # 空名 → 参数错误（不触碰 agent）
        empty = await tool.execute(name="  ")
        assert not empty.success and "name required" in empty.error, empty

        # 未找到 → 列出可用技能
        missing = await SkillTool(_FakeAgent({"docker": object(), "zeta": object()})).execute(
            name="nope"
        )
        assert not missing.success and "skill not found: nope" in missing.error
        assert "docker" in missing.error and "zeta" in missing.error, missing

        # 无技能时亦给出可读提示而非崩溃
        none = await SkillTool(_FakeAgent({})).execute(name="x")
        assert "(none)" in none.error, none

        # 正文为空 → 明确的占位输出
        class _Empty(_FakeAgent):
            async def activate_skill(self, name, arguments=""):
                return ""

        blank = await SkillTool(_Empty({})).execute(name="x")
        assert blank.success and blank.output == "(skill has no body)", blank

    asyncio.run(_self_check())
    print("openx/tools/skill_tool.py OK ✓")
