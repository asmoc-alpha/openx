"""晋升门（K6）—— 动态插入的准入与审批（内核详设 §2.4）。

**门只有一道**：boot 装配走 loader 五阶段（内置恒首、组合决议），会话内
动态插入走本门 + 同一五阶段校验的注册面——无第二条加载路径（"同源同门"）。

分级（§2.4）：

| 贡献分级 | 判据 | 流程 |
|---|---|---|
| 只读（observe） | ui_slot / prompt 片段 / 只读工具 / x-* 事件 | 校验过即热插（纯加法，回滚=卸载） |
| 变更（act） | 写工具 / hooks / 单槽实现 | 用户确认 + scope=session 灰度 -> 晋升 |

模型只产**候选 + 建议位置**；位置合法域 = 插件注册目录（内核枚举），目录外
一律拒——prompt injection 最坏结果 = 推荐错插件（§2.4）。

分工（与 ``Guard`` 同款）：本模块只**裁决**，审批通道（弹窗 / 远程批准）由
注入的 ``admitter`` 提供——UI 不进内核。**无审批通道 = fail-closed**：变更级
贡献在没有可问之人时一律 DENY（宁可少一个能力，不可多一条后门）。裁决本身
进会话账本（``admission_decision``，记账先于动作）。
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
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from .guard import Verdict

# 审批通道签名：async (contribution) -> 是否准入。None = 无人可问（fail-closed）。
Admitter = Callable[["Contribution"], Awaitable[bool]]


@dataclass(frozen=True)
class Contribution:
    """一份待准入的贡献（候选 + 建议位置）。

    ``observe`` 由调用方判定（只读与否是**值的语义**，不是注册项类型的
    属性：一个 ASK 级工具与一个只读工具同住 ``tools`` 注册表）。给出
    ``observe=False``（缺省）即按变更级走审批——**默认从严**：判不清的
    贡献先当写的看待。
    """

    kind: str                    # 注册项类型（tools/commands/contexts/...）
    name: str                    # 注册名
    plugin: str                  # provenance：贡献者 id（如 "mcp:filesystem"）
    summary: str = ""            # 人类可读说明（审批弹窗展示）
    observe: bool = False        # True = 只读贡献（纯加法）


class PromotionGate:
    """动态插入的准入裁决（K6）。

    ``emit`` 是内核记账出口（会话账本）；``admitter`` 是审批通道，可后挂
    （``set_admitter``）——启动时还没有可问之人（serve 尚无客户端）的场景
    由消费方决定何时挂上。
    """

    def __init__(
        self,
        emit: Optional[Callable[..., Any]] = None,
        admitter: Optional[Admitter] = None,
    ) -> None:
        self._emit = emit
        self._admitter = admitter

    # ── 配置 ────────────────────────────────────────────────────

    def set_admitter(self, admitter: Optional[Admitter]) -> None:
        """挂接审批通道（None = 撤下，回到 fail-closed）。"""
        self._admitter = admitter

    @property
    def has_admitter(self) -> bool:
        """是否有人可问（消费方据此决定"先插入后确认"还是"直接拒"）。"""
        return self._admitter is not None

    # ── 裁决 ────────────────────────────────────────────────────

    async def admit(self, contribution: Contribution) -> Verdict:
        """裁决一份贡献 → ``Verdict``（``ALLOW`` 准入 / ``DENY`` 拒绝）。

        只读贡献直接放行；变更级须审批通道点头。审批通道抛异常 = 拒绝
        （fail-closed：通道坏了不是"默认同意"的理由）。任何路径都记一条
        ``admission_decision``。
        """
        verdict, reason = await self._decide(contribution)
        self._record(contribution, verdict, reason)
        return verdict

    async def _decide(self, contribution: Contribution) -> tuple[Verdict, str]:
        if not (contribution.kind and contribution.name and contribution.plugin):
            return Verdict.DENY, "malformed contribution (kind/name/plugin required)"
        if contribution.observe:
            return Verdict.ALLOW, "read-only contribution (pure addition)"
        if self._admitter is None:
            return Verdict.DENY, "no approver available (fail-closed)"
        try:
            approved = await self._admitter(contribution)
        except Exception as exc:  # 审批通道故障 = 拒绝，绝不外抛
            return Verdict.DENY, f"approver failed: {type(exc).__name__}: {exc}"
        if approved:
            return Verdict.ALLOW, "approved by user"
        return Verdict.DENY, "rejected by user"

    def _record(self, contribution: Contribution, verdict: Verdict, reason: str) -> None:
        """记账先于动作：裁决先落账，注册后发生（宁可记了没插）。"""
        if self._emit is None:
            return
        self._emit(
            "admission_decision",
            {
                "type": "admission_decision",
                "kind": contribution.kind,
                "name": contribution.name,
                "plugin": contribution.plugin,
                "summary": contribution.summary,
                "observe": bool(contribution.observe),
                "verdict": verdict.name,
                "reason": reason,
            },
            origin="kernel",
        )


if __name__ == "__main__":
    events: list[dict] = []
    gate = PromotionGate(emit=lambda t, p, **kw: events.append(p))

    def c(**kw) -> Contribution:
        base = {"kind": "tools", "name": "mcp__fake__echo", "plugin": "mcp:fake"}
        return Contribution(**{**base, **kw})

    async def _check() -> None:
        # 只读：无人可问也放行（纯加法，回滚=卸载）
        v = await gate.admit(c(observe=True))
        assert v is Verdict.ALLOW and events[-1]["verdict"] == "ALLOW"

        # 变更级 + 无审批通道 = fail-closed（默认从严）
        v = await gate.admit(c())
        assert v is Verdict.DENY
        assert events[-1]["reason"] == "no approver available (fail-closed)"
        assert not gate.has_admitter

        # 审批通道点头 / 摇头
        seen: list[Contribution] = []

        async def _yes(contribution):
            seen.append(contribution)
            return True

        gate.set_admitter(_yes)
        assert gate.has_admitter
        assert await gate.admit(c()) is Verdict.ALLOW
        assert seen[0].plugin == "mcp:fake"
        assert events[-1]["reason"] == "approved by user"

        async def _no(contribution):
            return False

        gate.set_admitter(_no)
        assert await gate.admit(c()) is Verdict.DENY
        assert events[-1]["reason"] == "rejected by user"

        # 审批通道抛异常 = 拒绝（绝不外抛、绝不默认同意）
        async def _boom(contribution):
            raise RuntimeError("console gone")

        gate.set_admitter(_boom)
        assert await gate.admit(c()) is Verdict.DENY
        assert "approver failed" in events[-1]["reason"]

        # 形状不全 = 拒绝
        gate.set_admitter(_yes)
        assert await gate.admit(Contribution(kind="", name="x", plugin="p")) is Verdict.DENY

        # 每条裁决都留痕
        assert len(events) == 6 and all(e["type"] == "admission_decision" for e in events)

    asyncio.run(_check())
    print("openx/kernel/audit/admission.py OK ✓")
