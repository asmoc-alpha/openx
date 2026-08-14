"""Interactive REPL loop for OpenX.

The main ``run_interactive`` function drives the read-eval-print loop:
show startup → prompt → dispatch slash commands → stream agent responses →
repeat.  Extracted from ``main.py`` to keep the CLI entry-point lean.
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

import asyncio
import time
from pathlib import Path
from typing import Any

from ..agent import OpenXAgent
from ..core.hooks import build_userprompt_payload
from ..image import (
    extract_image_paths,
    image_to_base64_url,
    display_image,
)
from ..instructions import ProjectInfo
from ..services.streaming import StreamingService
from ..ui.console import Console
from .commands import handle_slash_command


class _StreamInterrupted(Exception):
    """消费子任务内捕获的 Ctrl-C 的替身异常（v0.4.1）。

    asyncio 对任务内逃逸的 ``KeyboardInterrupt`` 有特殊处理——直接抛
    出事件循环，跳过协程栈上的一切 except（包括本模块的清理路径：
    光标恢复 / termios 还原）。故在消费子任务内先捕获，换成普通异常
    在 await 点重抛，外层再还原成 ``KeyboardInterrupt`` 上抛，语义与
    旧版（直接在主任务消费流）逐点对齐。
    """


async def run_interactive(agent: OpenXAgent, console: Console) -> None:
    """Run the interactive REPL."""
    try:
        # MCP（Phase 9）：进入 REPL 前连接配置的 MCP servers 并登记
        # 远程工具（失败只警告、不阻塞）；无论 REPL 如何退出，finally
        # 里幂等关闭连接。
        await agent.startup()

        # Explore project and show structured startup
        try:
            info = await agent.explore_project()
        except Exception:
            info = None  # type: ignore[assignment]

        instructions_loaded = agent.instructions.has_any
        if info:
            console.show_startup(info, instructions_loaded=instructions_loaded)
        else:
            console.show_startup(ProjectInfo(), instructions_loaded=instructions_loaded)

        while True:
            # Messages queued during a previous stream are sent first; only
            # when the queue is empty do we block on a fresh prompt.
            if console._input_queue:
                user_input = console._input_queue.pop(0)
                # The previous stream left a frame on screen; clear it so the
                # banner can take its place (no fresh prompt was drawn).
                console.clear_input_frame()
            else:
                try:
                    user_input = console.print_user_prompt(
                        input_tokens=agent.total_input_tokens,
                        output_tokens=agent.total_output_tokens,
                    )
                except (KeyboardInterrupt, EOFError):
                    console.print_goodbye()
                    break

                # EOF（非 TTY stdin 已耗尽，如 `openx </dev/null`）：
                # print_user_prompt 返回 None——必须像 /quit 一样干净退出；
                # 若落到下面的 `not user_input: continue`，REPL 会空转死循环。
                # Non-TTY EOF yields None — break cleanly like /quit,
                # otherwise `not user_input: continue` would busy-loop.
                if user_input is None:
                    console.print_goodbye()
                    break

                if not user_input:
                    continue

            # A blank line above the banner separates this turn from the
            # previous content; the banner is the user's question, echoed
            # above the model's answer.
            console._console.print()
            console.print_sent_message(user_input)

            # ── slash commands ──────────────────────────────────────
            if user_input.startswith("/"):
                cmd = user_input[1:].strip().lower().split()
                if not cmd:
                    continue

                result = await handle_slash_command(cmd[0], agent, console, cmd[1:])
                if result is False:
                    break
                if result is True:
                    continue
                if result is None:
                    console.print_warning(f"Unknown command: /{cmd[0]}. Try /help")
                continue

            # ── UserPromptSubmit hook（Phase 5）─────────────────────
            # 提示词送达模型之前的最后一道关卡：策略钩子可整条驳回本次提问。
            # 钩子自身故障一律降级放行——绝不让钩子系统锁死 REPL。
            if agent.hooks.has_hooks("UserPromptSubmit"):
                try:
                    outcome = await agent.hooks.run(
                        "UserPromptSubmit",
                        build_userprompt_payload(
                            user_input,
                            workspace=agent.hooks.workspace,
                            session_id=agent.hooks.session_id,
                        ),
                    )
                except Exception:
                    outcome = None
                if outcome is not None:
                    for w in outcome.warnings:
                        console.print_warning(w)
                    if outcome.blocked:
                        console.print_error(f"Blocked by hook: {outcome.reason}")
                        continue  # 不发送给模型，回到提示符

            # ── agent query ─────────────────────────────────────────
            await _stream_response(agent, console, user_input)
    finally:
        await agent.shutdown()


# ── helpers ──────────────────────────────────────────────────────


async def _stream_response(
    agent: OpenXAgent,
    console: Console,
    user_input: str,
) -> None:
    """Build user content from *user_input*, stream the agent response."""

    # Check for drag-and-dropped image paths in the input
    image_paths = extract_image_paths(user_input)
    if image_paths:
        for ip in image_paths:
            display_image(ip)
        # If the input is JUST image paths, ask what to do
        remaining = user_input
        for ip in image_paths:
            remaining = remaining.replace(str(ip), "").replace("\\ ", " ")
        remaining = " ".join(remaining.split())
        if not remaining:
            console.print_info(
                f"Image{'s' if len(image_paths) > 1 else ''} loaded. "
                "What would you like to know?"
            )
            try:
                remaining = console.print_user_prompt(
                    input_tokens=agent.total_input_tokens,
                    output_tokens=agent.total_output_tokens,
                )
            except (KeyboardInterrupt, EOFError):
                return
            # None = EOF（非 TTY）：不发起 LLM 调用，回 REPL 主循环统一退出
            # None means EOF — return to the main loop, which exits cleanly.
            if remaining is None:
                return
            if not remaining:
                remaining = "Describe this image."
        user_input = remaining

    try:
        user_content: str | list[dict[str, Any]] = user_input
        if image_paths:
            data_urls = [image_to_base64_url(p) for p in image_paths]
            user_content = agent.build_user_content(user_input, images=data_urls)

        # A blank line separates the user's banner from the model's answer.
        console._console.print()

        # ── 非流式分支（--no-stream / config.stream=False）────────
        # Wait for the full response, then render it once (no typewriter).
        if not agent.config.stream:
            console.print_streaming_start()  # “Thinking…” indicator
            started = time.monotonic()
            response = await agent.run(user_content)
            # total_output_tokens is accumulated inside agent.run() now
            console.print_streaming_done(
                time.monotonic() - started, agent.total_output_tokens
            )
            console.print_assistant(response)
            return

        display = StreamingService(
            console, agent.total_input_tokens,
            todos_provider=lambda: agent.todos, fleet=agent.fleet,
        )
        display.start()
        # Bug 10: during streaming the InputCapture thread owns stdin
        # (cbreak). A permission prompt raised from inside stream_run needs
        # raw-mode stdin — the executor's prompt hooks pause/resume the
        # capture around each dialog so the two never fight over termios.
        # The non-stream branch runs no capture and needs no hooks.
        agent.tool_executor.on_prompt_start = display.pause_capture
        agent.tool_executor.on_prompt_end = display.resume_capture
        # ask_user / exit_plan_mode 的弹窗发生在工具 execute() 内部，executor
        # 的权限钩子够不到：经 Console 级弹窗钩子让流式服务在弹窗期间整体
        # 暂停（Live 重绘 + InputCapture），否则屏幕疯狂打印、按键被捕获线程
        # 偷走，问题迟迟无法作答。引用计数保证与权限钩子叠加时依然平衡。
        console.on_dialog_start = display.pause
        console.on_dialog_end = display.resume
        try:
            # 流消费跑在**子任务**里：Esc 打断取消的是这个任务，而非
            # 主任务——回合正常结束后任务已完成，流弹 Esc 的 cancel()
            # 即 no-op，绝不误杀下一回合（v0.4.1）。
            async def _consume() -> None:
                # feed 兼收文本 token 与结构化工具事件（ToolStartEvent /
                # ToolResultEvent），REPL 展示格式在服务层统一成型。
                try:
                    async for chunk in agent.stream_run(user_content):
                        display.feed(chunk)
                except KeyboardInterrupt as exc:
                    # 转成普通异常逃出任务（见 _StreamInterrupted 说明）
                    raise _StreamInterrupted from exc

            consume_task = asyncio.ensure_future(_consume())
            display.set_cancel_target(consume_task)
            try:
                await consume_task
                display.done()
            except _StreamInterrupted:
                # Ctrl-C：清理 Live/捕获（光标 + termios 恢复），然后
                # 还原成 KeyboardInterrupt 上抛（与旧版语义一致：
                # 打断即退出 REPL，main() 负责 goodbye）
                try:
                    display.cancel()
                except Exception:
                    pass
                raise KeyboardInterrupt from None
            except asyncio.CancelledError:
                # Esc 打断：清理 Live/捕获（光标 + termios 恢复），吞掉
                # 取消回到 REPL——排过队的消息由主循环下一轮立即发送
                # （"esc to interrupt & send"）。esc_interrupted 为 False
                # 说明是真实外部取消（如 asyncio.run 关闭），原样上抛。
                try:
                    display.cancel()
                except Exception:
                    pass
                if display.esc_interrupted:
                    return
                raise
        except KeyboardInterrupt:
            # Ctrl-C 打断流式：先把 Live（隐藏的光标）与捕获（cbreak 终端）
            # 干净停掉再上抛——否则外层兜底退出后，用户的 shell 会继承一个
            # 没有光标、处于 cbreak 模式的终端（"不再展示光标"回归的出口路径）。
            # Restore cursor + terminal before re-raising Ctrl-C.
            try:
                display.cancel()
            except Exception:
                pass
            raise
        finally:
            # 流结束（或出错）即清钩子，绝不泄漏到后续非流式路径
            agent.tool_executor.on_prompt_start = None
            agent.tool_executor.on_prompt_end = None
            console.on_dialog_start = None
            console.on_dialog_end = None
        # The frame is the last element of the Live render and doubles as
        # the next input — no trailing blank line is needed.
    except Exception as e:
        # Drop the partial live display + frame so the error reads cleanly.
        try:
            display.cancel()  # type: ignore[name-defined]
        except (NameError, UnboundLocalError):
            pass
        console.print_error(f"Error: {e}")
        if "API key" in str(e) or "authentication" in str(e).lower():
            console.print_info(
                "Set your API key with: export OPENAI_API_KEY=your-key-here"
            )


if __name__ == "__main__":
    import inspect
    print(f"entry: run_interactive{inspect.signature(run_interactive)}")
    print(f"helper: _stream_response{inspect.signature(_stream_response)}")
    print("openx/cli/interactive.py OK ✓")
