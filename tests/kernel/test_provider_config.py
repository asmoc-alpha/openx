"""模型接入层 M3/M5 测试：providers 配置解析、迁移、/provider 接线与记账。

运行：``python -m pytest tests/kernel/test_provider_config.py -q``
"""

from __future__ import annotations

from openx.config import OpenXConfig
from openx.kernel import get_kernel
from openx.llm.openai_compat import OpenAICompatProvider


class Sink:
    """收集事件的账本 sink（agent 绑定/切换的 provider_selected 断言）。"""

    def __init__(self):
        self.events: list = []

    def __call__(self, event) -> None:
        self.events.append(event)

    def of(self, type_: str) -> list:
        return [e for e in self.events if e.type == type_]


def _make_cfg(ws, **overrides):
    config = OpenXConfig()
    config.workspace = str(ws)
    defaults = dict(
        api_key="sk-test", api_base="https://example.com/v1", model="m1",
    )
    defaults.update(overrides)
    for k, v in defaults.items():
        setattr(config, k, v)
    return config


def _make_agent(ws, **overrides):
    from openx.agent import OpenXAgent

    return OpenXAgent(_make_cfg(ws, **overrides))


class TestResolveProvider:
    def test_migration_synthesizes_default(self, kernel_env):
        """无 providers 键 -> 隐式 default 实例（行为≡现状）。"""
        ws, _ = kernel_env
        name, settings = _make_cfg(ws).resolve_provider()
        assert name == "default"
        assert settings["kind"] == "openai-compat"
        assert settings["api_key"] == "sk-test"
        assert settings["api_base"] == "https://example.com/v1"
        assert settings["model"] == "m1"

    def test_providers_resolves_active_instance(self, kernel_env):
        ws, _ = kernel_env
        OpenXConfig.save_provider_settings(
            {
                "deepseek": {
                    "kind": "openai-compat",
                    "api_key": "sk-ds",
                    "api_base": "https://ds/v1",
                    "model": "deepseek-v3",
                },
                "claude": {
                    "kind": "anthropic",
                    "api_key": "sk-ant",
                    "model": "claude-3",
                },
            },
            "deepseek",
        )
        name, settings = _make_cfg(ws).resolve_provider()
        assert name == "deepseek"
        assert settings["kind"] == "openai-compat"
        assert settings["model"] == "deepseek-v3"

    def test_global_fallback_for_missing_fields(self, kernel_env):
        """实例缺字段回落全局（连接字段 + 参数字段）。"""
        ws, _ = kernel_env
        OpenXConfig.save_provider_settings(
            {"deepseek": {"kind": "openai-compat", "model": "ds"}}, "deepseek"
        )
        name, settings = _make_cfg(ws, temperature=0.7, max_tokens=4096).resolve_provider()
        assert settings["api_key"] == "sk-test"
        assert settings["api_base"] == "https://example.com/v1"
        assert settings["temperature"] == 0.7
        assert settings["max_tokens"] == 4096
        # 重试字段不回落进 settings：策略对象读 config 实时值（晚绑定）
        assert "max_retries" not in settings
        assert "retry_base_delay" not in settings

    def test_invalid_active_falls_back_to_first(self, kernel_env):
        ws, _ = kernel_env
        OpenXConfig.save_provider_settings(
            {
                "a": {"kind": "openai-compat", "model": "a-model"},
                "b": {"kind": "openai-compat", "model": "b-model"},
            },
            "no-such-instance",
        )
        name, settings = _make_cfg(ws).resolve_provider()
        assert name == "a"
        assert settings["model"] == "a-model"

    def test_is_configured_via_providers(self, kernel_env):
        ws, _ = kernel_env
        assert not OpenXConfig.is_configured()
        OpenXConfig.save_provider_settings(
            {
                "claude": {
                    "kind": "anthropic", "api_key": "sk-ant", "model": "claude-3",
                }
            },
            "claude",
        )
        assert OpenXConfig.is_configured()


class TestAgentProviderBinding:
    def test_binding_emits_provider_selected_kernel(self, kernel_env):
        """agent 绑定 provider 记一条 origin=kernel（M5）。"""
        ws, _ = kernel_env
        sink = Sink()
        get_kernel().attach_ledger(sink, session="s1")
        agent = _make_agent(ws)
        assert agent._provider_name == "default"
        sel = sink.of("provider_selected")
        assert len(sel) == 1
        assert sel[0].origin == "kernel"
        assert sel[0].payload["provider"] == "default"
        assert sel[0].payload["kind"] == "openai-compat"
        assert sel[0].payload["model"] == "m1"

    def test_switch_provider_rebuilds_and_emits_user(self, kernel_env):
        """/provider 切换：重建实现 + 记 origin=user，切换留痕。"""
        ws, _ = kernel_env
        OpenXConfig.save_provider_settings(
            {
                "default": {
                    "kind": "openai-compat",
                    "api_key": "sk-1",
                    "api_base": "https://a/v1",
                    "model": "m-a",
                },
                "alt": {
                    "kind": "openai-compat",
                    "api_key": "sk-2",
                    "api_base": "https://b/v1",
                    "model": "m-b",
                },
            },
            "default",
        )
        sink = Sink()
        get_kernel().attach_ledger(sink, session="s1")
        agent = _make_agent(ws)
        assert agent.llm._impl.config.model == "m-a"
        assert agent.switch_provider("alt") is True
        assert agent._provider_name == "alt"
        assert isinstance(agent.llm._impl, OpenAICompatProvider)
        assert agent.llm._impl.config.model == "m-b"
        assert agent.config.model == "m-b"  # config.model 随切换同步
        sel = sink.of("provider_selected")
        assert [e.payload["origin"] for e in sel] == ["kernel", "user"]
        assert sel[1].payload["provider"] == "alt"
        assert sel[1].payload["model"] == "m-b"

    def test_switch_provider_unknown_fails_unchanged(self, kernel_env):
        ws, _ = kernel_env
        OpenXConfig.save_provider_settings(
            {"default": {"kind": "openai-compat", "model": "m"}}, "default"
        )
        agent = _make_agent(ws)
        assert agent.switch_provider("nope") is False
        assert agent._provider_name == "default"  # 状态不变

    def test_switch_provider_unregistered_kind_fails(self, kernel_env):
        """kind 未注册（如 SDK 缺失的 anthropic）→ 拒绝切换，状态不变。"""
        ws, _ = kernel_env
        OpenXConfig.save_provider_settings(
            {
                "default": {"kind": "openai-compat", "model": "m"},
                "missing": {"kind": "no-such-impl", "model": "x"},
            },
            "default",
        )
        agent = _make_agent(ws)
        assert agent.switch_provider("missing") is False
        assert agent._provider_name == "default"

    def test_per_instance_retry_override(self, kernel_env):
        """实例显式声明 max_retries -> 覆盖全局；缺省则晚绑定 config（§6）。"""
        ws, _ = kernel_env
        OpenXConfig.save_provider_settings(
            {"default": {"kind": "openai-compat", "model": "m",
                         "max_retries": 2, "retry_base_delay": 0.5}},
            "default",
        )
        agent = _make_agent(ws)
        assert agent.llm._retrying.policy.max_retries == 2
        assert agent.llm._retrying.policy.base_delay == 0.5
        # 无显式覆盖：构造后再改 config 实时生效（晚绑定语义不变）
        OpenXConfig.save_provider_settings(
            {"default": {"kind": "openai-compat", "model": "m"}}, "default"
        )
        agent2 = _make_agent(ws)
        agent2.config.max_retries = 7
        assert agent2.llm._retrying.policy.max_retries == 7

    def test_set_active_model_updates_impl_and_persists(self, kernel_env):
        """/model：只改激活实例的 model，并持久化 + 回写实现侧。"""
        ws, _ = kernel_env
        OpenXConfig.save_provider_settings(
            {"default": {"kind": "openai-compat", "model": "old", "api_key": "sk"}},
            "default",
        )
        agent = _make_agent(ws)
        agent.set_active_model("new-model")
        assert agent.config.model == "new-model"
        assert agent.llm._impl.config.model == "new-model"  # 回写实现侧
        reloaded = OpenXConfig.load_provider_settings()
        assert reloaded["providers"]["default"]["model"] == "new-model"  # 持久化


class StubConsole:
    """/provider 命令测试用：捕获打印 + raw 行。"""

    def __init__(self):
        self.infos: list[str] = []
        self.successes: list[str] = []
        self.errors: list[str] = []
        self.raw_lines: list[str] = []

        class Raw:
            def print(self, *parts):
                for p in parts:
                    self.lines.append(str(p))

        self.raw = Raw()
        Raw.lines = self.raw_lines

    def print_info(self, m):
        self.infos.append(m)

    def print_success(self, m):
        self.successes.append(m)

    def print_error(self, m):
        self.errors.append(m)


class TestProviderCommand:
    async def test_list_no_instances(self, kernel_env):
        from openx.app.cli import commands

        ws, _ = kernel_env
        agent = _make_agent(ws)
        stub = StubConsole()
        assert await commands.handle_slash_command("provider", agent, stub, []) is True
        assert any("No provider instances" in i for i in stub.infos)

    async def test_list_instances_with_active_mark(self, kernel_env):
        from openx.app.cli import commands

        ws, _ = kernel_env
        OpenXConfig.save_provider_settings(
            {
                "deepseek": {"kind": "openai-compat", "model": "deepseek-v3"},
                "claude": {"kind": "anthropic", "model": "claude-3"},
            },
            "deepseek",
        )
        agent = _make_agent(ws)
        stub = StubConsole()
        assert await commands.handle_slash_command("provider", agent, stub, []) is True
        blob = "\n".join(stub.raw_lines)
        assert "deepseek" in blob and "claude" in blob
        assert "← active" in blob

    async def test_switch_valid_persists(self, kernel_env):
        from openx.app.cli import commands

        ws, _ = kernel_env
        OpenXConfig.save_provider_settings(
            {
                "default": {"kind": "openai-compat", "model": "m-a"},
                "alt": {"kind": "openai-compat", "model": "m-b"},
            },
            "default",
        )
        agent = _make_agent(ws)
        stub = StubConsole()
        assert (
            await commands.handle_slash_command("provider", agent, stub, ["alt"])
            is True
        )
        assert agent._provider_name == "alt"
        assert agent.llm._impl.config.model == "m-b"
        assert OpenXConfig.load_provider_settings()["active_provider"] == "alt"
        assert any("alt" in s for s in stub.successes)

    async def test_switch_unknown_reports_error(self, kernel_env):
        from openx.app.cli import commands

        ws, _ = kernel_env
        OpenXConfig.save_provider_settings(
            {"default": {"kind": "openai-compat", "model": "m"}}, "default"
        )
        agent = _make_agent(ws)
        stub = StubConsole()
        assert (
            await commands.handle_slash_command("provider", agent, stub, ["nope"])
            is True
        )
        assert any("not configured" in e for e in stub.errors)
        assert agent._provider_name == "default"
