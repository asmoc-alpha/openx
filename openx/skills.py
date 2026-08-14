"""Skill management for OpenX — installable instruction packs.

Skills 是 Markdown 文件，为 agent 注入领域专属指令（类似 Claude Code 的
Skill 机制）。每个 ``.md`` 文件包含极简 frontmatter + 正文指令::

    ---
    name: docker-expert
    description: Best practices for Dockerfile and compose files.
    trigger: docker, container, compose
    ---
    When working with Docker files, always:
    - Use multi-stage builds ...

存放位置（两级，项目级覆盖全局同名）：

- 全局：``~/.openx/skills/*.md``
- 项目：``<workspace>/.openx/skills/*.md``

核心承诺：skill 加载绝不拖垮 agent 启动——坏文件打印警告并跳过。
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

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# 全局 skills 目录
GLOBAL_SKILLS_DIR = Path.home() / ".openx" / "skills"


@dataclass
class Skill:
    """一个已解析的 skill 定义。"""

    name: str
    description: str = ""
    trigger: list[str] = field(default_factory=list)  # 触发关键词
    content: str = ""                                  # 正文指令
    source: str = ""                                   # 来源路径
    level: str = "global"                              # "global" | "project"


def _parse_skill_md(path: Path, level: str = "global") -> Skill:
    """解析单个 skill ``.md`` 文件：``---`` frontmatter + 正文。

    极简手写解析（无 PyYAML）：仅支持 ``key: value`` 单行。
    缺少开/闭 ``---`` 抛 ValueError，由调用方捕获降级。
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
    trigger_raw = meta.get("trigger", "")
    triggers = [t.strip().lower() for t in trigger_raw.split(",") if t.strip()]
    body = "\n".join(lines[closing + 1:]).strip()
    return Skill(
        name=meta.get("name") or path.stem,
        description=meta.get("description", ""),
        trigger=triggers,
        content=body,
        source=str(path),
        level=level,
    )


def load_skills(workspace: str | Path) -> dict[str, Skill]:
    """加载所有 skills：全局 + 项目级（同名以项目为准）。

    目录缺失 → 跳过；单个文件损坏 → 打印警告并跳过，绝不抛异常。
    """
    skills: dict[str, Skill] = {}

    # 全局 skills
    if GLOBAL_SKILLS_DIR.is_dir():
        for md in sorted(GLOBAL_SKILLS_DIR.glob("*.md")):
            try:
                skill = _parse_skill_md(md, level="global")
                skills[skill.name] = skill
            except Exception as e:
                print(f"warning: skipping malformed skill file {md.name}: {e}")

    # 项目级 skills（覆盖全局同名）
    project_dir = Path(workspace) / ".openx" / "skills"
    if project_dir.is_dir():
        for md in sorted(project_dir.glob("*.md")):
            try:
                skill = _parse_skill_md(md, level="project")
                skills[skill.name] = skill
            except Exception as e:
                print(f"warning: skipping malformed skill file {md.name}: {e}")

    return skills


def install_skill(
    source_path: str | Path,
    workspace: str | Path | None = None,
    *,
    global_install: bool = True,
) -> Skill:
    """从本地文件安装一个 skill。

    把 ``.md`` 文件复制到目标 skills 目录（全局或项目级）。
    返回解析后的 Skill 对象。

    Raises:
        FileNotFoundError: 源文件不存在。
        ValueError: 文件格式不合法（无法解析 frontmatter）。
    """
    src = Path(source_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"Skill file not found: {src}")

    # 先验证格式
    skill = _parse_skill_md(src, level="global" if global_install else "project")

    # 确定目标目录
    if global_install or workspace is None:
        target_dir = GLOBAL_SKILLS_DIR
    else:
        target_dir = Path(workspace) / ".openx" / "skills"
    target_dir.mkdir(parents=True, exist_ok=True)

    # 复制文件（以 skill name 命名，避免冲突）
    dest = target_dir / f"{skill.name}.md"
    shutil.copy2(src, dest)
    skill.source = str(dest)
    skill.level = "global" if global_install else "project"
    return skill


def install_skill_from_content(
    name: str,
    description: str,
    content: str,
    trigger: list[str] | None = None,
    workspace: str | Path | None = None,
    *,
    global_install: bool = True,
) -> Skill:
    """从内容直接创建并安装一个 skill。

    生成 frontmatter + 正文的 ``.md`` 文件写入目标目录。
    """
    if global_install or workspace is None:
        target_dir = GLOBAL_SKILLS_DIR
    else:
        target_dir = Path(workspace) / ".openx" / "skills"
    target_dir.mkdir(parents=True, exist_ok=True)

    # 构建 .md 内容
    lines = ["---", f"name: {name}", f"description: {description}"]
    if trigger:
        lines.append(f"trigger: {', '.join(trigger)}")
    lines.append("---")
    lines.append(content)
    md_content = "\n".join(lines) + "\n"

    dest = target_dir / f"{name}.md"
    dest.write_text(md_content, encoding="utf-8")

    return Skill(
        name=name,
        description=description,
        trigger=trigger or [],
        content=content,
        source=str(dest),
        level="global" if global_install else "project",
    )


def uninstall_skill(
    name: str,
    workspace: str | Path | None = None,
) -> bool:
    """卸载一个 skill（先查项目级，再查全局）。返回是否成功。"""
    # 项目级优先
    if workspace:
        project_file = Path(workspace) / ".openx" / "skills" / f"{name}.md"
        if project_file.is_file():
            project_file.unlink()
            return True
    # 全局
    global_file = GLOBAL_SKILLS_DIR / f"{name}.md"
    if global_file.is_file():
        global_file.unlink()
        return True
    return False


def build_skills_prompt(skills: dict[str, Skill]) -> str:
    """把所有已加载 skill 的指令构建成系统提示片段。

    无 skill 时返回空字符串。
    """
    if not skills:
        return ""
    parts = ["\n\n## Installed Skills\n"]
    parts.append(
        "The following skills provide specialized instructions. "
        "Follow them when working on related tasks.\n"
    )
    for skill in skills.values():
        parts.append(f"### Skill: {skill.name}")
        if skill.description:
            parts.append(f"*{skill.description}*")
        if skill.content:
            parts.append(skill.content)
        parts.append("")  # blank line separator
    return "\n".join(parts)


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as _td:
        td = Path(_td)

        # 创建测试 skill 文件
        skills_dir = td / ".openx" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "docker.md").write_text(
            "---\nname: docker-expert\n"
            "description: Docker best practices.\n"
            "trigger: docker, container\n---\n"
            "Always use multi-stage builds.\n",
            encoding="utf-8",
        )
        (skills_dir / "broken.md").write_text(
            "no frontmatter here\n", encoding="utf-8"
        )

        loaded = load_skills(td)
        assert "docker-expert" in loaded
        assert loaded["docker-expert"].trigger == ["docker", "container"]
        assert "multi-stage" in loaded["docker-expert"].content
        assert "broken" not in loaded  # 坏文件跳过
        print(f"load_skills: {sorted(loaded)} ✓")

        # build_skills_prompt
        prompt = build_skills_prompt(loaded)
        assert "## Installed Skills" in prompt
        assert "docker-expert" in prompt
        print(f"build_skills_prompt: {len(prompt)} chars ✓")

        # install_skill_from_content
        skill = install_skill_from_content(
            name="test-skill",
            description="A test skill",
            content="Do something useful.",
            trigger=["test"],
            workspace=td,
            global_install=False,
        )
        assert (skills_dir / "test-skill.md").is_file()
        print(f"install_skill_from_content: {skill.name} → {skill.source} ✓")

        # uninstall_skill
        assert uninstall_skill("test-skill", workspace=td)
        assert not (skills_dir / "test-skill.md").exists()
        assert not uninstall_skill("nonexistent", workspace=td)
        print("uninstall_skill ✓")

    print("openx/skills.py OK ✓")
