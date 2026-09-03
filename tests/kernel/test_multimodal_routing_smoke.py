"""多模态路由现状验证（离线冒烟）。

验证目标（对应产品需求"带图等内容 → 组内多模态模型；缺失降级主模型"）：
- 带图回合且组里配了 ``openx-modal-model`` → run() 实际用 modal 模型；
- 纯文本回合 → 用 main 模型；
- 带图回合但组里没配 modal → client_for("modal") is agent.llm，run() 用 main
  （降级成立）。

另含 2 个"现状缺口"探针（只断言现状、为是否要扩展提供依据）：
- 文本里的远程 http(s) 图片 / data URL **不会**被识别成图（现只认本地文件
  拖拽 → base64 dataURL → image_url part）；
- ``task`` 子代理参数无图片字段（父级图片 content 不会透传给子代理）。

全程不联网：monkeypatch ``LLMClient.chat`` 拦截，record 实际被调用的客户端模型。

运行：``python -m pytest tests/kernel/test_multimodal_routing_smoke.py -v``
"""

from __future__ import annotations

import pytest

from openx.agent import OpenXAgent
from openx.config import OpenXConfig
from openx.image import extract_image_paths
from openx.llm import LLMClient

DATA_URL = "data:image/png;base64,AAAA"


def _write_groups(groups, active):
    OpenXConfig.save_model_groups(groups)
    OpenXConfig.set_active_group(active)


def _agent(ws, groups, active="default"):
    _write_groups(groups, active)
    cfg = OpenXConfig.load(workspace=str(ws))
    return OpenXAgent(cfg)


def _group(modal: str | None):
    g = {
        "kind": "openai-compat",
        "apiKey": "sk-test",
        "apiBase": "https://example.com/v1",
        "openx-main-model": "m-main",
    }
    if modal:
        g["openx-modal-model"] = {"model": modal}
    return {"default": g}


@pytest.fixture
def _record_chat(monkeypatch):
    """拦截 LLMClient.chat：记录实际被调用的客户端模型，返回一轮即收尾。"""
    calls: list[str] = []

    async def _stub(self, **_kw):
        calls.append(self._impl.config.model)
        return {"content": "ok"}

    monkeypatch.setattr(LLMClient, "chat", _stub)
    return calls


class TestRoutingSmoke:
    @pytest.mark.asyncio
    async def test_image_turn_uses_modal_when_declared(
        self, kernel_env, _record_chat
    ):
        ws, _ = kernel_env
        agent = _agent(ws, _group("m-modal"))
        user = agent.build_user_content("describe this", images=[DATA_URL])
        out = await agent.run(user)
        assert out == "ok"
        assert agent.client_for("modal") is not agent.llm, "modal 应建独立客户端"
        assert _record_chat == ["m-modal"], (
            f"带图回合应切到 modal，实际调用模型 {_record_chat}"
        )

    @pytest.mark.asyncio
    async def test_text_turn_uses_main(self, kernel_env, _record_chat):
        ws, _ = kernel_env
        agent = _agent(ws, _group("m-modal"))
        await agent.run("plain text only")
        assert _record_chat == ["m-main"], (
            f"纯文本回合应走 main，实际 {_record_chat}"
        )

    @pytest.mark.asyncio
    async def test_image_turn_falls_back_to_main_without_modal(
        self, kernel_env, _record_chat
    ):
        ws, _ = kernel_env
        agent = _agent(ws, _group(None))
        user = agent.build_user_content("describe this", images=[DATA_URL])
        out = await agent.run(user)
        assert out == "ok"
        assert agent.client_for("modal") is agent.llm, "无 modal 应回落主客户端"
        assert _record_chat == ["m-main"], (
            f"无 modal 带图回合应降级用 main，实际 {_record_chat}"
        )


class TestCurrentGaps:
    """现状缺口探针（只钉当前行为；若后续要支持，改这些断言即可）。"""

    def test_remote_image_url_in_text_is_not_detected(self):
        """远程 http(s) 图片或 data URL 只作为文本，不触发多模路由。"""
        assert extract_image_paths("see https://example.com/a.png") == []
        assert extract_image_paths(f"see {DATA_URL}") == []

    def test_task_subagent_schema_has_no_image_param(self):
        """task 工具参数无图片字段 → 父级带图回合不能把图交给子代理。"""
        from openx.tools.subagent_tool import TaskTool

        props = TaskTool.parameters["properties"]
        assert "images" not in props
        assert "attachments" not in props
        assert "prompt" in props  # 子代理只收文本 prompt
