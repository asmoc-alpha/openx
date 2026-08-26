"""base bundle 内置 provider 插件 -- 模型接入的内置实现。

providers 注册项的内置挂载（见 docs/design/provider-access-design.md）：
注册表存**实现**（键 = kind），settings 存**实例**（用户配置）--两级
解耦。openai-compat 是缺省实现；anthropic 原生实现随 M4 加入（SDK 为
可选依赖，缺失时跳过注册而非失败--可选依赖不是产品缺陷）。

失败语义：内置插件 apply 抛异常 = 致命（产品带病不该运行）；禁用表对
内置 id 无效。
"""

from __future__ import annotations


def _create_openai_compat(settings: dict):
    """openai-compat 实现工厂：settings dict -> OpenAICompatProvider。

    settings 键：api_key / api_base / model / temperature / max_tokens。
    """
    from .config import OpenXConfig
    from .llm.client import OpenAICompatProvider

    cfg = OpenXConfig()
    cfg.api_key = str(settings.get("api_key", ""))
    cfg.api_base = str(settings.get("api_base", ""))
    cfg.model = str(settings.get("model", ""))
    cfg.temperature = float(settings.get("temperature", 0.0))
    cfg.max_tokens = int(settings.get("max_tokens", 8192))
    return OpenAICompatProvider(cfg)


def apply(ctx) -> None:
    """内置 provider 插件入口：注册 openai-compat 实现。"""
    ctx.register_provider("openai-compat", _create_openai_compat)
