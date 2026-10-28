"""K7 prompt 收口测试：context/v1 征集的 priority 排序 + payload（按形参容量）
+ 内置 prompt 片段（builtin-context）接线到真 agent 系统提示。

覆盖：
- priority 升序（小者先）；同 priority 保注册序；
- payload 按**形参容量**传（零参不传 / 收得下就传 / 带默认值两边都安全）；
- builtin-context 三片段：memory_hints 与 host 无关恒在，instructions / skills
  无 host 时零贡献；
- 真 agent（OpenXAgent）系统提示含 OPENX.md 正文 / 记忆说明 / skills 目录
  （且 skills **正文**不进提示--渐进披露）；
- 零引用破洞已修：agent 不再 import MEMORY_INSTRUCTIONS / build_skills_prompt。

环境：kernel_env fixture（临时 workspace + SETTINGS_PATH + 新内核）。
"""

from __future__ import annotations

import inspect
import json

from ._helpers import merge_settings
import textwrap

from openx.builtin.context import MEMORY_INSTRUCTIONS
from openx.kernel import get_kernel
from openx.kernel.assembly.context import PromptHost, call_with_payload
from openx.services.assembly import collect_context_fragments

# ── 插件样本 ─────────────────────────────────────────────────────


def _ctx_plugin(name: str, fragment: str, priority: int, *, arg: bool = False) -> str:
    """一个 context 插件：``arg=False`` -> 零参 contribute；否则收一个形参。"""
    sig = "(host)" if arg else "()"
    return textwrap.dedent(f'''\
        __openx_meta__ = {{"type": "context.memory", "summary": "{name}"}}
        def contribute{sig}:
            return {fragment!r}
        def apply(ctx):
            ctx.register_context("{name}", contribute, priority={priority})
    ''')


def _session_plugins(ws, settings, bodies: dict) -> object:
    """写插件文件 + boot 禁用 -> 会话内手动 load（可控加载序）。"""
    d = ws / ".openx" / "plugins"
    d.mkdir(parents=True, exist_ok=True)
    for name, body in bodies.items():
        (d / f"{name}.py").write_text(body, encoding="utf-8")
    merge_settings(settings, {"plugins": {"disabled": list(bodies)}})
    k = get_kernel()
    k.ensure_loaded(str(ws))
    return k


def _plugin_frags(frags) -> list[str]:
    """剔掉内置片段，只留插件贡献（便于断言相对序）。"""
    return [f for f in frags if f != MEMORY_INSTRUCTIONS]


# ── priority 排序 ────────────────────────────────────────────────


class TestPriorityOrdering:
    def test_lower_priority_collected_first(self, kernel_env):
        ws, settings = kernel_env
        k = _session_plugins(ws, settings, {
            "late": _ctx_plugin("late", "LATE_FRAGMENT", 200),
            "early": _ctx_plugin("early", "EARLY_FRAGMENT", 50),
        })
        assert k.load_plugin("late")[0]
        assert k.load_plugin("early")[0]

        # 加载序是 late -> early，但 priority 50 的 early 先于 200 的 late
        frags = _plugin_frags(collect_context_fragments(k))
        assert frags.index("EARLY_FRAGMENT") < frags.index("LATE_FRAGMENT")

    def test_tie_keeps_registration_order(self, kernel_env):
        ws, settings = kernel_env
        k = _session_plugins(ws, settings, {
            "tie_a": _ctx_plugin("tie_a", "TIE_A", 100),
            "tie_b": _ctx_plugin("tie_b", "TIE_B", 100),
        })
        assert k.load_plugin("tie_a")[0]
        assert k.load_plugin("tie_b")[0]

        frags = _plugin_frags(collect_context_fragments(k))
        assert frags.index("TIE_A") < frags.index("TIE_B")

    def test_builtin_precedes_default_priority_plugins(self, kernel_env):
        ws, settings = kernel_env
        k = _session_plugins(ws, settings, {
            "late": _ctx_plugin("late", "LATE_FRAGMENT", 200),
        })
        assert k.load_plugin("late")[0]

        frags = collect_context_fragments(k)
        assert frags.index(MEMORY_INSTRUCTIONS) < frags.index("LATE_FRAGMENT")


# ── payload 按形参容量 ───────────────────────────────────────────


class TestCallWithPayload:
    def test_zero_arg_called_without_payload(self):
        assert call_with_payload(lambda: "x", object()) == "x"

    def test_accepting_arg_receives_payload(self):
        sentinel = object()
        assert call_with_payload(lambda host: host is sentinel, sentinel) is True

    def test_none_payload_is_not_passed(self):
        # payload 为 None -> 零参调用（带默认值的回调因此两边都安全）
        assert call_with_payload(lambda host=None: "none-arg", None) == "none-arg"

    def test_var_positional_only_counts_as_unable(self):
        # 判据只数位置形参：仅 *args 的回调按"收不下"处理 -> 零参调用
        assert call_with_payload(lambda *a: len(a), object()) == 0


class TestCollectPayload:
    def test_zero_arg_contribution_ignores_payload(self, kernel_env):
        ws, settings = kernel_env
        k = _session_plugins(ws, settings, {
            "zero": _ctx_plugin("zero", "ZERO", 100),
        })
        assert k.load_plugin("zero")[0]

        host = PromptHost(workspace=str(ws))
        assert "ZERO" in collect_context_fragments(k, payload=host)

    def test_accepting_arg_contribution_receives_host(self, kernel_env):
        body = textwrap.dedent('''\
            __openx_meta__ = {"type": "context.memory", "summary": "host 感知"}
            def contribute(host):
                return f"HOST_WS={host.workspace}"
            def apply(ctx):
                ctx.register_context("hostaware", contribute, priority=100)
        ''')
        ws, settings = kernel_env
        k = _session_plugins(ws, settings, {"hostaware": body})
        assert k.load_plugin("hostaware")[0]

        host = PromptHost(workspace=str(ws))
        assert f"HOST_WS={ws}" in collect_context_fragments(k, payload=host)
        # 无 payload -> 形参没得可传，崩溃隔离 -> 该片段缺席（不炸征集）
        assert not any("HOST_WS=" in f for f in collect_context_fragments(k))


# ── builtin-context 三片段 ───────────────────────────────────────


class _FakeSkill:
    """build_skills_prompt 所需最小面（免造 skill 目录）。"""

    name = "demo"
    description = "Demo skill."
    allowed_tools = ("read_file",)
    legacy = False


class TestBuiltinContext:
    def test_memory_hints_always_contribute(self, kernel_env):
        ws, _ = kernel_env
        get_kernel().ensure_loaded(str(ws))
        assert collect_context_fragments(get_kernel()) == [MEMORY_INSTRUCTIONS]

    def test_host_dependent_fragments(self, kernel_env):
        from pathlib import Path

        from openx.instructions import (
            InstructionLevel,
            InstructionRegistry,
            InstructionSet,
        )

        ws, _ = kernel_env
        k = get_kernel()
        k.ensure_loaded(str(ws))

        reg = InstructionRegistry(
            project_instructions=InstructionSet(
                level=InstructionLevel.PROJECT,
                path=Path(ws) / "OPENX.md",
                content="PROJECT-RULE: always use pytest",
            )
        )
        host = PromptHost(
            workspace=str(ws), instructions=reg, skills={"demo": _FakeSkill()}
        )
        joined = "\n".join(collect_context_fragments(k, payload=host))
        assert "## Project Instructions" in joined
        assert "PROJECT-RULE: always use pytest" in joined
        assert "## Available Skills" in joined and "demo" in joined
        assert "pre-approved: read_file" in joined

    def test_empty_host_yields_only_memory_hints(self, kernel_env):
        ws, _ = kernel_env
        get_kernel().ensure_loaded(str(ws))
        host = PromptHost(workspace=str(ws))
        assert collect_context_fragments(get_kernel(), payload=host) == [
            MEMORY_INSTRUCTIONS
        ]


# ── 真 agent 接线 ────────────────────────────────────────────────


class TestAgentWiring:
    def _make_agent(self, ws):
        from openx.agent import OpenXAgent
        from openx.config import OpenXConfig

        config = OpenXConfig()
        config.workspace = str(ws)
        config.model = "test-model"
        return OpenXAgent(config)

    def test_system_prompt_carries_builtin_fragments(self, kernel_env):
        ws, _ = kernel_env
        (ws / "OPENX.md").write_text("PROJECT-RULE: always use pytest")
        skill_dir = ws / ".openx" / "skills" / "demo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo\ndescription: Demo skill.\n---\nSECRET BODY\n",
            encoding="utf-8",
        )

        prompt = self._make_agent(ws)._system_prompt
        assert "PROJECT-RULE: always use pytest" in prompt      # instructions
        assert "## Memory System" in prompt                     # memory_hints
        assert "## Available Skills" in prompt                  # skills 目录
        assert "Demo skill." in prompt
        assert "SECRET BODY" not in prompt                      # 渐进披露

    def test_system_prompt_carries_agent_md(self, kernel_env):
        """项目级 AGENT.md 与 OPENX.md 并存注入，OPENX.md 在前。"""
        ws, _ = kernel_env
        (ws / "OPENX.md").write_text("OPENX-RULE: always use pytest")
        (ws / "AGENT.md").write_text("AGENT-RULE: run ruff before commit")

        prompt = self._make_agent(ws)._system_prompt
        assert "OPENX-RULE: always use pytest" in prompt
        assert "AGENT-RULE: run ruff before commit" in prompt
        assert prompt.index("OPENX-RULE") < prompt.index("AGENT-RULE")

    def test_system_prompt_carries_agents_md_only(self, kernel_env):
        """只放 AGENTS.md 的项目也能拿到指令（不依赖 OPENX.md）。"""
        ws, _ = kernel_env
        (ws / "AGENTS.md").write_text("AGENTS-RULE: keep types strict")

        prompt = self._make_agent(ws)._system_prompt
        assert "AGENTS-RULE: keep types strict" in prompt
        assert "## Project Instructions" in prompt

    def test_agent_no_longer_imports_prompt_sources(self):
        """K7 零引用破洞：loop 不再直连具体提示源。"""
        import openx.agent as agent_mod

        src = inspect.getsource(agent_mod)
        assert "MEMORY_INSTRUCTIONS" not in src
        assert "build_skills_prompt" not in src
        assert "tools.memory_tool import" not in src

    def test_memory_instructions_migrated_out_of_tool_module(self):
        from openx.tools import memory_tool

        assert not hasattr(memory_tool, "MEMORY_INSTRUCTIONS")
        assert "## Memory System" in MEMORY_INSTRUCTIONS
