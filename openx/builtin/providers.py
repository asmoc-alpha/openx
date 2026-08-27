"""base bundle 内置插件之一：内置 provider（builtin-providers）。

providers 注册项的内置挂载（见 docs/design/provider-access-design.md）：
注册表存**实现**（键 = kind），settings 存**实例**（用户配置）--两级
解耦。openai-compat 是缺省实现；anthropic 原生实现（M4）SDK 为可选
依赖（``pip install openx[anthropic]``），缺失时**跳过注册而非失败**--
可选依赖不是产品缺陷，装不上 = 该实现零贡献。

失败语义：内置插件 apply 抛异常 = 致命（产品带病不该运行）；禁用表对
内置 id 无效。
"""

from __future__ import annotations


def _create_openai_compat(settings: dict):
    """openai-compat 实现工厂：settings dict -> OpenAICompatProvider。

    settings 键：api_key / api_base / model / temperature / max_tokens。
    """
    from ..config import OpenXConfig
    from ..llm.openai_compat import OpenAICompatProvider

    cfg = OpenXConfig()
    cfg.api_key = str(settings.get("api_key", ""))
    cfg.api_base = str(settings.get("api_base", ""))
    cfg.model = str(settings.get("model", ""))
    cfg.temperature = float(settings.get("temperature", 0.0))
    cfg.max_tokens = int(settings.get("max_tokens", 8192))
    return OpenAICompatProvider(cfg)


def _create_anthropic(settings: dict):
    """anthropic 实现工厂：settings dict -> AnthropicProvider。

    settings 键：api_key / model / temperature / max_tokens（无 base URL
    概念--Anthropic 原生端点）。
    """
    from ..config import OpenXConfig
    from ..llm.anthropic import AnthropicProvider

    cfg = OpenXConfig()
    cfg.api_key = str(settings.get("api_key", ""))
    cfg.model = str(settings.get("model", ""))
    cfg.temperature = float(settings.get("temperature", 0.0))
    cfg.max_tokens = int(settings.get("max_tokens", 8192))
    return AnthropicProvider(cfg)


def apply(ctx) -> None:
    """内置 provider 插件入口：注册 openai-compat，anthropic 视 SDK 而定。"""
    ctx.register_provider("openai-compat", _create_openai_compat)
    try:
        import anthropic  # noqa: F401 -- 可选依赖存在性探测
    except ImportError:
        # 可选依赖缺失 = 零贡献，不是缺陷；记一行日志供排查。
        ctx.logger.warning("anthropic SDK not installed; 'anthropic' provider skipped")
        return
    ctx.register_provider("anthropic", _create_anthropic)
