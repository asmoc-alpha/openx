"""First-run interactive setup wizard.

Prompts for provider type (OpenAI-compatible / Anthropic), then the
connection fields, and saves to ``~/.openx/settings.json``.
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

from ...config import OpenXConfig
from ...ui.console import Console

# Anthropic 分支写 providers 配置的实例名与默认模型
_ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-5"


async def run_setup_wizard() -> dict:
    """Run the interactive first-run setup wizard.

    Step 0 选实现（OpenAI 兼容 / Anthropic 原生），再收连接字段；收齐后
    把结果写成 ``default`` 模型组（modelGroups + activeGroup）并落盘。
    直接返回空 dict（配置已写入 settings.json，无需调用方再存 env）。

    组内只配 main 角色；其余 exec/mini/modal 可稍后经 settings.json /
    /config 补充。
    """
    # Use a bare-minimum console for setup (no config needed yet)
    console = Console(OpenXConfig())

    console.print_setup_welcome()

    # ── Step 0: Provider type ───────────────────────────────────
    console.raw.print()
    try:
        ptype = console._interactive_select(
            [
                ("OpenAI-compatible (OpenAI / DeepSeek / local endpoints)", "openai"),
                ("Anthropic (Claude)", "anthropic"),
            ],
            default_index=0,
            prompt="Provider type:",
        )
    except (KeyboardInterrupt, EOFError):
        ptype = "openai"

    env: dict[str, str] = {}

    if ptype == "anthropic":
        # ── Anthropic 原生：key + model（无 base URL 概念）────────
        env["OPENX_API_KEY"] = console.prompt_setup_field(
            step=1, total=2, label="Anthropic API Key", default="",
        )
        env["OPENX_DEFAULT_MODEL"] = console.prompt_setup_field(
            step=2, total=2, label="Default Model",
            default=_ANTHROPIC_DEFAULT_MODEL,
        )
        env["OPENX_BASE_URL"] = ""
        kind = "anthropic"
        while not console.print_setup_summary(env):
            console.raw.print("\n[dim]Let's try again...[/dim]")
            env["OPENX_API_KEY"] = console.prompt_setup_field(
                step="*", total=2, label="Anthropic API Key",
                default=env["OPENX_API_KEY"],
            )
            env["OPENX_DEFAULT_MODEL"] = console.prompt_setup_field(
                step="*", total=2, label="Default Model",
                default=env["OPENX_DEFAULT_MODEL"],
            )
    else:
        # ── OpenAI 兼容：base + key + model ──────────────────────
        env["OPENX_BASE_URL"] = console.prompt_setup_field(
            step=1, total=3, label="API Base URL",
            default="https://api.openai.com/v1",
        )
        env["OPENX_API_KEY"] = console.prompt_setup_field(
            step=2, total=3, label="API Key", default="",
        )
        env["OPENX_DEFAULT_MODEL"] = console.prompt_setup_field(
            step=3, total=3, label="Default Model", default="gpt-4o",
        )
        kind = "openai-compat"
        while not console.print_setup_summary(env):
            console.raw.print("\n[dim]Let's try again...[/dim]")
            env["OPENX_BASE_URL"] = console.prompt_setup_field(
                step="*", total=3, label="API Base URL",
                default=env["OPENX_BASE_URL"],
            )
            env["OPENX_API_KEY"] = console.prompt_setup_field(
                step="*", total=3, label="API Key", default=env["OPENX_API_KEY"],
            )
            env["OPENX_DEFAULT_MODEL"] = console.prompt_setup_field(
                step="*", total=3, label="Default Model",
                default=env["OPENX_DEFAULT_MODEL"],
            )

    # ── 落盘：写 default 模型组 + 激活 ──────────────────────────
    group: dict = {
        "kind": kind,
        "openx-main-model": env.get("OPENX_DEFAULT_MODEL", ""),
    }
    if env.get("OPENX_API_KEY"):
        group["apiKey"] = env["OPENX_API_KEY"]
    if env.get("OPENX_BASE_URL"):
        group["apiBase"] = env["OPENX_BASE_URL"]
    raw = OpenXConfig.load_model_groups_raw()
    raw["default"] = group
    OpenXConfig.save_model_groups(raw)
    OpenXConfig.set_active_group("default")

    console.print_success("Settings saved to ~/.openx/settings.json")
    console._console.print()
    return {}


if __name__ == "__main__":
    import inspect
    print(f"entry: run_setup_wizard{inspect.signature(run_setup_wizard)}")
    print("(wizard not launched — would prompt for input)")
    print("openx/cli/setup_wizard.py OK ✓")
