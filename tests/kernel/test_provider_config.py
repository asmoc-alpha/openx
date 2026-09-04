"""模型组（modelGroups）测试：解析、组绑定记账、/model 接线。

覆盖旧 provider_config 的三组语义并平移/扩展为组概念：
- TestResolveGroup：role_settings 解析（组共享默认、per-role 覆盖、load()
  无组=未配置、手写构造=极简内存 default、组缺凭据=空、retry 晚绑定、
  失效 activeGroup 回落、is_configured）；
- TestAgentGroupBinding：绑定记账（origin=kernel、payload 含 group/role）、
  switch_group 重建、set_role_model 持久化+回写、retry 组级覆盖；
- TestModelCommand：/model 列表/切组/改 main 模型/未知报错（StubConsole）。

settings 写读均走 monkeypatch 的 SETTINGS_PATH（tests/kernel/conftest.py）。

运行：``python -m pytest tests/kernel/test_provider_config.py -q``
"""

from __future__ import annotations

import pytest

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


def _write_groups(groups: dict, active: str) -> None:
    """把模型组写进当前 SETTINGS_PATH（groups 为 raw dict）。"""
    OpenXConfig.save_model_groups(groups)
    OpenXConfig.set_active_group(active)


def _make_config(ws, **overrides):
    """经 load() 构造 config（settings_loaded=True → 读文件组），再覆盖字段。"""
    config = OpenXConfig.load(workspace=str(ws))
    for k, v in overrides.items():
        setattr(config, k, v)
    return config


def _make_agent(ws, **overrides):
    from openx.agent import OpenXAgent

    return OpenXAgent(_make_config(ws, **overrides))


def _default_groups() -> dict:
    return {
        "default": {
            "kind": "openai-compat",
            "apiKey": "sk-1",
            "apiBase": "https://a/v1",
            "openx-main-model": "m-a",
        },
        "alt": {
            "kind": "openai-compat",
            "apiKey": "sk-2",
            "apiBase": "https://b/v1",
            "openx-main-model": "m-b",
            "openx-mini-model": {"model": "mini-b", "apiBase": "https://b-mini/v1"},
        },
    }


class TestResolveGroup:
    def test_load_without_groups_is_not_configured(self, kernel_env):
        """load() 配置无 modelGroups → 未配置：is_configured False，role_settings 抛错。"""
        ws, _ = kernel_env
        assert not OpenXConfig.is_configured()
        cfg = _make_config(ws)
        with pytest.raises(ValueError):
            cfg.role_settings("main")

    def test_handbuilt_config_synthesizes_minimal_default(self):
        """settings_loaded=False 手写 config（嵌入/测试）：合成极简 default 组。

        只带 model（无凭据——凭据只来自组配置）。
        """
        cfg = OpenXConfig()
        cfg.model = "m1"
        name, settings = cfg.role_settings("main")
        assert name == "default"
        assert settings["kind"] == "openai-compat"
        assert settings["api_key"] == ""
        assert settings["api_base"] == ""
        assert settings["model"] == "m1"
        # exec 缺席 → 整体回落 main 绑定
        _, ex = cfg.role_settings("exec")
        assert ex["model"] == "m1" and ex["api_key"] == ""

    def test_groups_resolve_active_and_role_override(self, kernel_env):
        ws, _ = kernel_env
        _write_groups(_default_groups(), "default")
        cfg = _make_config(ws)
        name, settings = cfg.role_settings("main")
        assert name == "default" and settings["model"] == "m-a"
        # exec 缺席（default 组）→ 回落 main 绑定
        _, ex = cfg.role_settings("exec")
        assert ex["model"] == "m-a"
        # alt 组 mini 显式覆盖：model/base 用自己的，key 继承组级
        _, mini = cfg.role_settings("mini", "alt")
        assert mini["model"] == "mini-b"
        assert mini["api_base"] == "https://b-mini/v1"
        assert mini["api_key"] == "sk-2"

    def test_group_without_creds_gets_empty_connection(self, kernel_env):
        """组级只给 model → 连接字段为空（不再回落任何扁平字段）。

        temperature/max_tokens 仍回落 cfg 通用默认（运行旋钮，非扁平兼容）。
        """
        ws, _ = kernel_env
        _write_groups(
            {"g": {"openx-main-model": "m-only"}}, "g"
        )
        cfg = _make_config(ws, temperature=0.7, max_tokens=4096)
        _, settings = cfg.role_settings("main", "g")
        assert settings["api_key"] == ""
        assert settings["api_base"] == ""
        assert settings["model"] == "m-only"
        assert settings["temperature"] == 0.7
        assert settings["max_tokens"] == 4096
        # retry 晚绑定：未声明 → 不进 settings
        assert "max_retries" not in settings

    def test_invalid_active_falls_back_to_first(self, kernel_env):
        ws, _ = kernel_env
        _write_groups(
            {
                "a": {"openx-main-model": "a-model"},
                "b": {"openx-main-model": "b-model"},
            },
            "no-such-group",
        )
        cfg = _make_config(ws)
        assert cfg.active_group_name() == "a"
        _, settings = cfg.role_settings("main")
        assert settings["model"] == "a-model"

    def test_is_configured(self, kernel_env):
        ws, _ = kernel_env
        assert not OpenXConfig.is_configured()
        _write_groups(
            {"claude": {"kind": "anthropic", "apiKey": "sk-ant",
                        "openx-main-model": "claude-3"}},
            "claude",
        )
        assert OpenXConfig.is_configured()

    def test_env_provider_vars_do_not_leak(self, kernel_env, monkeypatch):
        """组未声明凭据 → OPENAI_API_KEY/OPENX_MODEL 环境变量不进入 main settings。

        模型/凭据唯一来自 modelGroups；env 只能经组内 ``env:VAR`` 引用。
        """
        ws, _ = kernel_env
        _write_groups({"g": {"openx-main-model": "m-only"}}, "g")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
        monkeypatch.setenv("OPENAI_API_BASE", "https://env/v1")
        monkeypatch.setenv("OPENX_MODEL", "env-model")
        cfg = _make_config(ws)
        _, settings = cfg.role_settings("main", "g")
        assert settings["api_key"] == ""
        assert settings["api_base"] == ""
        assert settings["model"] == "m-only"
        # 组内显式 env:VAR 引用仍然可用（唯一的外部凭据通道）
        _write_groups({"g2": {"openx-main-model": "m2",
                              "apiKey": "env:OPENAI_API_KEY"}}, "g2")
        cfg2 = _make_config(ws)
        _, s2 = cfg2.role_settings("main", "g2")
        assert s2["api_key"] == "sk-env"


class TestAgentGroupBinding:
    def test_binding_emits_provider_selected_kernel(self, kernel_env):
        """agent 绑定记 origin=kernel，payload 带 group/role（M5）。"""
        ws, _ = kernel_env
        _write_groups(
            {"default": {"kind": "openai-compat", "apiKey": "sk-test",
                         "apiBase": "https://x/v1", "openx-main-model": "m1"}},
            "default",
        )
        sink = Sink()
        get_kernel().attach_ledger(sink, session="s1")
        agent = _make_agent(ws)
        assert agent._provider_name == "default"
        assert agent._bind_role == "openx-main-model"
        sel = sink.of("provider_selected")
        assert len(sel) == 1
        assert sel[0].origin == "kernel"
        p = sel[0].payload
        assert p["provider"] == "default"
        assert p["group"] == "default"
        assert p["role"] == "openx-main-model"
        assert p["kind"] == "openai-compat"
        assert p["model"] == "m1"

    def test_switch_group_rebuilds_and_emits_user(self, kernel_env):
        ws, _ = kernel_env
        _write_groups(_default_groups(), "default")
        sink = Sink()
        get_kernel().attach_ledger(sink, session="s1")
        agent = _make_agent(ws)
        assert agent.llm._impl.settings["model"] == "m-a"
        assert agent.switch_group("alt") is True
        assert agent._provider_name == "alt"
        assert isinstance(agent.llm._impl, OpenAICompatProvider)
        assert agent.llm._impl.settings["model"] == "m-b"
        assert agent.config.model == "m-b"  # 投影（echo）随切换同步
        sel = sink.of("provider_selected")
        assert [e.payload["origin"] for e in sel] == ["kernel", "user"]
        assert sel[1].payload["group"] == "alt"
        assert sel[1].payload["model"] == "m-b"

    def test_switch_group_unknown_fails_unchanged(self, kernel_env):
        ws, _ = kernel_env
        _write_groups(_default_groups(), "default")
        agent = _make_agent(ws)
        assert agent.switch_group("nope") is False
        assert agent._provider_name == "default"

    def test_switch_group_unregistered_kind_fails(self, kernel_env):
        ws, _ = kernel_env
        _write_groups(
            {
                "default": {"kind": "openai-compat", "openx-main-model": "m"},
                "missing": {"kind": "no-such-impl", "openx-main-model": "x"},
            },
            "default",
        )
        agent = _make_agent(ws)
        assert agent.switch_group("missing") is False
        assert agent._provider_name == "default"

    def test_group_retry_override(self, kernel_env):
        """组级显式声明 max_retries → 覆盖全局；缺省则晚绑定 config（§6）。"""
        ws, _ = kernel_env
        _write_groups(
            {"default": {"kind": "openai-compat", "openx-main-model": "m",
                         "max_retries": 2, "retry_base_delay": 0.5}},
            "default",
        )
        agent = _make_agent(ws)
        assert agent.llm._retrying.policy.max_retries == 2
        assert agent.llm._retrying.policy.base_delay == 0.5
        # 无显式覆盖：构造后再改 config 实时生效（晚绑定语义不变）
        _write_groups({"default": {"kind": "openai-compat", "openx-main-model": "m"}},
                      "default")
        agent2 = _make_agent(ws)
        agent2.config.max_retries = 7
        assert agent2.llm._retrying.policy.max_retries == 7

    def test_set_role_model_updates_impl_and_persists(self, kernel_env):
        """set_role_model(main)：持久化 + 重建 main 客户端 + 回写实现侧。"""
        ws, _ = kernel_env
        _write_groups({"default": {"kind": "openai-compat", "apiKey": "sk",
                                   "openx-main-model": "old"}}, "default")
        agent = _make_agent(ws)
        assert agent.set_role_model("main", "new-model") is True
        assert agent.config.model == "new-model"
        assert agent.llm._impl.settings["model"] == "new-model"
        raw = OpenXConfig.load_model_groups_raw()
        assert raw["default"]["openx-main-model"] == "new-model"


class StubConsole:
    """命令测试用：捕获 info/success/error + raw 行。"""

    def __init__(self):
        self.infos: list[str] = []
        self.successes: list[str] = []
        self.errors: list[str] = []
        self.raw_lines: list[str] = []

        class Raw:
            lines: list[str] = []

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


class TestModelCommand:
    async def test_list_no_groups(self, kernel_env):
        from openx.app.cli import commands

        ws, _ = kernel_env
        _write_groups(_default_groups(), "default")
        agent = _make_agent(ws)
        # 运行中途组被删（磁盘组空）→ /model 列表提示未配置组
        OpenXConfig.save_model_groups({})
        stub = StubConsole()
        assert await commands.handle_slash_command("model", agent, stub, []) is True
        assert any("No model groups configured" in i for i in stub.infos)

    async def test_list_groups_with_active_mark(self, kernel_env):
        from openx.app.cli import commands

        ws, _ = kernel_env
        _write_groups(_default_groups(), "default")
        agent = _make_agent(ws)
        stub = StubConsole()
        assert await commands.handle_slash_command("model", agent, stub, []) is True
        blob = "\n".join(stub.raw_lines)
        assert "default" in blob and "alt" in blob
        assert "← active" in blob
        # active 组下列角色绑定（含角色级 base 标注）
        assert "mini-b" in blob

    async def test_switch_group_persists(self, kernel_env):
        from openx.app.cli import commands
        from openx.config import SETTINGS_PATH

        ws, _ = kernel_env
        _write_groups(_default_groups(), "default")
        agent = _make_agent(ws)
        stub = StubConsole()
        assert (
            await commands.handle_slash_command("model", agent, stub, ["alt"])
            is True
        )
        assert agent._provider_name == "alt"
        assert agent.llm._impl.settings["model"] == "m-b"
        assert SETTINGS_PATH.read_text().count("alt")  # activeGroup 已落盘
        import json
        assert json.loads(SETTINGS_PATH.read_text())["activeGroup"] == "alt"
        assert any("alt" in s for s in stub.successes)

    async def test_literal_without_groups_reports_error(self, kernel_env):
        from openx.app.cli import commands

        ws, _ = kernel_env
        _write_groups({"default": {"kind": "openai-compat",
                                   "openx-main-model": "m1"}}, "default")
        agent = _make_agent(ws)
        # 运行中途组从磁盘清掉 → 无组可持久化 → 报错且状态不变
        OpenXConfig.save_model_groups({})
        stub = StubConsole()
        assert (
            await commands.handle_slash_command("model", agent, stub, ["some-model"])
            is True
        )
        assert any("not a configured group" in e for e in stub.errors)
        assert agent.config.model == "m1"  # 状态不变

    async def test_unknown_role_reports_error(self, kernel_env):
        from openx.app.cli import commands

        ws, _ = kernel_env
        _write_groups(_default_groups(), "default")
        agent = _make_agent(ws)
        stub = StubConsole()
        assert (
            await commands.handle_slash_command("model", agent, stub, ["default:bogus"])
            is True
        )
        assert any("Unknown role" in e for e in stub.errors)

    async def test_literal_arg_sets_active_group_main_model(self, kernel_env):
        from openx.app.cli import commands

        ws, _ = kernel_env
        _write_groups({"default": {"kind": "openai-compat", "openx-main-model": "old"}},
                      "default")
        agent = _make_agent(ws)
        stub = StubConsole()
        assert (
            await commands.handle_slash_command("model", agent, stub, ["brand-new"])
            is True
        )
        assert agent.config.model == "brand-new"
        raw = OpenXConfig.load_model_groups_raw()
        assert raw["default"]["openx-main-model"] == "brand-new"

    def test_set_role_cred_override_persists_and_clears(self, kernel_env):
        """set_role_cred：缺席角色挂覆盖时自动带上 main 当前 model；空值清除。"""
        ws, _ = kernel_env
        _write_groups({"default": {"kind": "openai-compat", "apiKey": "sk",
                                   "openx-main-model": "mm"}}, "default")
        agent = _make_agent(ws)

        assert agent.set_role_cred("mini", "api_base", "https://mini/v1") is True
        raw = OpenXConfig.load_model_groups_raw()
        mini = raw["default"]["openx-mini-model"]
        assert isinstance(mini, dict), "缺席角色应落成对象以承载覆盖"
        assert mini["model"] == "mm"          # main 回落 model 被带入
        assert mini["apiBase"] == "https://mini/v1"

        assert agent.set_role_cred("mini", "api_base", "") is True
        raw2 = OpenXConfig.load_model_groups_raw()
        assert "apiBase" not in raw2["default"]["openx-mini-model"]
