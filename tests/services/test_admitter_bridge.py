"""K6 admitter 桥：console 审批对话 → 晋升门回调（``services/assembly``）。

``admitter_from_console`` 是端层与内核之间那座桥——把 ``console.ask_permission``
的 ``(approved, remember)`` 折叠成晋升门要的 ``async (Contribution) -> bool``。
UI 不进内核，桥住消费方。

运行：``python -m pytest tests/services/test_admitter_bridge.py -q``
"""

from __future__ import annotations

from openx.kernel.audit.admission import Contribution
from openx.kernel.audit.guard import Verdict
from openx.services.assembly import admitter_from_console


class _FakeConsole:
    """鸭子 console：记录 ask_permission 调用并返回预设裁决。"""

    def __init__(self, approved: bool = True) -> None:
        self.approved = approved
        self.calls: list[dict] = []

    async def ask_permission(
        self, tool_name, reason, details="", args_summary="",
        can_remember=True, diff=None,
    ):
        self.calls.append(
            {"tool_name": tool_name, "reason": reason, "can_remember": can_remember}
        )
        return (self.approved, False)


async def test_admitter_maps_label_and_reason():
    console = _FakeConsole(approved=True)
    ok = await admitter_from_console(console)(
        Contribution(kind="tools", name="mcp__f__e", plugin="mcp:f", summary="ss")
    )
    assert ok is True
    call = console.calls[0]
    assert call["tool_name"] == "tools/mcp__f__e"
    assert call["reason"] == "ss"
    # 准入批准不是可持久化放行规则——绝不产生"以后都同意"
    assert call["can_remember"] is False


async def test_admitter_rejects_when_console_declines():
    console = _FakeConsole(approved=False)
    ok = await admitter_from_console(console)(
        Contribution(kind="tools", name="t", plugin="p")
    )
    assert ok is False


async def test_admitter_reason_falls_back_to_provenance():
    console = _FakeConsole(approved=True)
    await admitter_from_console(console)(
        Contribution(kind="hooks", name="h", plugin="vendor-x")
    )
    assert "vendor-x" in console.calls[0]["reason"]


async def test_bridge_feeds_kernel_promotion_gate():
    """端到端：桥经 set_admitter 挂上内核 → act 档 admit() 走弹窗。"""
    from openx.kernel import get_kernel, reset_kernel

    reset_kernel()
    try:
        console = _FakeConsole(approved=True)
        kernel = get_kernel()
        kernel.set_admitter(admitter_from_console(console))
        verdict = await kernel.admit(
            Contribution(kind="tools", name="t", plugin="p")
        )
        assert verdict is Verdict.ALLOW
        assert console.calls  # 审批确实被问到
    finally:
        reset_kernel()
