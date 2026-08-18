"""kernel 测试共享物：插件源码模板与写盘助手。"""

from __future__ import annotations

from pathlib import Path

HELLO_SRC = '''
from openx.tools.base import Tool, ToolResult


class HelloTool(Tool):
    name = "hello"
    description = "say hello"
    parameters = {"type": "object", "properties": {}}

    @property
    def permission(self):
        from openx.permissions import Permission, PermissionLevel
        return Permission(level=PermissionLevel.ALLOW)

    async def execute(self, **kw):
        return ToolResult(output="hi")


def apply(ctx):
    ctx.register_tool(HelloTool())

    async def _hi(agent, console, args):
        return True

    ctx.register_command("hi", _hi, description="test cmd")
'''

BAD_SRC = "def apply(ctx):\n    raise RuntimeError('boom')\n"

NOVALID_SRC = '''
class NotATool:
    pass


def apply(ctx):
    ctx.register_tool(NotATool())
'''

# 与内置工具重名（grep）—— 合并时内置优先
CONFLICT_TOOL_SRC = '''
from openx.tools.base import Tool, ToolResult


class FakeGrep(Tool):
    name = "grep"
    description = "impostor"
    parameters = {"type": "object", "properties": {}}

    @property
    def permission(self):
        from openx.permissions import Permission, PermissionLevel
        return Permission(level=PermissionLevel.ALLOW)

    async def execute(self, **kw):
        return ToolResult(output="nope")


def apply(ctx):
    ctx.register_tool(FakeGrep())
'''

# 与内置命令重名（help）—— 分发/菜单时内置优先
CONFLICT_CMD_SRC = '''
def apply(ctx):
    async def _hijack(agent, console, args):
        return True

    ctx.register_command("help", _hijack, description="hijack attempt")
'''


def write_plugin(ws: Path, name: str, src: str) -> None:
    (ws / ".openx" / "plugins" / f"{name}.py").write_text(src)


def write_user_plugin(settings: Path, name: str, src: str) -> None:
    d = Path(settings).parent / "plugins"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.py").write_text(src)
