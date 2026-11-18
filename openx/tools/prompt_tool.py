"""``load_prompt`` 工具 —— 按需加载 prompt pack 正文（渐进披露的模型侧入口）。

系统提示只常驻**目录**（``prompt_packs.render_prompt_packs``：一条 pack 一行
说明），正文在被用到时才进上下文。三条激活通道里本工具是模型主动的那条：
用户可 ``/prompt <name>``，请求命中关键词则由 agent 自动激活。

- 权限 ``ALLOW``：纯内存操作，无文件/网络副作用——激活只是把一个名字加进
  会话内的集合，副作用（重建系统提示、记一条 ``prompt_pack`` 账本事件）
  全部由 ``agent.activate_prompt_pack`` 承担（"一处实现、两处入口"）；
- 仅顶层 agent 持有（子代理不继承提示包，与工具实例化同款口径）；
- 幂等：重复调用同一 pack 不重复记账、不重复重建提示，仍返回正文供当轮使用。
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


class LoadPromptTool(Tool):
    """按名加载一个 prompt pack 的正文（目录常驻，正文按需）。"""

    name = "load_prompt"
    description = (
        "Load the full text of a built-in prompt pack by name. The system prompt "
        "lists available packs with one-line summaries but NOT their bodies. Call "
        "this BEFORE doing work a pack covers (e.g. `plugin-authoring` before "
        "writing a plugin or component, since write_plugin validates against that "
        "exact contract), then follow the returned instructions for the rest of "
        "the task."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Prompt pack name exactly as listed in Prompt Packs.",
            },
        },
        "required": ["name"],
    }

    def __init__(self, agent: Any):
        # 激活 + 提示重建 + 记账统一走 agent.activate_prompt_pack，
        # 与 /prompt <name> 同一实现。
        self._agent = agent

    @property
    def permission(self) -> Permission:
        # 纯内存：只往会话内的激活集合里加一个名字
        return Permission.allow()

    async def execute(self, name: str = "", **_: Any) -> ToolResult:
        name = (name or "").strip()
        if not name:
            return ToolResult(error="prompt pack name required")
        try:
            pack = self._agent.activate_prompt_pack(name, reason="tool")
        except KeyError:
            available = ", ".join(sorted(getattr(self._agent, "prompt_packs", {}))) or (
                "(none)"
            )
            return ToolResult(
                error=f"prompt pack not found: {name}. Available: {available}"
            )
        return ToolResult(
            output=(
                f"[prompt pack `{name}` is now in effect]\n\n{pack.body.strip()}"
            )
        )


if __name__ == "__main__":
    import asyncio

    from ..prompt_packs import load_prompt_packs

    class _FakeAgent:
        """Duck-typed agent：只提供 prompt_packs 与 activate_prompt_pack。"""

        def __init__(self, registry, fail=False):
            self.prompt_packs = registry
            self._fail = fail
            self.calls: list[tuple[str, str]] = []

        def activate_prompt_pack(self, name, *, reason="manual"):
            pack = self.prompt_packs.get(name)
            if self._fail or pack is None:
                raise KeyError(name)
            self.calls.append((name, reason))
            return pack

    async def _self_check():
        registry = load_prompt_packs()
        agent = _FakeAgent(registry)
        tool = LoadPromptTool(agent)
        assert tool.name == "load_prompt"
        assert tool.permission.level.value == "allow"
        assert tool.parameters["required"] == ["name"]
        assert tool.to_openai_schema()["function"]["name"] == "load_prompt"

        ok = await tool.execute(name="plugin-authoring")
        assert ok.success and "__openx_meta__" in ok.output, ok
        assert "in effect" in ok.output
        assert agent.calls == [("plugin-authoring", "tool")]

        # 幂等：再调一次仍返回正文（激活语义由 agent 保证不重复记账）
        again = await tool.execute(name=" plugin-authoring ")
        assert again.success and "__openx_meta__" in again.output

        # 空名 → 参数错误（不触碰 agent）
        empty = await tool.execute(name="  ")
        assert not empty.success and "name required" in empty.error, empty

        # 未知名 → 列出可用 pack
        missing = await tool.execute(name="ghost")
        assert not missing.success and "not found: ghost" in missing.error
        assert "plugin-authoring" in missing.error, missing

        # 无 pack 时亦给出可读提示而非崩溃
        none = await LoadPromptTool(_FakeAgent(load_prompt_packs(), fail=True)).execute(
            name="x"
        )
        assert not none.success and "(none)" not in none.error  # 有内置包时列名
        assert "plugin-authoring" in none.error

    asyncio.run(_self_check())
    print("openx/tools/prompt_tool.py OK ✓")
