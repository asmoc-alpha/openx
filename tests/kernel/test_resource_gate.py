"""K4 资源闸测试：判定边界 / 记账形状 / 内核接线。

**loop 行为 ≡ 现状**由既有 ``tests/kernel/test_checkpoint_wiring.py`` 的
``test_gate_trip_emits_event_and_keeps_checkpoint`` 不改而动地证明（触顶事件
+ 旁挂快照），本文件只测闸本体与内核接线面。

运行：``python -m pytest tests/kernel/test_resource_gate.py -q``
"""

from __future__ import annotations

from openx.kernel import get_kernel
from openx.kernel.audit.resource_gate import (
    GATE_MAX_TOOL_ROUNDS,
    GateTrip,
    ResourceGate,
)


class TestGateJudgement:
    """纯判定：与既有 loop 条件 ``while rounds < limit`` 逐字等价。"""

    def test_below_limit_does_not_trip(self):
        gate = ResourceGate(3)
        assert gate.check(0) is None
        assert gate.check(2) is None

    def test_at_and_over_limit_trips(self):
        gate = ResourceGate(3)
        assert gate.check(3) == GateTrip(GATE_MAX_TOOL_ROUNDS, 3, 3)
        assert gate.check(9) == GateTrip(GATE_MAX_TOOL_ROUNDS, 3, 9)

    def test_degenerate_zero_limit_matches_old_loop(self):
        """limit=0：旧 loop 条件 ``0 < 0`` 为假（一轮也不给）——闸同款。"""
        assert ResourceGate(0).check(0) is not None

    def test_limit_property_is_readable(self):
        assert ResourceGate(30).limit == 30

    def test_set_limits_default_is_noop(self):
        gate = ResourceGate(7)
        gate.set_limits()
        assert gate.limit == 7
        gate.set_limits(max_tool_rounds=12)
        assert gate.limit == 12


class TestGateLedger:
    """记账先于动作：check 不落事件，trip 才落；形状即协议 payload。"""

    def test_check_never_emits(self):
        events: list = []
        gate = ResourceGate(1, emit=lambda t, p, **kw: events.append((t, p)))
        assert gate.check(9) is not None
        assert events == []

    def test_trip_emits_protocol_payload(self):
        events: list = []
        gate = ResourceGate(3, emit=lambda t, p, **kw: events.append((t, p)))
        assert gate.trip(1) is None and events == []
        tripped = gate.trip(3, checkpoint_seq=7)
        assert tripped == GateTrip(GATE_MAX_TOOL_ROUNDS, 3, 3)
        assert events == [(
            "resource_gate_tripped",
            {"type": "resource_gate_tripped", "gate": "max_tool_rounds",
             "limit": 3, "rounds": 3, "checkpoint_seq": 7},
        )]

    def test_trip_without_emit_only_judges(self):
        """无 emit 的嵌入用法：只判定不记账，绝不抛。"""
        assert ResourceGate(1).trip(1) == GateTrip(GATE_MAX_TOOL_ROUNDS, 1, 1)


class TestExtraGates:
    """扩展位：停止语义 / 预算底线 = 注册一道判定，不改 loop。"""

    def test_extra_check_trips_before_round_limit(self):
        gate = ResourceGate(100)
        gate.register_check(
            "max_tokens",
            lambda used: GateTrip("max_tokens", 10, used) if used >= 5 else None,
        )
        assert gate.checks() == ("max_tokens",)
        assert gate.check(4) is None
        assert gate.check(5) == GateTrip("max_tokens", 10, 5)

    def test_round_limit_wins_when_both_would_trip(self):
        """轮次是既有底线，排在扩展闸之前（行为与今日一致）。"""
        gate = ResourceGate(2)
        gate.register_check("max_tokens", lambda used: GateTrip("max_tokens", 1, used))
        assert gate.check(2).gate == GATE_MAX_TOOL_ROUNDS


class TestKernelWiring:
    """内核持有闸：消费方推底线、读 limit、trip 走内核记账出口。"""

    def test_kernel_exposes_gate_and_pushes_limits(self, kernel_env):
        kernel = get_kernel()
        kernel.set_resource_limits(max_tool_rounds=4)
        assert kernel.resource_gate().limit == 4

    def test_trip_goes_through_kernel_emit(self, kernel_env):
        kernel = get_kernel()
        kernel.set_resource_limits(max_tool_rounds=1)
        seen: list = []
        kernel.attach_ledger(lambda ev: seen.append(ev))
        assert kernel.resource_gate().trip(1) is not None
        event = next(e for e in seen if e.type == "resource_gate_tripped")
        assert event.origin == "kernel"
        assert event.payload["limit"] == 1 and event.payload["rounds"] == 1
        assert event.payload["gate"] == "max_tool_rounds"
