"""Sub-agent specs — declarative definitions of delegatable agent types (Phase 8).

子代理规格定义（Phase 8）
=========================
- :class:`SubagentSpec` 描述一种可委派的子代理类型：名字、用途说明、
  允许的工具集（``None`` = 除结构性排除外全部）、可选模型覆盖、以及
  追加进子代理系统提示的角色指令（来自 ``.md`` 正文）。
- 内置两种：``general-purpose``（全工具，除 task/ask_user/exit_plan_mode）
  与 ``explore``（只读搜索专用）。
- 项目可在 ``<workspace>/.openx/agents/*.md`` 里用极简 frontmatter 追加
  或按名覆盖规格；格式::

      ---
      name: reviewer
      description: Reviews code for quality issues.
      tools: read_file, grep
      model: optional-model-override
      ---
      You review code.  ← 正文成为 system_prompt_extra

  故意不用 PyYAML：frontmatter 只支持 ``key: value`` 单行，手写解析约 20 行。
  坏文件打印警告并跳过，绝不抛异常——规格加载不能拖垮 agent 启动。
- :data:`CHILD_EXCLUDED_TOOLS` 是**所有**子代理的结构性排除集，无论规格
  如何声明：``task``（禁套娃）、``ask_user``（子代理不打断用户）、
  ``exit_plan_mode``（审批流只属于顶层）。
"""

from __future__ import annotations

# ── 独立调试支持：允许直接运行本文件（python openx/.../xxx.py）──────
if __name__ == "__main__" and not __package__:
    import sys as _sys
    from pathlib import Path as _Path
    _file = _Path(__file__).resolve()
    _root = _file.parent
    while _root != _root.parent and not (_root / "pyproject.toml").exists():
        _root = _root.parent
    _sys.path.insert(0, str(_root))
    __package__ = ".".join(_file.relative_to(_root).parts[:-1])

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class SubagentSpec:
    """一种子代理类型的声明。

    - ``tools=None`` → 允许全部工具（仍受 :data:`CHILD_EXCLUDED_TOOLS` 约束）；
      显式列表 → 只保留列表内工具（与结构性排除取交集后的最终集合）。
    - ``model=None`` → 继承父 agent 的模型配置。
    - ``system_prompt_extra`` → 追加进子代理系统提示的角色指令（.md 正文）。
    """

    name: str
    description: str
    tools: Optional[list[str]] = None      # None = all allowed (minus exclusions)
    model: Optional[str] = None            # None = inherit parent
    system_prompt_extra: str = ""          # from .md body


# 内置子代理类型（先于项目级 .md 注册；同名 .md 覆盖）
BUILTIN_SUBAGENTS: list[SubagentSpec] = [
    SubagentSpec(
        "general-purpose",
        "General-purpose agent for multi-step tasks and broad codebase search. "
        "Has all tools except task/ask_user.",
        tools=None,
    ),
    SubagentSpec(
        "explore",
        "Read-only search agent for locating code across many files. "
        "Cannot modify anything.",
        tools=["read_file", "grep", "glob", "list_directory",
               "git_status", "git_diff", "git_log", "git_branch"],
    ),
]

# 结构性排除：无论规格如何声明，所有子代理都没有这些工具
# （choose_mode/exit_plan_mode 的模式交互只属于顶层，子代理不打断用户）
CHILD_EXCLUDED_TOOLS = {"task", "ask_user", "exit_plan_mode", "choose_mode"}


def load_subagent_specs(workspace: str) -> dict[str, SubagentSpec]:
    """加载子代理规格：内置优先，项目 ``.openx/agents/*.md`` 按名覆盖/扩展。

    builtins first, then ``<workspace>/.openx/agents/*.md`` override or
    extend by name. 目录缺失 → 只返回内置规格；单个文件损坏 → 打印警告
    并跳过，绝不抛异常。
    """
    specs: dict[str, SubagentSpec] = {s.name: s for s in BUILTIN_SUBAGENTS}
    agents_dir = Path(workspace) / ".openx" / "agents"
    if not agents_dir.is_dir():
        return specs
    for md in sorted(agents_dir.glob("*.md")):
        try:
            spec = _parse_subagent_md(md)
        except Exception as e:
            print(f"warning: skipping malformed subagent file {md.name}: {e}")
            continue
        specs[spec.name] = spec
    return specs


def _parse_subagent_md(path: Path) -> SubagentSpec:
    """解析单个 ``.md`` 规格文件：``---`` 包围的 ``key: value`` frontmatter + 正文。

    极简手写解析（无 PyYAML）：仅支持 ``key: value`` 单行；未知键忽略；
    ``tools`` 为逗号分隔列表，缺省 → None（全部工具）。缺少开/闭 ``---``
    抛 ValueError，由调用方捕获降级。
    """
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing opening '---' frontmatter delimiter")
    meta: dict[str, str] = {}
    closing = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            closing = i
            break
        if ":" in lines[i]:
            key, _, value = lines[i].partition(":")
            meta[key.strip().lower()] = value.strip()
    if closing is None:
        raise ValueError("missing closing '---' frontmatter delimiter")
    tools_raw = meta.get("tools", "")
    tools = [t.strip() for t in tools_raw.split(",") if t.strip()] or None
    body = "\n".join(lines[closing + 1:]).strip()
    return SubagentSpec(
        name=meta.get("name") or path.stem,
        description=meta.get("description", ""),
        tools=tools,
        model=meta.get("model") or None,
        system_prompt_extra=body,
    )


if __name__ == "__main__":
    import tempfile

    # 内置规格完整性
    _specs = {s.name: s for s in BUILTIN_SUBAGENTS}
    assert _specs["general-purpose"].tools is None
    assert "read_file" in _specs["explore"].tools and len(_specs["explore"].tools) == 8
    assert CHILD_EXCLUDED_TOOLS == {"task", "ask_user", "exit_plan_mode", "choose_mode"}
    print(f"builtins: {sorted(_specs)} ✓")

    with tempfile.TemporaryDirectory() as _td:
        _agents = Path(_td) / ".openx" / "agents"
        _agents.mkdir(parents=True)
        (_agents / "reviewer.md").write_text(
            "---\nname: reviewer\ndescription: Reviews code.\n"
            "tools: read_file, grep\nmodel: cheap-model\n---\nYou review code.\n",
            encoding="utf-8",
        )
        (_agents / "broken.md").write_text(
            "---\nname: broken\nno closing fence here\n", encoding="utf-8"
        )
        _loaded = load_subagent_specs(_td)
        assert _loaded["reviewer"].tools == ["read_file", "grep"]
        assert _loaded["reviewer"].model == "cheap-model"
        assert "You review code." in _loaded["reviewer"].system_prompt_extra
        assert "broken" not in _loaded  # 坏文件被跳过
        assert set(_loaded) >= {"general-purpose", "explore"}
        print(f"load: {sorted(_loaded)} (malformed skipped) ✓")

    with tempfile.TemporaryDirectory() as _td2:
        # 目录缺失 → 仅内置
        assert set(load_subagent_specs(_td2)) == {"general-purpose", "explore"}
    print("openx/orchestration/subagent.py OK ✓")
