"""base bundle 内置插件之一：内置 provider（builtin-providers）。

providers 注册项的内置挂载（见 docs/design/provider-access-design.md）：
注册表存**实现**（键 = kind），settings 存**实例**（用户配置）--两级
解耦。openai-compat 是缺省实现；anthropic-compat 是 **Anthropic-format 兼容
协议**（官方 Claude 或任意兼容端点，base URL 可经组/角色的 apiBase 配置），
旧 kind ``anthropic`` 注册为别名。anthropic SDK 为可选依赖
（``pip install openx[anthropic]``），缺失时**跳过注册而非失败**--可选
依赖不是产品缺陷，装不上 = 该实现零贡献。

失败语义：内置插件 apply 抛异常 = 致命（产品带病不该运行）；禁用表对
内置 id 无效。
"""

from __future__ import annotations

# P-A/P-B 自描述。providers 是会话边界的基础设施（非任务级能力），不设
# type/mount（模型不按任务组装它）；trust=builtin。
__openx_meta__ = {
    "trust": "builtin",
    "summary": "内置模型 provider：openai-compat（恒在）+ anthropic-compat（可选 SDK）",
    "cost": {"schemaTokens": 0},
}


def _create_openai_compat(settings: dict):
    """openai-compat 实现工厂：settings dict -> OpenAICompatProvider。

    settings 键：api_key / api_base / model / temperature / max_tokens。
    实现直接持有 settings dict（凭据/模型只来自模型组解析结果，不再经
    OpenXConfig 扁平字段中转）。
    """
    from ..llm.openai_compat import OpenAICompatProvider

    return OpenAICompatProvider(settings)


def _create_anthropic(settings: dict):
    """anthropic-compat 实现工厂：settings dict -> AnthropicProvider。

    settings 键：api_key / api_base / model / temperature / max_tokens。
    ``api_base`` 可指向任意 Anthropic 兼容端点（如 DeepSeek 的
    https://api.deepseek.com/anthropic）；留空走 SDK 默认（官方）。
    """
    from ..llm.anthropic import AnthropicProvider

    return AnthropicProvider(settings)


def apply(ctx) -> None:
    """内置 provider 插件入口：注册 openai-compat；anthropic-compat 视 SDK 而定。

    canonical kind 为 ``anthropic-compat``（Anthropic-format 兼容协议族）；旧
    ``anthropic`` 保留为别名，指向同一工厂（存量配置零改动）。
    """
    ctx.register_provider("openai-compat", _create_openai_compat)
    try:
        import anthropic  # noqa: F401 -- 可选依赖存在性探测
    except ImportError:
        # 可选依赖缺失 = 零贡献，不是缺陷；记一行日志供排查。
        ctx.logger.warning(
            "anthropic SDK not installed; 'anthropic-compat' provider skipped"
        )
        return
    ctx.register_provider("anthropic-compat", _create_anthropic)
    ctx.register_provider("anthropic", _create_anthropic)  # 旧别名
