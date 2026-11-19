"""状态层（deck）的全屏实现：取数（:class:`DeckSource`）+ 承载
（:class:`DeckView` / :class:`DetailView`）。

行内把四块 deck 一帧一构塞进 Rich ``Live``；全屏没有"一帧"，有的是常驻
widget。于是这里只切两刀：

1. :class:`DeckSource` —— **取数与预算**：把 ``agent.todos`` / 排队消息 /
   ``agent.fleet.snapshot()`` / ``agent.ui_panels.panels()`` 四路数据按各自
   的预算切成行。行怎么拼**不在这里**：那是 ``ui/_deck.py``，与行内共用
   同一份（两个界面看到同样的 Plan/Queue/Agents 行）。
2. :class:`DeckView` / :class:`DetailView` —— **承载**：把行列表塞进 ``Static``，
   空则整块隐藏。高度是显式的（deck 行恒 ``no_wrap``，一行 ≡ 一行；详情正文
   反过来可折行、住在滚动容器里），不赌框架的 auto-height —— 与
   ``tui/prompt.py::_sync_height`` 同纪律。

布局（对齐行内）：

```
#transcript   （或 #detail —— 二者互斥，详情占据转录的位置）
#plan-deck    Plan + Queue        ← 输入框之上、状态行之上，隔 1 空行
#spinner      Thinking…/Answering…
#prompt       输入行
#agents-deck  Agents + 插件面板   ← 输入框之下
```

三段可见性规则（与行内**故意不同**，理由见 ``docs/subsystems/tui.md``）：

- **Plan/Queue 与插件面板**：只要还有内容就一直显示。行内是回合结束即消失
  —— 那是 ``Live`` 区域瞬态的副作用，不是需求；``ui.panel`` 的桌面宠物／
  监控小部件按定义就该常驻。
- **Agents**：只在回合进行中显示（舰队本就是每轮重建的临时活动）。

子代理详情视图（``Ctrl+O`` 循环切换）换掉整块转录，而不是叠在它上面：与行内
"正文区切到某个子代理"是同一处位置。按键不用方向键——全屏的输入行始终可
编辑，``↑``/``↓`` 的第一含义是翻历史/移光标，那两条语义都有测试钉着。
"""

from __future__ import annotations

import contextlib
from typing import Any, Callable, Optional, Sequence

from rich.console import Group
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from ..ui._deck import (
    deck_line, detail_body, detail_header, fleet_lines, plan_queue_lines,
    plugin_lines,
)

#: 舰队头行的操作提示（全屏）：``Ctrl+O`` 循环切换主视图与各子代理详情。
#: 行内是 ``↓`` 选中 + ``↵`` 确认——同一块面板、不同按键，故提示文案按界面
#: 传参（见 ``ui/_deck.fleet_lines`` 的 ``hints``）。
FLEET_HINT_TUI = "· ^O switch"
#: 详情视图头行的操作提示（全屏）：``Ctrl+O`` 下一个、``alt+0`` 回主视图。
DETAIL_HINT_TUI = "^O next · alt+0 main"

#: 单个 deck 最多占视口的多少分之一（两个 deck 合计 ≤ 半屏）
_DECK_SHARE = 4
#: 单个 deck 的绝对上限（与 ui/_deck 的分块上限叠加后仍不至于吃掉半屏）
MAX_DECK_ROWS = 10
#: 输入框之上/之下的固定占用：状态行 1 行 + 输入行 3 行（未长高时）
_CHROME_ROWS = 4
#: 两个 deck 各自的 margin（各 1 行，仅在该 deck 在场时占位）
_FRAME_GAPS = 2
#: 转录至少要留几行——状态层再重要也不能把会话流挤没
MIN_TRANSCRIPT_ROWS = 3


def deck_budget(height: int) -> int:
    """视口高度 → 单个 deck 的行预算（至少 1 行，至多 :data:`MAX_DECK_ROWS`）。

    两条约束取其小：

    - **比例**：``height // 4``——两个 deck 合计不超过半屏；
    - **减法**：``(height - 固定件 - 转录底线) // 2``——矮终端上按比例算仍会
      把转录挤到 1 行（12 行终端：4 + 4 的 deck 配 2 行间隙就吃光了），
      所以先把状态行、输入行、两条间隙与转录底线扣掉，剩下的才对半分。

    行内那边是纯减法（``height - _VIEWPORT_RESERVE - 5``），因为 Rich ``Live``
    区域是绝对行数、必须自己算死；全屏的转录是 ``1fr``，能自己让位——但
    "能无限让位"就等于"会被挤没"，所以这里也有一条底线。
    """
    height = int(height)
    room = height - _CHROME_ROWS - _FRAME_GAPS - MIN_TRANSCRIPT_ROWS
    share = min(room // 2, height // _DECK_SHARE)
    return max(1, min(MAX_DECK_ROWS, share))


class DeckSource:
    """四路 deck 数据 → 两段行（上半：输入框之上；下半：输入框之下）。

    所有 provider 调用一律 fail-open：坏 todos、fleet 快照里缺字段、插件
    收集器抛异常——状态层绝不拖垮主渲染（与行内 ``_deck_renderable`` 的
    回落哲学一致）。这也是为什么这里不 import ``Agent``：鸭子类型即可，
    测试可以直接喂字典。
    """

    def __init__(
        self,
        *,
        todos: Optional[Callable[[], Sequence[Any]]] = None,
        stale: Optional[Callable[[], bool]] = None,
        fleet: Optional[Any] = None,
        panels: Optional[Any] = None,
    ) -> None:
        #: ``() -> list[dict]``（``agent.todos`` 的只读投影）
        self._todos = todos
        #: ``() -> bool``（``agent.todos_stale``：清单是否已过期）。None →
        #: 不标注（旧调用方 / 演示）。独立一路见 ``services/streaming.py``
        #: 同名字段的说明：清单里的键会漏进系统提示，不能塞合成字段。
        self._stale = stale
        #: ``FleetMonitor``（``.snapshot()``）；None = 无舰队面板
        self._fleet = fleet
        #: ``UiPanelCollector``（``.panels()``）；None = 无插件面板
        self._panels = panels

    # ── 配置 ────────────────────────────────────────────────────

    def configure(
        self,
        *,
        todos: Optional[Callable[[], Sequence[Any]]] = None,
        stale: Optional[Callable[[], bool]] = None,
        fleet: Optional[Any] = None,
        panels: Optional[Any] = None,
    ) -> None:
        """接线/换线（每个回合 ``TuiStream`` 构造时调一次）。"""
        self._todos = todos
        self._stale = stale
        self._fleet = fleet
        self._panels = panels

    @property
    def attached(self) -> bool:
        """是否有任何一路数据源（全 None = 空闲期、还没跑过回合）。"""
        return (
            self._todos is not None
            or self._stale is not None
            or self._fleet is not None
            or self._panels is not None
        )

    # ── 取数（全部 fail-open）───────────────────────────────────

    def _todos_snapshot(self) -> list[Any]:
        if self._todos is None:
            return []
        try:
            return list(self._todos())
        except Exception:
            return []

    def _stale_snapshot(self) -> bool:
        """清单是否已过期。取不到就当新鲜——缺个标注好过整块消失。"""
        if self._stale is None:
            return False
        try:
            return bool(self._stale())
        except Exception:
            return False

    def _fleet_snapshot(self) -> list[dict]:
        if self._fleet is None:
            return []
        try:
            return list(self._fleet.snapshot())
        except Exception:
            return []

    def fleet_snapshot(self) -> list[dict]:
        """舰队快照的公开面（``Ctrl+O`` 导航与详情视图复用同一份取数）。"""
        return self._fleet_snapshot()

    def _panel_list(self) -> list[Any]:
        if self._panels is None:
            return []
        try:
            return list(self._panels.panels())
        except Exception:
            return []

    # ── 行 ──────────────────────────────────────────────────────

    def rows(
        self,
        *,
        height: int,
        turn_active: bool,
        queue: Sequence[str] = (),
        marked: int = -1,
    ) -> tuple[list[Text], list[Text]]:
        """``(上半行, 下半行)``。空列表 = 该 deck 隐藏（不占行）。

        ``queue`` 由调用方给（app 从 ``console._input_queue`` 取）而不是
        ``DeckSource`` 自己持有：排队通道属于 ``Console``，这里不该再长一个
        对它的引用。``marked`` 同理——它是"当前在看哪个子代理"的界面状态
        （0 = 主视图），由 app 持有并经 ``fleet_lines`` 的 ``❯`` 标记回显。
        """
        budget = deck_budget(height)
        top = self._clamp(
            plan_queue_lines(
                self._todos_snapshot(), queue, budget=budget,
                stale=self._stale_snapshot()),
            budget)
        bottom: list[Text] = []
        if turn_active:
            bottom += fleet_lines(
                self._fleet_snapshot(), budget=budget, marked=marked,
                hints=FLEET_HINT_TUI)
        # 插件面板与 Agents 共用**下半**那一个预算：两段加起来不超预算，
        # 否则"每段各自守规矩"合起来仍能把转录挤没。
        bottom += plugin_lines(
            self._panel_list(), budget=max(0, budget - len(bottom)))
        return top, self._clamp(bottom, budget)

    @staticmethod
    def _clamp(rows: list[Text], budget: int) -> list[Text]:
        """把一段行硬裁到预算内（末行让位给一条 ``+N more``）。

        为什么还需要这一刀：插件面板的折叠行允许比预算多一行（行内
        ``_plugin_deck_renderable`` 的既有行为，测试钉住了 ``h == budget+1``）。
        行内能忍——它只影响"正文最少 5 行"的余量；全屏这边两个 deck 各有
        预算，多出来的那一行必须在这里收掉，否则"每段守规矩、合计超一截"。
        """
        if len(rows) <= budget:
            return rows
        if budget <= 0:
            return []
        kept = rows[: budget - 1]
        kept.append(deck_line(f"  +{len(rows) - len(kept)} more", "dim"))
        return kept


class DeckView(Static):
    """一段 deck 行（``Static`` 承载 Rich ``Group``）。空 → ``display: none``。

    三个承重细节：

    1. **高度显式写**：``styles.height = len(rows)``。deck 行是 ``no_wrap``
       的，一行恒等于一个终端行；把高度交给 auto-height 就是在赌框架的
       量测语义（``tui/prompt.py`` 已经因为同样的理由自己算高度）。
    2. **不增删子控件**：内容变化只 ``update()`` 同一个 ``Group``。
       ``SlashMenu`` 踩过这个坑——``remove_children()`` 之后、新控件
       mount 生效之前，容器会以空 visual 参与渲染（``render_strips``
       NoneType 崩溃，``run_test`` 下必现）。
    3. **不可聚焦**：点击 deck 绝不把焦点从输入行带走（同转录容器的纪律）。
    """

    can_focus = False

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(Text(""), **kwargs)
        #: 当前写入的行（测试与 ``rows_count`` 读它；``Static`` 的私有
        #: ``__content`` 不该从外面摸）
        self._rows: list[Text] = []
        #: 上一次写入的行签名（内容没变就别写控件：5Hz 定时器下大部分
        #: 帧都是同一份内容，无意义的 ``update()`` 会触发重排）
        self._signature: tuple[str, ...] = ()
        # 出厂即隐藏：没有数据时连自己那 1 行（含 margin）都不该占——
        # 静态 reserve 会白扔一行，正是行内 _DECK_SPINNER_GAP 记过的坑。
        self.display = False

    @property
    def rows_count(self) -> int:
        """当前占用行数（0 = 隐藏）。测试与预算断言用。"""
        return len(self._rows)

    def rows(self) -> list[Text]:
        """当前写入的行（只读副本；测试断言行内容用）。"""
        return list(self._rows)

    def set_rows(self, rows: Sequence[Text]) -> bool:
        """写入一段行。返回**是否发生了变化**（调用方据此决定是否跟滚动）。"""
        signature = tuple(row.plain for row in rows)
        if signature == self._signature:
            return False
        self._signature = signature
        self._rows = list(rows)
        if not rows:
            self.display = False
            self.styles.height = 0
            return True
        self.display = True
        self.styles.height = len(rows)
        self.update(Group(*rows))
        return True


class DetailView(VerticalScroll):
    """子代理详情视图：**占据转录的位置**，跑 `Ctrl+O` 时换上来。

    行内实现把详情塞进 ``Live`` 的正文槽（``_focus > 0`` 时不画易变正文）；
    全屏的等价物是把 ``#transcript`` 隐藏、这块顶上去——同一处位置、同样的
    "输入行与 deck 都还在"。

    两个承重细节：

    1. **头行与正文分开两个 ``Static``**：头行含转帧与耗时，每拍都在变；
       正文是子代理的捕获流，只在真有新行时变。合成一个控件就得按前者的
       节奏重渲后者（≤200 行 × 5Hz 是白烧布局），所以正文按 ``plain`` 签名
       去重（同 :class:`DeckView`）。
    2. **正文可折行**：它是子代理的原始输出，截断比折行更糟；高度交给框架
       （滚动容器里），不参与 deck 的"1 行 ≡ 1 终端行"算式——头行才是单行。

    与转录容器同样 ``can_focus = False``：点详情不该把焦点从输入行带走。
    """

    can_focus = False

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        #: 上一次写进正文的行签名（内容没变就别重渲）
        self._signature: tuple[str, ...] = ()
        #: 上一次写进头行的纯文本（诊断与测试；不摸 ``Static`` 的私有内容）
        self._head_plain = ""
        #: 当前显示的视图编号（None = 没在显示）
        self._view_id: Optional[int] = None
        self.display = False   # 出厂隐藏：没选子代理时不占一行

    def compose(self) -> ComposeResult:
        # 两个子控件在 compose 期一次性建好，运行期只 update、绝不增删——
        # SlashMenu 踩过的坑：容器在"已 remove、未 mount"之间会以空 visual
        # 参与渲染（render_strips 崩）。两者也**永远有内容**（正文最少一行
        # "(no output yet)"），同一个坑的第二个保险。
        yield Static(Text(""), id="detail-head")
        yield Static(Text(""), id="detail-body")

    # ── 读（测试与诊断）─────────────────────────────────────────

    @property
    def showing(self) -> bool:
        return bool(self.display) and self._view_id is not None

    @property
    def view_id(self) -> Optional[int]:
        """当前显示的子代理编号（None = 主视图/未显示）。"""
        return self._view_id

    def head_text(self) -> str:
        """头行的纯文本（测试断言用）。"""
        return self._head_plain

    def body_text(self) -> list[str]:
        """正文行（纯文本）。测试断言内容用。"""
        return list(self._signature)

    # ── 写（唯一消费者是 app 的 deck 定时器）──────────────────────

    def show(self, view: dict, *, hint: str = DETAIL_HINT_TUI) -> bool:
        """显示某个子代理视图。返回**是否发生了可见变化**。

        头行每拍都写（耗时/转帧本就逐拍变）；正文按签名去重——子代理没输出
        新行就不碰它。
        """
        changed = not self.display or self._view_id != view.get("id")
        header = detail_header(view, hint=hint)
        self._head_plain = header.plain
        with contextlib.suppress(Exception):
            self.query_one("#detail-head", Static).update(header)
        rows = detail_body(view)
        signature = tuple(row.plain for row in rows)
        if signature != self._signature:
            self._signature = signature
            with contextlib.suppress(Exception):
                self.query_one("#detail-body", Static).update(Group(*rows))
            changed = True
        self._view_id = view.get("id")
        if not self.display:
            self.display = True
            changed = True
        return changed

    def hide(self) -> bool:
        """收起来（回主视图）。返回是否发生变化。"""
        if not self.display:
            return False
        self.display = False
        self._view_id = None
        self._signature = ()   # 下次进来必须重画（内容可能已变）
        return True


__all__ = [
    "DETAIL_HINT_TUI",
    "FLEET_HINT_TUI",
    "MAX_DECK_ROWS",
    "MIN_TRANSCRIPT_ROWS",
    "DeckSource",
    "DeckView",
    "DetailView",
    "deck_budget",
]
