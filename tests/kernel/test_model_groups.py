"""modelGroups 配置模块测试：schema 解析 / 角色路由单测。

覆盖：
- parse/validate：简写字符串 vs 完整对象、main 必填、未知键警告、组名
  正则、``env:VAR`` 展开、kind 默认；
- 路由单测：mini 缺席 client_for==self.llm、mini 声明则独立 LLMClient、
  modal 缺席回落 main、_has_image 判定。

（旧扁平结构 env/providers/profiles 的迁移已在代码层移除——模型/凭据只经
modelGroups，无迁移用例。）

运行：``python -m pytest tests/kernel/test_model_groups.py -q``
"""

from __future__ import annotations

import pytest

from openx import model_groups as mg


# ── schema 解析 / 校验 ────────────────────────────────────────────


class TestParse:
    def test_shorthand_string_and_object(self):
        g = mg.parse_group("dev", {
            "openx-main-model": "m1",
            "openx-exec-model": {"model": "m2", "apiBase": "https://b/v1"},
        })
        assert g.roles[mg.MAIN_ROLE].model == "m1"
        ex = g.roles[mg.ROLE_KEYS[1]]
        assert ex.model == "m2" and ex.api_base == "https://b/v1"
        assert ex.kind is None  # 由解析合并层默认

    def test_main_required(self):
        with pytest.raises(ValueError):
            mg.parse_group("bad", {"openx-exec-model": "m"})

    def test_role_object_missing_model_rejected(self):
        with pytest.raises(ValueError):
            mg.parse_group("bad", {"openx-main-model": {"apiBase": "x"}})

    def test_unknown_keys_are_warnings_not_errors(self):
        raw = {"openx-main-model": "m", "futuristicField": 1}
        assert mg.group_warnings("g", raw)  # 未知键只告警
        g = mg.parse_group("g", raw)  # 不抛
        assert g.roles[mg.MAIN_ROLE].model == "m"

    def test_group_name_regex_rejects_colon(self):
        assert mg.validate_group_name("my.group_1") is True
        assert mg.validate_group_name("a:b") is False

    def test_env_expansion(self, monkeypatch):
        monkeypatch.setenv("MG_TEST_KEY", "sk-xyz")
        assert mg.expand_secret("env:MG_TEST_KEY") == "sk-xyz"
        assert mg.expand_secret("env:MG_MISSING_ZZZ") == ""
        assert mg.expand_secret("sk-literal") == "sk-literal"

    def test_role_alias_mapping(self):
        assert mg.canonical_role("exec") == "openx-exec-model"
        assert mg.canonical_role("openx-mini-model") == "openx-mini-model"
        assert mg.canonical_role("bogus") is None
        assert mg.role_short("openx-modal-model") == "modal"


# ── 角色路由单测（agent 层） ──────────────────────────────────────


def _write_groups(groups, active):
    from openx.config import OpenXConfig

    OpenXConfig.save_model_groups(groups)
    OpenXConfig.set_active_group(active)


def _agent_for(ws, groups, active="default"):
    from openx.agent import OpenXAgent

    _write_groups(groups, active)
    from openx.config import OpenXConfig

    cfg = OpenXConfig.load(workspace=str(ws))
    return OpenXAgent(cfg)


def _group_with_mini(mini_model: str | None):
    g = {
        "kind": "openai-compat",
        "apiKey": "sk-test",
        "apiBase": "https://example.com/v1",
        "openx-main-model": "m-main",
    }
    if mini_model:
        g["openx-mini-model"] = {"model": mini_model}
    return {"default": g}


class TestRoleRouting:
    def test_mini_absent_reuses_main_client(self, kernel_env):
        ws, _ = kernel_env
        agent = _agent_for(ws, _group_with_mini(None))
        assert agent.client_for("mini") is agent.llm
        assert agent.client_for("modal") is agent.llm  # 缺席 → main

    def test_mini_declared_builds_distinct_client(self, kernel_env):
        ws, _ = kernel_env
        agent = _agent_for(ws, _group_with_mini("m-mini"))
        mini = agent.client_for("mini")
        assert mini is not agent.llm
        assert mini._impl.settings["model"] == "m-mini"
        # main 仍是 m-main
        assert agent.llm._impl.settings["model"] == "m-main"
        # 缓存命中同一实例
        assert agent.client_for("mini") is mini

    def test_has_image_detects_image_url_parts(self):
        from openx.agent import _has_image

        assert _has_image("plain text") is False
        assert _has_image([{"type": "text", "text": "hi"}]) is False
        assert _has_image([
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
        ]) is True
