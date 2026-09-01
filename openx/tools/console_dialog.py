"""工具侧 async 优先的对话框通道（ask_user / plan 审批）。

serve（web）下 ``ServeConsole`` 提供协程变体 ``ask_user_question_async`` /
``confirm_plan_async``——广播 ``ask_user`` / ``plan_request`` 并等待客户端
应答（见 ``app/serve/bridge.py``）；TUI 的 ``Console`` 只有同步版（阻塞读
stdin，``ui/_components/dialogs.py``）。三个交互工具（ask_user /
exit_plan_mode / choose_mode）的 ``execute`` 本就是 ``async``，统一经本模块
**async 优先**分发：console 有协程变体则 ``await``，否则回退同步版。

约定
====
- 探测用 ``asyncio.iscoroutinefunction``：``ServeConsole`` 的 ``*_async``
  是真协程函数 → 命中；TUI ``Console`` / 测试桩没有该属性（``getattr`` 落
  ``None``）→ 回退同步版。``ServeConsole`` 的 ``__getattr__`` 只兜不存在
  的属性，不会凭空造出 ``*_async``。
- 本模块只负责"选通道"，不负责 fail-closed：断流/超时/无应答的保守默认由
  ``ServeConsole``（bridge）内部处理；TUI 版本就是真交互。
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
from typing import Any, Optional


async def ask_user_question(
    console: Any,
    question: str,
    options: Optional[list] = None,
    multi_select: bool = False,
) -> Any:
    """async 优先的提问：console 有 ``ask_user_question_async`` 协程变体
    → await；否则调用同步 ``ask_user_question``。返回形态由 console 定
    （single → str，multi → list[str]，与 TUI/bridge 契约一致）。"""
    async_impl = getattr(console, "ask_user_question_async", None)
    if asyncio.iscoroutinefunction(async_impl):
        return await async_impl(question, options or [], multi_select)
    return console.ask_user_question(question, options or [], multi_select)


async def confirm_plan(console: Any, plan: str = "") -> bool:
    """async 优先的计划审批：console 有 ``confirm_plan_async`` 协程变体
    → await（传 plan）；否则调用同步 ``confirm_plan``（无参，TUI 契约）。"""
    async_impl = getattr(console, "confirm_plan_async", None)
    if asyncio.iscoroutinefunction(async_impl):
        return await async_impl(plan)
    return console.confirm_plan()


if __name__ == "__main__":
    # 独立调试：两条通道各走一遍（假 console，绝不真弹窗）
    class _SyncConsole:
        """无 async 变体（TUI / 测试桩）→ 回退同步版。"""

        def __init__(self, answer: str = "A"):
            self._answer = answer
            self.calls: list[str] = []

        def ask_user_question(self, question, options, multi_select=False):
            self.calls.append("ask")
            return [self._answer]

        def confirm_plan(self) -> bool:
            self.calls.append("plan")
            return True

    class _AsyncConsole:
        """有协程变体（ServeConsole）→ 走交互通道，绝不碰同步版。"""

        def __init__(self):
            self.calls: list[str] = []

        async def ask_user_question_async(
            self, question, options, multi_select=False
        ):
            self.calls.append("ask")
            return options[0]["label"]

        async def confirm_plan_async(self, plan: str = "") -> bool:
            self.calls.append("plan")
            return True

        def ask_user_question(self, *a, **k):
            raise AssertionError("async console must not use sync ask path")

        def confirm_plan(self, *a, **k):
            raise AssertionError("async console must not use sync plan path")

    async def _self_check():
        sc = _SyncConsole(answer="B")
        assert await ask_user_question(
            sc, "q", [{"label": "A"}, {"label": "B"}]
        ) == ["B"]
        assert await confirm_plan(sc) is True
        assert sc.calls == ["ask", "plan"]

        ac = _AsyncConsole()
        assert await ask_user_question(
            ac, "q", [{"label": "A"}, {"label": "B"}]
        ) == "A"
        assert await confirm_plan(ac, "# plan") is True
        assert ac.calls == ["ask", "plan"]

    asyncio.run(_self_check())
    print("openx/tools/console_dialog.py OK ✓")
