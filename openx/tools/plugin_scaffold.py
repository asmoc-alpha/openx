"""插件脚手架生成器（S3a）——``/plugin new <name>`` 的模板侧。

生成一个**可直接装载**的最小工具插件：manifest（trust=user、permissions
留空并注释 S1c 语义）+ 一个示例工具 + ``apply(ctx)`` + ``self_test()``
（admission 门的放行前提）。模板刻意保持可读——它是作者读到的第一份
插件代码，注释即教学。
"""

from __future__ import annotations

import re

__all__ = ["scaffold_source", "normalize_plugin_name"]


def normalize_plugin_name(name: str) -> str:
    """插件名规范化：小写、非法字符转下划线。**不加** ``auto-`` 前缀
    （那是模型自产插件的命名域，人写插件不进批量回滚域）。"""
    base = re.sub(r"[^a-z0-9_-]", "_", str(name).strip().lower()).strip("_-")
    return base or "my-plugin"


_TEMPLATE = '''\
"""{name} —— OpenX 插件（由 /plugin new 生成，改掉 TODO 即是你的了）。"""
__openx_meta__ = {{
    "type": "{ptype}",
    "mount": "{mount}",
    "trust": "user",          # 人写插件：user 级（模型自产是 auto）
    "summary": "TODO: 一句话描述这个插件干什么（进 /plugins 清单）",
    # permissions = 能力上限的声明：不声明 → 工具自声明级别不被信任
    # （压到 ASK、auto_allowed 失效）。用到什么声明什么，如：
    # "permissions": ["fs:read"],
}}

from openx.tools.base import Tool, ToolResult


class {class_name}(Tool):
    name = "{tool_name}"
    description = "TODO: 模型据此决定何时调用（一句话说清输入输出）"
    parameters = {{
        "type": "object",
        "properties": {{
            "input": {{"type": "string", "description": "TODO: 参数含义"}},
        }},
    }}

    @property
    def permission(self):
        # 自声明权限级：ALLOW 不弹窗 / ASK 每次确认（manifest 未声明
        # permissions 时此声明会被压到 ASK——见上方注释）。
        from openx.permissions import Permission, PermissionLevel
        return Permission(level=PermissionLevel.ASK,
                          reason="TODO: 为什么需要这个级别")

    async def execute(self, input: str = "", **kw):
        # TODO: 实现。异常会被 ProtectPluginTool 收敛成结构化错误；
        # 连续崩溃 3 次熔断自动摘除。
        return ToolResult(output=f"echo: {{input}}")


def apply(ctx):
    ctx.register_tool({class_name}())


def self_test():
    """admission 门的放行前提（N6：宁缺勿滥）——装机即跑，挂了拒载。"""
    import asyncio

    tool = {class_name}()
    result = asyncio.run(tool.execute(input="ping"))
    assert result.success and "ping" in result.output
'''

# type → mount 由协议表派生（与 write_plugin 同一事实源）
_TYPE_TO_MOUNT = {
    "capability.tool": "loop.tool-call",
    "context.memory": "loop.pre-inference",
    "lifecycle": "lifecycle.session",
    "ui.panel": "ui.deck",
}


def scaffold_source(name: str, ptype: str = "capability.tool") -> str:
    """生成插件源码。type 的挂载点按协议表；未知 type 原样落 manifest
    （装载时按协议默认路由 + 警告，与手写一致）。"""
    tool_name = normalize_plugin_name(name).replace("-", "_")
    class_name = "".join(
        part.capitalize() if part else "_" for part in tool_name.split("_")
    ) + "Tool"
    return _TEMPLATE.format(
        name=normalize_plugin_name(name),
        ptype=ptype,
        mount=_TYPE_TO_MOUNT.get(ptype, "loop.tool-call"),
        class_name=class_name,
        tool_name=tool_name,
    )


if __name__ == "__main__":
    import ast

    src = scaffold_source("my-checker")
    ast.parse(src)  # 语法必须成立
    assert "my_checker" in src and "MyCheckerTool" in src
    assert "permissions" in src  # S1c 语义注释在场
    print("openx/tools/plugin_scaffold.py OK ✓")
