"""状态层（deck）渲染：**行内与全屏共用的唯一一份**行格式。

deck 是输入框上下那几块"钉住"的状态面板——计划（todos）、排队待发的跟进
消息（queue）、子代理舰队（fleet）、插件 ``ui.panel`` 面板；此外**子代理
详情视图**（``Ctrl-O`` 切过去的那一屏）也在这里成行。两个界面消费同一份
数据、也要长得一样，所以行怎么拼只写在这里：

行内 ``StreamingService._build_renderable`` 每帧把 ``Group`` 塞进 ``Live``；
全屏 ``tui/deck.py::DeckView``（deck）与 ``tui/deck.py::DetailView``（详情）
把同一个 ``Group`` 塞进 ``Static``。

两端各自只负责**预算**（自己还有多少行可花）与**摆放**（框上 / 框下），
行内容与语义一律来自本模块。这是 ``ui/_style.py`` 那条纪律的延伸：字形与
配色只定义一次，deck 行也只定义一次。

两条硬不变量：

1. **1 deck 行 ≡ 1 终端行**：deck 行每行 ``no_wrap=True,
   overflow="ellipsis"``。行内靠它算视口预算与擦除行数，全屏靠它把
   ``styles.height`` 钉成 ``len(rows)``——任何一行偷偷折行，两边的几何都会崩。
   **详情视图的正文行是这条的例外**（见 :func:`detail_body`）：它们承载子
   代理的原始输出，折行比截断有用；住滚动容器里、高度交给框架，不参与
   deck 的行数算式。头部行仍是 ``no_wrap`` 的单行。
2. **fail-open 是调用方的责任**：本模块只做纯数据 → 行，不抓 provider
   异常（那属于取数的一侧）；但**坏 markup 单面板兜底**在
   :func:`plugin_lines` 内部（插件面板是外部输入，一行坏标签不该让整帧消失），
   **坏 markup 单行兜底**在 :func:`detail_body` 内部（子代理输出是模型文本，
   一个裸 ``[`` 不该让整屏消失）。

``budget`` 由调用方给：行内是 ``height - _VIEWPORT_RESERVE - 5``（保证正文
窗口恒 ≥ 5 行），全屏是视口的一个比例（见 ``tui/deck.py::deck_budget``）。
预算为 0 → 返回空列表（"没地方就别画"，而不是画一半）。
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

from rich.console import Group
from rich.markup import escape
from rich.text import Text

from ._helpers import fmt_duration
from ._style import (
    ACCENT, ACCENT_BOLD, DIM, ERROR_STYLE, GUTTER, MARK_BULLET, MARK_FAIL,
    MARK_OK, MARK_PENDING, SUCCESS_STYLE, spin_glyph,
)

#: 各块的行数上限（超出折叠成 "+N more"）。实际渲染还会被 ``budget``
#: 二次裁剪——上限只是"即便视口很大也别把版面交给状态层"。
DECK_PLAN_ROWS = 6
DECK_QUEUE_ROWS = 4
DECK_FLEET_ROWS = 4

#: 舰队头行的操作提示（行内）：``↓`` 选中 + ``↵`` 确认切主视图。
FLEET_HINT_INLINE = "· ↓ select · ↵ view"
#: 子代理详情视图头行的操作提示（行内）：``↑``/``↓`` 在视图间循环、
#: ``alt+0`` 回主视图。
DETAIL_HINT_INLINE = "↑/↓ switch · alt+0 back"


def deck_line(text: str, style: str = "") -> Text:
    """单行 deck 行（纯文本 + 样式）。见模块 docstring 的单行不变量。"""
    return Text(text, style=style, no_wrap=True, overflow="ellipsis")


def deck_markup(markup: str) -> Text:
    """带 markup 的单行 deck 行。"""
    text = Text.from_markup(markup)
    text.no_wrap = True
    text.overflow = "ellipsis"
    return text


def plan_queue_lines(
    todos: Sequence[Any],
    queue_items: Sequence[str],
    *,
    budget: int,
    spin: Callable[[], str] = spin_glyph,
) -> list[Text]:
    """计划面板 + 队列面板（渲染在输入框**之上**）→ 行列表。

    自上而下两块：**Plan**（todos 计划面板）→ **Queue**（排队待发、FIFO
    全列）。``budget`` 是两个面板**共享**的行预算：不够时先裁较大的块
    （"较大的块信息密度更低"），直到放下——保证合计不超预算。

    全部完成的计划折叠成单行汇总：逐条列出已完成项只是占地方，收尾时把
    版面还给正文。折叠态没有逐条项，也不产生 "+N more" 折叠行。

    ``spin``：进行中项的转帧来源。行内实现传它自己的 ``_deck_spin``（那里的
    ``_SPIN_MS`` 是模块级名字、测试要 monkeypatch 它），全屏用默认值——
    两边都是"同一时钟、同一家族"，屏上不会出现两个不同步的转针。
    """
    plan_items = [t for t in (todos or []) if isinstance(t, dict)]
    queue_items = [str(q) for q in (queue_items or [])]
    if not plan_items and not queue_items:
        return []
    if budget <= 0:
        return []

    plan_all_done = bool(plan_items) and all(
        t.get("status") == "completed" for t in plan_items)
    plan_allow = 0 if plan_all_done else min(len(plan_items), DECK_PLAN_ROWS)
    queue_allow = min(len(queue_items), DECK_QUEUE_ROWS)
    show_plan = bool(plan_items)
    show_queue = bool(queue_items)

    def _count() -> int:
        """按当前取舍算出**真实**行数（头行 + 条目 + 折叠行）。"""
        total = (1 if show_plan else 0) + (1 if show_queue else 0)
        total += plan_allow + queue_allow
        if show_plan and not plan_all_done and len(plan_items) > plan_allow:
            total += 1     # 计划块的 "+N more"
        if show_queue and len(queue_items) > queue_allow:
            total += 1     # 队列块的 "+N more"
        return total

    # 超预算时的让位次序：先砍较大的块（信息密度更低），再整块撤掉队列
    # （计划是主线，队列只是待发的跟进），最后才撤计划。只砍条目而不撤块
    # 是不够的——两块各有头行与折叠行，条目砍到 0 仍可能超预算。
    while _count() > budget:
        if plan_allow >= queue_allow and plan_allow > 0:
            plan_allow -= 1
        elif queue_allow > 0:
            queue_allow -= 1
        elif show_queue:
            show_queue = False
        elif show_plan:
            show_plan = False
        else:
            break

    rows: list[Text] = []
    if show_plan:
        done = sum(1 for t in plan_items if t.get("status") == "completed")
        if plan_all_done:
            # 折叠态：单行汇总，与逐条态的项行同缩进（2 空格）
            rows.append(deck_line(
                f"  {MARK_OK} Plan {done}/{len(plan_items)}", SUCCESS_STYLE))
        else:
            rows.append(deck_markup(
                f" [bold]Plan[/bold] [dim]{done}/{len(plan_items)}[/dim]"))
        for item in plan_items[:plan_allow]:
            status = item.get("status")
            if status == "completed":
                rows.append(deck_line(
                    f"  {MARK_OK} {item.get('content') or ''}", "green"))
            elif status == "in_progress":
                # activeForm 的存在意义：进行中一项显示"正在做什么"
                label = item.get("activeForm") or item.get("content") or ""
                rows.append(deck_line(f"  {spin()} {label}", ACCENT))
            else:
                rows.append(deck_line(
                    f"  {MARK_PENDING} {item.get('content') or ''}", "dim"))
        if not plan_all_done and len(plan_items) > plan_allow:
            rows.append(deck_line(
                f"  +{len(plan_items) - plan_allow} more", "dim"))
    if show_queue:
        # 队列面板：按序全列（超 DECK_QUEUE_ROWS 折叠），位置在 Plan 之下、
        # 输入框之上——发出后从头部 pop，面板随之清空。
        rows.append(deck_markup(
            f" [bold]Queue[/bold] [dim]({len(queue_items)})[/dim]"))
        for item in queue_items[:queue_allow]:
            rows.append(deck_line(f"  {MARK_BULLET} {item}", "dim"))
        if len(queue_items) > queue_allow:
            rows.append(deck_line(
                f"  +{len(queue_items) - queue_allow} more", "dim"))
    return rows


def fleet_lines(
    snap: Sequence[dict],
    *,
    budget: int,
    marked: int = -1,
    hints: "bool | str" = True,
    spin: Callable[[], str] = spin_glyph,
) -> list[Text]:
    """子代理舰队面板（渲染在输入框**之下**）→ 行列表。

    含**主代理条目（编号 0）**，因为它是"视图列表"的第 0 项而非"另一个
    子代理"：主条目永远是回到主回答的入口。``marked`` 是待确认选择（优先）
    或当前视图条目（-1 = 无标记）。每个**运行中**的子代理在其行下补一条缩进
    活动行（``⎿ 工具(摘要)``）——已结束的代理没有"当前活动"可言，硬留一行
    是噪音；活动行**必须计入行数**，否则整组会撑爆预算。

    ``hints``：头行那句操作提示，按界面传——

    - ``True``（默认）→ :data:`FLEET_HINT_INLINE`，行内 deck 的 ``↓`` 选择
      + ``↵`` 确认；
    - ``str`` → 原样（全屏界面用 ``Ctrl+O`` 循环切换，键不同、文案就该不同）；
    - ``False`` → 什么都不写。

    **头行不能承诺按不动的键**——所以这不是"有/无提示"的布尔开关，而是
    "提示是什么"的参数。
    """
    snap = list(snap or [])
    if not snap or budget <= 0:
        return []

    def _activity(view: dict) -> str:
        return (view.get("last_tool") or "") if view["status"] == "running" else ""

    def _rows_for(allow: int) -> int:
        """裁到 allow 个子代理行时的总行数：头 1 + 主条目 1 + 子代理行
        + 各自活动行 + 折叠行。"""
        activity = sum(1 for v in snap[:allow] if _activity(v))
        fold = 1 if len(snap) > allow else 0
        return 2 + allow + activity + fold

    allow = min(len(snap), DECK_FLEET_ROWS)
    while _rows_for(allow) > budget:
        if allow <= 0:
            break
        allow -= 1

    header = f" [bold]Agents[/bold] [dim]({len(snap)})"
    if hints:
        header += FLEET_HINT_INLINE if hints is True else str(hints)
    header += "[/dim]"
    rows: list[Text] = [deck_markup(header)]
    # 主代理条目（0）：选中它 = 回到主回答视图
    main_head = f"[{ACCENT_BOLD}]❯0[/]" if marked == 0 else "[dim] 0[/dim]"
    main_style = "bold" if marked == 0 else "dim"
    rows.append(deck_markup(
        f"{main_head} [{main_style}]main[/][dim] · main answer[/dim]"))
    for view in snap[:allow]:
        secs = fmt_duration(view["elapsed"])
        tools = f"{view['tools_count']} tools"
        # 编号 + 标记：恒 2 格宽前缀（"❯N" / " N"）。
        # 标签经 markup 转义：子代理描述可含方括号。
        head = (
            f"[{ACCENT_BOLD}]❯{view['id']}[/]"
            if marked == view["id"] else f"[dim] {view['id']}[/dim]"
        )
        label = escape(view["label"])
        if view["status"] == "running":
            body = (f"[{ACCENT}] {spin()} {label}"
                    f" · {tools} · {secs}[/]")
        elif view["status"] == "error":
            body = f"[red] {MARK_FAIL} {label} · {secs}[/]"
        else:
            body = f"[green] {MARK_OK} {label} · {tools} · {secs}[/]"
        rows.append(deck_markup(head + body))
        activity = _activity(view)
        if activity:
            # 活动行缩进与转录的结果槽线同级（5 空格 + ⎿ + 2 空格）。
            # 摘要来自子代理的工具参数，可能含方括号 → 必须转义。
            rows.append(deck_line(
                f"     {GUTTER}  {escape(activity)}", DIM))
    if len(snap) > allow:
        rows.append(deck_line(f"  +{len(snap) - allow} more", "dim"))
    return rows


def plugin_lines(
    panel_list: Sequence[tuple[str, str, list[str]]],
    *,
    budget: int,
) -> list[Text]:
    """插件 UI 面板（``ui/v1``）→ 行列表。**逐面板**隔离故障。

    征集、节流、行数限额与崩溃熔断全在 ``UiPanelCollector``
    （``services/assembly.py``）——渲染帧绝不能被插件拖死。这里只做 markup
    行化与两条兜底：

    - ``svg:`` 行是插件资产引用（serve 端渲染为图），终端渲染不了 SVG →
      本帧跳过（插件应自带 ASCII 回退行）；
    - 坏 markup（未闭合标签等）→ **该面板**本帧缺席，其余面板照常。

    超预算的行折叠成一行 ``+N more``（计数含被丢的面板内行）。
    """
    if not panel_list or budget <= 0:
        return []
    rows: list[Text] = []
    dropped = 0
    for _name, _plugin, lines in panel_list:
        try:
            panel_rows = [
                deck_markup(line) for line in lines
                if not line.startswith("svg:")
            ]
        except Exception:
            continue
        for row in panel_rows:
            if len(rows) >= budget:
                dropped += 1
            else:
                rows.append(row)
    if not rows:
        return []
    if dropped:
        rows.append(deck_line(f"  +{dropped} more", "dim"))
    return rows


def detail_header(
    view: dict,
    *,
    hint: str = DETAIL_HINT_INLINE,
    spin: Callable[[], str] = spin_glyph,
) -> Text:
    """子代理详情视图的**头行**（单行 ``no_wrap``）。

    ``Agent N: 标签 · running · 3 tools · 4.2s · <hint>``——编号与标签给出
    "现在看的是谁"，状态/工具数/耗时给出"它到哪儿了"，末尾那句是按键提示
    （行内 ``↑/↓ switch · alt+0 back``，全屏 ``^O next · alt+0 main``）。

    标签经 ``escape``：子代理描述是模型生成的，可能含方括号。
    """
    status_label = {
        "running": f"[{ACCENT}]{spin()} running[/{ACCENT}]",
        "done": f"[{SUCCESS_STYLE}]{MARK_OK} done[/{SUCCESS_STYLE}]",
        "error": f"[{ERROR_STYLE}]{MARK_FAIL} error[/{ERROR_STYLE}]",
    }.get(view["status"], str(view["status"]))
    return deck_markup(
        f" [bold]Agent {view['id']}: {escape(str(view['label']))}[/bold]"
        f" [dim]·[/dim] {status_label}"
        f" [dim]· {view['tools_count']} tools"
        f" · {fmt_duration(view['elapsed'])}"
        f" · {hint}[/dim]",
    )


def _output_line(raw: str) -> Text:
    """子代理输出的一行 → ``Text``。坏 markup 回落纯文本，绝不抛。

    子代理的 ``str`` token 由 ``format_stream_event`` **原样**返回（不转义），
    所以模型输出里一个裸 ``[``（比如 markdown 链接、``[TODO]``）就足以让
    ``Text.from_markup`` 抛 ``MarkupError``——在行内那等于整帧消失。输出是
    外部输入，兜底在这里：能解析就保留颜色，解析不了就把它当纯文本显示。
    """
    try:
        return Text.from_markup(raw)
    except Exception:
        return Text(raw)


def detail_body(view: dict, *, budget: Optional[int] = None) -> list[Text]:
    """子代理详情视图的**正文行**（捕获事件流的末尾窗口）。

    ``budget``：最多给几行；``None`` = 全部（全屏界面用——正文住在滚动容器
    里，能翻就别替用户裁）。末尾的 ``pending``（未遇 ``\\n`` 的尾片段）一并
    展示，否则正在写的那一行要等下一个换行才可见。

    **这几行允许折行**（不加 ``no_wrap``）：它们是子代理的原始输出，长行截断
    比折行更糟；因此它们不是 deck 行、不参与"1 行 ≡ 1 终端行"的几何算式。
    """
    lines = [str(ln) for ln in (view.get("lines") or ())]
    pending = view.get("pending") or ""
    if pending:
        lines.append(str(pending))
    if budget is not None:
        lines = lines[-budget:] if budget > 0 else []
    if not lines:
        return [Text("  (no output yet)", style="dim")]
    return [_output_line(ln) for ln in lines]


def detail_lines(
    view: dict,
    *,
    budget: int,
    hint: str = DETAIL_HINT_INLINE,
    spin: Callable[[], str] = spin_glyph,
) -> list[Text]:
    """详情视图的完整行列表（头行 + 正文窗口）——行内 ``_detail_view`` 用。

    ``budget`` 只约束正文行数（头行恒在）：行内把它算成
    ``height - _VIEWPORT_RESERVE - deck_h - 1``。
    """
    return [detail_header(view, hint=hint, spin=spin),
            *detail_body(view, budget=budget)]


def to_group(rows: Optional[Sequence[Text]]) -> Optional[Group]:
    """行列表 → ``Group``（两个界面都塞进 Rich 渲染面）。空 → ``None``。"""
    if not rows:
        return None
    return Group(*rows)


__all__ = [
    "DECK_FLEET_ROWS",
    "DECK_PLAN_ROWS",
    "DECK_QUEUE_ROWS",
    "DETAIL_HINT_INLINE",
    "FLEET_HINT_INLINE",
    "deck_line",
    "deck_markup",
    "detail_body",
    "detail_header",
    "detail_lines",
    "fleet_lines",
    "plan_queue_lines",
    "plugin_lines",
    "to_group",
]
