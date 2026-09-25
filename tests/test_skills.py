"""Skill 子系统回归测试（对齐 Agent Skills / Claude Code 的 SKILL.md 格式）。

覆盖：
- frontmatter 解析（name/description/allowed-tools/license/trigger）与校验；
- 目录布局装载、六路优先级、Claude 目录互操作、旧扁平兼容、坏文件跳过；
- **渐进披露**：系统提示只含目录（name+description），正文按需加载；
- ``$ARGUMENTS`` 替换与 ``!`cmd` `` 注入命令抽取（不在加载期执行）；
- supporting files 列举；三种安装输入 + 卸载（目录/旧扁平）；
- agent 接线：``activate_skill`` / allowed-tools **会话内存态**作用域（不落盘）、
  ``deactivate_skill``、``reload_skills``、``skill`` 工具与目录注入；
- 动态 ``/<skill-name>`` 命令注册（内置同名不覆盖）。

全部经临时目录与 monkeypatch 隔离，绝不触碰真实 ~/.openx 或 ~/.claude。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openx import skills as sk
from openx.skills import (
    SKILL_FILE,
    Skill,
    build_skills_prompt,
    extract_injected_commands,
    install_skill,
    install_skill_from_content,
    list_supporting_files,
    load_skill_body,
    load_skills,
    parse_skill_text,
    render_skill_body,
    uninstall_skill,
    validate_name,
)

# ── helpers / fixtures ──────────────────────────────────────────


def write_skill(
    root: Path,
    name: str,
    description: str = "",
    body: str = "",
    *,
    allowed_tools: list[str] | None = None,
    license: str = "",
    trigger: list[str] | None = None,
    frontmatter: bool = True,
) -> Path:
    """在 *root* 下写一个目录布局 skill（``<name>/SKILL.md``）。"""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    dest = skill_dir / SKILL_FILE
    if not frontmatter:
        dest.write_text("no frontmatter here\n", encoding="utf-8")
        return dest
    lines = ["---", f"name: {name}"]
    if description:
        lines.append(f"description: {description}")
    if allowed_tools:
        lines.append(f"allowed-tools: {' '.join(allowed_tools)}")
    if license:
        lines.append(f"license: {license}")
    if trigger:
        lines.append(f"trigger: {', '.join(trigger)}")
    lines += ["---", body]
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """隔离个人级 / Claude 级 skill 目录（指向 tmp），返回 (global, claude, ws)。"""
    global_dir = tmp_path / "home" / ".openx" / "skills"
    claude_dir = tmp_path / "home" / ".claude" / "skills"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(sk, "GLOBAL_SKILLS_DIR", global_dir)
    monkeypatch.setattr(sk, "CLAUDE_SKILLS_DIR", claude_dir)
    return global_dir, claude_dir, workspace


# ── 1. frontmatter 解析与校验 ───────────────────────────────────


class TestFrontmatter:
    def test_dir_name_is_the_command_name(self):
        """目录名即命令名（对齐 Agent Skills）；frontmatter name 仅为回退。"""
        text = "---\nname: other-name\ndescription: d\n---\nbody\n"
        skill = parse_skill_text(text, name="dir-name", directory="/x/dir-name")
        assert skill.name == "dir-name"
        assert skill.description == "d" and skill.body == "body"

    def test_allowed_tools_accepts_commas_and_spaces(self):
        text = "---\nallowed-tools: read_file, grep  shell\n---\nbody\n"
        skill = parse_skill_text(text, name="a")
        assert skill.allowed_tools == ["read_file", "grep", "shell"]

    def test_license_trigger_and_argument_hint_parsed(self):
        text = (
            "---\ndescription: d\nlicense: MIT\ntrigger: one, two\n"
            "argument-hint: <file>\n---\nbody\n"
        )
        skill = parse_skill_text(text, name="a")
        assert skill.license == "MIT"
        assert skill.trigger == ["one", "two"]
        assert skill.argument_hint == "<file>"

    def test_content_alias_matches_body(self):
        skill = parse_skill_text("---\n---\nhello\n", name="a")
        assert skill.content == skill.body == "hello"

    def test_description_truncated_to_standard_limit(self):
        long_desc = "x" * (sk.DESCRIPTION_MAX + 50)
        skill = parse_skill_text(f"---\ndescription: {long_desc}\n---\nb\n", name="a")
        assert len(skill.description) == sk.DESCRIPTION_MAX

    def test_missing_delimiters_raise(self):
        with pytest.raises(ValueError):
            parse_skill_text("no frontmatter\n", name="a")
        with pytest.raises(ValueError):
            parse_skill_text("---\nname: a\nbody without closing\n", name="a")

    def test_invalid_name_raises(self):
        with pytest.raises(ValueError):
            parse_skill_text("---\n---\nb\n", name="Bad_Name")

    @pytest.mark.parametrize(
        ("name", "ok"),
        [
            ("docker-expert", True),
            ("a", True),
            ("n1", True),
            ("Docker", False),
            ("under_score", False),
            ("trailing-", False),
            ("-leading", False),
            ("double--hyphen", False),
            ("", False),
            ("a" * (sk.NAME_MAX + 1), False),
        ],
    )
    def test_validate_name(self, name, ok):
        assert (validate_name(name) is None) is ok


# ── 2. 装载与优先级 ─────────────────────────────────────────────


class TestLoad:
    def test_directory_layout_loaded(self, dirs):
        global_dir, _, ws = dirs
        write_skill(global_dir, "personal-one", "from personal", "body")
        loaded = load_skills(ws, global_dir=global_dir, claude_dir=dirs[1])
        assert loaded["personal-one"].level == "global"
        assert loaded["personal-one"].body == "body"
        assert not loaded["personal-one"].legacy

    def test_legacy_flat_layout_still_loads(self, dirs):
        global_dir, claude_dir, ws = dirs
        global_dir.mkdir(parents=True)
        (global_dir / "flat-one.md").write_text(
            "---\ndescription: old style\n---\nflat body\n", encoding="utf-8",
        )
        loaded = load_skills(ws, global_dir=global_dir, claude_dir=claude_dir)
        assert loaded["flat-one"].legacy is True
        assert loaded["flat-one"].body == "flat body"

    def test_dir_layout_wins_over_flat_in_same_root(self, dirs):
        global_dir, claude_dir, ws = dirs
        global_dir.mkdir(parents=True)
        (global_dir / "dup.md").write_text(
            "---\ndescription: flat\n---\nflat body\n", encoding="utf-8",
        )
        write_skill(global_dir, "dup", "dir", "dir body")
        loaded = load_skills(ws, global_dir=global_dir, claude_dir=claude_dir)
        assert loaded["dup"].legacy is False and loaded["dup"].body == "dir body"

    def test_project_wins_over_personal(self, dirs):
        global_dir, claude_dir, ws = dirs
        write_skill(global_dir, "dup", "personal", "personal body")
        write_skill(ws / ".openx" / "skills", "dup", "project", "project body")
        loaded = load_skills(ws, global_dir=global_dir, claude_dir=claude_dir)
        assert loaded["dup"].level == "project"
        assert loaded["dup"].body == "project body"

    def test_native_wins_over_claude_interop(self, dirs):
        global_dir, claude_dir, ws = dirs
        write_skill(claude_dir, "dup", "from claude", "claude body")
        write_skill(global_dir, "dup", "ours", "our body")
        loaded = load_skills(ws, global_dir=global_dir, claude_dir=claude_dir)
        assert loaded["dup"].level == "global" and loaded["dup"].body == "our body"

    def test_claude_interop_read_when_unique(self, dirs):
        _, claude_dir, ws = dirs
        write_skill(claude_dir, "claude-only", "from claude", "claude body")
        loaded = load_skills(ws, global_dir=dirs[0], claude_dir=claude_dir)
        assert loaded["claude-only"].level == "claude"
        assert loaded["claude-only"].body == "claude body"

    def test_claude_project_level(self, dirs):
        _, claude_dir, ws = dirs
        write_skill(ws / ".claude" / "skills", "proj-claude", "d", "body")
        loaded = load_skills(ws, global_dir=dirs[0], claude_dir=claude_dir)
        assert loaded["proj-claude"].level == "claude-project"

    def test_missing_dirs_and_broken_files_never_raise(self, dirs, capsys):
        global_dir, claude_dir, ws = dirs
        write_skill(global_dir, "good", "ok", "body")
        write_skill(global_dir, "broken", frontmatter=False)
        # 目录名非法（大写）→ 解析报错、跳过，绝不中断装载
        bad = global_dir / "BadName"
        bad.mkdir()
        (bad / SKILL_FILE).write_text("---\ndescription: d\n---\nb\n", encoding="utf-8")
        loaded = load_skills(ws, global_dir=global_dir, claude_dir=claude_dir)
        assert "good" in loaded
        assert "broken" not in loaded  # 坏文件跳过，启动不被打断
        assert "BadName" not in loaded
        assert "warning: skipping malformed skill file" in capsys.readouterr().out

    def test_all_missing_dirs_is_empty(self, dirs):
        """目录全缺失时返回空（首次运行 / 无技能）——绝不抛。"""
        assert load_skills(dirs[2], global_dir=dirs[0], claude_dir=dirs[1]) == {}

    def test_no_workspace_loads_personal_only(self, dirs):
        global_dir, claude_dir, _ = dirs
        write_skill(global_dir, "p", "d", "body")
        loaded = load_skills(None, global_dir=global_dir, claude_dir=claude_dir)
        assert set(loaded) == {"p"}


# ── 3. 渐进披露：目录 + 按需正文 ────────────────────────────────


class TestProgressiveDisclosure:
    def test_catalog_has_description_but_not_body(self, dirs):
        global_dir, claude_dir, ws = dirs
        write_skill(global_dir, "one", "Does a thing.", "SECRET BODY TEXT")
        loaded = load_skills(ws, global_dir=global_dir, claude_dir=claude_dir)
        prompt = build_skills_prompt(loaded)
        assert "## Available Skills" in prompt
        assert "one" in prompt and "Does a thing." in prompt
        assert "SECRET BODY TEXT" not in prompt, "正文不得进系统提示"

    def test_catalog_marks_allowed_tools_and_legacy(self, dirs):
        global_dir, claude_dir, ws = dirs
        write_skill(global_dir, "new", "d", "b", allowed_tools=["read_file"])
        global_dir.mkdir(parents=True, exist_ok=True)
        (global_dir / "old.md").write_text("---\ndescription: d\n---\nb\n", encoding="utf-8")
        loaded = load_skills(ws, global_dir=global_dir, claude_dir=claude_dir)
        prompt = build_skills_prompt(loaded)
        assert "pre-approved: read_file" in prompt
        assert "[legacy layout]" in prompt

    def test_empty_catalog_is_empty_string(self):
        assert build_skills_prompt({}) == ""

    def test_load_body_replaces_arguments_and_extracts_commands(self, dirs):
        global_dir, claude_dir, ws = dirs
        write_skill(
            global_dir, "diff", "d",
            "Diff: !`git diff HEAD`\nArgs: $ARGUMENTS",
        )
        loaded = load_skills(ws, global_dir=global_dir, claude_dir=claude_dir)
        skill, body, commands = load_skill_body(loaded, "diff", "main..HEAD")
        assert skill.name == "diff"
        assert commands == ["git diff HEAD"]
        assert "Args: main..HEAD" in body
        # 命令原样保留（调用方过闸后替换）——加载期绝不执行
        assert "!`git diff HEAD`" in body

    def test_load_body_missing_raises_keyerror(self, dirs):
        global_dir, claude_dir, ws = dirs
        loaded = load_skills(ws, global_dir=global_dir, claude_dir=claude_dir)
        with pytest.raises(KeyError):
            load_skill_body(loaded, "nope")

    def test_render_without_commands(self):
        body, commands = render_skill_body(Skill(name="x", body="plain $ARGUMENTS"))
        assert body == "plain " and commands == []

    def test_extract_injected_commands_ignores_empty_and_multiline(self):
        assert extract_injected_commands("!`ls`\n!` `` \n!`git status`") == ["ls", "git status"]

    def test_supporting_files_listed(self, dirs):
        global_dir, claude_dir, ws = dirs
        write_skill(global_dir, "with-files", "d", "b")
        (global_dir / "with-files" / "reference.md").write_text("ref", encoding="utf-8")
        (global_dir / "with-files" / "helper.py").write_text("print(1)", encoding="utf-8")
        loaded = load_skills(ws, global_dir=global_dir, claude_dir=claude_dir)
        assert list_supporting_files(loaded["with-files"]) == ["helper.py", "reference.md"]

    def test_supporting_files_empty_for_legacy(self):
        assert list_supporting_files(Skill(name="x")) == []


# ── 4. 安装 / 卸载 ──────────────────────────────────────────────


class TestInstall:
    def test_install_from_content_writes_dir_layout(self, dirs):
        global_dir, _, ws = dirs
        skill = install_skill_from_content(
            name="made", description="d", content="body",
            allowed_tools=["read_file"], workspace=ws, global_install=False,
            global_dir=global_dir,
        )
        dest = ws / ".openx" / "skills" / "made" / SKILL_FILE
        assert dest.is_file()
        assert skill.directory == str(dest.parent)
        assert skill.allowed_tools == ["read_file"] and skill.level == "project"
        # 回读校验（写出的 frontmatter 可被解析）
        assert load_skills(ws, global_dir=global_dir, claude_dir=dirs[1])["made"].body == "body"

    def test_install_from_directory_copies_supporting_files(self, dirs):
        global_dir, _, ws = dirs
        src = ws / "portable"
        write_skill(src, "portable", "d", "body")
        (src / "portable" / "helper.py").write_text("print(1)", encoding="utf-8")
        skill = install_skill(
            src / "portable", workspace=ws, global_install=True, global_dir=global_dir,
        )
        assert (global_dir / "portable" / "helper.py").is_file()
        assert skill.level == "global"
        assert list_supporting_files(skill) == ["helper.py"]

    def test_install_from_flat_md_migrates_to_dir_layout(self, dirs):
        global_dir, _, ws = dirs
        flat = ws / "old.md"
        flat.write_text("---\ndescription: d\n---\nold body\n", encoding="utf-8")
        skill = install_skill(
            flat, workspace=ws, global_install=False, global_dir=global_dir,
        )
        assert (ws / ".openx" / "skills" / "old" / SKILL_FILE).is_file()
        assert skill.legacy is False and skill.level == "project"

    def test_install_invalid_name_or_missing_source(self, dirs):
        global_dir, _, ws = dirs
        with pytest.raises(ValueError):
            install_skill_from_content(
                name="Bad Name", description="d", content="b",
                workspace=ws, global_install=False, global_dir=global_dir,
            )
        with pytest.raises(FileNotFoundError):
            install_skill(ws / "nope", workspace=ws, global_dir=global_dir)
        empty_dir = ws / "empty"
        empty_dir.mkdir()
        with pytest.raises(ValueError):
            install_skill(empty_dir, workspace=ws, global_dir=global_dir)

    def test_uninstall_project_before_global_and_both_layouts(self, dirs):
        global_dir, _, ws = dirs
        # 项目级目录布局
        install_skill_from_content(
            name="proj", description="d", content="b",
            workspace=ws, global_install=False, global_dir=global_dir,
        )
        assert uninstall_skill("proj", workspace=ws, global_dir=global_dir)
        assert not (ws / ".openx" / "skills" / "proj").exists()
        # 个人级
        install_skill_from_content(
            name="pers", description="d", content="b",
            workspace=ws, global_install=True, global_dir=global_dir,
        )
        assert uninstall_skill("pers", workspace=ws, global_dir=global_dir)
        # 旧扁平
        global_dir.mkdir(parents=True, exist_ok=True)
        (global_dir / "flat.md").write_text("---\n---\nb\n", encoding="utf-8")
        assert uninstall_skill("flat", workspace=ws, global_dir=global_dir)
        # 幂等
        assert not uninstall_skill("nothing", workspace=ws, global_dir=global_dir)


# ── 5. agent 接线：按需加载 + allowed-tools 作用域 ──────────────


def _make_agent(workspace: Path, monkeypatch):
    """构造 OpenXAgent（隔离 settings/hooks/tasks，不碰真实 home）。"""
    from openx.agent import OpenXAgent
    from openx.config import OpenXConfig

    monkeypatch.setattr(
        "openx.kernel.audit.hooks.SETTINGS_PATH", workspace / "no-hooks.json"
    )
    monkeypatch.setattr("openx.orchestration.tasks.TASKS_DIR", workspace / "tasks")
    config = OpenXConfig()
    config.workspace = str(workspace)
    config.model = "test-model"
    return OpenXAgent(config)


class TestAgentSkills:
    async def test_catalog_in_prompt_body_not(self, dirs, monkeypatch):
        _, _, ws = dirs
        write_skill(ws / ".openx" / "skills", "docs", "Write docs.", "BODY-ONLY-MARKER")
        agent = _make_agent(ws, monkeypatch)
        assert "docs" in agent._system_prompt
        assert "Write docs." in agent._system_prompt
        assert "BODY-ONLY-MARKER" not in agent._system_prompt

    async def test_skill_tool_registered(self, dirs, monkeypatch):
        _, _, ws = dirs
        agent = _make_agent(ws, monkeypatch)
        assert "skill" in agent.tools
        names = [s["function"]["name"] for s in agent.tool_schemas]
        assert "skill" in names

    async def test_activate_skill_returns_body_and_scopes_allowed_tools(
        self, dirs, monkeypatch
    ):
        _, _, ws = dirs
        write_skill(
            ws / ".openx" / "skills", "fmt", "Format code.",
            "Always run the formatter.", allowed_tools=["read_file", "grep"],
        )
        agent = _make_agent(ws, monkeypatch)
        from openx.permissions import PermissionLevel, PermissionRules

        agent.tool_executor._rules = PermissionRules()  # 忽略真实 settings.json

        # 落盘绝不允许发生：任何 save() 都判失败
        def _boom(*a, **kw):
            raise AssertionError("allowed-tools 绝不允许落盘")

        monkeypatch.setattr(PermissionRules, "save", _boom)

        body = await agent.activate_skill("fmt")
        assert body == "Always run the formatter."
        # 会话内存态 allow 已挂上
        assert agent.tool_executor.rules.check("read_file", "x.py") is PermissionLevel.ALLOW
        assert agent.tool_executor.rules.check("grep", "pat") is PermissionLevel.ALLOW
        assert agent._active_skills["fmt"] == ["read_file(*)", "grep(*)"]

        # 卸载即摘
        agent.deactivate_skill("fmt")
        assert agent.tool_executor.rules.check("read_file", "x.py") is None
        assert "fmt" not in agent._active_skills
        agent.deactivate_skill("fmt")  # 幂等

    async def test_allowed_tools_do_not_override_deny(self, dirs, monkeypatch):
        """预批准只加 allow；deny 规则仍然优先（安全棘轮单向）。"""
        _, _, ws = dirs
        write_skill(
            ws / ".openx" / "skills", "risky", "d", "b", allowed_tools=["shell"],
        )
        agent = _make_agent(ws, monkeypatch)
        from openx.permissions import PermissionLevel, PermissionRules

        agent.tool_executor._rules = PermissionRules(deny=["shell(rm *)"])
        await agent.activate_skill("risky")
        assert agent.tool_executor.rules.check("shell", "rm -rf /") is PermissionLevel.DENY
        assert agent.tool_executor.rules.check("shell", "ls") is PermissionLevel.ALLOW

    async def test_activate_unknown_skill_raises(self, dirs, monkeypatch):
        _, _, ws = dirs
        agent = _make_agent(ws, monkeypatch)
        with pytest.raises(KeyError):
            await agent.activate_skill("ghost")

    async def test_reload_skills_picks_up_new_and_drops_stale_scope(
        self, dirs, monkeypatch
    ):
        global_dir, _, ws = dirs
        write_skill(ws / ".openx" / "skills", "first", "d", "b", allowed_tools=["grep"])
        agent = _make_agent(ws, monkeypatch)
        from openx.permissions import PermissionRules

        agent.tool_executor._rules = PermissionRules()
        await agent.activate_skill("first")
        assert "first" in agent._active_skills

        # 新增第二个 skill 并重载：新 skill 可见，旧作用域保留
        write_skill(ws / ".openx" / "skills", "second", "d2", "b2")
        agent.reload_skills()
        assert "second" in agent.skills and "second" in agent._system_prompt
        assert "first" in agent._active_skills

        # 卸载 first 并重载：其作用域必须被摘下（无残留放宽）
        uninstall_skill("first", workspace=ws, global_dir=global_dir)
        agent.reload_skills()
        assert "first" not in agent.skills
        assert "first" not in agent._active_skills
        assert agent.tool_executor.rules.check("grep", "x") is None

    async def test_skill_summary(self, dirs, monkeypatch):
        _, _, ws = dirs
        write_skill(
            ws / ".openx" / "skills", "sum", "Described.", "b",
            allowed_tools=["read_file"],
        )
        (ws / ".openx" / "skills" / "sum" / "extra.md").write_text("x", encoding="utf-8")
        agent = _make_agent(ws, monkeypatch)
        text = agent.skill_summary("sum")
        assert "sum [project]" in text
        assert "Described." in text
        assert "read_file" in text and "extra.md" in text
        with pytest.raises(KeyError):
            agent.skill_summary("ghost")


# ── 6. 动态 `/<skill-name>` 命令 ────────────────────────────────


class TestDynamicCommands:
    @pytest.fixture(autouse=True)
    def _clean(self):
        from openx.app.cli.commands import clear_skill_commands

        clear_skill_commands()
        yield
        clear_skill_commands()

    async def test_sync_registers_and_dispatches(self, dirs, monkeypatch):
        _, _, ws = dirs
        write_skill(ws / ".openx" / "skills", "mytask", "Run my task.", "BODY")
        agent = _make_agent(ws, monkeypatch)
        agent.tool_executor._rules = None  # 无 rules 也不得崩（防御）

        from openx.app.cli.commands import (
            _sync_skill_commands,
            all_descriptions,
            find_handler,
            menu_entries,
        )

        registered = _sync_skill_commands(agent)
        assert "mytask" in registered
        handler = find_handler("mytask")
        assert handler is not None
        assert all_descriptions()["mytask"] == "Run my task."
        assert any(name == "mytask" for name, _, _ in menu_entries())

        # 分发：正文经 activate_skill 返回并打印
        class _Raw:
            def __init__(self):
                self.lines = []

            def print(self, *a, **kw):
                self.lines.append(" ".join(str(x) for x in a))

        class _Console:
            raw = _Raw()

            def print_error(self, msg):
                raise AssertionError(msg)

        console = _Console()
        assert await handler(agent, console, ["extra"]) is True
        assert any("BODY" in line for line in console.raw.lines)

    async def test_builtin_names_are_not_hijacked(self, dirs, monkeypatch):
        _, _, ws = dirs
        write_skill(ws / ".openx" / "skills", "help", "Hijack attempt.", "evil")
        agent = _make_agent(ws, monkeypatch)

        from openx.app.cli.commands import (
            _commands,
            _sync_skill_commands,
            find_handler,
        )

        registered = _sync_skill_commands(agent)
        assert "help" not in registered  # 内置优先，绝不被覆盖
        assert find_handler("help") is _commands["help"]

    async def test_sync_is_idempotent_and_clears_removed(self, dirs, monkeypatch):
        global_dir, _, ws = dirs
        write_skill(ws / ".openx" / "skills", "gone", "d", "b")
        agent = _make_agent(ws, monkeypatch)
        from openx.app.cli.commands import _sync_skill_commands, find_handler

        _sync_skill_commands(agent)
        assert find_handler("gone") is not None
        uninstall_skill("gone", workspace=ws, global_dir=global_dir)
        agent.reload_skills()
        _sync_skill_commands(agent)  # 全量重建：旧命令必须消失
        assert find_handler("gone") is None
