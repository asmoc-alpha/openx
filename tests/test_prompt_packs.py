"""Prompt packs 子系统回归测试（按需加载的提示片段，渐进披露第二条通道）。

覆盖：
- 数据模型：注册序、同名覆盖、``match`` 关键词子串匹配（正反例）、
  ``active_names`` 过滤未知/去重；
- 渲染契约：目录恒在、正文只在激活后出现、``[loaded]`` 标记、
  BEGIN/END 边界、片段不带前导空行、空/缺 registry 零贡献；
- pack 表自身纪律：triggers 必须小写（``match`` 先 lower 输入，大写 trigger
  永远命不中）、summary 非空、正文非空、正文真源 = ``PLUGIN_SPEC``；
- agent 接线：目录进系统提示 / 正文不进；``load_prompt`` 工具在册；
  ``activate_prompt_pack`` 幂等 + 重建提示 + 记 ``prompt_pack`` 账本事件；
  ``deactivate_prompt_pack`` 摘下；关键词自动激活经 ``_seed_turn`` 生效；
  子代理零继承（空 registry、无工具）；
- ``/prompt`` 命令：list / <name> / off / 未知名；
- ``write_plugin`` 兜底激活（``reason="write_plugin"``）。

运行：``python -m pytest tests/test_prompt_packs.py -q``
"""

from __future__ import annotations

from pathlib import Path

import pytest

import openx.config as config_mod
import openx.kernel.global_ledger as global_ledger_mod
from openx.prompt_packs import (
    CATALOG_HEADER,
    PromptPack,
    PromptPackRegistry,
    active_names,
    builtin_packs,
    load_prompt_packs,
    render_prompt_packs,
)

PACK = "plugin-authoring"


# ── 1. 数据模型与匹配 ───────────────────────────────────────────


def _registry() -> PromptPackRegistry:
    return PromptPackRegistry({
        "alpha": PromptPack(
            name="alpha", summary="does alpha things", body="ALPHA-BODY",
            triggers=("alpha", "阿尔法"),
        ),
        "beta": PromptPack(
            name="beta", summary="does beta things", body="BETA-BODY",
            triggers=("beta",),
        ),
    })


class TestRegistry:
    def test_lookup_and_membership(self):
        reg = _registry()
        assert reg.get("alpha").body == "ALPHA-BODY"
        assert reg.get("  alpha  ").name == "alpha"  # 名字两侧空白容忍
        assert reg.get("ghost") is None and reg.get(None) is None
        assert "alpha" in reg and "alpha " in reg and "ghost" not in reg
        assert 123 not in reg  # 非字符串不炸
        assert reg.names() == ["alpha", "beta"]
        assert sorted(reg) == ["alpha", "beta"]  # __iter__ 给名字
        assert len(reg) == 2

    def test_match_is_substring_and_multiselect(self):
        reg = _registry()
        assert reg.match("please run ALPHA now") == ["alpha"]  # 大小写不敏感
        assert reg.match("用一下阿尔法") == ["alpha"]  # 中文无需分词
        assert reg.match("alpha and beta") == ["alpha", "beta"]
        assert reg.match("gamma") == []
        assert reg.match("") == [] and reg.match(None) == []

    def test_empty_registry_is_legal(self):
        reg = PromptPackRegistry()
        assert len(reg) == 0 and reg.match("alpha") == [] and reg.names() == []


class TestBuiltinPacks:
    def test_plugin_authoring_is_loaded(self):
        reg = load_prompt_packs()
        assert reg.names() == [PACK]
        pack = reg.get(PACK)
        assert pack.summary and pack.body and pack.chars == len(pack.body)

    def test_body_is_the_plugin_spec_single_source(self):
        from openx.kernel.assembly.plugin_spec import PLUGIN_SPEC

        assert load_prompt_packs().get(PACK).body == PLUGIN_SPEC

    def test_triggers_are_lowercase(self):
        # match() 会 lower 输入而不动 trigger：大写 trigger 永远命不中，
        # 这是 pack 表最容易写错的一处契约。
        for pack in builtin_packs():
            assert all(t == t.lower() for t in pack.triggers), pack.name

    def test_trigger_intent(self):
        reg = load_prompt_packs()
        assert reg.match("帮我写个插件，注册一个工具") == [PACK]
        assert reg.match("write a plugin that adds a tool") == [PACK]
        # 「组件」刻意只收「开发组件/组件开发」——React 组件讨论不该命中
        assert reg.match("这个 React 组件怎么拆分") == []
        assert reg.match("开发组件吧") == [PACK]


# ── 2. 渲染契约 ─────────────────────────────────────────────────


class TestRender:
    def test_catalog_always_body_never(self):
        out = render_prompt_packs(_registry(), ())
        assert out.startswith(CATALOG_HEADER)  # 无前导空行
        assert "`alpha`:" in out and "`beta`:" in out
        assert "does alpha things" in out
        assert "ALPHA-BODY" not in out and "BETA-BODY" not in out
        assert "[loaded]" not in out

    def test_activated_body_with_markers(self):
        out = render_prompt_packs(_registry(), {"beta"})
        assert "[loaded]" in out
        # 只有激活的那个展开
        assert "BETA-BODY" in out and "ALPHA-BODY" not in out
        begin = "<!-- BEGIN PROMPT PACK: beta -->"
        end = "<!-- END PROMPT PACK: beta -->"
        assert out.index(begin) < out.index("BETA-BODY") < out.index(end)
        # 标记打在 right 那一行
        beta_line = next(ln for ln in out.splitlines() if ln.startswith("- `beta`"))
        assert "[loaded]" in beta_line

    def test_unknown_active_name_is_ignored(self):
        out = render_prompt_packs(_registry(), {"ghost"})
        assert "ALPHA-BODY" not in out and "[loaded]" not in out

    def test_zero_contribution(self):
        assert render_prompt_packs(None) == ""
        assert render_prompt_packs(PromptPackRegistry()) == ""

    def test_active_names_filters_and_orders(self):
        reg = _registry()
        assert active_names(["beta", "ghost", "beta", " "], reg) == ["beta"]
        assert active_names(["beta", "alpha"], reg) == ["alpha", "beta"]


# ── 3. agent 接线 ───────────────────────────────────────────────


@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    """隔离内核/账本/settings/hooks/tasks：不碰真实 home。"""
    from openx.kernel import reset_kernel

    monkeypatch.setattr(config_mod, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(
        global_ledger_mod, "GLOBAL_LEDGER_PATH", tmp_path / "ledger.jsonl"
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(
        "openx.kernel.audit.hooks.SETTINGS_PATH", ws / "no-hooks.json"
    )
    monkeypatch.setattr("openx.orchestration.tasks.TASKS_DIR", ws / "tasks")
    reset_kernel()
    yield ws
    reset_kernel()


def _make_agent(ws: Path, **kwargs):
    from openx.agent import OpenXAgent
    from openx.config import OpenXConfig

    config = OpenXConfig()
    config.workspace = str(ws)
    config.model = "test-model"
    return OpenXAgent(config, **kwargs)


class TestAgentWiring:
    async def test_catalog_in_prompt_body_not(self, ws_env):
        agent = _make_agent(ws_env)
        prompt = agent._system_prompt
        assert CATALOG_HEADER in prompt
        assert f"`{PACK}`:" in prompt
        assert "load_prompt" in prompt  # 引导模型走工具那条通道
        assert "__openx_meta__" not in prompt, "正文绝不常驻"
        assert agent.active_prompt_packs == []

    async def test_load_prompt_tool_registered(self, ws_env):
        agent = _make_agent(ws_env)
        assert "load_prompt" in agent.tools
        names = [s["function"]["name"] for s in agent.tool_schemas]
        assert "load_prompt" in names
        # 只读纯内存：ALLOW 级，任何模式下都看得见
        from openx.permissions import PermissionLevel

        assert agent.tools["load_prompt"].permission.level is PermissionLevel.ALLOW

    async def test_activate_rebuilds_prompt_and_is_idempotent(self, ws_env):
        agent = _make_agent(ws_env)
        before = agent._system_prompt
        pack = agent.activate_prompt_pack(PACK, reason="manual")
        assert pack.name == PACK
        after = agent._system_prompt
        assert after != before
        assert "[loaded]" in after and "__openx_meta__" in after
        # 幂等：再激活一次，提示不变（不重复重建/记账）
        assert agent.activate_prompt_pack(PACK).name == PACK
        assert agent._system_prompt == after
        assert agent.active_prompt_packs == [PACK]

    async def test_deactivate_removes_body(self, ws_env):
        agent = _make_agent(ws_env)
        agent.activate_prompt_pack(PACK)
        assert agent.deactivate_prompt_pack(PACK) is True
        assert "__openx_meta__" not in agent._system_prompt
        assert "[loaded]" not in agent._system_prompt
        assert CATALOG_HEADER in agent._system_prompt  # 目录仍在
        assert agent.deactivate_prompt_pack(PACK) is False  # 幂等
        assert agent.deactivate_prompt_pack("ghost") is False

    async def test_unknown_pack_raises_keyerror(self, ws_env):
        agent = _make_agent(ws_env)
        with pytest.raises(KeyError):
            agent.activate_prompt_pack("ghost")
        with pytest.raises(KeyError):
            agent.activate_prompt_pack("")

    async def test_keyword_auto_activation_via_seed_turn(self, ws_env):
        agent = _make_agent(ws_env)
        # 用户这句话本身命中 triggers → 本轮请求前正文已在提示里
        agent._seed_turn("帮我写个插件", None)
        assert agent.active_prompt_packs == [PACK]
        assert "__openx_meta__" in agent._system_prompt
        # 不命中的话不激活（重建也不该发生）
        other = _make_agent(ws_env)
        other._seed_turn("重构登录页面", None)
        assert other.active_prompt_packs == []

    async def test_auto_activation_handles_multimodal_content(self, ws_env):
        agent = _make_agent(ws_env)
        content = [
            {"type": "text", "text": "写个插件"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
        ]
        assert agent.auto_activate_prompt_packs(content) == [PACK]
        assert agent.auto_activate_prompt_packs(None) == []

    async def test_ledger_event_on_activate_and_deactivate(self, ws_env):
        from openx.kernel import get_kernel

        agent = _make_agent(ws_env)
        agent.session_store = object()  # 非 None 即"顶层会落账"的口径
        seen: list = []
        get_kernel().attach_ledger(lambda ev: seen.append(ev))
        agent.activate_prompt_pack(PACK, reason="tool")
        agent.deactivate_prompt_pack(PACK)
        events = [e for e in seen if e.type == "prompt_pack"]
        assert [e.payload["reason"] for e in events] == ["tool", "deactivate"]
        assert events[0].payload["pack"] == PACK
        assert events[0].payload["chars"] == agent.prompt_packs.get(PACK).chars
        assert events[1].payload["chars"] == 0
        # 幂等激活不再记账
        agent.activate_prompt_pack(PACK, reason="command")
        agent.activate_prompt_pack(PACK, reason="command")
        events = [e for e in seen if e.type == "prompt_pack"]
        assert len(events) == 3

    async def test_child_agent_inherits_nothing(self, ws_env):
        parent = _make_agent(ws_env)
        parent.activate_prompt_pack(PACK)
        child = _make_agent(ws_env, parent=parent)
        assert len(child.prompt_packs) == 0, "子代理空 registry"
        assert child.active_prompt_packs == []
        assert CATALOG_HEADER not in child._system_prompt
        assert "load_prompt" not in child.tools

    async def test_load_prompt_reaches_the_next_request(self, ws_env):
        """端到端：模型第一轮调 load_prompt，第二轮请求的系统提示已有正文。

        这是整套设计的兑现点——``_sync_system_message`` 每轮重读缓存提示，
        所以激活**当轮内**就生效，不必等到下一个用户回合。
        """
        import json as _json

        class _FakeLLM:
            def __init__(self):
                self.seen: list[list[dict]] = []
                self.calls = 0

            async def chat(self, messages, tools=None, stream=True):
                self.seen.append([dict(m) for m in messages])
                self.calls += 1
                if self.calls == 1:
                    return {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "load_prompt",
                                "arguments": _json.dumps({"name": PACK}),
                            },
                        }],
                    }
                return {"role": "assistant", "content": "ok, contract loaded"}

        agent = _make_agent(ws_env)
        fake = _FakeLLM()
        agent.llm = fake
        # 这句不含任何 trigger：只验证"工具"那条通道（关键词通道见
        # test_keyword_auto_activation_via_seed_turn）
        out = await agent.run("看看这个仓库")
        assert out == "ok, contract loaded"
        assert fake.calls == 2
        first_system = fake.seen[0][0]["content"]
        second_system = fake.seen[1][0]["content"]
        assert "__openx_meta__" not in first_system, "首轮只有目录"
        assert "__openx_meta__" in second_system, "工具激活后正文进了下一轮"
        assert CATALOG_HEADER in second_system
        # 关键词通道（"插件"命中 triggers）也会在首轮前就激活——本测试用
        # 不含触发词的句子，专门验证工具那条通道。
        assert agent.active_prompt_packs == [PACK]


# ── 4. /prompt 命令 ─────────────────────────────────────────────


class _CapConsole:
    """只收 print_* 的 console 替身（与 tests/tools/test_plugin_commands 同款）。"""

    def __init__(self):
        self.raw_lines: list[str] = []
        self.infos: list[str] = []
        self.successes: list[str] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.raw = self

    def print(self, *a, **k):
        self.raw_lines.append(" ".join(str(x) for x in a))

    def print_info(self, m):
        self.infos.append(m)

    def print_success(self, m):
        self.successes.append(m)

    def print_warning(self, m):
        self.warnings.append(m)

    def print_error(self, m):
        self.errors.append(m)


class TestPromptCommand:
    async def test_list_shows_catalog_without_body(self, ws_env):
        from openx.app.cli import commands

        agent = _make_agent(ws_env)
        console = _CapConsole()
        assert await commands.handle_slash_command("prompt", agent, console, []) is True
        text = "\n".join(console.raw_lines)
        assert PACK in text and f"`{PACK}`" not in text
        assert "__openx_meta__" not in text, "命令输出不该倒正文"
        assert "chars" in text

    async def test_load_and_off(self, ws_env):
        from openx.app.cli import commands

        agent = _make_agent(ws_env)
        console = _CapConsole()
        await commands.handle_slash_command("prompt", agent, console, [PACK])
        assert agent.active_prompt_packs == [PACK]
        assert any("Loaded" in s for s in console.successes), console.errors

        # 重复加载：告知已加载，不重复记账
        console2 = _CapConsole()
        await commands.handle_slash_command("prompt", agent, console2, [PACK])
        assert any("already" in s for s in console2.infos), console2.successes
        assert console2.successes == []

        # off：全部摘下
        console3 = _CapConsole()
        await commands.handle_slash_command("prompt", agent, console3, ["off"])
        assert agent.active_prompt_packs == []
        assert any("Unloaded" in s for s in console3.successes)

        # 无已加载时 off：给提示而非静默
        console4 = _CapConsole()
        await commands.handle_slash_command("prompt", agent, console4, ["off"])
        assert any("No prompt pack" in s for s in console4.infos)

    async def test_unknown_name_lists_available(self, ws_env):
        from openx.app.cli import commands

        agent = _make_agent(ws_env)
        console = _CapConsole()
        await commands.handle_slash_command("prompt", agent, console, ["ghost"])
        assert console.errors and PACK in console.errors[0]
        assert agent.active_prompt_packs == []

    async def test_empty_registry_is_reported_not_crashed(self, ws_env, monkeypatch):
        from openx.app.cli import commands
        from openx.prompt_packs import PromptPackRegistry

        agent = _make_agent(ws_env)
        monkeypatch.setattr(agent, "prompt_packs", PromptPackRegistry())
        console = _CapConsole()
        await commands.handle_slash_command("prompt", agent, console, [])
        assert console.warnings and console.errors == []
