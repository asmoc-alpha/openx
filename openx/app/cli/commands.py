"""Slash-command registry for the interactive REPL.

Each command handler is an async function that receives
``(agent, console, args)`` and returns ``True`` to continue the
REPL or ``False`` to exit.

Usage::

    from .commands import handle_slash_command

    result = await handle_slash_command("help", agent, console, [])
    if result is False:
        break  # exit REPL
    if result is True:
        continue  # command handled, show next prompt
    # None → not a command, process as agent query
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

import json
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from ...ui._components.prompt import paste_aware_input

if TYPE_CHECKING:
    from ...agent import OpenXAgent
    from ...ui.console import Console

# ── registry ────────────────────────────────────────────────────

_commands: dict[str, Callable[..., Awaitable[bool]]] = {}
_aliases: dict[str, str] = {}  # alias → canonical name
_descriptions: dict[str, str] = {}


def register(
    name: str,
    description: str = "",
    aliases: list[str] | None = None,
):
    """Decorator: register a slash-command handler.

    The decorated function should be ``async def handler(agent, console,
    args) -> bool`` where *args* is ``list[str]`` (command arguments,
    may be empty).  Return ``True`` to continue the REPL, ``False`` to
    exit.
    """

    def decorator(func):
        _commands[name] = func
        _descriptions[name] = description
        if aliases:
            for a in aliases:
                _aliases[a] = name
        return func

    return decorator


def _plugin_commands():
    """插件命令注册表（只读视图；K3a 取用通道收敛后由消费方直查）。"""
    from ...kernel import get_kernel

    return get_kernel().registry("commands")


def find_handler(name: str) -> Optional[Callable[..., Awaitable[bool]]]:
    """Return the handler for *name*, or ``None``.

    内置优先；未命中才查插件命令注册表（主名 -> 别名）。
    """
    if name in _commands:
        return _commands[name]
    canonical = _aliases.get(name)
    if canonical:
        return _commands.get(canonical)
    registry = _plugin_commands()
    if registry is None:
        return None
    entry = registry.get(name)
    if entry is not None:
        return entry.value.handler
    for e in registry.entries():
        if name in e.value.aliases:
            return e.value.handler
    return None


def all_descriptions() -> dict[str, str]:
    """Return ``{name: description}`` for every registered command."""
    desc = dict(_descriptions)
    registry = _plugin_commands()
    if registry is not None:
        # 插件命令并入帮助；内置名优先（setdefault 不覆盖）。
        for e in registry.entries():
            desc.setdefault(e.name, e.value.description)
    return desc


def menu_entries() -> list[tuple[str, str, list[str]]]:
    """斜杠命令补全菜单数据：``[(name, description, aliases)]``（按名排序）。

    输入框键入 ``/`` 时的候选列表数据源（v0.4.2）。别名按规范名归组，
    补全匹配同时命中主名与别名（菜单仅展示主名 + 别名提示）。
    插件命令追加其后（微内核 P1）；与内置重名时内置优先并记警告。
    """
    by_canonical: dict[str, list[str]] = {}
    for alias, canonical in _aliases.items():
        by_canonical.setdefault(canonical, []).append(alias)
    entries = [
        (name, _descriptions.get(name, ""), sorted(by_canonical.get(name, [])))
        for name in sorted(_commands)
    ]
    registry = _plugin_commands()
    if registry is not None:
        for e in registry.entries():
            if e.name in _commands or e.name in _aliases:
                # 内置优先跳过；记警告（inventory 投影据此展示）
                registry.note_conflict(e.name, e.name)
                continue
            entries.append((e.name, e.value.description, sorted(e.value.aliases)))
    return entries


async def handle_slash_command(
    cmd_name: str,
    agent: "OpenXAgent",
    console: "Console",
    args: list[str],
) -> Optional[bool]:
    """Dispatch *cmd_name* to its handler.

    Returns:
        ``True``  — command handled, continue REPL
        ``False`` — command requested exit
        ``None``  — unknown command
    """
    handler = find_handler(cmd_name)
    if handler is None:
        return None
    return await handler(agent, console, args)


# ── command handlers ────────────────────────────────────────────


@register("quit", description="Exit OpenX", aliases=["exit", "q"])
async def _cmd_quit(agent, console, args):
    console.print_goodbye(agent.session_token_usage())
    return False


@register("help", description="Show all available commands")
async def _cmd_help(agent, console, args):
    console.print_help()
    return True


@register("plugins", description="List loaded plugins and their contributions")
async def _cmd_plugins(agent, console, args):
    """微内核 inventory 面板：阶段/贡献/警告，只读投影。"""
    from ...kernel import get_kernel

    kernel = get_kernel()
    kernel.ensure_loaded(str(agent.workspace))
    console.print_plugins(kernel.inventory())
    return True


@register("clear", description="Clear screen and conversation history")
async def _cmd_clear(agent, console, args):
    agent.clear_history()
    console.raw.clear()
    console.print_header(instructions_loaded=agent.instructions.has_any)
    console.print_status_line()
    return True


@register("model", description="List model groups / switch active group / set a role model")
async def _cmd_model(agent, console, args):
    from ...config import OpenXConfig
    from ...model_groups import ROLE_KEYS, role_short

    # ── 组:角色 → 换该角色模型（交互输入新模型 id）────────────
    if args and ":" in args[0]:
        return await _cmd_set_role_model(agent, console, args[0])

    # ── 单参：是组 → 切组；否则设 active 组 main 模型（旧肌肉记忆）
    if args:
        name = args[0]
        groups_raw = OpenXConfig.load_model_groups_raw()
        if name in groups_raw:
            return _switch_group(agent, console, name)
        if not agent.set_role_model("main", name):
            console.print_error(
                f"'{name}' is not a configured group, and no model group is "
                "persisted to edit. Switch first with /model <group>."
            )
            return True
        console.print_success(f"Main model set to: {name}")
        return True

    # ── 无参：列出全部组与角色绑定 ────────────────────────────
    groups_raw = OpenXConfig.load_model_groups_raw()
    groups, active, _ = OpenXConfig.load_model_groups()
    if not groups_raw:
        console.print_info(
            "No model groups configured.\n\n"
            'Add groups under "modelGroups" in ~/.openx/settings.json, '
            "or use /config."
        )
        return True
    console.raw.print(
        "\n[bold]Model Groups[/bold]  [dim](~/.openx/settings.json)[/dim]\n"
    )
    for name, raw in groups_raw.items():
        g = groups.get(name)
        kind = (g.kind if g else None) or raw.get("kind") or "openai-compat"
        mark = "  [green]← active[/green]" if name == active else ""
        console.raw.print(
            f"  [bold cyan]{name}[/bold cyan] [dim]({kind})[/dim]{mark}"
        )
        if g is None:
            continue
        for role_key in ROLE_KEYS:
            rb = g.roles.get(role_key)
            if rb is None:
                continue
            deco = ""
            if rb.api_base and rb.api_base != g.api_base:
                deco += f"  [dim][base {rb.api_base}][/dim]"
            if rb.api_key and rb.api_key != g.api_key:
                deco += "  [dim][own key][/dim]"
            console.raw.print(
                f"      [dim]{role_short(role_key)}[/dim] → {rb.model}{deco}"
            )
    console.raw.print(
        "\n[dim]Switch: /model <group>   ·   change a role model: "
        "/model <group>:<role>   (roles: main | exec | mini | modal)[/dim]"
    )
    return True


def _save_group_role_model(group_name: str, role_key: str, model: str) -> bool:
    """文件级写入某组某角色的 model（不改当前绑定；非 active 组编辑用）。"""
    from ...config import OpenXConfig

    raw = OpenXConfig.load_model_groups_raw()
    if group_name not in raw:
        return False
    group_raw = dict(raw.get(group_name) or {})
    # 保留既有简写/对象形态：原字符串简写仍写字符串，原对象/缺席写对象
    if isinstance(group_raw.get(role_key), str):
        group_raw[role_key] = model
    else:
        group_raw[role_key] = {"model": model}
    raw[group_name] = group_raw
    OpenXConfig.save_model_groups(raw)
    return True


def _switch_group(agent, console, name) -> Optional[bool]:
    """切到模型组 ``name``：切换绑定 + 持久化 activeGroup + 会话留痕。"""
    from ...config import OpenXConfig

    if not agent.switch_group(name):
        console.print_error(
            f"Cannot switch to group '{name}': unknown group, or its "
            "implementation is unavailable (missing SDK?)."
        )
        return True
    OpenXConfig.set_active_group(name)
    try:
        store = getattr(agent, "session_store", None)
        if store is not None:
            store.update_meta(group=name, model=agent.config.model)
    except Exception:
        pass
    console.print_success(
        f"Active model group set to: {name}  ({agent.config.model})"
    )
    return True


async def _cmd_set_role_model(agent, console, target: str) -> Optional[bool]:
    """``/model <组>:<角色>``：交互输入新模型 id 并持久化。"""
    from ...config import OpenXConfig
    from ...model_groups import canonical_role
    from ...ui._style import PROMPT_STYLE

    group_name, _, role_part = target.partition(":")
    role_key = canonical_role(role_part)
    if role_key is None:
        console.print_error(
            f"Unknown role '{role_part}'. Use main | exec | mini | modal."
        )
        return True
    groups_raw = OpenXConfig.load_model_groups_raw()
    if group_name not in groups_raw:
        console.print_error(f"Unknown group '{group_name}'.")
        return True
    cur = ""
    role_raw = (groups_raw.get(group_name) or {}).get(role_key)
    if isinstance(role_raw, str):
        cur = role_raw
    elif isinstance(role_raw, dict):
        cur = str(role_raw.get("model") or "")

    console.raw.print()
    default = f"[dim][{cur}][/dim] " if cur else ""
    value = paste_aware_input(
        console.raw,
        f"  [{PROMPT_STYLE}]New {role_key} model[/{PROMPT_STYLE}] {default}: ",
    ).strip()
    if not value:
        console.print_info("Aborted.")
        return True

    if group_name == getattr(agent, "_group_name", ""):
        agent.set_role_model(role_key, value)
    elif not _save_group_role_model(group_name, role_key, value):
        console.print_error("Failed to persist the model change.")
        return True
    console.print_success(f"{group_name}:{role_part} model set to: {value}")
    return True


@register("workspace", description="Change workspace directory")
async def _cmd_workspace(agent, console, args):
    if not args:
        console.print_warning("Usage: /workspace <path>")
        return True
    new_ws = Path(args[0]).resolve()
    if not new_ws.exists():
        console.print_error(f"Directory not found: {args[0]}")
        return True
    agent.config.workspace = str(new_ws)
    agent.workspace = new_ws
    agent.tools = agent._build_tools()
    agent.tool_schemas = agent._compute_tool_schemas()
    agent.reload_instructions()
    console.print_success(f"Workspace set to: {new_ws}")
    try:
        new_info = await agent.explore_project()
        console._console.print()
        console.print_header(instructions_loaded=agent.instructions.has_any)
        console._console.print()
        console.print_project_overview(new_info)
        console._console.print()
        console.print_status_line()
    except Exception:
        pass
    return True


@register("auto-approve", description="Toggle auto-approve mode")
async def _cmd_auto_approve(agent, console, args):
    agent.config.auto_approve = not agent.config.auto_approve
    agent.tool_executor.auto_approve = agent.config.auto_approve
    status = "ON" if agent.config.auto_approve else "OFF"
    console.print_info(f"Auto-approve: {status}")
    return True


_MODE_MESSAGES = {
    "manual": (
        "Mode: manual — read-only tools run free; every write/shell call "
        "asks for confirmation (stored rules, whitelist and -y are ignored). "
        "手动模式：只读工具免确认，写入/Shell 逐次询问。"
    ),
    "auto": (
        "Mode: auto — writes ask unless allowed by rules/whitelist/-y; "
        "dangerous commands (config.dangerous_commands) ALWAYS ask. "
        "自动模式：按规则/白名单/-y 免询问；高危命令始终弹窗。"
    ),
    "plan": (
        "Mode: plan — write tools are gated; the agent explores read-only, "
        "then presents its plan for approval via exit_plan_mode. "
        "计划模式：写入工具被禁用，探索完成后 agent 会提交计划供你审批。"
    ),
}


@register("mode", description="Show or switch permission mode (manual/auto/plan)")
async def _cmd_mode(agent, console, args):
    if not args:
        console.print_info(
            f"Current mode: {agent.mode}. Usage: /mode [manual|auto|plan] — "
            f"manual: confirm every write (startup default); "
            f"auto: normal permission flow, dangerous commands always ask; "
            f"plan: read-only + plan approval. 当前模式：{agent.mode}。"
        )
        return True
    target = args[0].strip().lower()
    if target not in _MODE_MESSAGES:
        console.print_warning(
            f"Unknown mode {args[0]!r} — choose from: manual, auto, plan"
        )
        return True
    # set_mode 统一同步 executor 闸门 + schema 过滤 + 系统提示 + console 状态
    agent.set_mode(target)
    console.print_info(_MODE_MESSAGES[target])
    return True


@register("explore", description="Show project overview")
async def _cmd_explore(agent, console, args):
    info = await agent.explore_project()
    console._console.print()
    console.print_project_overview(info)
    console._console.print()
    return True


@register("image", description="Load and analyze an image file")
async def _cmd_image(agent, console, args):
    if not args:
        console.print_warning("Usage: /image <path-to-image> [optional prompt]")
        return True
    from ...image import is_image_file, image_to_base64_url, display_image, get_image_metadata
    from ...services.streaming import StreamingService

    image_path = Path(args[0]).expanduser().resolve()
    if not image_path.is_file() or not is_image_file(image_path):
        console.print_error(f"Not a valid image: {args[0]}")
        return True

    meta = get_image_metadata(image_path)
    console.print_image_loaded(Path(image_path), meta)
    display_image(image_path)

    prompt = " ".join(args[1:]) if len(args) > 1 else ""
    if not prompt.strip():
        console.print_info("Type your question (or Enter for auto-describe):")
        try:
            raw = console.print_user_prompt()
        except (KeyboardInterrupt, EOFError):
            return True
        # None = EOF（非 TTY stdin 已耗尽）：回 REPL 主循环统一退出，
        # 绝不把 None 当空串触发自动描述。None → return, let the REPL exit.
        if raw is None:
            return True
        prompt = raw.strip()
        if not prompt:
            prompt = "Describe this image."

    data_url = image_to_base64_url(image_path)
    user_content = agent.build_user_content(prompt, images=[data_url])
    display = StreamingService(
        console, agent.total_input_tokens,
        todos_provider=lambda: agent.todos, fleet=agent.fleet,
        panels=getattr(agent, "ui_panels", None),
    )
    display.start()
    async for chunk in agent.stream_run(user_content):
        display.feed(chunk)
    display.done()
    return True


@register("clipboard", description="Paste and analyze a clipboard screenshot")
async def _cmd_clipboard(agent, console, args):
    from ...image import (
        check_clipboard_for_image,
        save_clipboard_image,
        display_image,
        image_to_base64_url,
    )
    from ...services.streaming import StreamingService

    png_data = check_clipboard_for_image()
    if png_data is None:
        console.print_warning("No image on clipboard. Copy an image first.")
        return True
    saved = save_clipboard_image()
    if not saved:
        console.print_error("Failed to save clipboard image.")
        return True
    console.print_success(f"Clipboard image saved: {saved.name}")
    display_image(saved)
    console.print_info("Type your question about this image (or press Enter to skip):")
    try:
        raw = console.print_user_prompt()
    except (KeyboardInterrupt, EOFError):
        raw = ""
    question = (raw or "").strip()  # None (EOF) → 空串 → 自动描述 / empty → auto-describe
    if not question:
        question = "Describe this image."
    data_url = image_to_base64_url(saved)
    user_content = agent.build_user_content(question, images=[data_url])
    display = StreamingService(
        console, agent.total_input_tokens,
        todos_provider=lambda: agent.todos, fleet=agent.fleet,
        panels=getattr(agent, "ui_panels", None),
    )
    display.start()
    async for chunk in agent.stream_run(user_content):
        display.feed(chunk)
    display.done()
    return True


@register("init", description="Create an OPENX.md instruction file")
async def _cmd_init(agent, console, args):
    from ...instructions import scaffold_openx_md

    try:
        path, existed = scaffold_openx_md(agent.workspace)
        if existed:
            console.print_warning(
                f"OPENX.md already exists at {path}. Use --force to overwrite."
            )
        else:
            console.print_success(f"Created {path}")
            agent.reload_instructions()
            console.print_info(
                "Instructions loaded. Use /instructions to view them."
            )
    except FileExistsError:
        console.print_warning(
            f"OPENX.md already exists in {agent.workspace}.\n"
            "Run /init --force to overwrite, or edit it directly."
        )
    except Exception as e:
        console.print_error(f"Failed to create OPENX.md: {e}")
    return True


@register("instructions", description="Show loaded OPENX.md instructions")
async def _cmd_instructions(agent, console, args):
    registry = agent.instructions
    all_inst = registry.all_instructions
    if not all_inst:
        console.print_info(
            "No OPENX.md files loaded.\n\n"
            "Create one with /init, or manually place an OPENX.md file in:\n"
            f"  • {agent.workspace}/OPENX.md  (project-level)\n"
            f"  • ~/.openx/OPENX.md           (global, applies everywhere)"
        )
    else:
        console.raw.print("\n[bold]Loaded Instructions[/bold]\n")
        for iset in all_inst:
            level_style = {
                "global": "blue",
                "project": "cyan",
                "subdir": "magenta",
            }.get(iset.level.value, "white")
            console.raw.print(
                f"[bold {level_style}]{iset.level.value.upper()}[/bold {level_style}] "
                f"[dim]{iset.path}[/dim] "
                f"([{level_style}]{len(iset.content)} chars[/{level_style}])"
            )
        console.raw.print()
    return True


@register("config", description="Show current configuration and edit model group / API settings")
async def _cmd_config(agent, console, args):
    from ...config import OpenXConfig
    from ...ui._helpers import mask_key
    from ...ui._style import PROMPT_STYLE
    c = agent.config

    def _active_group_name() -> str:
        return getattr(agent, "_group_name", "") or c.active_group or "(none)"

    def _show() -> None:
        from ...model_groups import ROLE_KEYS, role_short

        gname = _active_group_name()
        kind = agent._provider_settings.get("kind", "openai-compat") if hasattr(
            agent, "_provider_settings"
        ) else "openai-compat"
        console.raw.print(
            f"\n[bold]Configuration[/bold]\n\n"
            f"  Group:        [cyan]{gname}[/cyan] [dim]({kind})[/dim]\n"
            f"  Main model:   [cyan]{c.model}[/cyan]\n"
            f"  API Base:     [dim]{c.api_base}[/dim]\n"
            f"  API Key:      [dim]{mask_key(c.api_key) if c.api_key else '(not set)'}[/dim]\n"
            f"  Workspace:    [dim]{c.workspace}[/dim]\n"
            f"  Auto-approve: [{'green' if c.auto_approve else 'red'}]{c.auto_approve}[/]\n"
            f"  Temperature:  {c.temperature}\n"
            f"  Max rounds:   {c.max_tool_rounds}\n"
            f"  Tools loaded: {len(agent.tools)}\n"
        )
        # 展示组内各角色绑定（exec/mini/modal；main 已在上面主模型行）
        roles_raw = _current_group_raw()
        if roles_raw:
            for rk in ROLE_KEYS:
                if rk == "openx-main-model":
                    continue
                short = role_short(rk)
                e = roles_raw.get(rk)
                if isinstance(e, str):
                    model, over = e, ""
                elif isinstance(e, dict):
                    model = str(e.get("model") or "")
                    tags = [t for t, k in (("key", "apiKey"), ("base", "apiBase"))
                            if e.get(k)]
                    over = f" [dim](own {'/'.join(tags)})[/dim]" if tags else ""
                else:
                    model, over = "(= main)", ""
                console.raw.print(f"  [dim]{short}:[/dim] {model}{over}")
            console.raw.print()

    def _current_group_raw() -> dict:
        """当前绑定组的原始 dict（未持久化/内存合成时返回 {}）。"""
        groups = OpenXConfig.load_model_groups_raw()
        return groups.get(_active_group_name(), {}) if _active_group_name() in groups else {}

    def _edit_active_group(api_base: bool, value: str) -> bool:
        """改当前组共享 apiBase/apiKey；成功后重建客户端（凭据变了）。"""
        group_raw = _current_group_raw()
        if not group_raw:
            console.print_error("No persisted model group to edit.")
            return False
        key = "apiBase" if api_base else "apiKey"
        group_raw[key] = value
        raw = OpenXConfig.load_model_groups_raw()
        raw[_active_group_name()] = group_raw
        OpenXConfig.save_model_groups(raw)
        setattr(c, "api_base" if api_base else "api_key", value)
        agent._drop_role_clients()
        agent._rebuild_llm()
        return True

    def _add_model_group() -> None:
        console.raw.print("\n[bold]Add Model Group[/bold]\n")
        name = paste_aware_input(console.raw,
            f"  [{PROMPT_STYLE}]Group name[/{PROMPT_STYLE}] "
            f"[dim](letters/digits/./_/-)[/dim]: "
        ).strip()
        if not name:
            console.print_warning("Group name cannot be empty — skipped.")
            return
        groups = OpenXConfig.load_model_groups_raw()
        if name in groups:
            console.print_warning(f"Group '{name}' already exists.")
            return
        model = paste_aware_input(console.raw,
            f"  [{PROMPT_STYLE}]Main model[/{PROMPT_STYLE}] "
            f"[dim](required)[/dim]: "
        ).strip()
        if not model:
            console.print_warning("Main model cannot be empty — skipped.")
            return
        api_base = paste_aware_input(console.raw,
            f"  [{PROMPT_STYLE}]API base URL[/{PROMPT_STYLE}] "
            f"[dim](Enter to skip)[/dim]: "
        ).strip()
        api_key = paste_aware_input(console.raw,
            f"  [{PROMPT_STYLE}]API key[/{PROMPT_STYLE}] "
            f"[dim](Enter to skip; env-var fallback applies)[/dim]: "
        ).strip()
        group: dict = {"kind": "openai-compat", "openx-main-model": model}
        if api_base:
            group["apiBase"] = api_base
        if api_key:
            group["apiKey"] = api_key
        raw = OpenXConfig.load_model_groups_raw()
        raw[name] = group
        OpenXConfig.save_model_groups(raw)
        console.print_success(
            f"Model group '{name}' saved. Activate with /model {name}"
        )

    _ROLE_CHOICES = ["exec", "mini", "modal"]

    def _pick_role() -> str | None:
        """在 exec/mini/modal 里选角色；取消返回 None。"""
        options = [(r, r) for r in _ROLE_CHOICES]
        options.append(("Cancel", None))
        console.raw.print()
        try:
            picked = console._interactive_select(
                options, default_index=0, prompt="Role:",
            )
        except (KeyboardInterrupt, EOFError):
            return None
        return picked

    def _role_key(role: str) -> str:
        from ...model_groups import canonical_role

        return canonical_role(role) or role

    def _prompt_new_role_model(role: str) -> None:
        console.raw.print()
        value = paste_aware_input(console.raw,
            f"  [{PROMPT_STYLE}]{role} model[/{PROMPT_STYLE}] "
            f"[dim](type - to clear back to main)[/dim]: "
        ).strip()
        if value == "-":
            _clear_role(role)  # 显式清除：回到 main 回落
            return
        if not value:
            console.print_info("Aborted.")
            return
        if agent.set_role_model(role, value):
            console.print_success(f"{role} model set to: {value}")

    def _clear_role(role: str) -> None:
        """移除当前组某角色的显式绑定（回落 main）。"""
        role_key = _role_key(role)
        groups_raw = OpenXConfig.load_model_groups_raw()
        gname = _active_group_name()
        if gname not in groups_raw:
            return
        entry = dict(groups_raw[gname])
        entry.pop(role_key, None)
        groups_raw[gname] = entry
        OpenXConfig.save_model_groups(groups_raw)
        agent._drop_role_clients()
        if role_key == agent._bind_role:
            agent._rebuild_llm()
        console.print_success(f"{role} cleared (falls back to main)")

    def _edit_role_cred() -> None:
        role = _pick_role()
        if not role:
            return
        options = [
            ("API key (this role)", "key"),
            ("API base URL (this role)", "base"),
            ("Cancel", None),
        ]
        console.raw.print()
        try:
            field = console._interactive_select(
                options, default_index=0, prompt=f"{role} override:",
            )
        except (KeyboardInterrupt, EOFError):
            return
        if field not in ("key", "base"):
            return
        label = "API key" if field == "key" else "API base URL"
        console.raw.print()
        value = paste_aware_input(console.raw,
            f"  [{PROMPT_STYLE}]{role} {label}[/{PROMPT_STYLE}] "
            f"[dim](Enter empty to clear override)[/dim]: "
        ).strip()
        if agent.set_role_cred(role, "api_key" if field == "key" else "api_base", value):
            console.print_success(
                f"{role} {label} override {'set' if value else 'cleared'}"
            )

    def _delete_model_group() -> None:
        groups = OpenXConfig.load_model_groups_raw()
        active = _active_group_name()
        candidates = [n for n in groups if n != active]
        if not candidates:
            console.print_info(
                "Cannot delete the only/active group — switch first."
            )
            return
        options = [(n, n) for n in candidates]
        options.append(("Cancel", None))
        console.raw.print()
        try:
            picked = console._interactive_select(
                options, default_index=0, prompt="Delete group:",
            )
        except (KeyboardInterrupt, EOFError):
            return
        if not picked:
            return
        raw = OpenXConfig.load_model_groups_raw()
        raw.pop(picked, None)
        OpenXConfig.save_model_groups(raw)
        agent._drop_role_clients()
        console.print_success(f"Deleted model group: {picked}")

    _show()
    while True:
        console.raw.print()
        try:
            choice = console._interactive_select(
                [
                    ("Change main model", "model"),
                    ("Change exec model", "role:exec"),
                    ("Change mini model", "role:mini"),
                    ("Change modal model", "role:modal"),
                    ("Role key/base override", "role_cred"),
                    ("Shared API base URL", "base"),
                    ("Shared API key", "key"),
                    ("Add model group", "add_group"),
                    ("Switch active group", "switch_group"),
                    ("Delete model group", "del_group"),
                    ("Done", None),
                ],
                default_index=0,
                prompt="Configure:",
            )
        except (KeyboardInterrupt, EOFError):
            return True
        if choice == "model":
            console.raw.print()
            value = paste_aware_input(console.raw,
                f"  [{PROMPT_STYLE}]Main model[/{PROMPT_STYLE}] [dim][{c.model}][/dim]: "
            ).strip()
            if value and agent.set_role_model("main", value):
                console.print_success(f"Main model set to {value}")
        elif isinstance(choice, str) and choice.startswith("role:"):
            _prompt_new_role_model(choice[5:])
        elif choice == "role_cred":
            _edit_role_cred()
        elif choice == "base":
            console.raw.print()
            value = paste_aware_input(console.raw,
                f"  [{PROMPT_STYLE}]Shared API base URL[/{PROMPT_STYLE}] "
                f"[dim][{c.api_base}][/dim]: "
            ).strip()
            if value and _edit_active_group(api_base=True, value=value):
                console.print_success(f"Shared API base set to {value}")
        elif choice == "key":
            console.raw.print()
            disp = mask_key(c.api_key) if c.api_key else "(not set)"
            value = paste_aware_input(console.raw,
                f"  [{PROMPT_STYLE}]Shared API key[/{PROMPT_STYLE}] "
                f"[dim][{disp}][/dim]: "
            ).strip()
            if value and _edit_active_group(api_base=False, value=value):
                console.print_success("Shared API key updated")
        elif choice == "add_group":
            _add_model_group()
        elif choice == "switch_group":
            groups_raw = OpenXConfig.load_model_groups_raw()
            if not groups_raw:
                console.print_info("No model groups to switch to.")
                continue
            options = [
                (f"{n}" + ("  ← active" if n == _active_group_name() else ""), n)
                for n in groups_raw
            ]
            options.append(("Cancel", None))
            console.raw.print()
            try:
                picked = console._interactive_select(
                    options, default_index=0, prompt="Switch active group:",
                )
            except (KeyboardInterrupt, EOFError):
                continue
            if picked:
                _switch_group(agent, console, picked)
        elif choice == "del_group":
            _delete_model_group()
        else:
            break
    _show()
    return True


@register("git", description="Show git status")
async def _cmd_git(agent, console, args):
    result = await agent.tools["git_status"].execute()
    console.raw.print(result.output or result.error)
    return True


@register("diff", description="Show git diff")
async def _cmd_diff(agent, console, args):
    result = await agent.tools["git_diff"].execute()
    console.raw.print(result.output or result.error)
    return True


@register("tips", description="Show usage tips")
async def _cmd_tips(agent, console, args):
    console.print_tips()
    return True


@register("release-notes", description="Browse release notes — pick a version to view",
          aliases=["release"])
async def _cmd_release_notes(agent, console, args):
    from ...ui._components.layout import LayoutMixin

    # /release <version> → 直达指定版本
    if args:
        wanted = args[0].lstrip("vV")
        for version, title, bullets in LayoutMixin.RELEASES:
            if version == wanted:
                console.print_release_version(version, title, bullets)
                return True
        console.print_warning(
            f"No release notes for v{wanted} — try /release to browse versions"
        )
        return True

    # 无参数 → 版本列表选择（↑↓ + Enter；非 TTY 退化数字菜单）
    options = [
        (f"v{version} — {title}", (version, title, bullets))
        for version, title, bullets in LayoutMixin.RELEASES
    ]
    options.append(("All versions", None))
    console.raw.print()
    try:
        choice = console._interactive_select(
            options, default_index=0, prompt="View:",
        )
    except (KeyboardInterrupt, EOFError):
        return True
    if choice is None:
        console.print_release_notes()
    else:
        console.print_release_version(*choice)
    return True


@register("todos", description="Show the agent's task list")
async def _cmd_todos(agent, console, args):
    console.print_todos(agent.todos)
    return True


@register("cost", description="Show cumulative token usage")
async def _cmd_cost(agent, console, args):
    console.print_session_usage(agent.session_token_usage())
    return True


@register("memory", description="Show all stored memories")
async def _cmd_memory(agent, console, args):
    entries = agent.memory.list_all()
    if not entries:
        console.print_info(
            "No memories stored yet.\n\n"
            "Use /remember <fact> to store something the agent should remember "
            "across sessions. Examples:\n"
            '  /remember "Prefer pytest over unittest for new tests"\n'
            '  /remember "The API server runs on port 8080 in dev"'
        )
        return True
    console.raw.print("\n[bold]Persistent Memories[/bold]  [dim](~/.openx/memory/)[/dim]\n")
    for e in entries:
        tag = f" [dim]{e.metadata.get('type', '')}[/dim]" if e.metadata.get("type") else ""
        console.raw.print(f"  [bold cyan]{e.name}[/bold cyan]{tag}")
        console.raw.print(f"  [dim]{e.description}[/dim]")
        console.raw.print()
    console.raw.print(f"[dim]{len(entries)} memories stored.[/dim]")
    return True


@register("remember", description="Save a fact to persistent memory")
async def _cmd_remember(agent, console, args):
    if not args:
        console.print_warning("Usage: /remember <fact to remember>")
        return True
    text = " ".join(args)
    # Derive a short slug from the first few words
    words = text.split()
    slug = "-".join(w.lower().rstrip(".,;:!?") for w in words[:4] if len(w) > 2)
    if not slug:
        slug = "memory"
    entry = agent.memory.save(
        name=slug,
        description=text[:120],
        content=text,
        metadata={"type": "user"},
    )
    agent.reload_instructions()
    console.print_success(f"Stored: {entry.name}")
    console.raw.print(f"  [dim]{entry.description}[/dim]")
    return True


@register(
    "permissions",
    description="Show and manage stored permission rules",
    aliases=["perms"],
)
async def _cmd_permissions(agent, console, args):
    if args and args[0] == "clear":
        agent.tool_executor.rules.allow.clear()
        agent.tool_executor.rules.deny.clear()
        agent.tool_executor.rules.save()
        console.print_success("All stored permission rules cleared.")
        return True
    if args and args[0] == "rm" and len(args) > 1:
        removed = agent.tool_executor.rules.remove(args[1])
        if removed:
            console.print_success(f"Removed rule: {args[1]}")
        else:
            console.print_error(f"Rule not found: {args[1]}")
        return True
    console.raw.print(agent.tool_executor.rules.format_rules())
    console.raw.print(
        "\n[dim]Manage: /permissions rm <pattern>  |  /permissions clear[/dim]"
    )
    return True


@register("hooks", description="Show configured hooks")
async def _cmd_hooks(agent, console, args):
    lines = agent.hooks.describe()
    if not lines:
        console.print_info(
            "No hooks configured.\n\n"
            "Add hooks under the \"hooks\" key in ~/.openx/settings.json or\n"
            "<workspace>/.openx/settings.json (events: PreToolUse, PostToolUse,\n"
            "UserPromptSubmit, Stop)."
        )
        return True
    console.raw.print("\n[bold]Configured Hooks[/bold]\n")
    for line in lines:
        console.raw.print(f"  {line}")
    return True


@register("mcp", description="Manage MCP servers (status / add / remove)")
async def _cmd_mcp(agent, console, args):
    from ...config import OpenXConfig
    from ...ui._style import PROMPT_STYLE

    subcmd = args[0].lower() if args else ""

    # ── /mcp add — 交互式安装一个新的 MCP server ──────────────
    if subcmd == "add":
        console.raw.print("\n[bold]Install MCP Server[/bold]\n")
        name = paste_aware_input(console.raw, 
            f"  [{PROMPT_STYLE}]Server name[/{PROMPT_STYLE}] "
            f"[dim](e.g. filesystem, github, brave-search)[/dim]: "
        ).strip()
        if not name:
            console.print_warning("Server name cannot be empty — skipped.")
            return True
        command = paste_aware_input(console.raw, 
            f"  [{PROMPT_STYLE}]Command[/{PROMPT_STYLE}] "
            f"[dim](e.g. npx, node, python, docker)[/dim]: "
        ).strip()
        if not command:
            console.print_warning("Command cannot be empty — skipped.")
            return True
        args_str = paste_aware_input(console.raw, 
            f"  [{PROMPT_STYLE}]Arguments[/{PROMPT_STYLE}] "
            f"[dim](space-separated, e.g. -y @modelcontextprotocol/server-filesystem /tmp)[/dim]: "
        ).strip()
        env_str = paste_aware_input(console.raw, 
            f"  [{PROMPT_STYLE}]Environment vars[/{PROMPT_STYLE}] "
            f"[dim](KEY=VALUE space-separated, Enter to skip)[/dim]: "
        ).strip()

        server_cfg: dict = {"command": command}
        if args_str:
            server_cfg["args"] = args_str.split()
        if env_str:
            env_dict = {}
            for pair in env_str.split():
                if "=" in pair:
                    k, _, v = pair.partition("=")
                    env_dict[k] = v
            if env_dict:
                server_cfg["env"] = env_dict

        OpenXConfig.save_mcp_server(name, server_cfg)
        console.print_success(
            f"MCP server '{name}' saved to ~/.openx/settings.json.\n"
            f"  Restart OpenX or run /mcp to reconnect."
        )
        return True

    # ── /mcp remove <name> — 卸载一个 MCP server ─────────────
    if subcmd in ("remove", "rm", "uninstall"):
        if len(args) < 2:
            # 交互式选择要删除的 server
            servers = OpenXConfig.load_mcp_servers()
            if not servers:
                console.print_info("No MCP servers configured.")
                return True
            options: list[tuple[str, object]] = [
                (f"{n}  ({s.get('command', '?')})", n)
                for n, s in servers.items()
            ]
            options.append(("Cancel", None))
            console.raw.print()
            try:
                choice = console._interactive_select(
                    options, default_index=0, prompt="Remove server:",
                )
            except (KeyboardInterrupt, EOFError):
                return True
            if choice and OpenXConfig.delete_mcp_server(choice):
                console.print_success(f"Removed MCP server: {choice}")
            return True
        target = args[1]
        if OpenXConfig.delete_mcp_server(target):
            console.print_success(f"Removed MCP server: {target}")
        else:
            console.print_error(f"MCP server not found: {target}")
        return True

    # ── /mcp (无子命令) — 显示状态 + 配置列表 ─────────────────
    servers = OpenXConfig.load_mcp_servers()
    lines = agent.mcp.status()
    if not servers and not lines:
        console.print_info(
            "No MCP servers configured.\n\n"
            "Install one with:\n"
            "  /mcp add\n\n"
            "Or manually add under \"mcpServers\" in ~/.openx/settings.json:\n"
            "  {\"mcpServers\": {\"name\": {\"command\": \"npx\", "
            "\"args\": [\"-y\", \"some-mcp-server\"]}}}"
        )
        return True
    console.raw.print("\n[bold]MCP Servers[/bold]  [dim](~/.openx/settings.json)[/dim]\n")
    if lines:
        for line in lines:
            console.raw.print(f"  {line}")
    # 显示已配置但未连接的（如项目级配置）
    connected_names = {l.split(":")[0] for l in lines}
    for name, cfg in servers.items():
        if name not in connected_names:
            cmd = cfg.get("command", "?")
            console.raw.print(f"  [dim]{name}: configured ({cmd}) — not connected[/dim]")
    console.raw.print(
        "\n[dim]Manage: /mcp add  |  /mcp remove [name][/dim]"
    )
    return True


@register(
    "skill",
    description="Manage skills (list / add / install / remove)",
    aliases=["skills"],
)
async def _cmd_skill(agent, console, args):
    from ...skills import (
        load_skills, install_skill, install_skill_from_content,
        uninstall_skill, GLOBAL_SKILLS_DIR,
    )
    from ...ui._style import PROMPT_STYLE

    subcmd = args[0].lower() if args else ""

    # ── /skill install <path> — 从本地文件安装 ────────────────
    if subcmd == "install":
        if len(args) < 2:
            console.print_warning("Usage: /skill install <path-to-skill.md>")
            return True
        source = args[1]
        # 可选 --project 标志：安装到项目级而非全局
        global_install = "--project" not in args
        try:
            skill = install_skill(
                source,
                workspace=str(agent.workspace),
                global_install=global_install,
            )
            # 重新加载 skills 并重建系统提示
            agent.skills = load_skills(agent.workspace)
            agent._system_prompt = agent._build_system_prompt()
            scope = "global (~/.openx/skills/)" if global_install else "project (.openx/skills/)"
            console.print_success(
                f"Skill '{skill.name}' installed to {scope}\n"
                f"  [dim]{skill.description}[/dim]"
            )
        except FileNotFoundError as e:
            console.print_error(str(e))
        except ValueError as e:
            console.print_error(f"Invalid skill file: {e}")
        return True

    # ── /skill add — 交互式创建新 skill ─────────────────────
    if subcmd == "add":
        console.raw.print("\n[bold]Create New Skill[/bold]\n")
        name = paste_aware_input(console.raw, 
            f"  [{PROMPT_STYLE}]Skill name[/{PROMPT_STYLE}] "
            f"[dim](e.g. docker-expert, api-designer)[/dim]: "
        ).strip()
        if not name:
            console.print_warning("Skill name cannot be empty — skipped.")
            return True
        description = paste_aware_input(console.raw, 
            f"  [{PROMPT_STYLE}]Description[/{PROMPT_STYLE}] "
            f"[dim](what this skill does)[/dim]: "
        ).strip()
        trigger_str = paste_aware_input(console.raw, 
            f"  [{PROMPT_STYLE}]Trigger keywords[/{PROMPT_STYLE}] "
            f"[dim](comma-separated, Enter to skip)[/dim]: "
        ).strip()
        triggers = [t.strip() for t in trigger_str.split(",") if t.strip()] if trigger_str else []
        console.raw.print(
            f"  [{PROMPT_STYLE}]Instructions[/{PROMPT_STYLE}] "
            f"[dim](multi-line; end with an empty line)[/dim]:\n"
        )
        content_lines: list[str] = []
        while True:
            try:
                line = paste_aware_input(console.raw, "  ")
            except (KeyboardInterrupt, EOFError):
                break
            if not line.strip():
                break
            content_lines.append(line)
        content = "\n".join(content_lines).strip()
        if not content:
            console.print_warning("Skill instructions cannot be empty — skipped.")
            return True

        # 选择安装范围
        scope_options = [
            ("Global (~/.openx/skills/)", "global"),
            ("Project (.openx/skills/)", "project"),
        ]
        console.raw.print()
        try:
            scope = console._interactive_select(
                scope_options, default_index=0, prompt="Install to:",
            )
        except (KeyboardInterrupt, EOFError):
            return True
        global_install = scope == "global"

        skill = install_skill_from_content(
            name=name,
            description=description,
            content=content,
            trigger=triggers,
            workspace=str(agent.workspace),
            global_install=global_install,
        )
        agent.skills = load_skills(agent.workspace)
        agent._system_prompt = agent._build_system_prompt()
        console.print_success(f"Skill '{skill.name}' created and installed.")
        return True

    # ── /skill remove <name> — 卸载 skill ───────────────────
    if subcmd in ("remove", "rm", "uninstall"):
        if len(args) < 2:
            # 交互式选择
            skills = agent.skills
            if not skills:
                console.print_info("No skills installed.")
                return True
            options: list[tuple[str, object]] = [
                (f"{n}  ({s.description or s.level})", n)
                for n, s in skills.items()
            ]
            options.append(("Cancel", None))
            console.raw.print()
            try:
                choice = console._interactive_select(
                    options, default_index=0, prompt="Remove skill:",
                )
            except (KeyboardInterrupt, EOFError):
                return True
            if choice:
                if uninstall_skill(choice, workspace=str(agent.workspace)):
                    agent.skills = load_skills(agent.workspace)
                    agent._system_prompt = agent._build_system_prompt()
                    console.print_success(f"Removed skill: {choice}")
                else:
                    console.print_error(f"Failed to remove skill: {choice}")
            return True
        target = args[1]
        if uninstall_skill(target, workspace=str(agent.workspace)):
            agent.skills = load_skills(agent.workspace)
            agent._system_prompt = agent._build_system_prompt()
            console.print_success(f"Removed skill: {target}")
        else:
            console.print_error(f"Skill not found: {target}")
        return True

    # ── /skill (无子命令) — 列出已安装的 skills ───────────────
    skills = agent.skills
    if not skills:
        console.print_info(
            "No skills installed.\n\n"
            "Skills are markdown instruction packs that teach the agent "
            "specialized capabilities.\n\n"
            "Manage skills:\n"
            "  /skill add              Create a new skill interactively\n"
            "  /skill install <file>   Install from a .md file\n"
            "  /skill remove <name>    Uninstall a skill\n\n"
            f"Skill directories:\n"
            f"  Global:  {GLOBAL_SKILLS_DIR}\n"
            f"  Project: {agent.workspace}/.openx/skills/"
        )
        return True
    console.raw.print(
        "\n[bold]Installed Skills[/bold]  "
        f"[dim](global: {GLOBAL_SKILLS_DIR} | project: .openx/skills/)[/dim]\n"
    )
    for name, skill in skills.items():
        level_tag = f"[dim][{skill.level}][/dim]"
        trigger_str = f"  [dim]trigger: {', '.join(skill.trigger)}[/dim]" if skill.trigger else ""
        console.raw.print(f"  [bold cyan]{name}[/bold cyan] {level_tag}")
        if skill.description:
            console.raw.print(f"    {skill.description}")
        if trigger_str:
            console.raw.print(f"   {trigger_str}")
    console.raw.print(
        f"\n[dim]{len(skills)} skills loaded. "
        f"Manage: /skill add | /skill install <file> | /skill remove <name>[/dim]"
    )
    return True


@register(
    "workflow",
    description="List or run saved workflows (.openx/workflows/)",
    aliases=["workflows"],
)
async def _cmd_workflow(agent, console, args):
    # 无参 → 列举已保存工作流；带名参数 → 直接经 WorkflowEngine 运行。
    # 所有 handler 都是 async 且由 handle_slash_command await 调度（/compact
    # 同款先例），因此这里直接跑引擎即可——此时 REPL 没有活跃的 Live 区域，
    # 子代理走非流式 child.run()，绝不争抢终端。
    from ...orchestration.workflow import WorkflowEngine, WorkflowError, list_workflows, load_workflow

    if not args:
        rows = list_workflows(str(agent.workspace))
        if not rows:
            console.print_info(
                "No saved workflows.\n\n"
                "Create .openx/workflows/<name>.py with a `meta = {...}` dict and\n"
                "an `async def main(agent, parallel, pipeline, phase, log, args)`\n"
                "entry point, then run it with /workflow <name>."
            )
            return True
        console.raw.print(
            "\n[bold]Saved Workflows[/bold]  [dim](.openx/workflows/)[/dim]\n"
        )
        for row in rows:
            console.raw.print(f"  [bold cyan]/workflow {row['name']}[/bold cyan]")
            if row["description"]:
                console.raw.print(f"    [dim]{row['description']}[/dim]")
        console.raw.print()
        return True

    name = args[0]
    try:
        source, path = load_workflow(str(agent.workspace), name)
    except WorkflowError as e:
        console.print_error(str(e))
        return True
    console.print_info(f"Running workflow '{name}' … (unsandboxed Python)")
    engine = WorkflowEngine(agent)
    try:
        result, stats = await engine.run(source, script_name=str(path))
    except WorkflowError as e:
        console.print_error(f"Workflow failed: {e}")
        return True
    try:
        body = json.dumps(result, ensure_ascii=False, default=str, indent=2)
    except Exception:
        body = repr(result)
    console.raw.print(body)
    console.print_success(
        f"Workflow '{name}' finished — {stats.agents_run} agents, "
        f"{stats.agents_failed} failed, {stats.total_output_tokens} tokens, "
        f"{stats.elapsed_seconds:.1f}s"
    )
    return True


@register("forget", description="Delete a memory by name")
async def _cmd_forget(agent, console, args):
    if not args:
        console.print_warning("Usage: /forget <memory-name>")
        return True
    name = args[0].strip()
    if agent.memory.delete(name):
        agent.reload_instructions()
        console.print_success(f"Deleted memory: {name}")
    else:
        console.print_error(f"No memory found with name: {name}")
    return True


@register("compact", description="Summarize history to free up context")
async def _cmd_compact(agent, console, args):
    console.print_info("Compacting conversation history…")
    try:
        summary = await agent.compact_history()
        console.print_success("History compacted.")
        console._console.print(f"[dim]{summary[:300]}…[/dim]")
    except Exception as e:
        console.print_error(f"Compaction failed: {e}")
    return True


@register(
    "cmemory",
    description="Manage coding memory (list / search / add / clear)",
    aliases=["cmem"],
)
async def _cmd_cmemory(agent, console, args):
    from ...coding_memory import CATEGORIES

    store = agent.coding_memory
    subcmd = args[0].lower() if args else ""

    # ── /cmemory add <content> — 快速记住一条编程知识 ─────────
    if subcmd == "add":
        if len(args) < 2:
            console.print_warning(
                "Usage: /cmemory add <content> [--category <cat>] [--paths <glob,...>]\n\n"
                f"Categories: {', '.join(sorted(CATEGORIES))}"
            )
            return True
        # 解析可选标志
        content_parts: list[str] = []
        category = "project_fact"
        paths: list[str] = []
        keywords: list[str] = []
        i = 1
        while i < len(args):
            if args[i] == "--category" and i + 1 < len(args):
                category = args[i + 1]
                i += 2
            elif args[i] == "--paths" and i + 1 < len(args):
                paths = [p.strip() for p in args[i + 1].split(",") if p.strip()]
                i += 2
            elif args[i] == "--keywords" and i + 1 < len(args):
                keywords = [k.strip() for k in args[i + 1].split(",") if k.strip()]
                i += 2
            else:
                content_parts.append(args[i])
                i += 1
        content = " ".join(content_parts)
        if not content:
            console.print_warning("Content cannot be empty.")
            return True
        mem = store.remember(
            content,
            category=category,
            keywords=keywords,
            related_paths=paths,
            scope="project",
            source="user",
        )
        # 重建系统提示让新记忆立即生效
        agent._system_prompt = agent._build_system_prompt()
        console.print_success(
            f"Remembered [{mem.category}] (id: {mem.id}):\n"
            f"  {mem.content[:100]}"
        )
        return True

    # ── /cmemory search <query> — 搜索记忆 ───────────────────
    if subcmd in ("search", "find", "recall"):
        query = " ".join(args[1:]) if len(args) > 1 else ""
        results = store.recall(query=query, limit=15)
        if not results:
            console.print_info("No matching memories found.")
            return True
        console.raw.print(f"\n[bold]Coding Memory Search[/bold]  [dim]'{query}'[/dim]\n")
        for score, mem in results:
            cat_style = {
                "code_convention": "cyan",
                "architecture_decision": "magenta",
                "debug_pattern": "yellow",
                "project_fact": "green",
            }.get(mem.category, "white")
            path_hint = f"  [dim]({', '.join(mem.related_paths[:2])})[/dim]" if mem.related_paths else ""
            console.raw.print(
                f"  [{cat_style}]{mem.category}[/{cat_style}] "
                f"[dim]#{mem.id}[/dim] {mem.content[:80]}{path_hint}"
            )
        console.raw.print(f"\n[dim]{len(results)} results.[/dim]")
        return True

    # ── /cmemory clear — 清空项目记忆 ────────────────────────
    if subcmd == "clear":
        target = args[1] if len(args) > 1 else ""
        if target == "--global":
            count = store.forget_by_content("")  # 清空全部
            console.print_success(f"Cleared all coding memories ({count} removed).")
        else:
            # 只清项目级
            entries = store.list_all(scope="project")
            for e in entries:
                store.forget(e.id)
            console.print_success(f"Cleared project coding memories ({len(entries)} removed).")
        agent._system_prompt = agent._build_system_prompt()
        return True

    # ── /cmemory forget <id> — 删除指定记忆 ──────────────────
    if subcmd in ("forget", "rm", "delete"):
        if len(args) < 2:
            console.print_warning("Usage: /cmemory forget <memory-id>")
            return True
        if store.forget(args[1]):
            agent._system_prompt = agent._build_system_prompt()
            console.print_success(f"Forgot memory: {args[1]}")
        else:
            console.print_error(f"Memory not found: {args[1]}")
        return True

    # ── /cmemory (无子命令) — 列出所有记忆 + 统计 ─────────────
    stats = store.stats()
    entries = store.list_all()
    if not entries:
        console.print_info(
            "No coding memories stored yet.\n\n"
            "Coding memory stores project-specific knowledge that persists "
            "across sessions:\n"
            "  conventions, architecture decisions, debug patterns, etc.\n\n"
            "Usage:\n"
            "  /cmemory add <fact> [--category <cat>] [--paths <glob>]\n"
            "  /cmemory search <query>\n"
            "  /cmemory forget <id>\n"
            "  /cmemory clear\n\n"
            f"Categories: {', '.join(sorted(CATEGORIES))}"
        )
        return True

    console.raw.print(
        f"\n[bold]Coding Memory[/bold]  "
        f"[dim]({stats['total']} entries | "
        f"project: {stats['by_scope'].get('project', 0)} | "
        f"global: {stats['by_scope'].get('global', 0)})[/dim]\n"
    )
    # 按分类分组展示
    by_cat: dict[str, list] = {}
    for e in entries:
        by_cat.setdefault(e.category, []).append(e)
    for cat in sorted(by_cat):
        console.raw.print(f"  [bold]{cat}[/bold] ({len(by_cat[cat])})")
        for mem in by_cat[cat][:5]:  # 每类最多显示 5 条
            path_hint = f" [dim]({', '.join(mem.related_paths[:2])})[/dim]" if mem.related_paths else ""
            console.raw.print(
                f"    [dim]#{mem.id}[/dim] {mem.content[:70]}{path_hint}"
            )
        if len(by_cat[cat]) > 5:
            console.raw.print(f"    [dim]+{len(by_cat[cat]) - 5} more…[/dim]")
    console.raw.print(
        "\n[dim]Manage: /cmemory add | /cmemory search | "
        "/cmemory forget <id> | /cmemory clear[/dim]"
    )
    return True


if __name__ == "__main__":
    names = sorted(_commands)
    print(f"registered slash commands ({len(names)}):")
    for name in names:
        aliases = [a for a, target in _aliases.items() if target == name]
        alias_str = f" (aliases: {', '.join('/' + a for a in aliases)})" if aliases else ""
        print(f"  /{name}{alias_str} — {_descriptions[name]}")
    print("openx/cli/commands.py OK ✓")
