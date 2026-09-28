"""base bundle 内置插件之一：内置 prompt 片段（builtin-context，K7 收口）。

系统提示里那些**硬连线**的提示源--OPENX.md 指令（``instructions.py``）、
skills 目录（``skills.py``）、记忆系统使用说明（``MEMORY_INSTRUCTIONS``）
--从 agent 的直连 import 升格为 **context/v1 注册项**（"一切能力皆插件"，
断掉"loop 认识具体提示源"的零引用破洞）。

消费面 = ``services/assembly.py::collect_context_fragments``：pre-inference
阶段按 **priority 升序** + 字符预算 + 单插件崩溃隔离征集，片段并入系统提示。
片段生产者按**形参容量**收到只读 ``PromptHost``（``kernel/assembly/context.py``）
--``host is None``（子代理 / 测试的无 host 调用）时本插件零贡献，安全。

优先级排列复刻收口前的提示顺序：指令(10) → 记忆提示(40) → skills(50)，
用户上下文插件走默认 100（= 永远排在内置片段之后）。

失败语义：内置插件 apply 抛异常 = 致命（产品带病不该运行）；禁用表对
内置 id 无效。
"""

from __future__ import annotations

# ── 独立调试支持：允许直接运行本文件（python openx/builtin/context.py）──
if __name__ == "__main__" and not __package__:
    import sys as _sys
    from pathlib import Path as _Path

    _file = _Path(__file__).resolve()
    _root = _file.parent
    while _root != _root.parent and not (_root / "pyproject.toml").exists():
        _root = _root.parent
    _sys.path.insert(0, str(_root))
    __package__ = ".".join(_file.relative_to(_root).parts[:-1])

# P-A/P-B 自描述：内置上下文片段恒常驻（非动态装配），装配预算 0。
__openx_meta__ = {
    "type": "context.memory",
    "mount": "loop.pre-inference",
    "trust": "builtin",
    "summary": "内置 prompt 片段：OPENX.md 指令 / skills 目录 / 记忆系统说明",
    "cost": {"schemaTokens": 0},
}

from ..instructions import format_instructions
from ..skills import build_skills_prompt

# 内置片段的优先级（复刻收口前的提示顺序；用户插件默认 100 恒在其后）
PRIORITY_INSTRUCTIONS = 10
PRIORITY_MEMORY_HINTS = 40
PRIORITY_SKILLS = 50


# ── 记忆系统使用说明（K7 从 tools/memory_tool.py 迁来）──────────────
# 片段不含前导空行（context/v1 贡献契约：分隔由消费方统一施加）。

MEMORY_INSTRUCTIONS = """\
## Memory System

You have a persistent coding memory (tool: `memory`). Use it autonomously:

**Auto-remember when:**
- User corrects you ("no, we use X not Y") → remember the correct approach
- You discover project conventions by reading code → remember for next session
- A design/architecture decision is made → remember the rationale
- You solve a non-obvious bug → remember the pattern
- User expresses a preference → remember it

**Auto-recall when:**
- Starting work on a file/module → recall related conventions
- About to make a design choice → recall prior decisions
- Encountering an error → recall past debug patterns

**Rules:**
- Be concise: one memory = one atomic fact
- Always include keywords for future retrieval
- Link related_paths when the memory is file-specific
- Don't store what's obvious from reading the code
- Don't store temporary task details
"""


# ── 三条内置贡献（host=None 时零贡献，供无 host 调用安全）─────────────


def contribute_instructions(host=None):
    """OPENX.md 指令块（不含 base 系统提示--那是 loop 的常驻身份）。"""
    if host is None:
        return ""
    return format_instructions(getattr(host, "instructions", None))


def contribute_memory_hints(host=None):
    """记忆系统使用说明（常驻常量，与 host 无关）。"""
    return MEMORY_INSTRUCTIONS


def contribute_skills(host=None):
    """skills 目录（渐进披露：只注入名录，正文按需加载）。"""
    if host is None:
        return ""
    # 去前导空行：片段契约不放分隔符，消费方统一加
    return build_skills_prompt(dict(getattr(host, "skills", None) or {})).strip()


def apply(ctx) -> None:
    """内置插件入口：注册三条 context/v1 贡献（priority 决定征集序）。"""
    ctx.register_context(
        "instructions", contribute_instructions, priority=PRIORITY_INSTRUCTIONS
    )
    ctx.register_context(
        "memory_hints", contribute_memory_hints, priority=PRIORITY_MEMORY_HINTS
    )
    ctx.register_context(
        "skills", contribute_skills, priority=PRIORITY_SKILLS
    )


def _self_check() -> None:
    """进程内自检：无 host 零贡献；有 host 生产片段且顺序正确。"""
    from pathlib import Path

    from ..instructions import InstructionLevel, InstructionRegistry, InstructionSet
    from ..kernel.assembly.context import PromptHost

    # 无 host（子代理 / 测试的无 host 调用）-> 零贡献，不炸
    assert contribute_instructions() == ""
    assert contribute_skills() == ""
    assert contribute_memory_hints(), "记忆提示是常驻常量，与 host 无关"

    reg = InstructionRegistry(
        project_instructions=InstructionSet(
            level=InstructionLevel.PROJECT,
            path=Path("/ws/OPENX.md"),
            content="Always run the tests.",
        )
    )
    host = PromptHost(workspace="/ws", instructions=reg, skills={})
    out = contribute_instructions(host)
    assert "Always run the tests." in out and "## Project Instructions" in out
    print("openx/builtin/context.py OK ✓")


if __name__ == "__main__":
    _self_check()
