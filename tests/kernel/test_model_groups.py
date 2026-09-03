"""modelGroups 配置模块测试：schema 解析 / 迁移 / 角色路由单测。

覆盖：
- parse/validate：简写字符串 vs 完整对象、main 必填、未知键警告、组名
  正则、``env:VAR`` 展开、kind 默认；
- 迁移：扁平 env 三件套、providers+active_provider、models profiles 三类
  旧结构 -> modelGroups；无关顶层键保留；幂等；已有 modelGroups 跳过；
- 路由单测：mini 缺席 client_for==self.llm、mini 声明则独立 LLMClient、
  modal 缺席回落 main、_has_image 判定。

运行：``python -m pytest tests/kernel/test_model_groups.py -q``
"""

from __future__ import annotations

import json

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


# ── 迁移 ──────────────────────────────────────────────────────────


class TestMigrate:
    def test_flat_env_trio(self):
        data = {
            "env": {
                "OPENX_API_KEY": "sk-1", "OPENX_BASE_URL": "https://a/v1",
                "OPENX_DEFAULT_MODEL": "m1", "OPENX_AUTO_APPROVE": "true",
            },
            "trusted_dirs": ["/x"],
        }
        new, notes = mg.migrate_legacy(data)
        assert new["modelGroups"]["default"]["openx-main-model"] == "m1"
        assert new["activeGroup"] == "default"
        assert new["modelGroups"]["default"]["apiKey"] == "sk-1"
        # LLM 三件套删除、无关 env 键保留
        assert new["env"] == {"OPENX_AUTO_APPROVE": "true"}
        assert new["trusted_dirs"] == ["/x"]

    def test_providers_each_becomes_group(self):
        data = {
            "providers": {
                "ds": {"kind": "openai-compat", "api_key": "k1",
                       "api_base": "https://ds/v1", "model": "dm"},
                "cl": {"kind": "anthropic", "api_key": "k2", "model": "cm"},
            },
            "active_provider": "cl",
        }
        new, _ = mg.migrate_legacy(data)
        assert set(new["modelGroups"]) == {"ds", "cl"}
        assert new["activeGroup"] == "cl"
        assert new["modelGroups"]["ds"]["apiKey"] == "k1"
        assert new["modelGroups"]["cl"]["kind"] == "anthropic"
        assert "providers" not in new and "active_provider" not in new

    def test_model_profiles_folded(self):
        data = {"models": {"gpt": {"model": "gpt-4o", "api_base": "https://o/v1"}}}
        new, notes = mg.migrate_legacy(data)
        assert new["modelGroups"]["gpt"]["openx-main-model"] == "gpt-4o"
        assert new["modelGroups"]["gpt"]["apiBase"] == "https://o/v1"
        assert "models" not in new
        assert notes  # 有迁移说明

    def test_already_modelgroups_is_noop(self):
        data = {"modelGroups": {"g": {"openx-main-model": "m"}}, "activeGroup": "g"}
        new, notes = mg.migrate_legacy(data)
        assert new is data and notes == []

    def test_idempotent_and_preserves_unrelated(self, tmp_path, monkeypatch):
        import openx.config as cfg
        from openx.config import OpenXConfig

        settings = tmp_path / "settings.json"
        monkeypatch.setattr(cfg, "SETTINGS_PATH", settings)
        settings.write_text(json.dumps({
            "env": {"OPENX_BASE_URL": "https://a", "OPENX_DEFAULT_MODEL": "m"},
            "mcpServers": {"srv": {}},
        }))
        notes1 = OpenXConfig.ensure_model_groups()
        assert notes1  # 只迁移一次
        notes2 = OpenXConfig.ensure_model_groups()
        assert notes2 == []
        data = json.loads(settings.read_text())
        assert data["mcpServers"] == {"srv": {}}
        assert data["modelGroups"]["default"]["openx-main-model"] == "m"


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
        assert mini._impl.config.model == "m-mini"
        # main 仍是 m-main
        assert agent.llm._impl.config.model == "m-main"
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
