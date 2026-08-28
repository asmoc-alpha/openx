"""Web 端权限桥与无 TUI ServeConsole（openx serve，P4）。

WebPermissionBridge
====================
把 ``console.ask_permission`` 的下行广播到全部 WS 客户端，等待任一客户端
的 ``permission_response``，遵守内核详设 §2.6 fail-closed 三律：
**EOF/断流=拒绝、超时=拒绝、未匹配 request_id=拒绝**（不猜、不默认、
不重放旧批准）。headless 的 ``_NdjsonPermissionBridge``（single_shot.py）
先例只换载体：stdin NDJSON → aiohttp WebSocket。

ServeConsole
==============
agent / tool_executor 可用的**无终端** stub console（rich TUI 必须不写
终端）。两条硬纪律：

1. 显式定义的属性（``_streaming_service=None`` 等）是契约——executor 的
   ``_prompt_user`` 用 ``getattr(console, "_streaming_service", None)`` 判断
   是否走 Live 内嵌面板，若 ``__getattr__`` 凭空造出一个 callable 会让
   ``svc.is_live_active()`` 炸掉；
2. ``confirm_plan`` / ``ask_user_question`` 是工具在 async execute **内部
   同步调用**的弹窗——web 无法在同一同步调用里 await 客户端应答（会死锁），
   MVP 采用**广播通知 + fail-closed 默认**（拒绝 / 保守默认），异步化改造
   列 P4.1 跟进。
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
import io
import logging
import uuid
from typing import Any, Optional

from rich.console import Console as RichConsole

from ...core import protocol

_log = logging.getLogger("openx.serve")


class WebPermissionBridge:
    """远程权限桥：广播 permission_request、按 request_id 等待应答。

    ``session`` 为鸭子类型，需提供 ``broadcast(obj: dict)``（入队即可，本桥
    不做发送）与 ``has_clients() -> bool``。
    """

    def __init__(self, session: Any, timeout: float = 300.0) -> None:
        self._session = session
        self._timeout = timeout
        self._pending: dict[str, asyncio.Future] = {}

    # ── 权限 ─────────────────────────────────────────────────────

    async def ask_permission(
        self,
        tool_name: str,
        reason: str,
        details: str = "",
        args_summary: str = "",
        can_remember: bool = True,
        diff: Optional[tuple] = None,
    ) -> tuple[bool, bool]:
        """广播一次权限请求并等待任一客户端的裁决。

        fail-closed：无客户端 → 立即 ``(False, False)``；超时 → ``(False,
        False)``；回合被打断（CancelledError）→ 向上传播，``finally`` 清理
        待决条目。返回 ``(approved, remember)``。
        """
        if not self._session.has_clients():
            _log.info("permission denied: no web client attached")
            return (False, False)
        request_id = uuid.uuid4().hex
        self._session.broadcast(protocol.permission_request(
            request_id,
            tool_name,
            reason,
            args_summary or details,
            can_remember=can_remember,
        ))
        fut = asyncio.get_event_loop().create_future()
        self._pending[request_id] = fut
        try:
            allowed, remember = await asyncio.wait_for(fut, self._timeout)
            return (bool(allowed), bool(remember))
        except asyncio.TimeoutError:
            _log.info("permission %s timed out after %.0fs; denied",
                      request_id, self._timeout)
            return (False, False)
        finally:
            self._pending.pop(request_id, None)

    def on_response(
        self, request_id: str, allowed: bool, remember: bool = False
    ) -> None:
        """收到客户端 permission_response：匹配到待决 future 才落裁决。

        未匹配 / 已决的 request_id 一律忽略（不猜、不默认、不重放）。
        """
        fut = self._pending.get(request_id)
        if fut is None or fut.done():
            return
        fut.set_result((bool(allowed), bool(remember)))

    def deny_all(self) -> None:
        """客户端全部断开：所有待决裁决按拒绝处理（断流律）。

        WS 收尾路径调用；已返回的裁决不受影响。
        """
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_result((False, False))

    # ── 同步弹窗的 fail-closed 通知（P4.1 前不等待应答）─────────────

    def notify_plan_request(self) -> None:
        """plan 审批请求：广播通知；ServeConsole 侧按拒绝处理（MVP）。"""
        self._session.broadcast({
            "type": "plan_request",
            "plan": "",
            "note": "Plan approval is not interactive on the web yet "
                    "(P4.1); treated as rejected.",
        })

    def notify_ask_user(
        self, question: str, options: list
    ) -> None:
        """ask_user 提问：广播通知供展示（MVP 不等待应答）。"""
        labels = [
            (o.get("label") if isinstance(o, dict) else str(o))
            for o in options or []
        ]
        self._session.broadcast({
            "type": "ask_user",
            "question": question,
            "options": labels,
            "note": "Interactive answers are not supported on the web yet "
                    "(P4.1); a conservative default was used.",
        })


class ServeConsole:
    """agent / tool_executor 可用的无终端 stub console。

    显式属性（含 ``_streaming_service=None``）是硬契约，见模块 docstring。
    TUI 专属 ``print_*`` / ``show_*`` 方法经 ``__getattr__`` 落 no-op——
    任何探活路径都不会因方法缺失而炸。
    """

    def __init__(self, bridge: Optional[WebPermissionBridge] = None) -> None:
        self.bridge = bridge
        # rich Console 定向到内存缓冲区：工具经 console.raw.print 渲染的
        # Markdown/摘要不写终端（web 消费协议事件，不是终端转录）。
        self._console = RichConsole(file=io.StringIO(), highlight=False)
        self._mode = "manual"          # serve 默认手动：写入逐项经 web 批准
        # 弹窗钩子与流式服务引用：显式为 None——executor 的
        # `getattr(console, "_streaming_service", None)` 若经 __getattr__
        # 造出一个 callable 会让 `svc.is_live_active()` 炸掉。
        self.on_dialog_start = None
        self.on_dialog_end = None
        self._streaming_service = None
        self._input_queue: list[str] = []

    # ── 状态访问（agent 会读写 console.mode）─────────────────────

    @property
    def raw(self) -> RichConsole:
        """底层 Rich Console（写内存，不触终端）。"""
        return self._console

    @property
    def mode(self) -> str:
        """当前权限模式（manual / auto / plan）。"""
        return self._mode

    @mode.setter
    def mode(self, value: str) -> None:
        self._mode = value

    # ── 权限（executor 的 prompter 委托至此）──────────────────────

    async def ask_permission(
        self,
        tool_name: str,
        reason: str,
        details: str = "",
        args_summary: str = "",
        can_remember: bool = True,
        diff: Optional[tuple] = None,
    ) -> tuple[bool, bool]:
        """权限询问：委托 bridge；未接线时 fail-closed 拒绝。"""
        if self.bridge is not None:
            return await self.bridge.ask_permission(
                tool_name,
                reason,
                details,
                args_summary=args_summary,
                can_remember=can_remember,
                diff=diff,
            )
        _log.info("permission denied: serve bridge not wired")
        return (False, False)

    # ── 同步弹窗（MVP fail-closed，见模块 docstring）──────────────

    def confirm_plan(self, *args: Any, **kwargs: Any) -> bool:
        """plan 审批：广播通知 + 拒绝（模型据此修订计划后重交）。"""
        if self.bridge is not None:
            self.bridge.notify_plan_request()
        _log.info("plan approval via web is fail-closed (P4.1); rejecting")
        return False

    def ask_user_question(
        self,
        question: str,
        options: Optional[list] = None,
        multi_select: bool = False,
    ) -> Any:
        """提问弹窗：广播通知 + 保守默认（绝不选成高权限项）。

        mode 询问的选项 0 是 "Auto"——直接取 options[0] 会在无人应答时把
        模式切成 auto，违反 fail-closed。因此优先选语义为"保持现状/拒绝"
        的选项（mode 的安全默认就是留在 manual），否则取最后一项。
        """
        if self.bridge is not None:
            self.bridge.notify_ask_user(question, options or [])
        labels = [
            (o.get("label") if isinstance(o, dict) else str(o))
            for o in options or []
        ]
        _CONSERVATIVE = ("stay", "manual", "keep", "cancel", "deny",
                         "decline", "no")
        default = next(
            (label for label in labels
             if any(k in label.lower() for k in _CONSERVATIVE)),
            (labels[-1] if labels else ""),
        )
        return [default] if multi_select else default

    # ── TUI 专属方法兜底：no-op，绝不打扰 serve 日志 ──────────────

    def __getattr__(self, name: str):
        def _noop(*args: Any, **kwargs: Any) -> None:
            return None

        return _noop


if __name__ == "__main__":
    import asyncio

    # 自检：fail-closed 三律 + 应答唤醒（假 session，无真 WS）
    class _FakeSession:
        def __init__(self, clients: bool = True):
            self._clients = clients
            self.broadcasts: list[dict] = []

        def has_clients(self) -> bool:
            return self._clients

        def broadcast(self, obj: dict) -> None:
            self.broadcasts.append(obj)

    async def _self_check() -> None:
        # ① 无客户端 → 立即拒绝，不发广播
        s0 = _FakeSession(clients=False)
        b0 = WebPermissionBridge(s0, timeout=1.0)
        assert await b0.ask_permission("shell", "run") == (False, False)
        assert not s0.broadcasts

        # ② 有客户端 + 应答 → 放行并回传 remember
        s1 = _FakeSession()
        b1 = WebPermissionBridge(s1, timeout=1.0)
        async def _respond() -> None:
            await asyncio.sleep(0.01)
            req = s1.broadcasts[0]
            assert req["type"] == "permission_request" and req["request_id"]
            b1.on_response(req["request_id"], True, remember=True)
        t = asyncio.ensure_future(_respond())
        approved, remember = await b1.ask_permission("shell", "run")
        await t
        assert (approved, remember) == (True, True)

        # ③ 超时 → 拒绝（wait_for 超时）
        s2 = _FakeSession()
        b2 = WebPermissionBridge(s2, timeout=0.05)
        assert await b2.ask_permission("shell", "run") == (False, False)

        # ④ 未匹配 request_id → 忽略，仍超时拒绝
        s3 = _FakeSession()
        b3 = WebPermissionBridge(s3, timeout=0.05)
        b3.on_response("no-such-id", True)  # 不猜、不重放
        assert await b3.ask_permission("shell", "run") == (False, False)

        # ⑤ deny_all（断流律）：全部待决按拒绝
        s4 = _FakeSession()
        b4 = WebPermissionBridge(s4, timeout=5.0)
        async def _ask():
            return await b4.ask_permission("shell", "run")
        fut = asyncio.ensure_future(_ask())
        await asyncio.sleep(0.01)
        b4.deny_all()
        assert await fut == (False, False)

    asyncio.run(_self_check())

    # ServeConsole：显式契约 + fail-closed 默认 + no-op 兜底
    c = ServeConsole()
    assert c.mode == "manual"
    c.mode = "auto"
    assert c.mode == "auto"
    assert c._streaming_service is None and c.on_dialog_start is None
    assert c.ask_user_question("mode?", [
        {"label": "Auto"}, {"label": "Plan"},
        {"label": "Stay in manual"},
    ]) == "Stay in manual"          # 保守默认：绝不切成 Auto
    assert c.ask_user_question("q", [{"label": "A"}, {"label": "B"}],
                               multi_select=True) == ["B"]  # 无保守项 → 末项
    assert c.ask_user_question("q", []) == ""
    assert c.confirm_plan() is False
    assert c.print_warning("ignored") is None  # __getattr__ 兜底

    print("openx/app/serve/bridge.py OK ✓")
