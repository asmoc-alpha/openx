"""输入行的编辑控件 —— 多行 + 历史 + 斜杠菜单键位（``TextArea`` 之上）。

为什么不是 ``Input``：Textual 的 ``Input`` 物理上是单行的（文档模型里没有
``\\n``），多行粘贴只能被压成一行、光标进不了第二行。``TextArea`` 有真正的
文档模型：折行、选区、撤销、括号粘贴全部现成，代价是键位语义要自己定。

键位语义（对齐行内 ``ui/_components/editor.py`` 的 ptk 编辑器）
----------------------------------------------------------------

============  ====================================================
``Enter``     提交（多行内容整体提交；流式期由 app 改成"排队"）
``Ctrl+J``    插入字面换行
``Alt+Enter`` 同上（终端最可靠的换行键：``ESC CR`` 是可区分的字节对）
``Shift+Enter`` 同上（仅 kitty 键盘协议等能区分的终端会真报这个键）
``↑``/``↓``   斜杠菜单开 → 移动候选；否则光标在首/末行时翻历史
``Tab``       菜单开 → 应用候选 + 尾随空格；菜单关 → 焦点链（框架默认）
``Esc``       菜单开 → 只收菜单；菜单关 → 冒泡给 app 打断回合
============  ====================================================

行内是 ``multiline=False`` + ``("escape","enter")`` 绑定插入字面 ``\\n``，
所以"回车提交、Alt+Enter 换行"这套语义在两侧是同一个（TUI 多的只是
``Ctrl+J``：不需要终端支持修饰键）。

两个承重细节
------------

1. **菜单关时用 ``check_action`` 让绑定整体失效，不用 ``stop()``**：Textual 的键路
   是两条——① MRO 分发的 ``_on_key``（TextArea 在这里把可打印字符插入、把
   ↑/↓ 交给光标），② 冒泡到 App 后的 ``_check_bindings``。``prevent_default``
   只断 ①，``stop()`` 会把 ② 也断掉（按键永远到不了绑定 action）。菜单关时
   让 ``slash_*`` 绑定整体失效，``Tab``/``Esc`` 才会照旧走焦点链与打断。
2. **高度自己算**（``styles.height = 行数 + 2``，两条框线各占一行）：不赌
   框架的 auto-height 语义。行数取 ``wrapped_document.height``——那是按当前
   宽度软折行之后的真实行数，长单行与多行一视同仁。
"""

from __future__ import annotations

from typing import Any, ClassVar, Optional

from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.widgets import TextArea

from .history import PromptHistory
from .slash import SlashMenu

#: 输入框最多长到几行（超出后内部滚动；再高就把转录挤没了）
MAX_ROWS = 8

#: 上下两条框线占的行数（``height = 内容行 + _FRAME_ROWS``）
_FRAME_ROWS = 2


class PromptEditor(TextArea):
    """聊天输入行：多行编辑 + ``↑``/``↓`` 历史 + 斜杠命令菜单路由。"""

    #: 菜单开时才生效（见模块 docstring 的 ``check_action`` 说明）
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("tab", "slash_tab", "应用候选", show=False),
        Binding("escape", "slash_esc", "收菜单", show=False),
    ]

    class Submitted(Message):
        """``Enter`` 提交：``value`` 是提交文本（可能含 ``\\n``）。"""

        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    def __init__(
        self,
        *,
        prompt_history: Optional[PromptHistory] = None,
        id: Optional[str] = None,  # noqa: A002 - Textual 的既有参数名
        **kwargs: Any,
    ) -> None:
        # 光标行高亮是 TextArea 的"代码编辑器"观感（整行铺一层 $boost），
        # 与行内输入框的素面不符；placeholder / 软折行走框架原生能力。
        kwargs.setdefault("highlight_cursor_line", False)
        kwargs.setdefault("soft_wrap", True)
        super().__init__(id=id, **kwargs)
        #: 输入历史（None = 不记历史，弹窗里的自由输入框走这条）
        self._prompt_history = prompt_history
        #: 当前 ``styles.height`` 的去重缓存（见 :meth:`_sync_height`）
        self._height = 0

    # ── 兼容面：``Input.value`` ─────────────────────────────────

    @property
    def value(self) -> str:
        """当前输入内容（含 ``\\n``）——``Input.value`` 的对位。"""
        return self.text

    @value.setter
    def value(self, text: str) -> None:
        self.load_text(text or "")

    # ── 生命周期 ────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._sync_height()

    def _on_resize(self) -> None:
        """宽度变了 → 软折行行数变了 → 高度重算。

        **不调 super()**：``TextArea._on_resize`` 是它自己的处理入口（负责重新
        折行），MRO 分发会各自调用一遍；这里只需排在它之后取折行结果——
        所以推迟到下一帧刷新（``call_after_refresh``）。
        """
        self.call_after_refresh(self._sync_height)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """内容变化：高度跟随 + 斜杠菜单联动（``Input.Changed`` 的对位）。"""
        self._sync_height()
        self._refresh_slash_menu()

    def _sync_height(self) -> None:
        """按软折行行数调整高度（1..``MAX_ROWS`` 行 + 上下框线）。"""
        try:
            rows = self.wrapped_document.height
        except Exception:  # pragma: no cover - 布局未就绪（首帧前）
            return
        height = max(1, min(MAX_ROWS, rows)) + _FRAME_ROWS
        if height == self._height:
            return  # 值没变就别写 styles：那会触发一次无意义的布局
        self._height = height
        self.styles.height = height

    # ── 斜杠菜单 ────────────────────────────────────────────────

    @property
    def _slash_menu(self) -> Optional[SlashMenu]:
        try:
            return self.app.query_one("#slash-menu", SlashMenu)
        except Exception:
            return None

    def _menu_open(self) -> bool:
        menu = self._slash_menu
        return menu is not None and bool(menu.matches)

    def check_action(
        self, action: str, parameters: tuple[object, ...]
    ) -> bool | None:
        """菜单关时全部 slash 绑定整体失效——Esc/Tab 自然冒泡。

        不做这一层门控，Esc 绑定会先于 app 的 interrupt 吃掉按键：流式
        打断永远不触发（回归于 test_esc_cancels_registered_task_once）。
        """
        if action.startswith("slash_"):
            return self._menu_open()
        return super().check_action(action, parameters)

    def _refresh_slash_menu(self) -> None:
        """输入变化 → 弹/收菜单。"""
        menu = self._slash_menu
        if menu is not None:
            menu.refresh_for(self.value)

    def action_slash_tab(self) -> None:
        menu = self._slash_menu
        if menu is None or not menu.matches:
            return  # 无候选：不拦截（Textual 默认 Tab 是焦点移动）
        applied = menu.apply(with_space=True)
        if applied is not None:
            self.value = applied
            menu.hide()

    def action_slash_esc(self) -> None:
        menu = self._slash_menu
        if menu is not None:
            menu.hide()

    def action_submit(self) -> None:
        """提交当前内容（菜单开时先应用候选，同 ``_enter_with_menu``）。"""
        menu = self._slash_menu
        if menu is not None and menu.matches:
            applied = menu.apply()
            if applied is not None:
                self.value = applied
        self.post_message(self.Submitted(self.value))

    # ── 键路 ────────────────────────────────────────────────────

    async def _on_key(self, event: events.Key) -> None:
        """接管"菜单导航 / 提交 / 换行 / 翻历史"，其余交回 ``TextArea``。

        必须 ``prevent_default()``：基类的 ``_on_key`` 会把 ``enter`` 插成
        换行、把 ``↑``/``↓`` 当光标移动——两者正是这里要改写的行为。
        ``stop()`` 只在**自己处理完整按键**时用（提交/换行/翻历史），否则
        冒泡会断掉、绑定 action 收不到（见模块 docstring）。
        """
        menu_open = self._menu_open()

        if menu_open and event.key in ("up", "down", "tab", "escape"):
            # 菜单优先：只断基类默认行为，键继续冒泡给绑定（tab/escape）
            event.prevent_default()
            menu = self._slash_menu
            if menu is not None:
                if event.key == "up":
                    menu.move(-1)
                elif event.key == "down":
                    menu.move(1)
            return

        if event.key in ("enter", "ctrl+j", "alt+enter", "shift+enter"):
            event.prevent_default()
            event.stop()
            if event.key == "enter":
                self.action_submit()
            else:
                self.insert("\n")
            return

        if event.key in ("up", "down") and self._prompt_history is not None:
            if self._recall_history(event):
                event.prevent_default()
                event.stop()

    def _recall_history(self, event: events.Key) -> bool:
        """光标在首行按 ``↑`` / 末行按 ``↓`` → 翻历史。已消费返回 True。

        只在**边界**接管：多行内容里 ↑/↓ 仍是光标移动（用户的表里，方向键
        第一含义是移动）。历史为空（``prev``/``next`` 返回 None）时不消费，
        按键照旧落到基类——空历史下按 ↑ 什么都不该发生，但不该变成"没响应"。
        """
        history = self._prompt_history
        if history is None:
            return False
        row = self.cursor_location[0]
        rows = self.document.line_count
        if event.key == "up":
            if row != 0:
                return False
            recalled = history.prev(self.value)
        else:
            if row != rows - 1:
                return False
            recalled = history.next(self.value)
        if recalled is None:
            return False
        self.value = recalled
        self.cursor_location = self.document.end  # 光标落到末尾，接着编辑
        return True


__all__ = ["MAX_ROWS", "PromptEditor"]
