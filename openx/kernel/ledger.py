"""④ 轨迹跟踪 / 记账（microkernel-design §0 五件套）——事件账本的唯一出口。

``Ledger`` 持有挂接的账本 sink、会话与 seq/digest 哈希链；append-only
（内核无 update/delete API）。**协议 = 账本的外化**：``kernel/protocol.py``
的 Event 信封去掉簿记字段即下行事件；回放 = 把存储的事件再发一遍。

从 PluginKernel 析出（2026-08-31 kernel 分包）：记账职责显式化为独立模块，
facade 委托本类，公共 API（``kernel.emit`` / ``attach_ledger``）不变。
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

import logging
import time
from typing import Any, Callable, Optional

from .protocol import Event, digest_of

_log = logging.getLogger("openx.kernel")


class Ledger:
    """事件账本：唯一事件出口，seq/digest 哈希链，append-only。

    挂接 sink（``SessionStore.append_event``）即落盘；未挂接时仅内存计数。
    记账先于动作、决策留痕覆盖自身的纪律由消费方（PluginKernel）把关——
    本类只保证：信封形状、seq/digest 连续性、sink 故障不炸内核。
    """

    def __init__(self) -> None:
        self._sink: Optional[Callable[[Event], None]] = None
        self._session: str = ""
        self._seq: int = 0
        self._prev_digest: str = ""

    def attach(
        self,
        sink: Callable[[Event], None],
        session: str = "",
        start_seq: int = 0,
    ) -> None:
        """挂接账本出口：seq 从 start_seq 续起（恢复会话不重号）。"""
        self._sink = sink
        self._session = session
        self._seq = start_seq
        self._prev_digest = ""

    def emit(
        self,
        type_: str,
        payload: dict[str, Any],
        cause: Optional[int] = None,
        origin: str = "kernel",
    ) -> Event:
        """分配 seq/ts/digest，append-only 投递到 sink。

        sink 故障不炸内核（记日志降级丢弃）——账本是证据系统，不该成为
        单点；未挂接时事件仅在内存计数。
        """
        event = Event(
            seq=self._seq + 1,
            ts=time.time(),
            session=self._session,
            type=type_,
            payload=payload,
            cause=cause,
            origin=origin,
        )
        event.digest = digest_of(self._prev_digest, event)
        self._seq = event.seq
        self._prev_digest = event.digest
        if self._sink is not None:
            try:
                self._sink(event)
            except Exception:
                _log.exception("ledger sink failed; event %r dropped", type_)
        return event


if __name__ == "__main__":
    # 自检：seq 单调 + digest 链连续 + sink 挂接 + 故障隔离
    events: list[Event] = []

    class _Sink:
        def __call__(self, event: Event) -> None:
            events.append(event)

    ledger = Ledger()
    ledger.attach(_Sink(), session="s1")
    e1 = ledger.emit("a", {"type": "a"})
    e2 = ledger.emit("b", {"type": "b"}, cause=e1.seq, origin="user")
    assert e1.seq == 1 and e2.seq == 2 and e2.cause == e1.seq
    assert e1.digest and e2.digest and e1.digest != e2.digest
    # 哈希链：digest(prev || canonical)
    from .protocol import canonical_event
    assert e2.digest == digest_of(e1.digest, e2)
    assert [e.type for e in events] == ["a", "b"]

    # sink 故障 → 不炸（降级丢弃）
    class _Boom:
        def __call__(self, event: Event) -> None:
            raise RuntimeError("sink down")
    ledger2 = Ledger()
    ledger2.attach(_Boom())
    ledger2.emit("c", {"type": "c"})  # 不抛
    print("openx/kernel/ledger.py OK ✓")
