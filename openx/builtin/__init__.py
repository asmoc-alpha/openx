"""base bundle 内置插件包 -- 内置插件统一收口。

内置插件 = 随产品发行的插件（"一切能力皆插件"的第一批）：失败=致命、
禁用表对其无效、id 用户插件不得占用。全部收口在本包，不再散落于
openx 顶层；内核加载处遍历 ``BUILTIN_PLUGINS`` 装配（列表序即优先级，
先见者赢）。

新增内置插件 = 本包加一个子模块 + ``BUILTIN_PLUGINS`` 加一条，内核主体
不动（与注册目录表同款"加一行"纪律）。

当前内置插件：
- ``tools``（builtin-tools）：内置工具工厂（core-tools）；
- ``providers``（builtin-providers）：openai-compat 恒在，anthropic 视
  可选 SDK（``pip install openx[anthropic]``）而定。
"""

from __future__ import annotations

from . import providers, tools
from ..kernel.loader import PluginSpec

BUILTIN_TOOLS_ID = "builtin-tools"
BUILTIN_PROVIDERS_ID = "builtin-providers"

# base bundle 内置插件列表（加载序即优先级）：builtin-tools 在前，组合决议
# /首条注册事件的既有次序不变。``loaded`` 为已导入的模块（不经文件解析）。
BUILTIN_PLUGINS: tuple[PluginSpec, ...] = (
    PluginSpec(
        id=BUILTIN_TOOLS_ID,
        source="base-bundle",
        loaded=tools,
        builtin=True,
    ),
    PluginSpec(
        id=BUILTIN_PROVIDERS_ID,
        source="base-bundle",
        loaded=providers,
        builtin=True,
    ),
)

__all__ = [
    "BUILTIN_PLUGINS",
    "BUILTIN_TOOLS_ID",
    "BUILTIN_PROVIDERS_ID",
    "providers",
    "tools",
]
