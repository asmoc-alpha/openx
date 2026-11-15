"""弹窗的 widget 形态：把"阻塞终端的 raw-mode 菜单"换成浮在转录上的屏幕。

行内实现的弹窗（``openx/ui/_components/dialogs.py``）靠 ``termios.setcbreak``
+ 裸 ``sys.stdout.write`` 读键定位光标。全屏界面里这套是**硬冲突**：stdin 归
Textual，屏幕也归 Textual，弹窗一开就是花屏。于是同样的三个问题改成
``ModalScreen``：

| 行内弹窗 | 这里 | 取消语义（fail-closed） |
|---|---|---|
| 权限审批 ``ask_permission`` | :class:`ChoiceScreen` | ``(False, False)`` — 不运行 |
| 计划批准 ``confirm_plan`` | :class:`ChoiceScreen` | ``False`` — 不执行 |
| 提问 ``ask_user_question`` | :class:`ChoiceScreen` (+ :class:`TextScreen`) | ``""`` / ``[]`` — 未作答 |

**视觉 = 行内的开放式弹窗**（``_raw_select`` 菜单原样）：**无遮罩无卡片**——
screen 背景透明，转录在弹窗后面照常可见；内容通栏左对齐（padding 与转录一致），
像行内输出一样直接铺在屏幕底部：标题一行亮字 → 正文 → 裸 ``●`` 选项列
（选中行 ``●`` + 亮字，其余 dim）→ 尾部 ``(↑/↓ to choose, …)`` 提示行。
模态语义保留（挡住下方的输入行、Esc 取消），但没有任何"对话框"的盒子——
行内用户伸手即可用的肌肉记忆原样生效。

设计上只有**两个**控件：一个选择屏、一个文本屏。三种弹窗的差异全部是数据
（标题 / 正文 renderable / 选项表）——加第四种弹窗不该需要写第三个屏。

键位沿用行内肌肉记忆：``↑``/``↓`` 移动、``Enter`` 确认、``Esc`` 取消。
多选额外用 ``space`` 勾选。取消值一律取"最保守的那个"：权限拒绝、计划不执行、
提问算未作答——弹窗被意外关掉绝不该变成一次隐式授权。
"""

from __future__ import annotations

from typing import Any, ClassVar, Optional

from rich.markdown import Markdown
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from ..ui._style import DIM

#: 多选时的勾选标记（与单选的高亮区分：勾是"选中了"，高亮是"光标在这"）
_CHECKED = "✓"
_UNCHECKED = "·"
_OTHER = "__other__"
_OTHER_LABEL = "Other (type your own)"


class ChoiceScreen(ModalScreen[Any]):
    """选项屏：标题 + 可选正文 + 选项表。单选返回值，多选返回列表。

    ``options`` 是 ``(label, value)`` 序列；``value`` 原样返回（可以是
    tuple / bool / str —— 权限需要 ``(approved, remember)`` 这种复合值）。
    """

    #: space 勾选（只在多选屏有意义）。priority：``OptionList`` 不绑 space，
    #: 但 ``Screen`` 的普通绑定在焦点落在子控件时不会触发，故显式提权。
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "取消", show=False, priority=True),
        Binding("space", "toggle", "勾选", show=False, priority=True),
    ]

    def __init__(
        self,
        title: str,
        options: list[tuple[str, Any]],
        *,
        body: Optional[Any] = None,
        default_index: int = 0,
        cancel_value: Any = None,
        multi: bool = False,
        hint: str = "",
    ) -> None:
        super().__init__()
        self._title_text = title
        self._options = list(options)
        self._body = body
        self._default_index = max(0, min(default_index, len(self._options) - 1))
        self._cancel_value = cancel_value
        self._multi = multi
        self._hint = hint or (
            "(↑/↓ to choose, space to check, Enter to confirm, Esc to cancel)"
            if multi else "(↑/↓ to choose, Enter to confirm, Esc to cancel)"
        )
        self._checked: set[int] = set()
        self._labels = [label for label, _ in self._options]

    def compose(self) -> ComposeResult:
        # 行内菜单样式：标题一行亮字（非粗体大标题）、选项表裸列、
        # 尾部 "(↑/↓ to choose, …)" 提示行——没有边框卡片，没有底色块。
        with Vertical(id="modal-card"):
            yield Static(Text(self._title_text, style="bold white"),
                         id="modal-title")
            if self._body is not None:
                with VerticalScroll(id="modal-body"):
                    yield Static(self._body, id="modal-body-text")
            yield OptionList(
                *[
                    Option(self._prompt_for(i), id=str(i))
                    for i in range(len(self._options))
                ],
                id="modal-options",
            )
            yield Static(Text(self._hint, style=DIM), id="modal-hint")

    def on_mount(self) -> None:
        options = self.query_one("#modal-options", OptionList)
        if self._options:
            options.highlighted = self._default_index
        options.focus()
        self._sync_cursor(options)

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        # 光标随高亮走（行内菜单语义：● 只在当前行）
        self._sync_cursor(event.option_list)

    def _sync_cursor(self, options: OptionList) -> None:
        """把 ``●`` 光标写到高亮行的 prompt 里（就地换，不重建列表）。"""
        highlighted = options.highlighted
        for i in range(len(self._options)):
            options.replace_option_prompt_at_index(i, self._prompt_for(
                i, cursor=(i == highlighted)))

    # ── 标签（多选勾选标记 + 单选 ● 光标，均在 prompt 文本内）──────

    def _label_for(self, index: int) -> str:
        """纯标签文本（测试与旧调用点兼容面）。"""
        return self._labels[index]

    def _prompt_for(self, index: int, *, cursor: bool = False) -> Text:
        """选项 → 行内菜单样式的 prompt。

        行内形态（``_render_options``）：单选选中行 ``● label``（cyan 圆点 +
        亮字加粗）、未选中 ``  label``（dim）；多选在光标标记前再带
        ``✓``/``·`` 勾选标记。CSS 已剥掉 OptionList 的反白高亮块，颜色
        全由 prompt 自带。
        """
        label = self._labels[index]
        if self._multi:
            check = _CHECKED if index in self._checked else _UNCHECKED
            head = f"{check} ● " if cursor else f"{check}   "
        else:
            head = "● " if cursor else "  "
        style = "bold white" if cursor else DIM
        return Text(f"{head}{label}", style=style)

    def _refresh_labels(self) -> None:
        # 就地换 prompt（``replace_option_prompt_at_index``）：重建整个
        # OptionList 会抖掉高亮位置——用户勾完一项，光标必须留在原处。
        options = self.query_one("#modal-options", OptionList)
        self._sync_cursor(options)

    # ── 交互 ────────────────────────────────────────────────────

    def action_toggle(self) -> None:
        """space：勾选/取消当前高亮项（仅多选）。"""
        if not self._multi:
            return
        options = self.query_one("#modal-options", OptionList)
        index = options.highlighted
        if index is None:
            return
        self._checked.symmetric_difference_update({index})
        self._refresh_labels()

    def action_cancel(self) -> None:
        self.dismiss(self._cancel_value)

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        index = int(event.option.id or 0)
        if not self._multi:
            self.dismiss(self._options[index][1])
            return
        if index not in self._checked:
            # Enter 落在未勾选项上：先勾上再提交（"只选一个多选项"不该是
            # 一次静默空提交）
            self._checked.add(index)
            self._refresh_labels()
        ordered = [self._options[i][1] for i in sorted(self._checked)]
        self.dismiss(ordered)


class TextScreen(ModalScreen[Optional[str]]):
    """自由文本屏（``ask_user`` 的 "Other" 分支用）。

    返回 ``value.strip()``；空串视为未作答 → ``None``。``Esc`` 同样 ``None``。
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "取消", show=False, priority=True)
    ]

    def __init__(self, title: str, *, placeholder: str = "") -> None:
        super().__init__()
        self._title_text = title
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-card"):
            yield Static(Text(self._title_text, style="bold white"),
                         id="modal-title")
            yield Input(placeholder=self._placeholder, id="modal-input")
            yield Static(Text("(Enter to confirm, Esc to cancel)", style=DIM),
                         id="modal-hint")

    def on_mount(self) -> None:
        self.query_one("#modal-input", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = (event.value or "").strip()
        self.dismiss(value or None)


# ── 三种弹窗的构造器（纯数据，便于单测与复用）─────────────────────


def plan_body(plan: str, limit: int = 40) -> Any:
    """计划正文 → Markdown（超长截断，完整计划已在转录里）。"""
    text = (plan or "").strip()
    if not text:
        return Markdown("_(no plan text)_")
    lines = text.splitlines()
    if len(lines) > limit:
        text = "\n".join(lines[:limit]) + f"\n\n_… {len(lines) - limit} more lines_"
    return Markdown(text)


def permission_body(
    reason: str, details: str, diff: Optional[tuple[str, str, str]], theme: str
) -> Any:
    """权限弹窗正文：原因 + diff 预览（或 details 面板）。

    复用行内实现的多语言高亮 diff（``ui/_diff.render_diff_lines``）——审批
    时看清"到底要改什么"是这条弹窗存在的理由，压缩成一行摘要会把它废掉。
    """
    from ..ui._diff import diff_stat, render_diff_lines

    parts: list[Any] = []
    if reason:
        parts.append(Text(reason, style=DIM))
    if diff is not None:
        path, old, new = diff
        added, removed = diff_stat(old, new)
        head = Text()
        head.append(f"\n{path}", style="bold")
        head.append(f"  +{added}", style="green")
        head.append(f" -{removed}", style="red")
        parts.append(head)
        try:
            parts.extend(render_diff_lines(old, new, path, theme=theme, max_lines=10))
        except Exception:
            pass  # 渲染失败不许拖垮审批本身
    elif details:
        parts.append(Text("\n" + details[:500], style=DIM))
    if not parts:
        return Text("")
    from rich.console import Group

    return Group(*parts)


def ask_user_options(
    options: list[dict], multi_select: bool
) -> list[tuple[str, str]]:
    """``ask_user`` 选项 → ``(label, label)``；末尾附 Other 入口。

    选项的返回值就是它的 ``label``（与行内实现一致：``ask_user`` 工具收到
    的是字符串），所以 value 与 label 同形。
    """
    pairs: list[tuple[str, str]] = []
    for opt in options or []:
        label = str(opt.get("label", ""))
        desc = str(opt.get("description", "") or "")
        shown = f"{label} — {desc}" if desc else label
        pairs.append((shown, label))
    pairs.append((_OTHER_LABEL, _OTHER))
    return pairs


__all__ = [
    "_OTHER",
    "ChoiceScreen",
    "TextScreen",
    "ask_user_options",
    "permission_body",
    "plan_body",
]
