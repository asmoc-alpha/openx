"""Skill management for OpenX — SKILL.md instruction packs.

Skills 是**目录**，每个技能目录含一个 ``SKILL.md``（frontmatter + 正文指令），
可附带 supporting files（脚本/模板/参考文档）。格式对齐 Agent Skills 开放标准
/ Claude Code::

    ~/.openx/skills/<name>/SKILL.md            # 个人级（所有项目）
    <workspace>/.openx/skills/<name>/SKILL.md  # 项目级（随仓库提交共享）

frontmatter（极简手写解析，无 PyYAML）::

    ---
    name: docker-expert
    description: Best practices for Dockerfile and compose files.
    allowed-tools: read_file grep shell
    license: MIT
    ---
    When working with Docker files, always:
    - Use multi-stage builds ...

**渐进披露（progressive disclosure）**：系统提示只注入 ``name + description``
的**目录**（:func:`build_skills_prompt`）；正文经 ``/<name>`` 调用或 ``skill``
工具按需加载（:func:`load_skill_body`）。长参考材料在被用到之前不占上下文。

向后兼容：

- 仍读取旧**扁平**布局 ``<dir>/<name>.md``（标记 ``legacy=True``）；
- 只读地识别 Claude 自己的目录 ``~/.claude/skills`` / ``<ws>/.claude/skills``
  （互操作，可被本家同名 skill 覆盖）。

装载优先级（低 → 高，同名后者胜）::

    ~/.claude/skills  <  <ws>/.claude/skills
                      <  旧扁平 ~/.openx/skills/*.md  <  旧扁平 <ws>/.openx/skills/*.md
                      <  ~/.openx/skills/*/SKILL.md   <  <ws>/.openx/skills/*/SKILL.md

即「项目 > 个人、本家 > 互操作、目录 > 旧扁平」。

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

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# ── 目录与常量 ──────────────────────────────────────────────────

# 本家 skills 目录（个人级）
GLOBAL_SKILLS_DIR = Path.home() / ".openx" / "skills"
# Claude 自己的 skills 目录（只读互操作）
CLAUDE_SKILLS_DIR = Path.home() / ".claude" / "skills"
# 技能定义文件名（Agent Skills 标准）
SKILL_FILE = "SKILL.md"

# 名字与描述上限（对齐 Agent Skills 标准）
NAME_MAX = 64
DESCRIPTION_MAX = 1024

# 合法 skill 名：小写字母/数字，连字符分隔（如 docker-expert）
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# 动态上下文注入：!`cmd`（执行后以其输出替换）
_INJECT_RE = re.compile(r"!`([^`\n]+)`")


def validate_name(name: str) -> str | None:
    """校验 skill 名；合法返回 ``None``，否则返回错误说明。"""
    if not name:
        return "skill name cannot be empty"
    if len(name) > NAME_MAX:
        return f"skill name exceeds {NAME_MAX} characters"
    if not _NAME_RE.match(name):
        return (
            "skill name must be lowercase letters, digits and single hyphens "
            f"(got {name!r})"
        )
    return None


# ── 数据模型 ────────────────────────────────────────────────────


@dataclass
class Skill:
    """一个已解析的 skill 定义。"""

    name: str
    description: str = ""
    allowed_tools: list[str] = field(default_factory=list)  # 预批准工具
    license: str = ""
    argument_hint: str = ""                                 # /<name> 参数提示
    body: str = ""                                          # 正文指令
    trigger: list[str] = field(default_factory=list)        # OpenX 旧键（容忍）
    directory: str = ""                                     # 技能目录（目录布局）
    source: str = ""                                        # SKILL.md / .md 路径
    level: str = "global"      # global | project | claude | claude-project
    legacy: bool = False       # 旧扁平布局（<name>.md）

    @property
    def content(self) -> str:
        """正文别名（旧字段名，serve/Web 兼容）。"""
        return self.body


# ── frontmatter 解析 ────────────────────────────────────────────


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """拆 ``---`` frontmatter 与正文，返回 ``(meta, body)``。

    极简手写解析（无 PyYAML）：仅支持 ``key: value`` 单行。缺少开/闭
    ``---`` 分隔符抛 ``ValueError``，由调用方捕获降级。
    """
    lines = text.splitlines()
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
    return meta, "\n".join(lines[closing + 1:]).strip()


def _split_list(value: str) -> list[str]:
    """把 ``allowed-tools`` 值切成列表（逗号与空白均可作分隔符）。"""
    return [t for t in re.split(r"[,\s]+", value.strip()) if t]


def parse_skill_text(
    text: str,
    *,
    name: str,
    level: str = "global",
    directory: str = "",
    source: str = "",
    legacy: bool = False,
) -> Skill:
    """从 ``SKILL.md`` 文本解析出 :class:`Skill`（纯函数，不碰盘）。

    目录布局下**目录名即命令名**（对齐 Agent Skills），frontmatter 的
    ``name`` 仅在扁平布局下作为回退。
    """
    meta, body = _split_frontmatter(text)
    resolved = directory and Path(directory).name or meta.get("name") or name
    error = validate_name(resolved)
    if error:
        raise ValueError(error)
    description = meta.get("description", "")
    if len(description) > DESCRIPTION_MAX:
        description = description[:DESCRIPTION_MAX]
    return Skill(
        name=resolved,
        description=description,
        allowed_tools=_split_list(meta.get("allowed-tools", "")),
        license=meta.get("license", ""),
        argument_hint=meta.get("argument-hint", ""),
        body=body,
        trigger=[t.lower() for t in _split_list(meta.get("trigger", ""))],
        directory=directory,
        source=source,
        level=level,
        legacy=legacy,
    )


def _parse_skill_md(
    path: Path,
    level: str,
    *,
    directory: str | None = None,
    legacy: bool = False,
) -> Skill:
    """解析单个技能文件（目录布局的 ``SKILL.md`` 或旧扁平 ``.md``）。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    return parse_skill_text(
        text,
        name=Path(directory).name if directory else path.stem,
        level=level,
        directory=directory or "",
        source=str(path),
        legacy=legacy,
    )


# ── 装载 ────────────────────────────────────────────────────────


def _iter_skill_files(root: Path, *, layout: str):
    """枚举一个 skills 根目录下的技能定义。

    ``layout="dir"`` → ``<root>/<name>/SKILL.md``（返回 ``(SKILL.md, name)``）；
    ``layout="flat"`` → ``<root>/<name>.md``（返回 ``(.md, None)``）。
    """
    if layout == "dir":
        for child in sorted(root.iterdir()):
            if child.is_dir():
                skill_md = child / SKILL_FILE
                if skill_md.is_file():
                    yield skill_md, str(child)
    else:
        for md in sorted(root.glob("*.md")):
            yield md, None


def load_skills(
    workspace: str | Path | None,
    *,
    global_dir: str | Path | None = None,
    claude_dir: str | Path | None = None,
) -> dict[str, Skill]:
    """加载所有 skills（低 → 高优先级叠加，同名后者覆盖）。

    ``global_dir`` / ``claude_dir`` 可注入（测试用，避免触碰真实 home）。
    目录缺失 → 跳过；单个文件损坏 → 打印警告并跳过，**绝不抛异常**。
    """
    ws = Path(workspace) if workspace else None
    gdir = Path(global_dir) if global_dir is not None else GLOBAL_SKILLS_DIR
    cdir = Path(claude_dir) if claude_dir is not None else CLAUDE_SKILLS_DIR

    # (root, level, layout, legacy) —— 顺序即优先级，低到高
    sources: list[tuple[Path, str, str, bool]] = [
        (cdir, "claude", "dir", False),
    ]
    if ws is not None:
        sources.append((ws / ".claude" / "skills", "claude-project", "dir", False))
    sources.append((gdir, "global", "flat", True))
    if ws is not None:
        sources.append((ws / ".openx" / "skills", "project", "flat", True))
    sources.append((gdir, "global", "dir", False))
    if ws is not None:
        sources.append((ws / ".openx" / "skills", "project", "dir", False))

    skills: dict[str, Skill] = {}
    for root, level, layout, legacy in sources:
        if not root.is_dir():
            continue
        for path, directory in _iter_skill_files(root, layout=layout):
            try:
                skill = _parse_skill_md(
                    path, level, directory=directory, legacy=legacy,
                )
            except Exception as e:
                print(f"warning: skipping malformed skill file {path.name}: {e}")
                continue
            skills[skill.name] = skill
    return skills


# ── 渐进披露：目录 + 按需正文 ───────────────────────────────────


def build_skills_prompt(skills: dict[str, Skill]) -> str:
    """把已加载 skill 构建成系统提示中的**目录**（不含正文）。

    正文经 ``/<name>`` 或 ``skill`` 工具按需加载——长参考材料在被用到
    之前不占上下文（对齐 Agent Skills 的渐进披露）。无 skill 时返回空串。
    """
    if not skills:
        return ""
    parts = ["\n\n## Available Skills\n"]
    parts.append(
        "Each skill below is an instruction pack that is NOT loaded yet. "
        "When a task matches a skill's description, load it before proceeding: "
        "the user types `/<skill-name>`, or you call the `skill` tool with its name. "
        "Then follow the loaded instructions for the rest of the task.\n"
    )
    for skill in skills.values():
        line = f"- `{skill.name}`: {skill.description or '(no description)'}"
        if skill.allowed_tools:
            line += f"  [pre-approved: {', '.join(skill.allowed_tools)}]"
        if skill.legacy:
            line += "  [legacy layout]"
        parts.append(line)
    return "\n".join(parts) + "\n"


def extract_injected_commands(body: str) -> list[str]:
    """抽取正文里的 ``!`cmd` `` 动态注入命令（**不执行**，由调用方过闸）。"""
    return [
        m.group(1).strip() for m in _INJECT_RE.finditer(body) if m.group(1).strip()
    ]


def render_skill_body(skill: Skill, arguments: str = "") -> tuple[str, list[str]]:
    """渲染 skill 正文，返回 ``(正文, 待执行命令清单)``。

    ``$ARGUMENTS`` 替换为用户输入；``!`cmd` `` 命令**原样保留在正文中**并
    单独返回，由调用方经执行闸（Guard/权限/审计）跑完后再替换——skill
    文本绝不自行启动子进程。
    """
    body = skill.body.replace("$ARGUMENTS", arguments)
    return body, extract_injected_commands(skill.body)


def load_skill_body(
    skills: dict[str, Skill],
    name: str,
    arguments: str = "",
) -> tuple[Skill, str, list[str]]:
    """按名查找并渲染正文，返回 ``(skill, 正文, 待执行命令)``。

    未找到抛 ``KeyError``（调用方转成用户可读提示）。
    """
    skill = skills.get(name)
    if skill is None:
        raise KeyError(name)
    body, commands = render_skill_body(skill, arguments)
    return skill, body, commands


def list_supporting_files(skill: Skill) -> list[str]:
    """技能目录内 ``SKILL.md`` 之外的附带文件（文件名，排序）。"""
    if not skill.directory:
        return []
    directory = Path(skill.directory)
    if not directory.is_dir():
        return []
    return sorted(
        p.name for p in directory.iterdir() if p.is_file() and p.name != SKILL_FILE
    )


# ── 安装 / 卸载 ─────────────────────────────────────────────────


def _target_dir(
    workspace: str | Path | None,
    global_install: bool,
    global_dir: str | Path | None = None,
) -> Path:
    """解析安装目标根目录（全局或项目级）。"""
    if global_install or workspace is None:
        return Path(global_dir) if global_dir is not None else GLOBAL_SKILLS_DIR
    return Path(workspace) / ".openx" / "skills"


def install_skill(
    source_path: str | Path,
    workspace: str | Path | None = None,
    *,
    global_install: bool = True,
    global_dir: str | Path | None = None,
) -> Skill:
    """安装一个 skill，**统一落目录布局** ``<root>/<name>/SKILL.md``。

    接受三种输入：技能目录、``SKILL.md`` 文件（连带同目录附带文件一起
    复制）、旧扁平 ``.md``（迁移为标准布局）。

    Raises:
        FileNotFoundError: 源不存在。
        ValueError: 格式不合法（缺 frontmatter / 名字非法 / 目录无 SKILL.md）。
    """
    src = Path(source_path).expanduser().resolve()
    if src.is_dir():
        if not (src / SKILL_FILE).is_file():
            raise ValueError(f"skill directory has no {SKILL_FILE}: {src}")
        src_dir: Path | None = src
        src_file = src / SKILL_FILE
    elif src.is_file():
        if src.name != SKILL_FILE and src.suffix.lower() != ".md":
            raise ValueError(f"not a markdown skill file: {src}")
        src_dir = src.parent if src.name == SKILL_FILE else None
        src_file = src
    else:
        raise FileNotFoundError(f"Skill path not found: {src}")

    level = "global" if global_install or workspace is None else "project"
    skill = _parse_skill_md(
        src_file,
        level,
        directory=str(src_dir) if src_dir is not None else None,
    )

    dest_dir = _target_dir(workspace, global_install, global_dir) / skill.name
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    if src_dir is not None:
        # 目录布局或 SKILL.md 输入：整目录复制，保留附带文件
        shutil.copytree(src_dir, dest_dir)
    else:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest_dir / SKILL_FILE)
    skill.directory = str(dest_dir)
    skill.source = str(dest_dir / SKILL_FILE)
    skill.level = level
    skill.legacy = False
    return skill


def _skill_markdown(
    name: str,
    description: str = "",
    body: str = "",
    *,
    allowed_tools: list[str] | None = None,
    license: str = "",
    trigger: list[str] | None = None,
) -> str:
    """拼出 ``SKILL.md`` 文本（frontmatter + 正文）。"""
    lines = ["---", f"name: {name}"]
    if description:
        lines.append(f"description: {description}")
    if allowed_tools:
        lines.append(f"allowed-tools: {' '.join(allowed_tools)}")
    if license:
        lines.append(f"license: {license}")
    if trigger:
        lines.append(f"trigger: {', '.join(trigger)}")
    lines.append("---")
    lines.append(body)
    return "\n".join(lines) + "\n"


def install_skill_from_content(
    name: str,
    description: str,
    content: str,
    trigger: list[str] | None = None,
    workspace: str | Path | None = None,
    *,
    global_install: bool = True,
    allowed_tools: list[str] | None = None,
    license: str = "",
    global_dir: str | Path | None = None,
) -> Skill:
    """从内容直接创建并安装一个 skill（写目录布局 ``<name>/SKILL.md``）。"""
    error = validate_name(name)
    if error:
        raise ValueError(error)
    level = "global" if global_install or workspace is None else "project"
    dest_dir = _target_dir(workspace, global_install, global_dir) / name
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest = dest_dir / SKILL_FILE
    dest.write_text(
        _skill_markdown(
            name,
            description,
            content,
            allowed_tools=allowed_tools,
            license=license,
            trigger=trigger,
        ),
        encoding="utf-8",
    )
    return Skill(
        name=name,
        description=description,
        allowed_tools=list(allowed_tools or []),
        license=license,
        body=content,
        trigger=list(trigger or []),
        directory=str(dest_dir),
        source=str(dest),
        level=level,
    )


def uninstall_skill(
    name: str,
    workspace: str | Path | None = None,
    *,
    global_dir: str | Path | None = None,
) -> bool:
    """卸载一个 skill。项目级优先，其次全局；目录布局优先，其次旧扁平。

    返回是否成功删除了某个已存在的 skill。
    """
    gdir = Path(global_dir) if global_dir is not None else GLOBAL_SKILLS_DIR
    candidates: list[Path] = []
    if workspace:
        base = Path(workspace) / ".openx" / "skills"
        candidates += [base / name, base / f"{name}.md"]
    candidates += [gdir / name, gdir / f"{name}.md"]
    for candidate in candidates:
        if candidate.is_dir():
            shutil.rmtree(candidate)
            return True
        if candidate.is_file():
            candidate.unlink()
            return True
    return False


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as _td:
        td = Path(_td)
        gdir = td / "home" / ".openx" / "skills"
        cdir = td / "home" / ".claude" / "skills"
        ws = td / "ws"

        # ── 目录布局装载 + frontmatter 字段 ────────────────────────
        sdir = ws / ".openx" / "skills" / "docker-expert"
        sdir.mkdir(parents=True)
        (sdir / SKILL_FILE).write_text(_skill_markdown(
            "docker-expert",
            "Docker best practices.",
            "Always use multi-stage builds.\nArgs: $ARGUMENTS",
            allowed_tools=["read_file", "grep"],
            license="MIT",
            trigger=["docker", "container"],
        ), encoding="utf-8")
        (sdir / "reference.md").write_text("extra", encoding="utf-8")  # 附带文件

        loaded = load_skills(ws, global_dir=gdir, claude_dir=cdir)
        assert "docker-expert" in loaded, sorted(loaded)
        sk = loaded["docker-expert"]
        assert sk.allowed_tools == ["read_file", "grep"], sk.allowed_tools
        assert sk.license == "MIT" and sk.trigger == ["docker", "container"]
        assert sk.level == "project" and not sk.legacy
        assert list_supporting_files(sk) == ["reference.md"]
        assert sk.content == sk.body  # 旧字段名别名
        print(f"load_skills: {sorted(loaded)} ✓")

        # ── 渐进披露：目录含 description 但不含正文 ────────────────
        prompt = build_skills_prompt(loaded)
        assert "## Available Skills" in prompt
        assert "docker-expert" in prompt and "Docker best practices." in prompt
        assert "multi-stage" not in prompt, "正文不得进目录"
        print(f"build_skills_prompt: {len(prompt)} chars, body withheld ✓")

        # ── 按需加载正文：$ARGUMENTS 替换 + 注入命令抽取 ────────────
        _sk2 = Skill(name="x", body="run !`git diff` for $ARGUMENTS")
        body, cmds = render_skill_body(_sk2, "HEAD~1")
        assert body == "run !`git diff` for HEAD~1", body
        assert cmds == ["git diff"], cmds
        _s, body3, cmds3 = load_skill_body(loaded, "docker-expert", "now")
        assert "Args: now" in body3 and cmds3 == []
        try:
            load_skill_body(loaded, "nope")
            raise AssertionError("expected KeyError")
        except KeyError:
            pass
        print("render_skill_body / load_skill_body ✓")

        # ── 旧扁平布局仍可读，且目录布局覆盖之 ─────────────────────
        flat = ws / ".openx" / "skills"
        (flat / "legacy-one.md").write_text(_skill_markdown(
            "legacy-one", "Old flat skill.", "flat body",
        ), encoding="utf-8")
        (flat / "broken.md").write_text("no frontmatter\n", encoding="utf-8")
        loaded2 = load_skills(ws, global_dir=gdir, claude_dir=cdir)
        assert loaded2["legacy-one"].legacy is True
        assert loaded2["legacy-one"].body == "flat body"
        assert "broken" not in loaded2  # 坏文件跳过（启动不被打断）
        # 同名目录布局覆盖旧扁平
        same = flat / "legacy-one"
        same.mkdir()
        (same / SKILL_FILE).write_text(
            _skill_markdown("legacy-one", "Dir wins.", "dir body"), encoding="utf-8",
        )
        loaded3 = load_skills(ws, global_dir=gdir, claude_dir=cdir)
        assert loaded3["legacy-one"].legacy is False
        assert loaded3["legacy-one"].body == "dir body"
        print("legacy flat + dir-over-flat precedence ✓")

        # ── Claude 目录互操作（最低优先级）────────────────────────
        cskill = cdir / "shared-name"
        cskill.mkdir(parents=True)
        (cskill / SKILL_FILE).write_text(
            _skill_markdown("shared-name", "From Claude.", "claude body"),
            encoding="utf-8",
        )
        loaded4 = load_skills(ws, global_dir=gdir, claude_dir=cdir)
        assert loaded4["shared-name"].level == "claude"
        own = flat / "shared-name"
        own.mkdir()
        (own / SKILL_FILE).write_text(
            _skill_markdown("shared-name", "Ours.", "own body"), encoding="utf-8",
        )
        loaded5 = load_skills(ws, global_dir=gdir, claude_dir=cdir)
        assert loaded5["shared-name"].level == "project"  # 本家覆盖互操作
        print("claude interop read (lowest precedence) ✓")

        # ── 名字校验 ───────────────────────────────────────────────
        assert validate_name("docker-expert") is None
        assert validate_name("Docker_Expert") is not None
        assert validate_name("") is not None
        assert validate_name("a" * 65) is not None
        print("validate_name ✓")

        # ── 安装（内容 / 目录 / 旧扁平）与卸载 ─────────────────────
        made = install_skill_from_content(
            name="test-skill",
            description="A test skill",
            content="Do something useful.",
            trigger=["test"],
            allowed_tools=["read_file"],
            workspace=ws,
            global_install=False,
            global_dir=gdir,
        )
        assert (flat / "test-skill" / SKILL_FILE).is_file()
        assert made.directory == str(flat / "test-skill")
        # 从目录安装（保留附带文件）到个人级
        src_dir = ws / "portable-skill"
        src_dir.mkdir()
        (src_dir / SKILL_FILE).write_text(
            _skill_markdown("portable-skill", "Portable.", "body"), encoding="utf-8",
        )
        (src_dir / "helper.py").write_text("print(1)\n", encoding="utf-8")
        ported = install_skill(
            src_dir, workspace=ws, global_install=True, global_dir=gdir,
        )
        assert (gdir / "portable-skill" / "helper.py").is_file()
        assert ported.level == "global"
        # 旧扁平 .md 安装 → 标准化为目录布局
        flat_md = ws / "old.md"
        flat_md.write_text(
            _skill_markdown("old-flat", "Old.", "old body"), encoding="utf-8",
        )
        migrated = install_skill(
            flat_md, workspace=ws, global_install=False, global_dir=gdir,
        )
        assert (flat / "old-flat" / SKILL_FILE).is_file()
        assert migrated.legacy is False and migrated.level == "project"

        assert uninstall_skill("test-skill", workspace=ws, global_dir=gdir)
        assert not (flat / "test-skill").exists()
        assert uninstall_skill("portable-skill", workspace=ws, global_dir=gdir)
        assert not uninstall_skill("nonexistent", workspace=ws, global_dir=gdir)
        print("install / uninstall (dir layout) ✓")

    print("openx/skills.py OK ✓")
