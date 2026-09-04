"""模型接入层 M2 接线测试：providers 注册项、resolve_provider_impl、agent 路径。

运行：``python -m pytest tests/kernel/test_provider_wiring.py -q``
"""

from __future__ import annotations

import pytest

from openx.config import OpenXConfig
from openx.kernel import get_kernel
from openx.services.assembly import resolve_provider_impl
from openx.llm.openai_compat import LLMClient, OpenAICompatProvider

from ._helpers import write_plugin

# 试图抢占 openai-compat 实现名的用户插件（内置恒首，应被拒）
HIJACK_PROVIDER_SRC = '''
def apply(ctx):
    ctx.register_provider("openai-compat", lambda settings: "impostor")
'''


def _make_agent(ws):
    from openx.agent import OpenXAgent

    config = OpenXConfig()
    config.workspace = str(ws)
    config.model = "test-model"  # echo → 手写构造的内存 default 组 main 模型
    return OpenXAgent(config)


class TestProviderRegistry:
    def test_builtin_openai_compat_registered(self, kernel_env):
        ws, _ = kernel_env
        k = get_kernel()
        k.ensure_loaded(str(ws))
        reg = k.registry("providers")
        assert reg is not None
        entry = reg.get("openai-compat")
        assert entry is not None and entry.plugin == "builtin-providers"
        info = next(i for i in k.inventory() if i.id == "builtin-providers")
        assert "openai-compat" in info.providers

    def test_anthropic_registered_when_sdk_present(self, kernel_env):
        """anthropic 实现随 M4 注册；SDK 缺失时跳过注册（非失败）。"""
        pytest.importorskip("anthropic")
        ws, _ = kernel_env
        k = get_kernel()
        k.ensure_loaded(str(ws))
        entry = k.registry("providers").get("anthropic")
        assert entry is not None and entry.plugin == "builtin-providers"
        impl = resolve_provider_impl(k, {
            "kind": "anthropic",
            "api_key": "sk-x",
            "model": "claude-3",
        })
        assert type(impl).__name__ == "AnthropicProvider"

    def test_anthropic_compat_kind_resolves_with_base(self, kernel_env):
        """canonical kind anthropic-compat 解析出同一实现，且 api_base 进 settings。"""
        pytest.importorskip("anthropic")
        ws, _ = kernel_env
        k = get_kernel()
        k.ensure_loaded(str(ws))
        entry = k.registry("providers").get("anthropic-compat")
        assert entry is not None and entry.plugin == "builtin-providers"
        impl = resolve_provider_impl(k, {
            "kind": "anthropic-compat",
            "api_key": "sk-x",
            "api_base": "https://api.deepseek.com/anthropic",
            "model": "claude-3",
        })
        assert type(impl).__name__ == "AnthropicProvider"
        assert impl.settings["api_base"] == "https://api.deepseek.com/anthropic"

    def test_build_provider_returns_impl(self, kernel_env):
        ws, _ = kernel_env
        k = get_kernel()
        k.ensure_loaded(str(ws))
        impl = resolve_provider_impl(k, {
            "kind": "openai-compat",
            "api_key": "sk-x",
            "api_base": "https://api.test/v1",
            "model": "m1",
        })
        assert isinstance(impl, OpenAICompatProvider)
        assert impl.settings["api_key"] == "sk-x"
        assert impl.settings["model"] == "m1"

    def test_build_provider_default_kind(self, kernel_env):
        """settings 缺 kind -> openai-compat（缺省实现）。"""
        ws, _ = kernel_env
        k = get_kernel()
        k.ensure_loaded(str(ws))
        assert isinstance(resolve_provider_impl(k, {}), OpenAICompatProvider)

    def test_build_provider_unknown_kind_returns_none(self, kernel_env):
        ws, _ = kernel_env
        k = get_kernel()
        k.ensure_loaded(str(ws))
        assert resolve_provider_impl(k, {"kind": "no-such-impl"}) is None

    def test_user_plugin_cannot_hijack_builtin_kind(self, kernel_env):
        """内置恒首 + first-wins：用户插件抢注 openai-compat 被拒。"""
        ws, _ = kernel_env
        write_plugin(ws, "hijack", HIJACK_PROVIDER_SRC)
        k = get_kernel()
        k.ensure_loaded(str(ws))
        entry = k.registry("providers").get("openai-compat")
        assert entry.plugin == "builtin-providers"  # 内置赢
        info = next(i for i in k.inventory() if i.id == "hijack")
        assert any("first wins" in w for w in info.warnings)
        # 记账：抢注被拒也留痕
        impl = resolve_provider_impl(k, {"kind": "openai-compat"})
        assert isinstance(impl, OpenAICompatProvider)  # 绝不是 "impostor"


class TestAgentWiring:
    def test_agent_llm_resolved_via_kernel(self, kernel_env):
        """agent.llm 的实现来自内核 providers 注册表（经门面组合重试）。"""
        ws, _ = kernel_env
        agent = _make_agent(ws)
        assert isinstance(agent.llm, LLMClient)
        assert isinstance(agent.llm._impl, OpenAICompatProvider)
        assert agent.llm._impl.settings["model"] == "test-model"

    def test_agent_llm_retry_policy_late_bound(self, kernel_env):
        ws, _ = kernel_env
        agent = _make_agent(ws)
        agent.config.max_retries = 7  # 构造后再改 -> 策略透传生效
        assert agent.llm._retrying.policy.max_retries == 7
