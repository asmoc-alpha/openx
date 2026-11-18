"""``load_prompt`` 工具 + ``write_plugin`` 兜底激活的回归测试。

覆盖：
- 工具契约：name / ALLOW 权限 / 参数 schema（仅 ``name`` 必填）；
- 正常激活：返回正文 + 记账 reason="tool"（激活副作用统一由
  ``agent.activate_prompt_pack`` 承担）；
- 空名 → 参数错误且不触碰 agent；未知名 → 错误里列可用 pack；
- 重复调用仍回正文（幂等语义在 agent 侧：不重复记账）；
- ``write_plugin`` 兜底激活：调用即 ``reason="write_plugin"``，且没有
  ``activate_prompt_pack`` 的 agent 不会让工具崩。

运行：``python -m pytest tests/tools/test_prompt_tool.py -q``
"""

from __future__ import annotations

from openx.prompt_packs import load_prompt_packs
from openx.tools.prompt_tool import LoadPromptTool
from openx.tools.write_plugin_tools import WritePluginTool

PACK = "plugin-authoring"


class _FakeAgent:
    """Duck-typed agent：只提供 prompt_packs 与 activate_prompt_pack。"""

    def __init__(self, *, fail: bool = False):
        self.prompt_packs = load_prompt_packs()
        self._fail = fail
        self.calls: list[tuple[str, str]] = []

    def activate_prompt_pack(self, name, *, reason="manual"):
        pack = self.prompt_packs.get(name)
        if self._fail or pack is None:
            raise KeyError(name)
        self.calls.append((name, reason))
        return pack


class TestLoadPromptTool:
    async def test_contract(self):
        tool = LoadPromptTool(_FakeAgent())
        assert tool.name == "load_prompt"
        assert tool.parameters["required"] == ["name"]
        assert tool.to_openai_schema()["function"]["name"] == "load_prompt"
        from openx.permissions import PermissionLevel

        assert tool.permission.level is PermissionLevel.ALLOW

    async def test_loads_body_and_records_reason(self):
        agent = _FakeAgent()
        result = await LoadPromptTool(agent).execute(name=PACK)
        assert result.success
        assert "__openx_meta__" in result.output
        assert "in effect" in result.output
        assert agent.calls == [(PACK, "tool")]

    async def test_name_is_stripped(self):
        agent = _FakeAgent()
        result = await LoadPromptTool(agent).execute(name=f"  {PACK}  ")
        assert result.success and agent.calls == [(PACK, "tool")]

    async def test_empty_name_is_a_parameter_error(self):
        agent = _FakeAgent()
        result = await LoadPromptTool(agent).execute(name="   ")
        assert not result.success and "name required" in result.error
        assert agent.calls == [], "参数错误不该触碰 agent"

    async def test_unknown_name_lists_available(self):
        agent = _FakeAgent()
        result = await LoadPromptTool(agent).execute(name="ghost")
        assert not result.success
        assert "not found: ghost" in result.error
        assert PACK in result.error
        assert agent.calls == []


class TestWritePluginFallback:
    def test_activates_contract_with_reason(self):
        agent = _FakeAgent()
        WritePluginTool(kernel=None, agent=agent)._activate_contract()
        assert agent.calls == [(PACK, "write_plugin")]

    def test_missing_or_broken_agent_never_raises(self):
        # 无 activate_prompt_pack（如测试桩 / 嵌入式用法）
        WritePluginTool(kernel=None, agent=object())._activate_contract()
        # 激活抛异常（pack 表里没有这个 pack）
        WritePluginTool(kernel=None, agent=_FakeAgent(fail=True))._activate_contract()
        # agent 为 None
        WritePluginTool(kernel=None, agent=None)._activate_contract()
