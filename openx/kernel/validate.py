"""加载期校验器 —— 约束即代码。

微内核验*形状*（声明完整、接口合规）；语义裁决（该不该弹窗、允不
允许执行）在控制平面，两层不混。校验失败 = 拒载并记入 inventory，
不炸主进程。
"""

from __future__ import annotations

import inspect
import re

_COMMAND_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_PROVIDER_KIND = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def validate_tool(name: str, tool: object) -> list[str]:
    """工具形状校验：permission 自声明必填（裁决权在控制平面）。"""
    problems: list[str] = []
    tname = getattr(tool, "name", None)
    if not isinstance(tname, str) or not tname:
        problems.append("tool.name missing or not a str")
    elif tname != name:
        problems.append(f"tool.name {tname!r} != registered name {name!r}")
    if not getattr(tool, "description", ""):
        problems.append("tool.description empty")
    if not isinstance(getattr(tool, "parameters", None), dict):
        problems.append("tool.parameters not a dict")
    # permission 是 Tool 的 property；任意对象无此属性 → 声明缺失。
    level = getattr(getattr(tool, "permission", None), "level", None)
    if level is None:
        problems.append("tool.permission declaration missing (required)")
    if not callable(getattr(tool, "execute", None)):
        problems.append("tool.execute not callable")
    return problems


def validate_command(name: str, value: object) -> list[str]:
    """命令形状校验：名字小写连字符、handler 为协程函数。"""
    problems: list[str] = []
    if not _COMMAND_NAME.match(name):
        problems.append(f"command name {name!r} not [a-z0-9_-]+")
    contrib = getattr(value, "handler", None)
    if contrib is None:  # value 应为 CommandContribution
        problems.append("command contribution malformed")
    elif not inspect.iscoroutinefunction(contrib):
        problems.append("command handler not an async function")
    aliases = getattr(value, "aliases", [])
    if not isinstance(aliases, list) or not all(
        isinstance(a, str) and _COMMAND_NAME.match(a) for a in aliases
    ):
        problems.append("command aliases must be list of [a-z0-9_-]+ strs")
    return problems


def validate_provider(name: str, value: object) -> list[str]:
    """providers 注册项校验：实现名小写连字符，值为可调用工厂。

    工厂签名 ``create(settings: dict) -> Provider``；返回 Provider 的
    接口形状由内核 RetryingProvider 包装时自然暴露（形状错误在使用处
    炸，符合"注册期只验能验的"原则）。
    """
    problems: list[str] = []
    if not _PROVIDER_KIND.match(name):
        problems.append(f"provider kind {name!r} not [a-z0-9-]+")
    if not callable(value):
        problems.append(f"provider factory not callable")
    return problems
