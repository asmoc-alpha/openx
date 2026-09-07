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
  实时与迟加入渲染一致。执行计划（``todos``）与子 agent（``fleet``）
  是状态型数据，attach 时非空即补发快照。
- **执行计划 / 子 agent**：``todo_write`` 收尾触发计划全量广播；子代理
  运行态由回合级 ticker 轮询 ``FleetMonitor`` 快照、变化才广播（父
  agent 等工具时不 yield 事件，事件驱动看不到回合中段的子代理活动）。
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

from ...kernel import protocol
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

# 子 agent 广播节拍（秒）：task 工具委派的子代理在回合内持续活动，但父
# agent 的 stream_run 在等工具期间不再 yield 事件——只能靠回合级 ticker
# 轮询 FleetMonitor 快照，变化才广播（同面板通道的纪律）。
_FLEET_TICK = 0.25


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
        # 子 agent（fleet）回合级广播：_run_turn 起、回合收尾停；_fleet_sig
        # 是上帧指纹（变化才广播）
        self._fleet_task: Optional[asyncio.Task] = None
        self._fleet_sig: Optional[tuple] = None

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
        if self._fleet_task is not None:
            self._fleet_task.cancel()
            self._fleet_task = None

    def has_clients(self) -> bool:
        """是否有已 attach 的客户端（权限桥据此判定 fail-closed）。"""
        return bool(self._clients)

    def is_busy(self) -> bool:
        """当前是否有未完成/待消费的回合（切工作区前必须为空）。

        只查 ``_turn_task`` 不够：消息可能正等在 ``_queue`` 里（worker 挂在
        ``await self._queue.get()``），切换后该条提示会在**新工作区**执行。
        故「回合进行中 或 队列非空」都算忙。
        """
        task = self._turn_task
        return (task is not None and not task.done()) or not self._queue.empty()

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
        elif isinstance(msg, protocol.AskUserResponse):
            self.bridge.on_ask_user_response(msg.request_id, msg.answers)
        elif isinstance(msg, protocol.PlanResponse):
            self.bridge.on_plan_response(msg.request_id, msg.approved)
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
        # 执行计划 / 子 agent 快照：状态型数据（非增量事件），attach 即补发，
        # 迟到客户端的任务面板与实时一致。空快照不发——端默认即空态。
        todos = self._todos_snapshot()
        if todos:
            self._enqueue(client, protocol.serve_todos(todos))
        fleet = self._fleet_snapshot()
        if fleet:
            self._enqueue(client, protocol.serve_fleet(fleet))
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

    # ── 执行计划 / 子 agent 广播（右栏任务面板数据源）──────────────

    def _todos_snapshot(self) -> list[dict]:
        """agent.todos → 下行快照（字段清洗；无 todos 面 → 空表）。

        与 CLI 同源：直接读 agent 持有的共享列表（todo_write 原地替换它）。
        快照是状态而非事件——attach 补发与 todo_write 结果触发共用本函数。
        """
        todos = getattr(self.agent, "todos", None)
        if not isinstance(todos, list):
            return []
        return [
            {
                "content": str(t.get("content", "")),
                "activeForm": str(t.get("activeForm", t.get("content", ""))),
                "status": str(t.get("status", "pending")),
            }
            for t in todos
            if isinstance(t, dict)
        ]

    def _broadcast_todos(self) -> None:
        """todo_write 收尾后广播当前执行计划（全量替换语义）。"""
        self.broadcast(protocol.serve_todos(self._todos_snapshot()))

    def _fleet_snapshot(self) -> list[dict]:
        """FleetMonitor 快照 → 下行投影（只带状态与活跃度，不带行缓冲）。"""
        fleet = getattr(self.agent, "fleet", None)
        if fleet is None:
            return []
        try:
            views = fleet.snapshot()
        except Exception:
            _log.exception("fleet snapshot failed; broadcasting none")
            return []
        return [
            {
                "id": v.get("id"),
                "label": str(v.get("label") or ""),
                "subagent_type": str(v.get("subagent_type") or ""),
                "status": str(v.get("status") or "running"),
                "tools_count": int(v.get("tools_count") or 0),
                "elapsed": int(v.get("elapsed") or 0),
            }
            for v in views
        ]

    def _broadcast_fleet(self, force: bool = False) -> None:
        """fleet 快照变化才广播（指纹比对）；force 用于回合收尾定格终态。"""
        snap = self._fleet_snapshot()
        sig = tuple(
            (a["id"], a["status"], a["tools_count"], a["elapsed"]) for a in snap
        )
        if not force and sig == self._fleet_sig:
            return
        self._fleet_sig = sig
        self.broadcast(protocol.serve_fleet(snap))

    async def _fleet_ticker(self) -> None:
        """回合内轮询 fleet 快照（父 agent 等工具时不 yield 事件，只能靠拍）。"""
        while True:
            await asyncio.sleep(_FLEET_TICK)
            self._broadcast_fleet()

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
        from ...agent import ToolResultEvent

        started = time.monotonic()
        self._live_user = text
        self._live_events = []
        self.broadcast(protocol.user_message(text))
        # 子 agent 视图按回合隔离（镜像 CLI StreamingService.start 的
        # fleet.reset()）：上轮委派的子代理不该挂在本轮任务流里
        fleet = getattr(self.agent, "fleet", None)
        if fleet is not None:
            try:
                fleet.reset()
            except Exception:
                pass  # 视图重置失败不阻断回合
        # 指纹初始化为「空快照」：本轮无子代理时 ticker 一条都不发
        self._fleet_sig = ()
        self._fleet_task = asyncio.ensure_future(self._fleet_ticker())
        try:
            async for ev in self.agent.stream_run(text):
                # 产物：写类工具入参 → artifact 增量广播（右侧面板实时增长）
                for tool, path in self._artifacts_of(ev):
                    art = protocol.artifact(path, tool)
                    self._live_events.append(art)
                    self.broadcast(art)
                # 执行计划：todo_write 收尾即广播全量快照（任务是状态不是增量）
                if isinstance(ev, ToolResultEvent) and ev.name == "todo_write":
                    self._broadcast_todos()
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
            if self._fleet_task is not None:
                self._fleet_task.cancel()
                self._fleet_task = None
            # 定格子 agent 终态（最后一拍可能错过 done/error 翻转）；
            # 本轮从未出现过子代理则不发（空事件只是噪声）
            if fleet is not None and (self._fleet_sig or self._fleet_snapshot()):
                self._broadcast_fleet(force=True)
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
            # 展示字段（摘要 / 目标路径）由入参派生后同行下发——端不再只
            # 看到一个光秃秃的工具名，任务流与上下文面板才有真实内容。
            from .api import tool_display

            summary, target = tool_display(ev.name, ev.arguments)
            return protocol.tool_use(ev.name, summary, target)
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

    def _artifacts_of(self, ev: Any) -> list[tuple[str, str]]:
        """写类工具事件 → ``[(tool, path), ...]``；非写工具返回 ``[]``。

        产物的实时源。与复盘端点（``api.artifacts_get``）共用提取函数，
        保证"进行中看到的"与"回头复盘看到的"是同一口径——两处都从工具
        入参派生，内核本身没有 artifact 概念。
        """
        from ...agent import ToolStartEvent
        from .api import _paths_from_tool_calls

        if not isinstance(ev, ToolStartEvent):
            return []
        name = str(getattr(ev, "name", "") or "")
        args = getattr(ev, "arguments", "")
        return [(name, p) for p in _paths_from_tool_calls(name, args)]

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
