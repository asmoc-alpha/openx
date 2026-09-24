"""④ 轨迹跟踪 · **全局账本**（K5，kernel 详设 §3.2）——跨会话决策留痕。

会话账本（``~/.openx/sessions/*.jsonl``）记单会话的转录/控制/容灾/组合事件；
**决策事件族**（``protocol.DECISION_EVENTS``：晋升 / 回滚 / 退场 / 棘轮收紧）
是**跨会话事实**，塞进任一会话都是错误归属——"这个插件哪来的、为什么它
没了"的答案只该有一个权威所在地。故：

- 全文落本模块持有的 ``~/.openx/ledger.jsonl``（append-only，复用
  ``Ledger`` 的 seq/digest 哈希链）；
- 会话账本只留一条 ``decision_ref`` 引用（``(ledger, seq)``，不复制内容）——
  回放单会话时按需展开。

本模块**只做落盘与读取**（信封行的 append / scan / read / verify），seq 与
哈希链的分配仍由 ``Ledger`` 负责（与 ``SessionStore`` 对会话账本的分工同款）。
``GLOBAL_LEDGER_PATH`` 是模块级常量，测试 monkeypatch 它以隔离真实 ``~/.openx``
（镜像 ``sessions.SESSIONS_DIR`` / ``config.SETTINGS_PATH`` 模式）。
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
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Optional

from .ledger import verify_chain
from .protocol import Event

_log = logging.getLogger("openx.kernel")

#: 全局账本落点；测试 monkeypatch 本模块属性以隔离真实用户数据。
GLOBAL_LEDGER_PATH = Path.home() / ".openx" / "ledger.jsonl"


def retired_scaffolds(events: Iterable[Event]) -> dict[str, dict[str, Any]]:
    """折叠退场决策 → ``{scaffold_id: 退场条目 payload}``（E4）。

    按事件序（seq 序）消费：``scaffold_retired`` 记一名，``scaffold_restored``
    除一名（回挂）。**账本即单一真源**——退场集合从决策历史推导，不另设配置表，
    故"摘除不是删除、可恢复"由数据结构本身保证（回挂 = 后续一条 restored）。
    返回的 payload 带 ``compensates`` / ``exit_when`` / ``eval_set`` 与评测
    ``evidence``，供读面（/plugins · plugin_help）在插件**不被导入**时仍能
    展示其演进声明。
    """
    out: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.type == "scaffold_retired":
            plugin = str(event.payload.get("plugin") or "")
            if plugin:
                out[plugin] = dict(event.payload)
        elif event.type == "scaffold_restored":
            out.pop(str(event.payload.get("plugin") or ""), None)
    return out


def _event_from_line(line: dict[str, Any]) -> Optional[Event]:
    """信封行 -> ``Event``（字段缺失/畸形返回 None，供读取侧跳过）。"""
    try:
        return Event(
            seq=int(line["seq"]),
            ts=float(line.get("ts", 0.0)),
            session=str(line.get("session", "")),
            type=str(line["type"]),
            payload=dict(line.get("payload") or {}),
            cause=line.get("cause"),
            origin=str(line.get("origin", "kernel")),
            digest=str(line.get("digest", "")),
        )
    except (KeyError, TypeError, ValueError):
        return None


class GlobalLedgerStore:
    """全局账本的文件面：append（sink）/ scan（续接起点）/ read / verify。

    ``path`` 缺省取**调用期**的模块常量 ``GLOBAL_LEDGER_PATH``——这样测试
    monkeypatch 常量即可重定向（与 ``SessionStore`` 读 ``SESSIONS_DIR`` 同理）。
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else Path(GLOBAL_LEDGER_PATH)

    # ── 记账 sink：Ledger 的 append 出口 ──────────────────────────

    def append(self, event: Event) -> None:
        """追加一条信封行（append-only）。失败抛 ``OSError`` 由 ``Ledger`` 兜底。

        ``Ledger.emit`` 把 sink 调用包在 try/except 里（sink 故障不炸内核，
        降级丢弃并记日志），故本方法不自行吞异常——把"写失败"暴露给唯一
        的兜底点，避免两处各吞一半。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_line(), ensure_ascii=False, default=str) + "\n"
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line)

    # ── 续接：进程（或重启）后从既有末条续 seq 与哈希链 ───────────

    def scan(self) -> tuple[int, str]:
        """扫全部信封行，返回 ``(条目数, 末条 digest)``。

        对应 ``Ledger.attach(start_seq=…, start_digest=…)``：只续 seq 不续
        链，会在恢复后的第一条事件上断链——恰是最需要链证明"历史没被改过"
        的时刻。读不到文件 / 损坏行跳过（与 ``SessionStore`` 同纪律）。
        """
        count = 0
        tail = ""
        for line in self._read_lines():
            if "seq" in line and "digest" in line:
                count += 1
                digest = line.get("digest")
                if isinstance(digest, str):
                    tail = digest
        return count, tail

    def read_all(self) -> list[Event]:
        """读全部信封行 -> ``Event`` 列表（坏行跳过），供校验与展示。"""
        events: list[Event] = []
        for line in self._read_lines():
            if "seq" in line and "payload" in line:
                event = _event_from_line(line)
                if event is not None:
                    events.append(event)
        return events

    def verify(self) -> list[int]:
        """§3.4 校验工具：复算哈希链，返回断裂处的 seq（空 = 完好）。

        "断裂本身记账"（§3.4 后半）暂不做——写回同一账本会形成自指回环；
        先只做"可发现"，留到有真实消费方时再加。
        """
        return verify_chain(self.read_all())

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """最近若干条决策（供 ``/ledger`` 展示）：payload 投影 + 归因字段。"""
        events = self.read_all()
        out: list[dict[str, Any]] = []
        for event in events[-limit:] if limit > 0 else events:
            item = dict(event.payload)
            item.setdefault("seq", event.seq)
            if event.ts:
                item.setdefault("ts", event.ts)
            item.setdefault("origin", event.origin)
            out.append(item)
        return out

    # ── 内部 ─────────────────────────────────────────────────────

    def _read_lines(self) -> list[dict[str, Any]]:
        try:
            raw_text = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for lineno, raw in enumerate(raw_text.splitlines(), 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                line = json.loads(raw)
            except json.JSONDecodeError:
                _log.warning("skipping corrupt ledger line %s:%d", self.path.name, lineno)
                continue
            if isinstance(line, dict):
                out.append(line)
        return out


if __name__ == "__main__":
    import tempfile

    # 自检全程临时目录，绝不触碰真实 ~/.openx/ledger.jsonl
    with tempfile.TemporaryDirectory() as _td:
        path = Path(_td) / "ledger.jsonl"
        store = GlobalLedgerStore(path)
        assert store.scan() == (0, "") and store.read_all() == []

        # 复用 Ledger 分配 seq/哈希链，store 只做落盘
        from .ledger import Ledger
        ledger = Ledger()
        count, tail = store.scan()
        ledger.attach(store.append, session="", start_seq=count, start_digest=tail)
        e1 = ledger.emit("plugin_promoted", {"type": "plugin_promoted", "plugin": "auto-x"})
        e2 = ledger.emit("plugin_rolled_back", {"type": "plugin_rolled_back", "plugin": "auto-x"})
        assert (e1.seq, e2.seq) == (1, 2) and path.is_file()

        # 读取侧：Event 往返 + 链完好
        events = store.read_all()
        assert [e.type for e in events] == ["plugin_promoted", "plugin_rolled_back"]
        assert events[1].payload["plugin"] == "auto-x"
        assert store.verify() == []

        # 跨"进程"续接：新 store 重扫 -> seq 续起、链不断
        count2, tail2 = store.scan()
        assert count2 == 2 and tail2 == e2.digest
        ledger2 = Ledger()
        ledger2.attach(store.append, session="", start_seq=count2, start_digest=tail2)
        e3 = ledger2.emit("ratchet_tightened", {"type": "ratchet_tightened"})
        assert e3.seq == 3 and store.verify() == []

        # 篡改中段 -> verify 报断链
        lines = path.read_text(encoding="utf-8").splitlines()
        tampered = json.loads(lines[0])
        tampered["payload"]["plugin"] = "evil"
        lines[0] = json.dumps(tampered, ensure_ascii=False)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert store.verify() == [1]

        # recent：payload 投影 + seq/归因
        assert [r["type"] for r in store.recent()] == [
            "plugin_promoted", "plugin_rolled_back", "ratchet_tightened",
        ]
        assert store.recent(1)[0]["type"] == "ratchet_tightened"

        # 退场折叠（E4）：retired -> restored -> retired 的历史收敛为"在册"一名
        from .protocol import Event as _Ev
        _mk = lambda seq, t, p: _Ev(  # noqa: E731
            seq=seq, ts=0.0, session="", type=t, payload={"type": t, **p}, digest=""
        )
        _fold = retired_scaffolds([
            _mk(1, "scaffold_retired", {"plugin": "histcompact",
                                        "compensates": "上下文有限"}),
            _mk(2, "scaffold_retired", {"plugin": "router"}),
            _mk(3, "scaffold_restored", {"plugin": "histcompact"}),
            _mk(4, "scaffold_retired", {"plugin": "histcompact",
                                        "compensates": "上下文有限"}),
        ])
        assert set(_fold) == {"histcompact", "router"}
        assert _fold["histcompact"]["compensates"] == "上下文有限"
        assert retired_scaffolds([]) == {}

        # 坏行跳过，不抛
        with path.open("a", encoding="utf-8") as f:
            f.write("not json\n")
        assert len(store.read_all()) == 3

    print("openx/kernel/global_ledger.py OK ✓")
