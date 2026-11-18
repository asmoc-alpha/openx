"""可折叠块：thinking / 工具调用的 widget 形态。

行内实现里"折叠"是一个人肉状态机（``_reasoning_expanded`` /
``_tools_expanded`` + Ctrl+R / Ctrl+T + 未固化窗口），因为 scrollback 里的
行不可重写。全屏实现里这件事由 :class:`textual.widgets.Collapsible` 承担：

- **默认折叠** = ``collapsed=True``；
- **点击展开** = 标题栏的 ``CollapsibleTitle`` 自带 ``_on_click`` → post
  ``Toggle``（Textual 8.2.8 实测，见 ``tests/tui/test_spike.py``）；
- **键盘** = 标题栏聚焦后 Enter（``CollapsibleTitle.BINDINGS`` 自带），
  Tab / Shift+Tab 在块间移动焦点（Textual 默认走焦点链）。

这正是"全屏更容易"的那部分：用户最初的需求（thinking 默认折叠、点开可看）
在这里只是构造参数，不再需要任何视口预算或光标算术。

数据与视图的分工沿用行内纪律：正文 **绝不进 Markdown 重排**的规则对
thinking 仍然成立（围栏/反引号会被 markdown 重排乱）——thinking 用纯
``Text`` + dim，答案才走 ``Markdown``（且先经 ``render_math`` 做 LaTeX →
Unicode 近似，与行内逐字一致）。
"""

from __future__ import annotations

import time
from typing import Optional

from rich.markdown import Markdown
from rich.text import Text
from textual.widgets import Collapsible, Static

from ..ui._diff import TEXT_OP_TOOLS, colorized_output_lines, text_op_lines
from ..ui._helpers import fmt_duration
from ..ui._math_text import render_math
from ..ui._style import DIM, ERROR_STYLE, MARK_INFO, MARK_PENDING

#: thinking 正文的样式：纯 dim 文本（行内实现同款）
THINKING_STYLE = DIM
#: 折叠时标题栏右侧的操作提示（Textual 用 ▸/▼ 表示折叠态，此处补语义）
BODY_CLASS = "block-body"


def markdown_answer(text: str) -> Markdown:
    """答案文本 → Rich Markdown（先做数学记号转换，与行内一致）。"""
    return Markdown(render_math(text), code_theme="monokai")


class ThinkingBlock(Collapsible):
    """推理块：默认折叠，标题栏点击展开。

    ``feed()`` 追加 token、``freeze()`` 冻结耗时并改标题——对应行内实现里
    ``_reasoning_buffer`` 增长 → 首个非推理 chunk 到来即冻结成
    ``● Thought for Ns``。区别是这里**冻结后仍可折叠**（widget 树可重渲），
    行内实现冻结即固化进 scrollback、此后 Ctrl+R 永久失效。
    """

    def __init__(self, *, collapsed: bool = True, **kwargs) -> None:
        super().__init__(
            Static(Text("", style=THINKING_STYLE), classes=BODY_CLASS),
            title=f"{MARK_PENDING} Thinking…",
            collapsed=collapsed,
            classes="thinking-block",
            **kwargs,
        )
        self._buffer = ""
        self._frozen = False

    # ── 数据 ────────────────────────────────────────────────────
    @property
    def text(self) -> str:
        return self._buffer

    @property
    def frozen(self) -> bool:
        return self._frozen

    @property
    def body(self) -> Static:
        return self.query_one(f".{BODY_CLASS}", Static)

    def _body_or_none(self) -> Optional[Static]:
        """正文 widget；尚未挂载（mount 是异步的）时返 None。

        流式喂 token 与 mount 之间本来就没有顺序保证（流式管线的
        ``feed()`` 来自网络事件、mount 来自消息队列），所以写入必须容忍
        "体还没长出来"——``on_mount`` 会把缓冲补写一次。
        """
        found = list(self.query(f".{BODY_CLASS}"))
        return found[0] if found else None

    def on_mount(self) -> None:
        self._sync_body()

    def _sync_body(self) -> None:
        body = self._body_or_none()
        if body is not None:
            # 纯文本 + dim：**绝不**过 Markdown（围栏/反引号会被重排乱）
            body.update(Text(self._buffer.rstrip("\n"), style=THINKING_STYLE))

    def feed(self, token: str) -> None:
        """追加推理 token（幂等安全：空串直接忽略）。"""
        if not token:
            return
        self._buffer += token
        self._sync_body()

    def freeze(self, elapsed: float) -> None:
        """推理阶段结束：标题改为 ``● Thought for Ns``（静态，不带滴答计时）。"""
        self._frozen = True
        self.title = f"{MARK_INFO} Thought for {fmt_duration(elapsed)}"


class ToolBlock(Collapsible):
    """工具调用块：标题 = 状态点 + 名字(+摘要)，体 = 结果正文。

    状态语义沿用行内实现的三态：running（dim）/ done（绿）/ error（红）。
    结果正文默认折叠——长输出（测试日志、grep 结果）在行内实现里靠"折叠 3
    行 + ctrl+t"省屏，这里由 Collapsible 直接承载。

    **正文分三层着色**（语义判定来自 ``ui/_diff.py``，与行内同一份）：

    1. 文本操作且有变更预览（``diff`` 三元组）→ ``⎿ +A −D`` 统计行 + 按扩展名
       做语言高亮的 diff（绿增/红删/头行 dim），**不再回显原始输出**——否则
       diff 在屏上出现两遍；
    2. 其余工具的原始输出 → 逐行原样；``edit_file`` / ``write_file`` 的输出
       里那段 unified diff 按 diff 语义着色（``+`` 绿、``-`` 红、``@@`` dim）；
    3. 出错 → 整段红，且**绝不展示预览的 diff**：预览是执行前探测的，操作失败
       意味着那个变更并未发生（同行内 ``_text_op_body`` 的纪律）。
    """

    #: 正文最多展开多少行（超出折成一行 dim 提示）。行内折叠是 3 行 + ctrl+t；
    #: 这里整块可折叠，正文可以给得更宽，但仍要挡住"工具吐了 5000 行"。
    MAX_BODY_LINES = 40

    def __init__(self, name: str, summary: str = "", *,
                 diff: Optional[tuple[str, str, str]] = None,
                 collapsed: bool = True, **kwargs) -> None:
        # 注意：不能叫 ``self.name``——``Widget.name`` 是 Textual 的
        # reactive，在 super().__init__() 之前赋值会炸、之后赋值会覆盖
        # 控件自身的名字语义。工具名单独放 ``tool_name``。
        self.tool_name = name
        self.summary = summary
        #: 变更预览 ``(path, old, new)``；None = 非文本操作 / 探测失败
        self.diff = diff
        self._output = ""
        super().__init__(
            Static(Text("  running…", style=DIM), classes=BODY_CLASS),
            title=self._make_title("running"),
            collapsed=collapsed,
            classes="tool-block",
            **kwargs,
        )
        self._status = "running"

    @property
    def status(self) -> str:
        return self._status

    @property
    def is_text_op(self) -> bool:
        """有变更预览的文本操作——正文按 diff 渲染。"""
        return self.diff is not None and self._status != "error"

    @property
    def body(self) -> Static:
        return self.query_one(f".{BODY_CLASS}", Static)

    def _body_or_none(self) -> Optional[Static]:
        found = list(self.query(f".{BODY_CLASS}"))
        return found[0] if found else None

    def on_mount(self) -> None:
        self._sync_body()

    def _body_rows(self) -> list[Text]:
        """正文行（``Text`` 列表）——分三层，见类 docstring。"""
        if self._status == "error":
            text = self._output or "(no output)"
            return [Text(text, style=ERROR_STYLE)]
        if self.is_text_op:
            return text_op_lines(self.diff, max_lines=self.MAX_BODY_LINES)
        if not self._output:
            return [Text("(no output)")]
        return colorized_output_lines(
            self._output,
            is_edit=self.tool_name in TEXT_OP_TOOLS,
            limit=self.MAX_BODY_LINES,
        )

    def _sync_body(self) -> None:
        body = self._body_or_none()
        if body is None:
            return
        rows = self._body_rows()
        # **必须仍是单个 ``Text``**：``body.visual.plain`` 是既有测试的断言面，
        # 而 ``Group`` 没有 ``.plain``。多行 + spans 一样能着色。
        body.update(Text("\n").join(rows))

    def _make_title(self, status: str) -> str:
        # 不能叫 ``_title``：``Collapsible`` 自己持有 ``self._title``
        # （标题栏 widget 引用），同名方法会被它覆盖成控件对象。
        dot = {"running": MARK_PENDING, "done": MARK_INFO, "error": "✕"}
        head = f"{dot.get(status, MARK_PENDING)} {self.tool_name}"
        return f"{head} ({self.summary})" if self.summary else head

    def set_result(self, output: str, *, is_error: bool = False) -> None:
        self._status = "error" if is_error else "done"
        self._output = (output or "").rstrip("\n")
        self.title = self._make_title(self._status)
        self._sync_body()


class StatusLine(Static):
    """一行状态（转帧 + 标签 + 耗时），对应行内实现的 spinner 行。

    渲染频率由 app 的定时器控制（默认 8Hz）；标签在 ``Thinking…`` 与
    ``Answering…`` 之间切换，与行内实现同款文案。
    """

    FRAMES = ("·", "✢", "✳", "✶", "✻", "✽")

    def __init__(self, **kwargs) -> None:
        super().__init__(Text(""), **kwargs)
        self._tick = 0
        self._label: Optional[str] = None
        self._t0: float = 0.0

    def set_busy(self, label: Optional[str], *, started_at: float = 0.0) -> None:
        self._label = label
        self._t0 = started_at
        self._render_frame()

    def _render_frame(self) -> None:
        if not self._label:
            self.update(Text(""))
            return
        elapsed = max(0.0, time.monotonic() - self._t0) if self._t0 else 0.0
        glyph = self.FRAMES[self._tick % len(self.FRAMES)]
        self._tick += 1
        text = Text()
        text.append(f"  {glyph} ", style="cyan")
        text.append(self._label)
        if elapsed:
            text.append(f"  ({fmt_duration(elapsed)})  ·  esc to interrupt",
                        style=DIM)
        self.update(text)


__all__ = [
    "THINKING_STYLE",
    "StatusLine",
    "ThinkingBlock",
    "ToolBlock",
    "markdown_answer",
]
