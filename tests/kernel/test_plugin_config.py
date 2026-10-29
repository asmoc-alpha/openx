"""S5a 插件配置（对标 Cordis Config）：manifest schema → 用户配置校验/注入。

覆盖：
- 合法配置装载成功，`ctx.config` 注入 apply（含 default 归并）；
- 校验失败（缺 required / 类型错）→ FAILED 且 error 带原因；
- 未声明 schema 的插件：settings 原样注入；
- `/plugin config` 查看 / 设置（落盘 + 重组生效）/ reset；
- 配置段进组合键：改配置 → ensure_loaded 自动重组。

运行：``python -m pytest tests/kernel/test_plugin_config.py -q``
"""

from __future__ import annotations

import json

from openx.app.cli import commands
from openx.config import OpenXConfig
from openx.kernel import get_kernel
from openx.kernel.assembly.manifest import config_schema_of, validate_manifest
from openx.kernel.assembly.plugin_config import merge_defaults, validate_config

from ._helpers import merge_settings, write_plugin

SCHEMA = {
    "type": "object",
    "properties": {
        "greeting": {"type": "string", "default": "hello"},
        "times": {"type": "integer", "minimum": 1, "default": 1},
    },
    "required": ["greeting"],
}


def _plugin_src() -> str:
    return (
        "__openx_meta__ = " + json.dumps({
            "summary": "configured greeter",
            "trust": "user",
            "config": SCHEMA,
        }) + "\n"
        "from openx.tools.base import Tool, ToolResult\n"
        "SEEN = {}\n"
        "class Greeter(Tool):\n"
        "    name = 'cfg-greet'\n"
        "    description = 'g'\n"
        "    parameters = {'type': 'object', 'properties': {}}\n"
        "    async def execute(self, **kw):\n"
        "        from openx.tools.base import ToolResult as TR\n"
        "        return TR(output=SEEN['greeting'])\n"
        "def apply(ctx):\n"
        "    SEEN.update(ctx.config)\n"
        "    ctx.register_tool(Greeter())\n"
        "def self_test():\n    assert True\n"
    )


def test_config_schema_manifest_gate():
    assert validate_manifest({"summary": "x", "config": {"type": "object"}}) == ([], [])
    problems, _ = validate_manifest({"summary": "x", "config": ["not", "a", "dict"]})
    assert problems and "dict" in problems[0]
    assert config_schema_of({"config": SCHEMA}) == SCHEMA
    assert config_schema_of({}) == {}


def test_validate_and_merge():
    assert validate_config({"greeting": "hi", "times": 2}, SCHEMA) == []
    problems = validate_config({}, SCHEMA)
    assert any("greeting" in p for p in problems)
    merged = merge_defaults(SCHEMA, {"greeting": "hi"})
    assert merged == {"greeting": "hi", "times": 1}


def test_valid_config_injected_with_defaults(kernel_env):
    ws, settings = kernel_env
    write_plugin(ws, "greeter", _plugin_src())
    merge_settings(settings, {"plugins": {"config": {"greeter": {"greeting": "你好"}}}})
    k = get_kernel()
    k.ensure_loaded(str(ws))
    # default 归并：未设置的 times=1 进 ctx.config（工具注册时可见）
    import sys
    mod = sys.modules["_openx_plugin_greeter"]
    assert mod.SEEN == {"greeting": "你好", "times": 1}


def test_invalid_config_fails_load(kernel_env):
    ws, settings = kernel_env
    write_plugin(ws, "greeter", _plugin_src())
    merge_settings(settings, {"plugins": {"config": {"greeter": {"times": 3}}}})
    k = get_kernel()
    k.ensure_loaded(str(ws))
    entry = next(c for c in k.list_plugins() if c["id"] == "greeter")
    assert entry["phase"] == "failed"
    assert "greeting" in k.plugin_help("greeter")["error"]


def test_no_schema_plugin_gets_raw_settings(kernel_env):
    ws, settings = kernel_env
    write_plugin(ws, "rawplug", (
        '__openx_meta__ = {"summary": "raw"}\n'
        "SEEN = {}\n"
        "def apply(ctx):\n    SEEN.update(ctx.config)\n"
    ))
    merge_settings(settings, {"plugins": {"config": {"rawplug": {"k": "v"}}}})
    k = get_kernel()
    k.ensure_loaded(str(ws))
    import sys
    assert sys.modules["_openx_plugin_rawplug"].SEEN == {"k": "v"}


async def test_plugin_config_command_view_set_reset(kernel_env):
    ws, settings = kernel_env
    write_plugin(ws, "greeter", _plugin_src())
    k = get_kernel()
    k.ensure_loaded(str(ws))

    class _C:
        infos: list = []
        successes: list = []
        warnings: list = []
        raw_lines: list = []
        raw = None

        def print(self, *a, **k):
            self.raw_lines.append(" ".join(str(x) for x in a))

        def print_info(self, m): self.infos.append(m)
        def print_success(self, m): self.successes.append(m)
        def print_warning(self, m): self.warnings.append(m)

    class _A:
        workspace = ws

    stub = _C()
    stub.raw = stub
    # 查看：schema + 当前值
    assert await commands.handle_slash_command(
        "plugin", _A(), stub, ["config", "greeter"]) is True
    blob = "\n".join(stub.raw_lines)
    assert "greeting" in blob and "current" in blob

    # 设置：校验通过 → 落盘 → 重组生效（工具输出反映新配置）
    assert await commands.handle_slash_command(
        "plugin", _A(), stub,
        ["config", "greeter", json.dumps({"greeting": "salut", "times": 2})],
    ) is True
    assert stub.successes
    # 直接经注册表工厂实例化后调用
    factory = k.registry("tools").get("cfg-greet").value
    result = await factory({})[0].execute()
    assert result.output == "salut"
    assert OpenXConfig.load_plugin_config("greeter") == {"greeting": "salut", "times": 2}

    # 非法 JSON / 校验失败
    stub2 = _C()
    stub2.raw = stub2
    assert await commands.handle_slash_command(
        "plugin", _A(), stub2, ["config", "greeter", "{bad json"]) is True
    assert stub2.warnings and "JSON" in stub2.warnings[-1]
    n = len(OpenXConfig.load_plugin_config("greeter"))
    assert await commands.handle_slash_command(
        "plugin", _A(), stub2, ["config", "greeter", json.dumps({"times": 5})]) is True
    assert any("greeting" in w for w in stub2.warnings)  # 缺 required 被拒

    # reset：清空
    assert await commands.handle_slash_command(
        "plugin", _A(), stub2, ["config", "greeter", "reset"]) is True
    assert OpenXConfig.load_plugin_config("greeter") == {}
