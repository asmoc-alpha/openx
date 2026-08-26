"""Single-shot (non-interactive) query mode.

Extracted from ``main.py`` to keep the CLI entry-point lean.

输出格式（``output_format``）
============================
- ``text``（默认）：人类可读——启动横幅、Thinking 指示、助手回复；
- ``json``：stdout 只打**一个** JSON 结果对象（Claude Code 兼容字段：
  type/subtype/is_error/duration_ms/num_turns/result/session_id/usage）。
  成功退出码 0、失败 1——供 CI 与脚本管道消费；
- ``stream-json``：NDJSON 事件流——``system/init`` 开场，文本增量
  ``text_delta``、工具 ``tool_use`` / ``tool_result``、收尾 ``result``。

json / stream-json 下一切人类噪音（横幅、警告、弹窗回退菜单）经
``console.raw.file`` 重定向到 **stderr**，stdout 保持纯 JSON。
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
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ...agent import OpenXAgent, ToolResultEvent, ToolStartEvent
from ...core import protocol
from ...image import is_image_file, image_to_base64_url, display_image, get_image_metadata
from ...llm import StreamReasoning
from ...ui.console import Console

# stream-json 单条 tool_result 输出的字符上限（防单事件撑爆管道缓冲）
_STREAM_TOOL_OUTPUT_LIMIT = 2000


def _emit(obj: dict[str, Any]) -> None:
    """NDJSON 一行一事件：stdout、立即 flush，失败即断管可见。"""
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _result_event(
    agent: OpenXAgent,
    started: float,
    result: str | None,
    error: Exception | None = None,
) -> dict[str, Any]:
    """终局 result 事件（json / stream-json 共用）。"""
    obj: dict[str, Any] = {
        "type": "result",
        "subtype": "error" if error is not None else "success",
        "is_error": error is not None,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "num_turns": agent.last_tool_rounds,
        "result": result,
        "session_id": agent.session_id,
        "usage": {
            "input_tokens": agent.total_input_tokens,
            "output_tokens": agent.total_output_tokens,
        },
    }
    if error is not None:
        obj["error"] = f"{type(error).__name__}: {error}"
    return obj


async def _stdin_lines() -> AsyncIterator[str]:
    """上行默认源：stdin 逐行（线程池读，不阻塞事件循环）；EOF 即停。"""
    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            return
        yield line


class _NdjsonPermissionBridge:
    """headless 双向权限（协议 P1）：下行 permission_request，上行
    permission_response 按 request_id 唤醒等待；EOF/断流/未匹配=拒绝
    （只紧不松：拿不到批准就当拒绝）。headless 永不落盘规则。
    """

    def __init__(self, uplink: AsyncIterator[str]):
        self._uplink = uplink
        self._pending: dict[str, asyncio.Future] = {}
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._read_loop())

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def _read_loop(self) -> None:
        try:
            async for line in self._uplink:
                msg = protocol.parse_uplink(line)
                if isinstance(msg, protocol.PermissionResponse):
                    fut = self._pending.get(msg.request_id)
                    if fut is not None and not fut.done():
                        fut.set_result(msg.allowed)
        except Exception:
            pass  # 上行源坏 = 下方待决全部拒绝（安全默认）
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_result(False)

    async def ask_permission(
        self, tool_name, reason, details="", args_summary="",
        can_remember=True, diff=None,
    ):
        request_id = uuid.uuid4().hex
        _emit(protocol.permission_request(
            request_id, tool_name, reason, args_summary or details,
        ))
        fut = asyncio.get_event_loop().create_future()
        self._pending[request_id] = fut
        try:
            allowed = await fut
        finally:
            self._pending.pop(request_id, None)
        return (allowed, False)


async def run_single_shot(
    agent: OpenXAgent,
    console: Console,
    prompt: str,
    image_paths: list[str] | None = None,
    output_format: str = "text",
    ndjson_permissions: bool = False,
    uplink: Optional[AsyncIterator[str]] = None,
) -> int:
    """Run a single-shot (non-interactive) query. Returns an exit code.

    ``ndjson_permissions``（须 stream-json）：权限改走协议双向——
    manual 模式下每个 ASK 下行 permission_request、等上行裁决；
    ``uplink`` 可注入上行行源（测试），默认 stdin。
    """
    # Headless：权限弹窗在非 TTY stdin 上会阻塞（数字菜单回退读 stdin），
    # 默认强制 auto 模式（仍受 -y/存储规则/危险命令闸门约束）；
    # ndjson 权限模式下改 manual——批准权交给协议对端。
    bridge: Optional[_NdjsonPermissionBridge] = None
    if ndjson_permissions and output_format == "stream-json":
        agent.set_mode("manual")
        bridge = _NdjsonPermissionBridge(
            uplink if uplink is not None else _stdin_lines()
        )
        console.ask_permission = bridge.ask_permission
        bridge.start()
    else:
        agent.set_mode("auto")
    machine = output_format in ("json", "stream-json")
    if machine:
        # stdout 只走 JSON；人类噪音（警告、信任回退等）一律去 stderr
        console.raw.file = sys.stderr

    user_content = await _build_user_content_with_images(
        prompt, image_paths or [], console
    )

    try:
        # MCP（Phase 9）：连接配置的 MCP servers（失败只警告、不阻塞）
        await agent.startup()
        try:
            if output_format == "json":
                return await _run_json(agent, console, user_content)
            if output_format == "stream-json":
                return await _run_stream_json(agent, user_content)
            return await _run_text(agent, console, prompt, user_content)
        except Exception as e:
            # _run_* 内部已各自收敛异常；这里是防御兜底（如图片构建后、
            # 分流前的意外）——机器格式绝不让 traceback 污染 stdout
            if machine:
                _emit(_result_event(agent, time.monotonic(), None, e))
            else:
                console.print_error(f"Error: {e}")
            return 1
    finally:
        if bridge is not None:
            bridge.stop()
        # 幂等关闭 MCP 连接——查询成功、失败还是被取消都要收干净
        await agent.shutdown()


async def _run_text(
    agent: OpenXAgent, console: Console, prompt: str, user_content
) -> int:
    """人类可读路径（默认）：横幅 + Thinking 指示 + 助手回复。"""
    console.show_startup_single_shot(prompt)
    try:
        # Console 没有 print_thinking()——复用既有 streaming 状态对：
        # 先打 "Thinking…" 指示，等 run() 完成再打耗时/token 小结。
        console.print_streaming_start()
        started = time.monotonic()
        response = await agent.run(user_content)
        console.print_streaming_done(
            time.monotonic() - started, agent.total_output_tokens
        )
        console.print_assistant(response)
        return 0
    except Exception as e:
        console.print_error(f"Error: {e}")
        return 1


async def _run_json(agent: OpenXAgent, console: Console, user_content) -> int:
    """单个 JSON 结果对象（CI / 脚本管道消费）。"""
    started = time.monotonic()
    try:
        response = await agent.run(user_content)
    except Exception as e:
        _emit(_result_event(agent, started, None, e))
        return 1
    _emit(_result_event(agent, started, response))
    return 0


async def _run_stream_json(agent: OpenXAgent, user_content) -> int:
    """NDJSON 事件流：init → text_delta / tool_use / tool_result → result。

    事件编码收敛在 ``openx/core/protocol``（线格式单一真源，协议 P1）。
    """
    _emit(protocol.init_event(
        agent.session_id, agent.config.model, sorted(agent.tools.keys()),
    ))
    started = time.monotonic()
    try:
        async for event in agent.stream_run(user_content):
            if isinstance(event, ToolStartEvent):
                _emit(protocol.tool_use(event.name))
            elif isinstance(event, ToolResultEvent):
                _emit(protocol.tool_result(
                    event.name, event.is_error,
                    event.output[:_STREAM_TOOL_OUTPUT_LIMIT],
                ))
            elif isinstance(event, StreamReasoning):
                # 推理内容独立事件类型（对标 Claude Code 的 thinking 块），
                # 消费者按需呈现；不混入 text_delta
                _emit(protocol.thinking_delta(event.text))
            elif isinstance(event, str) and event:
                _emit(protocol.text_delta(event))
    except Exception as e:
        _emit(_result_event(agent, started, None, e))
        return 1
    # stream_run 的最终文本不进事件流（模型原文在 text_delta 里逐段
    # 到达过）；result.result 从历史末条 assistant 消息重建，供只要
    # 终值的消费者使用
    final = ""
    for msg in reversed(agent.history.messages):
        if msg.get("role") == "assistant":
            final = msg.get("content") or ""
            break
    _emit(_result_event(agent, started, final))
    return 0


async def _build_user_content_with_images(
    prompt: str,
    image_paths: list[str],
    console: Console,
) -> str | list[dict[str, Any]]:
    """Build user content from a prompt and optional image paths.

    Loads each image, shows metadata, displays in terminal, and converts
    to base64 data URLs for the multimodal LLM.
    """
    if not image_paths:
        return prompt

    valid_paths: list[str] = []
    data_urls: list[str] = []

    for ip in image_paths:
        p = Path(ip).expanduser().resolve()
        if not p.is_file() or not is_image_file(p):
            console.print_warning(f"Skipping invalid image: {ip}")
            continue
        valid_paths.append(str(p))
        meta = get_image_metadata(p)
        console.print_image_loaded(p, meta)
        display_image(p)
        data_urls.append(image_to_base64_url(p))

    if not data_urls:
        return prompt

    if not prompt.strip():
        prompt = "Describe this image."

    return OpenXAgent.build_user_content(prompt, images=data_urls)


if __name__ == "__main__":
    import inspect
    print(f"entry: run_single_shot{inspect.signature(run_single_shot)}")
    print(f"helper: _build_user_content_with_images{inspect.signature(_build_user_content_with_images)}")
    print("openx/cli/single_shot.py OK ✓")
