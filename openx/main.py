"""OpenX CLI entry point.

Usage:
    openx                    # Interactive REPL mode
    openx "fix the bug"      # Single-shot mode
    openx --help             # Show help
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

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

from .config import OpenXConfig
from .agent import OpenXAgent
from .orchestration.sessions import SessionMeta, SessionStore, resolve_by_id, resolve_latest
from .ui.console import Console
from .app.cli.setup_wizard import run_setup_wizard
from .app.cli.interactive import run_interactive
from .app.cli.single_shot import run_single_shot

# --resume 不带值时的哨兵：进入交互式会话选择器
_PICK_SENTINEL = "__pick__"


def _rewrite_serve_argv(argv: Optional[list[str]]) -> list[str]:
    """把 ``openx serve [...]`` 改写成 ``openx --serve [...]``。

    "serve" 是子命令名（文档 UX），但 argparse 的 ``prompt`` 位置参数会把它
    当成提示词吃掉。纯函数，便于单测。字面提示词 "serve" 用 ``openx -- serve``
    显式结束选项（B12）。
    """
    if argv and argv[0] == "serve":
        return ["--serve", *argv[1:]]
    return list(argv) if argv else []


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="openx",
        description="OpenX — Agentic coding CLI. Chat with your codebase using LLMs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  openx                          Start interactive REPL
  openx "fix all type errors"    Single-shot mode
  openx --image shot.png "what's in this?"   Single-shot with an image
  openx --workspace /my/project  Set workspace
  openx --auto-approve           Skip permission prompts
  openx --continue               Resume the latest session for this workspace
  openx --resume [SESSION_ID]    Resume a session (no id: interactive picker)

Model & provider config lives in model groups (~/.openx/settings.json); manage
them interactively with /model and /config. Environment:
  OPENX_AUTO_APPROVE Set to 'true' to skip all prompts
  OPENX_WEB_SEARCH   'ddg' | 'bing' | 'auto' (web-search backend)
""",
    )

    parser.add_argument(
        "prompt",
        nargs="?",
        help="Single-shot prompt. If omitted, starts interactive REPL.",
    )
    parser.add_argument(
        "--workspace", "-w",
        default=os.getcwd(),
        help="Workspace directory (default: current directory)",
    )
    parser.add_argument(
        "--auto-approve", "-y",
        action="store_true",
        help="Skip all permission prompts",
    )
    parser.add_argument(
        "--image", "-i",
        action="append",
        default=[],
        help="Attach an image file for multimodal analysis. Repeatable.",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Disable streaming output",
    )
    parser.add_argument(
        "--output-format",
        choices=["text", "json", "stream-json"],
        default="text",
        help=(
            "Single-shot output format (requires a prompt): text (default, "
            "human-readable), json (one machine-readable result object on "
            "stdout; exit 0/1), stream-json (NDJSON events: init, "
            "text_delta, tool_use, tool_result, result)"
        ),
    )
    parser.add_argument(
        "--continue",
        dest="continue_session",
        action="store_true",
        help="Resume the most recent session for this workspace",
    )
    parser.add_argument(
        "--resume",
        nargs="?",
        const=_PICK_SENTINEL,
        default=None,
        metavar="SESSION_ID",
        help="Resume a session by id; omit the id for an interactive picker",
    )
    parser.add_argument(
        "--version", "-V",
        action="store_true",
        help="Show version and exit",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start a long-lived web server (Web 端) instead of the REPL",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Serve bind host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8787,
        help="Serve bind port (default: 8787)",
    )

    return parser.parse_args(argv)


def _open_session(
    args: argparse.Namespace,
    workspace: str,
    config: OpenXConfig,
    console: Console,
) -> tuple[SessionStore, Optional[SessionMeta], list[dict]]:
    """Resolve --continue/--resume into ``(store, resumed_meta, messages)``.

    会话解析（Phase 6）：

    - ``--continue`` → 该工作区最新会话；没有则警告并起新会话；
    - ``--resume``（无值）→ 交互式选择器；取消或列表为空 → 新会话；
    - ``--resume <id>`` → 精确匹配；未命中报错退出；
    - 缺省 → 新建会话。

    恢复成功时返回的 ``messages`` 已经过孤立 tool 消息清洗，可直接灌入
    agent 历史。精确 id 未命中是用户输入错误 → 走 stderr + exit(1)。
    """

    def _resume_from(meta: SessionMeta):
        loaded_meta, messages = SessionStore.load(meta.path)
        return SessionStore.open(loaded_meta), loaded_meta, messages

    if args.continue_session:
        meta = resolve_latest(workspace)
        if meta is None:
            console.print_warning("No previous session found — starting fresh.")
        else:
            return _resume_from(meta)
    elif args.resume is not None:
        if args.resume == _PICK_SENTINEL:
            metas = SessionStore.list_for_workspace(workspace)
            if not metas:
                console.print_warning("No previous sessions found — starting fresh.")
            else:
                picked = console.pick_session(metas)
                if picked is None:
                    console.print_warning("No session selected — starting fresh.")
                else:
                    return _resume_from(picked)
        else:
            meta = resolve_by_id(workspace, args.resume)
            if meta is None:
                print(
                    f"Error: session not found for this workspace: {args.resume}",
                    file=sys.stderr,
                )
                sys.exit(1)
            return _resume_from(meta)

    # 新会话
    store = SessionStore.create(workspace, config.model, group=config.active_group)
    return store, None, []


def _cleanup_background_tasks(agent: OpenXAgent) -> None:
    """Stop all background tasks on CLI exit — best-effort, never raises.

    Phase 7：single-shot / interactive 两条路径都走 ``asyncio.run``，回到
    这里时原 loop 已关闭（watcher 协程已被取消）。``TaskRegistry.stop()``
    内部有 OS 层（``killpg(pgid, 0)``）存亡探测兜底，因此这里只需在新
    loop 上跑一遍 ``cleanup()`` 即可收掉残留进程组。
    """
    try:
        registry = getattr(agent, "tasks", None)
        if registry is not None and any(h.running for h in registry.all()):
            asyncio.run(registry.cleanup())
    except Exception:
        pass
    # MCP（Phase 9）：best-effort 关闭 MCP server 连接。正常路径上
    # run_interactive / run_single_shot 的 finally 已经关过——shutdown()
    # 幂等，这里是 Ctrl-C/异常路径的兜底。
    try:
        asyncio.run(agent.shutdown())
    except Exception:
        pass


def _import_serve() -> tuple:
    """惰性导入 serve 模块；aiohttp 缺失时给出安装指引并退出。

    aiohttp 是 ``web`` optional extra——普通 CLI 路径绝不因缺失而受影响，
    serve 路径给出清晰报错（web extra 缺失不是产品缺陷）。
    """
    try:
        from .app.serve.bridge import ServeConsole
        from .app.serve.server import run_serve
    except ImportError as e:
        print(
            "Error: openx serve requires the 'web' extra.\n"
            "  Install with:  pip install 'openx[web]'\n"
            f"  ({e})",
            file=sys.stderr,
        )
        sys.exit(2)
    return ServeConsole, run_serve


def main(argv: Optional[list[str]] = None) -> None:
    """CLI entry point."""
    # argv=None（console 入口）→ 取 sys.argv[1:]；再经 serve 子命令改写。
    # 注意：改写前必须保留 None→sys.argv 语义——直接喂 [] 会让 parse_args
    # 忽略真实参数、CLI 完全失效（B12 回归）。
    raw = sys.argv[1:] if argv is None else argv
    args = parse_args(_rewrite_serve_argv(raw))

    if args.version:
        from . import __version__
        print(f"OpenX v{__version__}")
        return

    # 机器输出格式只对单次查询有效（REPL 有自己的交互 UI）
    if args.output_format != "text" and not args.prompt:
        print(
            f"Error: --output-format {args.output_format} requires a "
            f"single-shot prompt, e.g.: openx \"your task\" "
            f"--output-format {args.output_format}",
            file=sys.stderr,
        )
        sys.exit(2)

    # serve 模式守卫：不吞单次提示词；交互式会话选择器换用新鲜会话（B12）
    if args.serve and args.prompt:
        print(
            "Error: openx serve does not accept a prompt; it starts a web "
            "server. Omit the prompt (use '--' before a literal 'serve').",
            file=sys.stderr,
        )
        sys.exit(2)
    if args.serve and args.resume == _PICK_SENTINEL:
        print(
            "Warning: --resume with no id (interactive picker) is not "
            "available in serve mode; starting a fresh session.",
            file=sys.stderr,
        )
        args.resume = None

    # ── First-run check: modelGroups ─────────────────────────────
    # 模型/凭据只来自 settings.json 的 modelGroups；未配置（无激活组）→
    # 启动交互向导（env/CLI 不再被当作"已配置"）。
    if not OpenXConfig.is_configured():
        asyncio.run(run_setup_wizard())

    # Build config (project settings + 非 provider env；模型组经 role_settings)
    config = OpenXConfig.load(workspace=args.workspace)

    # 解析激活组 main 绑定并做启动校验（无组/字段缺 → 向导重来）
    active_group, main_settings = config.role_settings("main")
    if not main_settings.get("api_key", "").strip():
        print("Error: No API key configured.", file=sys.stderr)
        print("Run 'openx' to launch the setup wizard.", file=sys.stderr)
        sys.exit(1)
    # api_base 仅 openai-compat 必需；anthropic-compat base 可选（留空=官方，
    # 兼容端点经组/角色的 apiBase 配置）。
    if (
        main_settings.get("kind") == "openai-compat"
        and not main_settings.get("api_base", "").strip()
    ):
        print("Error: No API base URL configured.", file=sys.stderr)
        print("Run 'openx' to launch the setup wizard.", file=sys.stderr)
        sys.exit(1)
    if not main_settings.get("model", "").strip():
        print("Error: No model configured.", file=sys.stderr)
        print("Run 'openx' to launch the setup wizard.", file=sys.stderr)
        sys.exit(1)

    # 把生效的 main 绑定投影回 config（仅 echo：组名 + 模型；凭据不投影）
    config.active_group = active_group
    config.model = main_settings.get("model") or config.model

    if args.auto_approve:
        config.auto_approve = True
    config.stream = not args.no_stream

    # ── Trust check ──────────────────────────────────────────────
    # Ask user to trust the workspace before OpenX can access it.
    # Skip in single-shot --auto-approve mode or if already trusted.
    console = Console(config)
    workspace_abs = str(Path(config.workspace).resolve())

    if not OpenXConfig.is_trusted(workspace_abs):
        # Show a minimal trust screen — no logo yet, that comes after trust
        trusted = console.ask_trust_directory(workspace_abs)
        console._console.print()
        if not trusted:
            console._console.print(
                "  [yellow]Trust declined. Exiting.[/yellow]\n"
            )
            sys.exit(0)
        OpenXConfig.add_trusted_dir(workspace_abs)
        console.print_success("Directory trusted. You won't be asked again.")
        console._console.print()

    # ── Session persistence (Phase 6) ────────────────────────────
    # 解析 --continue/--resume，决定续写既有会话文件还是新建。
    store, resumed_meta, resumed_messages = _open_session(
        args, workspace_abs, config, console
    )

    # Create agent（session_id 与会话文件保持一致；console 传入同一实例——
    # 工具弹窗/模式状态与 REPL 状态行共用一个 console，弹窗钩子不再落空）。
    # serve 模式：agent 用无 TUI 的 ServeConsole（浏览器是端层，终端不渲染
    # 转录；权限批准经 Web 权限桥）——双 console 流：信任/会话选择仍走
    # 终端 console，agent 运行走 ServeConsole（B3）。
    serve_entry = None
    agent_console = console
    if args.serve:
        ServeConsole, serve_entry = _import_serve()
        agent_console = ServeConsole()

    agent = OpenXAgent(
        config,
        session_store=store,
        session_id=store.meta.session_id,
        console=agent_console,
    )
    if resumed_meta is not None:
        agent.load_session(resumed_meta, resumed_messages)

    # Run（Phase 7：无论正常返回、KeyboardInterrupt 还是异常，finally 里
    # best-effort 清理所有后台任务——绝不因清理失败影响退出）
    exit_code = 0
    try:
        if serve_entry is not None:
            # serve 默认 manual（写工具逐项经 web 批准）；--auto-approve
            # 显式切 auto，跳过批准（仍受危险命令闸门约束）
            if args.auto_approve:
                agent.set_mode("auto")
            exit_code = asyncio.run(
                serve_entry(
                    agent, agent_console, args.host, args.port, workspace_abs
                )
            )
        elif args.prompt:
            # 单次查询返回退出码：机器格式（json/stream-json）下 0=成功、
            # 1=失败，供 CI 与脚本管道判断；text 格式失败同样落 1
            exit_code = asyncio.run(
                run_single_shot(
                    agent, console, args.prompt,
                    image_paths=args.image,
                    output_format=args.output_format,
                )
            )
        else:
            try:
                asyncio.run(run_interactive(agent, console))
            except KeyboardInterrupt:
                console.print_goodbye(agent.session_token_usage())
    finally:
        _cleanup_background_tasks(agent)
    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
