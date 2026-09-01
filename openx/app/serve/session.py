"""ServeSession：agent 宿主 + 客户端注册表 + 串行回合队列 + 广播。

openx serve（P4）核心：一个 ServeSession 宿主一个 agent（长存会话），
多端 attach 同一会话，事件广播（架构详设 §5-§6）。

- **上行**经 aiohttp WS：``message`` → 入队串行回合；``permission_response``
  → 桥裁决；``interrupt`` → 打断当前回合。
- **下行**每客户端一条 downlink 任务独占 ``ws.send_json``（并发广播不撕裂
  帧）；``broadcast()`` 只入队、不发送。
- **回合串行**（REPL 语义）：``_worker`` 消费 ``_queue``，每条消息 await
  一个 ``_run_turn`` 子任务——任一时刻至多一个 ``stream_run``。
- **attach 快照**：新客户端先收 ``init`` + ``serve_history(agent.history)`` +
  （回合中）``_live_user`` + ``_live_events`` 缓冲重放——迟到客户端看到
  当前上下文，前端 reducer 对 text_delta 追加到末条 assistant 气泡，
  实时与迟加入渲染一致。
- **interrupt**：cancel ``_turn_task``；``_run_turn`` 捕获 CancelledError 后
  广播 ``{"type":"interrupted"}`` 并**正常返回**（不毒死 worker）。回合中
  cancel 安全：``history.add`` 只在回合末尾，部分回合丢弃（同 REPL Esc 语义）。

事件投影（``_project``）与服务端剥 ``[dim]...[/dim]``：``stream_run`` 会
yield 压缩提示等 rich 标签串，绝不能原样落到浏览器。
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
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from aiohttp import web

from ...core import protocol
from .bridge import WebPermissionBridge

_log = logging.getLogger("openx.serve")

# stream_run 事件里 rich 标签剥离（镜像 services/streaming.py _RICH_TAG）：
# 压缩提示等以 [dim]...[/dim] 包裹的文本只服务终端展示，浏览器不需要。
_RICH_TAG = re.compile(
    r"\[/(?:dim|red|green|yellow|blue|cyan|magenta|white|bold|italic|underline)\]"
    r"|\[(?:dim|red|green|yellow|blue|cyan|magenta|white|bold|italic|underline)"
    r"(?:\s+[^\]]*)?\]"
)

# stream-json 同款：单条 tool_result 输出字符上限（防单事件撑爆传输）
_STREAM_TOOL_OUTPUT_LIMIT = 2000

# 面板广播节拍（秒）：ui/v1 插件面板（如桌面宠物）不是回合产物——空闲
# 时也要动，走独立于 _worker 的常驻 ticker。征集器自带 refresh_hz 节流，
# 这里变化才广播（动画帧即天然变化源）。
_PANEL_TICK = 0.25


@dataclass
class Client:
    """一个已 attach 的 WebSocket 客户端。

    ``queue`` 持有发给该客户端的待发事件；``send_task`` 是唯一发送者——
    任何广播（含 attach 快照）都入队、由它串行 ``send_json``，帧永不撕裂。
    """

    ws: web.WebSocketResponse
    queue: "asyncio.Queue[dict]" = field(default_factory=asyncio.Queue)
    send_task: Optional[asyncio.Task] = None


def _panels_sig(panels: list[dict]) -> tuple:
    """面板快照指纹（变化才广播的比较键）。"""
    return tuple((p["name"], tuple(p["lines"])) for p in panels)


class ServeSession:
    """长存会话宿主：agent + 客户端 + 串行回合 + 权限桥。"""

    def __init__(
        self,
        agent: Any,
        console: Any = None,
        bridge: Optional[WebPermissionBridge] = None,
    ) -> None:
        self.agent = agent            # 鸭子类型：stream_run / history / config / ...
        self.console = console
        self._clients: dict[int, Client] = {}
        self._queue: "asyncio.Queue[str]" = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._turn_task: Optional[asyncio.Task] = None
        # 回合中的 live 快照：attach 重放给迟到客户端（history 只在回合末更新）
        self._live_user: Optional[str] = None
        self._live_events: list[dict] = []
        # 权限桥：ServeConsole.ask_permission 经 console.bridge 委托至此
        self.bridge = bridge if bridge is not None else WebPermissionBridge(self)
        if console is not None:
            console.bridge = self.bridge
        # 插件 UI 面板（ui/v1）常驻广播：有客户端才跑；_panel_sig 是上帧
        # 指纹（变化才广播，attach 快照与 ticker 共用）
        self._panel_task: Optional[asyncio.Task] = None
        self._panel_sig: Optional[tuple] = None

    # ── 生命周期 ─────────────────────────────────────────────────

    def start(self) -> None:
        """启动回合 worker（幂等）。"""
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.ensure_future(self._worker())

    def stop(self) -> None:
        """停止：先打断当前回合，再停 worker 与面板 ticker。幂等。"""
        if self._turn_task is not None:
            self._turn_task.cancel()
        if self._worker_task is not None:
            self._worker_task.cancel()
        if self._panel_task is not None:
            self._panel_task.cancel()
            self._panel_task = None

    def has_clients(self) -> bool:
        """是否有已 attach 的客户端（权限桥据此判定 fail-closed）。"""
        return bool(self._clients)

    # ── WS 入口 ──────────────────────────────────────────────────

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """aiohttp /ws handler：读上行、分发意图；断开时 detach。"""
        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(request)
        client = self.attach(ws)
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    self._handle_uplink(msg.data)
                elif msg.type == web.WSMsgType.ERROR:
                    _log.warning("ws error: %s", ws.exception())
        finally:
            self.detach(client)
        return ws

    def _handle_uplink(self, data: str) -> None:
        """一行上行 JSON → 意图分发。畸形/未知只记日志，不断流。"""
        msg = protocol.parse_uplink(data)
        if isinstance(msg, protocol.PermissionResponse):
            self.bridge.on_response(msg.request_id, msg.allowed, msg.remember)
        elif isinstance(msg, protocol.UserMessage):
            self.submit(msg.text)
        elif isinstance(msg, protocol.Interrupt):
            self.interrupt()
        elif isinstance(msg, protocol.UplinkUnknown):
            _log.warning("unknown uplink: %r", msg.type)
        # None：畸形行，静默跳过（协议文档：调用方记日志，不断流）

    # ── attach / detach ──────────────────────────────────────────

    def attach(self, ws: web.WebSocketResponse) -> Client:
        """注册新客户端并排队其快照（init + history + live 缓冲）。

        快照**全部入队后**才注册进 ``_clients`` 并启动 downlink：期间广播
        不达此客户端，之后的广播都在快照之后（队列 FIFO，序天然正确）。
        """
        client = Client(ws=ws)
        self._enqueue(client, protocol.init_event(
            getattr(self.agent, "session_id", ""),
            getattr(getattr(self.agent, "config", None), "model", ""),
            sorted(getattr(self.agent, "tools", {}) or {}),
        ))
        self._enqueue(client, protocol.serve_history(self._history_messages()))
        if self._live_user is not None:
            self._enqueue(client, protocol.user_message(self._live_user))
        for ev in list(self._live_events):
            self._enqueue(client, ev)
        # 面板快照（ui/v1）：宠物等常驻面板 attach 即可见（不等下一拍）。
        # 空面板不入快照——端默认无面板，多余空事件只扰动既有事件序。
        panels = self._current_panels()
        self._panel_sig = _panels_sig(panels)
        if panels:
            self._enqueue(client, protocol.serve_panels(panels))
        self._clients[id(ws)] = client
        client.send_task = asyncio.ensure_future(self._downlink(client))
        self._ensure_panel_ticker()
        return client

    def detach(self, client: Client) -> None:
        """注销客户端、停掉其 downlink；无客户端剩余时 deny_all（断流律）。"""
        if id(client.ws) in self._clients:
            del self._clients[id(client.ws)]
        if client.send_task is not None:
            client.send_task.cancel()
        if not self._clients:
            self.bridge.deny_all()
            # 面板广播随客户端清零停止（空转无意义）；指纹复位，重连时
            # attach 快照重发全量面板
            if self._panel_task is not None:
                self._panel_task.cancel()
                self._panel_task = None
            self._panel_sig = None

    # ── 插件 UI 面板广播（ui/v1，web 常驻面板）────────────────────

    def _ensure_panel_ticker(self) -> None:
        """有客户端时启动面板 ticker（幂等；attach 处调用）。"""
        if self._panel_task is None or self._panel_task.done():
            self._panel_task = asyncio.ensure_future(self._panel_ticker())

    async def _panel_ticker(self) -> None:
        """常驻面板广播：每拍征集一次，变化才广播。

        面板不是回合产物（宠物空闲时也要动）——独立于 _worker 的通道；
        征集器的故障隔离（崩溃跳过/熔断/限额）保证坏面板不拖死广播，
        征集本身再包一层兜底（collector 异常 → 本拍空面板）。
        """
        while True:
            await asyncio.sleep(_PANEL_TICK)
            if not self._clients:
                break
            panels = self._current_panels()
            sig = _panels_sig(panels)
            if sig == self._panel_sig:
                continue  # 无变化不广播（省带宽）
            self._panel_sig = sig
            self.broadcast(protocol.serve_panels(panels))

    def _current_panels(self) -> list[dict]:
        """征集当前面板快照（行剥 rich 标签——与 text_delta 同款，端哑渲染）。"""
        collector = getattr(self.agent, "ui_panels", None)
        if collector is None:
            return []
        try:
            raw = collector.panels()
        except Exception:
            _log.exception("ui panel collection failed; broadcasting none")
            return []
        return [
            {
                "name": name,
                "lines": [_RICH_TAG.sub("", ln) for ln in lines],
            }
            for name, lines in raw
        ]

    # ── 广播 / 入队 ──────────────────────────────────────────────

    def broadcast(self, obj: dict) -> None:
        """向全部客户端广播（只入队，不发送——发送由各自 downlink 独占）。"""
        for client in list(self._clients.values()):
            self._enqueue(client, obj)

    def _enqueue(self, client: Client, obj: dict) -> None:
        client.queue.put_nowait(obj)

    async def _downlink(self, client: Client) -> None:
        """客户端专属发送任务：唯一持有 ws.send_json 的协程。"""
        try:
            while True:
                obj = await client.queue.get()
                await client.ws.send_json(obj)
        except asyncio.CancelledError:
            pass
        except Exception:
            _log.exception("ws downlink failed; client dropped")

    # ── 回合驱动 ─────────────────────────────────────────────────

    def submit(self, text: str) -> None:
        """用户消息入队（任一客户端可发；回合串行消费）。"""
        if text and text.strip():
            self._queue.put_nowait(text)

    def interrupt(self) -> None:
        """打断当前回合（Web 的 Esc）：cancel _turn_task。"""
        task = self._turn_task
        if task is not None and not task.done():
            task.cancel()

    async def _worker(self) -> None:
        """串行回合循环：每条消息 await 一个 _run_turn 子任务。"""
        while True:
            text = await self._queue.get()
            self._turn_task = asyncio.ensure_future(self._run_turn(text))
            try:
                await self._turn_task
            finally:
                self._turn_task = None

    async def _run_turn(self, text: str) -> None:
        """跑一轮：stream_run 事件投影广播 + 终局 result / interrupted。"""
        started = time.monotonic()
        self._live_user = text
        self._live_events = []
        self.broadcast(protocol.user_message(text))
        try:
            async for ev in self.agent.stream_run(text):
                projected = self._project(ev)
                if projected is None:
                    continue
                self._live_events.append(projected)
                self.broadcast(projected)
            self.broadcast(self._result_event(started))
        except asyncio.CancelledError:
            # 客户端 interrupt：广播并正常返回，不毒死 worker
            self.broadcast({"type": "interrupted"})
            _log.info("turn interrupted by client")
        except Exception as e:
            _log.exception("turn failed")
            self.broadcast(self._result_event(started, error=f"{type(e).__name__}: {e}"))
        finally:
            self._live_user = None
            self._live_events = []

    def _result_event(self, started: float, error: str = "") -> dict:
        """终局事件：镜像 single_shot 的 result 形状（同 schema）。"""
        usage = {
            "input_tokens": getattr(self.agent, "total_input_tokens", 0),
            "output_tokens": getattr(self.agent, "total_output_tokens", 0),
        }
        return protocol.result_event(
            None if error else "",
            bool(error),
            int((time.monotonic() - started) * 1000),
            getattr(self.agent, "last_tool_rounds", 0),
            getattr(self.agent, "session_id", ""),
            usage,
            error=error,
        )

    # ── 事件投影 ─────────────────────────────────────────────────

    def _project(self, ev: Any) -> Optional[dict]:
        """stream_run 事件 → 协议下行事件；不可展示者返回 None。

        文本 token 剥 rich 标签（压缩提示等）；tool 事件截断输出上限。
        """
        from ...agent import ToolResultEvent, ToolStartEvent
        from ...llm import StreamReasoning

        if isinstance(ev, ToolStartEvent):
            return protocol.tool_use(ev.name)
        if isinstance(ev, ToolResultEvent):
            return protocol.tool_result(
                ev.name, ev.is_error, ev.output[:_STREAM_TOOL_OUTPUT_LIMIT]
            )
        if isinstance(ev, StreamReasoning):
            return protocol.thinking_delta(ev.text)
        if isinstance(ev, str):
            text = _RICH_TAG.sub("", ev)
            if text:
                return protocol.text_delta(text)
        return None

    def _history_messages(self) -> list:
        """attach 快照的历史消息（agent.history.messages，可能为空）。"""
        history = getattr(self.agent, "history", None)
        messages = getattr(history, "messages", None)
        return list(messages) if messages else []


if __name__ == "__main__":
    import asyncio

    from ...agent import ToolResultEvent, ToolStartEvent

    # 自检：投影 + attach 快照 + 串行回合 + interrupt（假 agent + 假 WS）
    class _FakeWS:
        def __init__(self):
            self.sent: list = []

        async def send_json(self, obj):
            self.sent.append(obj)

    class _FakeHistory:
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello!"},
        ]

    class _FakeAgent:
        session_id = "sess"
        history = _FakeHistory()
        tools = {"read_file": 1, "write_file": 1}
        last_tool_rounds = 2
        total_input_tokens = 10
        total_output_tokens = 4
        slow = False

        class _Cfg:
            model = "fake-model"

        config = _Cfg()

        async def stream_run(self, text):
            if self.slow:
                yield "Hel"
                await asyncio.sleep(0.3)  # 回合未完成，供 live-attach 测试
            yield "lo"
            yield ToolStartEvent(name="read_file", arguments='{"path": "x"}')
            yield ToolResultEvent(name="read_file", output="contents", is_error=False)
            yield "\n\n[dim]● Compacting conversation…[/dim]\n"

    async def _check() -> None:
        agent = _FakeAgent()
        session = ServeSession(agent)
        session.start()
        # attach：init + history + 无 live 缓冲
        ws = _FakeWS()
        session.attach(ws)
        await asyncio.sleep(0)  # 让 downlink 任务跑一帧
        kinds = [e["type"] for e in ws.sent]
        assert kinds == ["system", "history"], kinds  # init 是 system 子类
        assert ws.sent[0]["subtype"] == "init" and ws.sent[0]["model"] == "fake-model"
        assert len(ws.sent[1]["messages"]) == 2

        # 回合：user_message → text_delta → tool_use/tool_result → result
        ws.sent.clear()
        session.submit("do it")
        await asyncio.sleep(0.05)
        types = [e["type"] for e in ws.sent]
        assert types[0] == "user_message", types
        assert "text_delta" in types and "tool_use" in types
        # [dim] 标签被剥：合并 text_delta 后不含 '[dim]'
        all_text = "".join(e.get("text", "") for e in ws.sent if e["type"] == "text_delta")
        assert "[dim]" not in all_text and "● Compacting" in all_text
        assert types[-1] == "result" and ws.sent[-1]["subtype"] == "success"

        # 回合进行中 attach：慢 agent 先 yield 一个 token 即挂起，此时
        # _live_user/_live_events 未清——迟到客户端应看到 live 快照
        agent.slow = True
        ws.sent.clear()
        session.submit("slow turn")
        await asyncio.sleep(0.05)  # 首个 token 已广播，回合仍挂起
        ws2 = _FakeWS()
        session.attach(ws2)
        await asyncio.sleep(0)
        sent2 = [e["type"] for e in ws2.sent]
        assert "user_message" in sent2 and "text_delta" in sent2, sent2
        await asyncio.sleep(0.35)  # 回合跑完
        agent.slow = False

        # interrupt：回合被打断广播 interrupted，worker 仍活
        ws.sent.clear()
        agent.slow = True
        session.submit("interrupt me")
        await asyncio.sleep(0.05)
        session.interrupt()
        await asyncio.sleep(0.05)
        assert "interrupted" in [e["type"] for e in ws.sent]
        agent.slow = False
        session.submit("after interrupt")
        await asyncio.sleep(0.05)
        assert [e["type"] for e in ws.sent][-1] == "result"  # worker 未毒死
        session.stop()

    asyncio.run(_check())

    # 投影剥离测试（纯函数，无需事件循环）
    session = ServeSession(_FakeAgent())
    assert session._project("plain") == {"type": "text_delta", "text": "plain"}
    assert session._project("[dim][/dim]") is None            # 剥净 → 不广播
    assert session._project("[dim]x[/dim]") == {"type": "text_delta", "text": "x"}
    assert session._project("a[red]b[/red]c")["text"] == "abc"
    assert session._project(123) is None                     # 不可投影
    print("openx/app/serve/session.py OK ✓")
