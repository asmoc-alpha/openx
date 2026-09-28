"""资源闸（K4）—— 执行闸的"信任底线"面，由内核持有。

内核详设 §2.3：资源闸管的是**信任的底线**而非模型能力——停止语义、轮次
上限、预算底线（token / 时间 / 费用）。强制机制是物理的，不靠 loop 自觉：
**执行闸在内核手里**——loop 实现无论是 harness loop 还是 model-native，每轮
循环必过资源闸检查，触顶 = ``resource_gate_tripped`` 事件 + 停止语义
（graceful interrupt，排队意图不丢）。

职责边界（与 ``Guard`` / ``kernel/recovery/`` 同款分工）：本模块只给**机制**
——"什么算触顶、触顶怎么记账"；"触顶之后怎么办"（落 checkpoint、收尾请求、
Stop 钩子）是消费方（``agent`` / ``services``）的策略。故本模块**不认识
agent、不进事件循环**。

K4 之前这段判定长在 agent 的 loop 不变量里（
``while state.tool_rounds < config.max_tool_rounds``）；析出的目的是为 loop
槽化（P2）清场——**换 loop 不换闸**：闸不走 loop，loop 每轮来看闸。

判定是纯函数式的：``check`` 无副作用，``trip`` 才记账（"记账先于动作"：
触顶先落一条事件，再交给消费方停止）。``register_check`` 是停止语义 /
预算底线的扩展位——新增一道底线 = 注册一个 ``used -> GateTrip | None``，
不改 loop（今日无注册者，行为 ≡ 现状）。
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

from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..protocol import resource_gate_tripped

# 轮次底线的闸名（事件 payload 与 checkpoint 的 ``gate`` 字段共用同一字符串：
# 触顶事件与旁挂快照必须能被对上，见 ``agent._on_gate_tripped``）。
GATE_MAX_TOOL_ROUNDS = "max_tool_rounds"

# 扩展闸的判定签名：已消耗量 -> 触顶（None = 未触顶）。
GateCheck = Callable[[int], Optional["GateTrip"]]


@dataclass(frozen=True)
class GateTrip:
    """一次触顶：哪道闸、底线多少、已消耗多少。"""

    gate: str
    limit: int
    used: int


class ResourceGate:
    """轮次 / 停止 / 预算底线的判定与记账（内核详设 §2.3）。

    ``emit`` 是内核的记账出口（``kernel.emit`` 的 bound method）；不注入时
    ``trip`` 只判定不记账（测试与嵌入用）。``max_tool_rounds`` 由消费方在
    每轮起点推入（``set_limits``）——闸不读 settings，配置来源住消费方。
    """

    def __init__(
        self,
        max_tool_rounds: int = 0,
        emit: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._max_tool_rounds = int(max_tool_rounds or 0)
        self._emit = emit
        self._extra: dict[str, GateCheck] = {}

    # ── 配置 ────────────────────────────────────────────────────

    @property
    def limit(self) -> int:
        """轮次底线（loop 的 ``while rounds < limit`` 读它）。"""
        return self._max_tool_rounds

    def set_limits(self, *, max_tool_rounds: Optional[int] = None) -> None:
        """推入本轮的底线（消费方每轮起点调用；缺省不动）。"""
        if max_tool_rounds is not None:
            self._max_tool_rounds = int(max_tool_rounds)

    def register_check(self, name: str, check: GateCheck) -> None:
        """注册一道扩展底线（停止语义 / 预算底线）。

        轮次之外的底线（token / 时间 / 费用的停止点）由此接入：loop 每轮
        ``check`` 时逐道问过去，任一触顶即停——**不需要改 loop**。同名覆盖。
        """
        self._extra[str(name)] = check

    def checks(self) -> tuple[str, ...]:
        """已注册的扩展闸名（注册序）。"""
        return tuple(self._extra)

    # ── 判定 / 记账 ─────────────────────────────────────────────

    def check(self, rounds: int) -> Optional[GateTrip]:
        """本轮是否触顶：**纯判定，无副作用**。

        先看轮次底线（``used >= limit``——与既有 loop 条件
        ``while rounds < max_tool_rounds`` 逐字等价，含 ``limit=0`` 这种
        "一轮也不给"的退化配置），再逐道问扩展闸。
        """
        used = int(rounds or 0)
        if used >= self._max_tool_rounds:
            return GateTrip(GATE_MAX_TOOL_ROUNDS, self._max_tool_rounds, used)
        for name, check in self._extra.items():
            trip = check(used)
            if trip is not None:
                return trip
        return None

    def trip(self, rounds: int, checkpoint_seq: int = 0) -> Optional[GateTrip]:
        """判定 + 记账：触顶则落一条 ``resource_gate_tripped``（返回该次触顶）。

        记账先于动作：事件先落账，消费方随后才停止/收尾。未触顶返回 None、
        不记账。事件 payload 由 ``protocol.resource_gate_tripped`` 生成——
        线格式的单一真源在协议模块，闸不另造形状。
        """
        found = self.check(rounds)
        if found is None:
            return None
        if self._emit is not None:
            self._emit(
                "resource_gate_tripped",
                resource_gate_tripped(
                    found.gate, found.limit, found.used,
                    checkpoint_seq=checkpoint_seq,
                ),
                origin="kernel",
            )
        return found


if __name__ == "__main__":
    # 独立自检：判定边界、记账形状、扩展闸、无 emit 不炸
    events: list[tuple[str, dict]] = []
    gate = ResourceGate(3, emit=lambda t, p, **kw: events.append((t, p)))

    # 边界：rounds < limit 不触顶；== limit 触顶（与 while rounds < limit 等价）
    assert gate.check(2) is None
    assert gate.check(3) == GateTrip(GATE_MAX_TOOL_ROUNDS, 3, 3)
    assert gate.check(9) == GateTrip(GATE_MAX_TOOL_ROUNDS, 3, 9)
    assert gate.limit == 3

    # trip 才记账：未触顶不落事件，触顶落一条、形状即协议 payload
    assert gate.trip(1) is None and events == []
    tripped = gate.trip(3, checkpoint_seq=7)
    assert tripped is not None and len(events) == 1
    assert events[0] == (
        "resource_gate_tripped",
        {"type": "resource_gate_tripped", "gate": "max_tool_rounds",
         "limit": 3, "rounds": 3, "checkpoint_seq": 7},
    )

    # 退化配置 limit=0：一轮也不给（既有 loop 条件同款）
    assert ResourceGate(0).check(0) is not None

    # 扩展闸：轮次未触顶时也拦得住，且闸名如实回报
    budget = ResourceGate(100, emit=lambda t, p, **kw: events.append((t, p)))
    budget.register_check(
        "max_tokens",
        lambda used: GateTrip("max_tokens", 10, used) if used >= 5 else None,
    )
    assert budget.checks() == ("max_tokens",)
    assert budget.check(4) is None
    assert budget.check(5) == GateTrip("max_tokens", 10, 5)
    assert budget.trip(5) is not None and len(events) == 2

    # 无 emit：只判定不记账，绝不抛
    bare = ResourceGate(1)
    assert bare.trip(1) == GateTrip(GATE_MAX_TOOL_ROUNDS, 1, 1)

    # set_limits：缺省不动（None），给了才改
    bare.set_limits()
    assert bare.limit == 1
    bare.set_limits(max_tool_rounds=12)
    assert bare.limit == 12

    print("openx/kernel/audit/resource_gate.py OK ✓")
