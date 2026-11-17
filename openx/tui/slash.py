"""输入行的斜杠命令菜单 —— 行内 ptk 补全菜单的全屏对等物。

行内 REPL（``ui/_components/editor.py``）在输入以 ``/`` 起头且命令名未含
空白时弹 prompt_toolkit 的 COLUMN 补全菜单：前缀同时匹配主名与别名，命中
别名提交规范名，Enter 应用选中项并提交，Tab 应用 + 尾随空格，Esc 收菜单。

全屏 TUI 的输入行是自绘控件（``TextArea`` 之上，见 ``tui/prompt.py``），没有
这层。这里用 ``Vertical`` + ``Static`` 行拼同款菜单，数据源同一家
（``cli.commands.menu_entries``，延迟导入断开 ui/tui → cli 顶层环），键位
路由在 ``PromptEditor``（``tui/prompt.py``：Tab 应用、Esc 收菜单、↑/↓ 移光标）。

视觉对齐行内菜单（同 modals.py 的纪律）：裸列无框，选中行 ``●`` + 亮字
加粗，其余 dim；每行主名 + dim 描述（ptk 的 display_meta 位）。
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from ..ui._style import ACCENT, DIM

#: 菜单最多展示的候选数（行内 ptk 菜单满屏滚动；这里截断，多打几个字即收窄）
_MAX_ROWS = 10


def slash_matches(text: str) -> list[tuple[str, str, str]]:
    """输入行文本 → 候选 ``[(name, description, aliases_hint)]``。

    规则与行内 ``OpenXCompleter.get_completions`` 逐字一致：

    - 整行以 ``/`` 起头才出候选；命令名已含空白（正在写参数）→ 空；
    - 前缀匹配主名**或**任一别名（大小写不敏感）；
    - 返回按主名排序的 ``menu_entries()`` 子序列，别名聚成 ``(a,b)`` 提示。
    """
    if not text.startswith("/"):
        return []
    body = text[1:]
    if any(ch.isspace() for ch in body):
        return []
    from ..app.cli.commands import menu_entries

    q = body.lower()
    out: list[tuple[str, str, str]] = []
    for name, desc, aliases in menu_entries():
        if name.startswith(q) or any(a.startswith(q) for a in aliases):
            hint = f" ({','.join(aliases)})" if aliases else ""
            out.append((name, desc, hint))
    return out[:_MAX_ROWS]


class SlashMenu(Vertical):
    """输入行下方的候选浮层。

    行集合**恒定**：mount 时预挂 ``_MAX_ROWS`` 个 ``Static``，之后只
    ``update()`` 各行内容、用 ``display`` 隐藏空行——绝不在运行期增删子
    节点。增删会触发 layout 与重渲的交错（``remove_children`` 后、新
    ``mount`` 生效前，容器以空 visual 参与渲染 → ``render_strips``
    NoneType 崩溃，run_test 下必现）。
    """

    DEFAULT_CSS = """
    SlashMenu {
        height: auto;
        padding: 0 2;
        display: none;
    }
    SlashMenu.visible {
        display: block;
    }
    SlashMenu > Static {
        width: 1fr;
        height: auto;
        padding: 0;
    }
    """

    def __init__(self) -> None:
        super().__init__(id="slash-menu")
        #: 当前候选 ``[(name, desc, hint)]``（``set_matches`` 的镜像，测试可读）
        self.matches: list[tuple[str, str, str]] = []
        #: 光标行（0 基）；菜单隐藏时恒 0
        self.cursor = 0

    def compose(self) -> ComposeResult:
        for _ in range(_MAX_ROWS):
            yield Static(classes="slash-row")

    # ── 候选与重渲 ──────────────────────────────────────────────

    def on_mount(self) -> None:
        self._hide_all()

    def _rows(self) -> list[Static]:
        return list(self.query(Static))

    def _hide_all(self) -> None:
        for row in self._rows():
            row.display = False

    def set_matches(self, matches: list[tuple[str, str, str]]) -> None:
        """换候选并重渲（光标复位到首行——行内菜单同款）。"""
        self.matches = matches
        self.cursor = 0
        self._rerender()
        self.set_class(bool(matches), "visible")

    def refresh_for(self, text: str) -> None:
        """输入行文本变化 → 重新匹配并重渲。"""
        self.set_matches(slash_matches(text))

    def _rerender(self) -> None:
        rows = self._rows()
        for i, row in enumerate(rows):
            if i < len(self.matches):
                row.update(self._line_for(i))
                row.display = True
            else:
                row.display = False

    def _line_for(self, index: int) -> Text:
        name, desc, hint = self.matches[index]
        sel = index == self.cursor
        line = Text()
        if sel:
            line.append("● ", style=ACCENT)
            line.append(name, style="bold white")
        else:
            line.append("  ", style=DIM)
            line.append(name, style=DIM)
        if desc or hint:
            meta = f"  {desc}{hint}" if desc else f"  {hint.strip()}"
            line.append(meta, style=DIM)
        return line

    # ── 光标移动（键位由 PromptEditor 转发）─────────────────────

    def move(self, delta: int) -> bool:
        """``↑``/``↓`` 移动光标；有候选时返回 True（已消费）。"""
        if not self.matches:
            return False
        self.cursor = (self.cursor + delta) % len(self.matches)
        self._rerender()
        return True

    # ── 应用 ────────────────────────────────────────────────────

    def apply(self, *, with_space: bool = False) -> str | None:
        """当前光标行 → 替换输入行的 ``/命令名`` 部分。

        ``with_space``（Tab 语义）：尾随一个空格——参数位起编，同行内。
        无候选返回 None（调用方保持输入行不变）。
        """
        if not self.matches:
            return None
        name, _, _ = self.matches[self.cursor]
        return f"/{name} " if with_space else f"/{name}"

    def hide(self) -> None:
        """收菜单（Esc / 提交 / 失焦）。"""
        self.matches = []
        self.cursor = 0
        self.remove_class("visible")
