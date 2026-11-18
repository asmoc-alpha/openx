"""状态层（deck）面板：``ui/_deck.py`` 的行格式 + ``tui/deck.py`` 的承载与接线。

行内把 Plan / Queue / Agents / 插件面板一帧一构塞进 Rich ``Live``；全屏把它们
做成常驻 widget。两个界面**共用同一份行格式**（``ui/_deck.py``），本文件钉住
三件事：

1. **行语义**（纯函数，不需要 app）：计划三态与全完成折叠、队列 FIFO、舰队
   条目与运行中活动行、插件面板的 ``svg:`` 跳过与坏 markup 单面板兜底、共享
   预算的裁剪顺序、provider 抛异常时的 fail-open。
2. **承载**（``DeckView``）：空 → 隐藏且不占行；非空 → 高度恰为行数（deck 行
   恒 no_wrap，"一行 ≡ 一行"是两端几何的共同前提）；点击不夺输入行焦点。
3. **接线与生命周期**：``TuiStream.start()`` 后四块出现，回合结束 Agents 退场
   而 Plan 与插件面板留存，``todos`` 整体替换下一拍可见，``fleet`` 每轮归零。

**不赌定时器相位**：deck 的 5Hz 定时器保留在生产路径，测试用 :func:`_pump`
手动跑"排空 + 一拍 deck"，与行内 ``test_fleet_view`` 的 ``deterministic_live``
同一套思路。
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual", reason="TUI 依赖 textual")

from openx.tui.app import OpenXTui                                     # noqa: E402
from openx.tui.deck import (                                           # noqa: E402
    MAX_DECK_ROWS,
    DeckSource,
    DeckView,
    DetailView,
    deck_budget,
)
from openx.tui.prompt import PromptEditor                              # noqa: E402
from openx.tui.stream import TuiStream                                 # noqa: E402
from openx.ui._deck import (                                           # noqa: E402
    DECK_FLEET_ROWS,
    DECK_PLAN_ROWS,
    DECK_QUEUE_ROWS,
    detail_body,
    detail_header,
    detail_lines,
    fleet_lines,
    plan_queue_lines,
    plugin_lines,
)

# ── 夹具：不用真 Agent，鸭子类型足够 ────────────────────────────────


class _Fleet:
    """``FleetMonitor`` 的最小替身（``snapshot()`` / ``reset()``）。"""

    def __init__(self, views: list[dict] | None = None) -> None:
        self.views = list(views or [])
        self.resets = 0

    def snapshot(self) -> list[dict]:
        return list(self.views)

    def reset(self) -> None:
        self.resets += 1
        self.views = []


class _Panels:
    """``UiPanelCollector`` 的最小替身（``panels()``）。"""

    def __init__(self, panels: list[tuple[str, str, list[str]]] | None = None):
        self._panels = list(panels or [])
        self.calls = 0

    def panels(self) -> list[tuple[str, str, list[str]]]:
        self.calls += 1
        return list(self._panels)


def _view(
    idx: int,
    *,
    status: str = "running",
    last_tool: str = "",
    lines: list[str] | None = None,
    pending: str = "",
) -> dict:
    return {
        "id": idx,
        "label": f"sub {idx}",
        "status": status,
        "tools_count": 2,
        "elapsed": 3.4,
        "last_tool": last_tool,
        "lines": list(lines if lines is not None else [f"line {idx}"]),
        "pending": pending,
    }


def _planes(texts: list) -> list[str]:
    """行列表 → 纯文本行（断言只关心内容，不关心样式对象）。"""
    return [row.plain for row in texts]


def _pump(app) -> None:
    """跑一拍"排空 + deck"（测试里不依赖定时器相位）。"""
    app._drain()
    app._tick_decks()


# ── 1. 预算 ────────────────────────────────────────────────────────


class TestDeckBudget:
    def test_quarter_of_viewport_on_normal_screens(self):
        assert deck_budget(24) == 6     # 24//4 = 6，减法给 7 → 取比例
        assert deck_budget(32) == 8     # 32//4 = 8，减法给 11 → 取比例

    def test_capped_for_tall_terminals(self):
        assert deck_budget(80) == MAX_DECK_ROWS

    def test_short_terminal_reserves_transcript_rows(self):
        """矮终端上按比例算会把转录挤到 1 行——预留底线后只有 1 行预算。"""
        assert deck_budget(12) == 1
        # 12 行：deck 1+1 + 间隙 2 + 状态行 1 + 输入行 3 = 8 → 转录 4 行
        assert 12 - 2 * deck_budget(12) - 6 >= 3

    def test_never_zero(self):
        """再矮的终端也留 1 行预算——"没地方"由裁剪逻辑处理，不是干脆不画。"""
        assert deck_budget(0) == 1
        assert deck_budget(3) == 1


# ── 2. 行格式（ui/_deck.py，纯函数）─────────────────────────────────


class TestPlanQueueLines:
    def test_three_states_and_header(self):
        todos = [
            {"content": "写测试", "status": "completed"},
            {"content": "接面板", "status": "in_progress",
             "activeForm": "接面板中"},
            {"content": "更新文档", "status": "pending"},
        ]
        lines = _planes(plan_queue_lines(todos, [], budget=10))
        assert lines[0].strip() == "Plan 1/3"
        assert lines[1].startswith("  ✓ 写测试")
        # 进行中显示 activeForm（"正在做什么"），不是 content
        assert lines[2].strip().endswith("接面板中")
        assert "接面板中" in lines[2]
        assert lines[3].startswith("  ○ 更新文档")

    def test_all_done_collapses_to_one_line(self):
        todos = [{"content": f"t{i}", "status": "completed"} for i in range(5)]
        lines = _planes(plan_queue_lines(todos, [], budget=10))
        assert len(lines) == 1
        assert lines[0].strip() == "✓ Plan 5/5"

    def test_plan_overflow_folds(self):
        todos = [{"content": f"t{i}", "status": "pending"} for i in range(12)]
        lines = _planes(plan_queue_lines(todos, [], budget=DECK_PLAN_ROWS + 2))
        assert len(lines) <= DECK_PLAN_ROWS + 2
        assert lines[-1].startswith("  +")
        assert lines[-1].endswith(" more")

    def test_queue_is_fifo_with_count(self):
        lines = _planes(plan_queue_lines([], ["先这个", "再那个"], budget=10))
        assert lines[0].strip() == "Queue (2)"
        assert "先这个" in lines[1]
        assert "再那个" in lines[2]

    def test_queue_overflow_folds(self):
        queue = [f"m{i}" for i in range(9)]
        lines = _planes(plan_queue_lines([], queue, budget=20))
        assert len(lines) == 1 + DECK_QUEUE_ROWS + 1
        assert lines[-1].strip() == f"+{9 - DECK_QUEUE_ROWS} more"

    def test_plan_and_queue_share_one_budget(self):
        """两块**共享**预算：合计行数绝不超（否则叠加后仍会撑爆视口）。"""
        todos = [{"content": f"t{i}", "status": "pending"} for i in range(6)]
        queue = [f"m{i}" for i in range(6)]
        for budget in (3, 4, 6, 10):
            assert len(plan_queue_lines(todos, queue, budget=budget)) <= budget

    def test_larger_block_shrinks_before_the_smaller_one(self):
        """超预算时先裁较大的块（信息密度更低的那块让位）。

        这条钉的是行内既有几何：预算 5、6 条计划 + 2 条队列时，被砍的是
        计划条目（队列那条仍然在场）。算法本身与行内 ``_deck_renderable``
        逐字一致——不在这里"优化"排版语义。
        """
        todos = [{"content": f"t{i}", "status": "pending"} for i in range(6)]
        queue = ["a", "b"]
        lines = _planes(plan_queue_lines(todos, queue, budget=5))
        assert len(lines) <= 5
        assert any("▸ a" in line for line in lines), "队列条目应优先保住"
        assert sum(1 for line in lines if line.startswith("  ○")) < 6

    def test_budget_zero_or_empty_data_renders_nothing(self):
        assert plan_queue_lines([{"content": "x", "status": "pending"}], [],
                                budget=0) == []
        assert plan_queue_lines([], [], budget=10) == []

    def test_ignores_non_dict_todos(self):
        lines = plan_queue_lines(["垃圾", None, {"content": "ok",
                                                "status": "pending"}],
                                 [], budget=10)
        assert len([ln for ln in _planes(lines) if "ok" in ln]) == 1


class TestFleetLines:
    def test_header_main_entry_and_subagent(self):
        lines = _planes(fleet_lines([_view(1)], budget=10))
        assert lines[0].strip().startswith("Agents (1)")
        assert lines[1].startswith(" 0 main")
        assert "sub 1" in lines[2]
        assert "2 tools" in lines[2]

    def test_hints_are_per_surface(self):
        """头行那句提示按界面传：行内是 ↓/↵，全屏是 ^O——都不能空口承诺按键。"""
        header = _planes(fleet_lines([_view(1)], budget=10))[0]
        assert "↓ select" in header
        assert "↓ select" not in _planes(
            fleet_lines([_view(1)], budget=10, hints=False))[0]
        custom = _planes(fleet_lines([_view(1)], budget=10,
                                     hints="· ^O switch"))[0]
        assert "^O switch" in custom and "↓ select" not in custom

    def test_running_agent_gets_activity_line(self):
        running = _view(1, status="running", last_tool="read_file(a.py)")
        lines = _planes(fleet_lines([running], budget=10))
        assert any("⎿" in ln and "read_file(a.py)" in ln for ln in lines)

    def test_finished_agent_has_no_activity_line(self):
        done = _view(1, status="done", last_tool="stale")
        lines = _planes(fleet_lines([done], budget=10))
        assert not any("⎿" in ln for ln in lines)

    def test_overflow_folds_and_respects_row_cap(self):
        views = [_view(i, status="done") for i in range(1, 8)]
        lines = _planes(fleet_lines(views, budget=20))
        assert f"Agents ({len(views)})" in lines[0]
        assert lines[-1].strip() == f"+{len(views) - DECK_FLEET_ROWS} more"

    def test_activity_lines_count_toward_budget(self):
        """活动行也是行：计入预算，不能"每行守规矩、合计撑爆"。"""
        views = [_view(i, status="running", last_tool="grep(x)")
                 for i in range(1, 6)]
        for budget in (3, 5, 8):
            assert len(fleet_lines(views, budget=budget)) <= budget

    def test_label_brackets_are_escaped(self):
        view = _view(1)
        view["label"] = "[bold]危险[/bold]"
        lines = _planes(fleet_lines([view], budget=10))
        assert "[bold]" in lines[2], "方括号应原样显示，而不是被当成 markup"

    def test_empty_snapshot_renders_nothing(self):
        assert fleet_lines([], budget=10) == []


class TestDetailLines:
    """子代理详情视图的行（``detail_header`` / ``detail_body``）——两端共用。"""

    def test_header_carries_identity_state_and_keys(self):
        view = _view(2, status="running")
        header = detail_header(view, hint="^O next").plain
        assert "Agent 2: sub 2" in header
        assert "running" in header
        assert "2 tools" in header
        assert "3s" in header                      # fmt_duration(3.4)
        assert "^O next" in header
        assert header.isprintable()

    def test_hint_is_per_surface(self):
        """默认提示是行内那串（`alt+0 back` 被行内测试钉着）。"""
        view = _view(1)
        assert "alt+0 back" in detail_header(view).plain
        assert "alt+0 main" in detail_header(view, hint="^O next · alt+0 main").plain

    def test_header_is_single_line(self):
        """头行是 deck 行：no_wrap，长标签靠 ellipsis 收——不折行。"""
        view = _view(1)
        view["label"] = "长" * 200
        assert detail_header(view).no_wrap is True

    def test_body_windows_to_budget(self):
        view = _view(1, lines=[f"l{i}" for i in range(10)])
        assert [r.plain for r in detail_body(view, budget=3)] == ["l7", "l8", "l9"]

    def test_body_without_budget_gives_everything(self):
        """全屏界面不裁正文：它住在滚动容器里，能翻就别替用户裁。"""
        view = _view(1, lines=[f"l{i}" for i in range(250)])
        assert len(detail_body(view)) == 250

    def test_pending_tail_is_shown(self):
        """未遇换行的尾片段也要可见，否则正在写的那一行要等下一个 \\n。"""
        view = _view(1, lines=["done line"], pending="still typing")
        assert [r.plain for r in detail_body(view)] == ["done line", "still typing"]

    def test_empty_body_says_no_output_yet(self):
        assert [r.plain for r in detail_body({})] == ["  (no output yet)"]
        # 预算为 0 也不至于抛（行内 max(3, …) 保底，但别让负数成为地雷）
        assert [r.plain for r in detail_body(_view(1), budget=0)] == \
            ["  (no output yet)"]

    def test_bad_markup_degrades_to_plain_text(self):
        """子代理的 raw token 会带着裸 `[` 进来——一行坏标签不该让整屏消失。"""
        view = _view(1, lines=["[dim]● grep[/dim]", "见 [TODO] 清单"])
        rows = detail_body(view)
        assert rows[0].plain == "● grep"            # 好 markup 照常解析
        assert rows[1].plain == "见 [TODO] 清单"     # 坏的回落纯文本，不抛

    def test_detail_lines_is_header_plus_body(self):
        view = _view(1, lines=["a", "b"])
        rows = _planes(detail_lines(view, budget=1))
        assert rows[0].startswith(" Agent 1: sub 1")
        assert rows[1:] == ["b"]


class TestPluginLines:
    def test_lines_pass_through_with_markup(self):
        panels = [("pet", "p1", ["[dim](^_^)[/dim] pet"])]
        assert _planes(plugin_lines(panels, budget=8)) == ["(^_^) pet"]

    def test_svg_lines_are_skipped(self):
        """终端渲染不了 SVG（插件应自带 ASCII 回退行）。"""
        panels = [("pet", "p1", ["svg:pet.svg", "ASCII pet"])]
        assert _planes(plugin_lines(panels, budget=8)) == ["ASCII pet"]

    def test_bad_markup_drops_only_that_panel(self):
        panels = [
            ("bad", "p1", ["[/bold]stray close"]),
            ("good", "p2", ["ok"]),
        ]
        assert _planes(plugin_lines(panels, budget=8)) == ["ok"]

    def test_overflow_folds(self):
        panels = [("clock", "p1", [f"tick{i}" for i in range(12)])]
        lines = _planes(plugin_lines(panels, budget=5))
        assert len(lines) == 6
        assert lines[-1].strip() == "+7 more"

    def test_no_panels_renders_nothing(self):
        assert plugin_lines([], budget=8) == []


# ── 3. DeckSource：四路取数 + 分半 + fail-open ──────────────────────


class TestDeckSource:
    def _source(self, *, todos=None, fleet=None, panels=None) -> DeckSource:
        return DeckSource(todos=todos, fleet=fleet, panels=panels)

    def test_not_attached_renders_nothing(self):
        source = self._source()
        assert source.attached is False
        assert source.rows(height=30, turn_active=True) == ([], [])

    def test_top_holds_plan_and_queue(self):
        source = self._source(
            todos=lambda: [{"content": "x", "status": "in_progress"}])
        top, bottom = source.rows(height=30, turn_active=False, queue=["later"])
        assert any("Plan" in line.plain for line in top)
        assert any("Queue" in line.plain for line in top)
        assert bottom == [], "计划与队列在输入框之上，不该出现在下半"

    def test_agents_only_while_turn_active(self):
        fleet = _Fleet([_view(1)])
        source = self._source(fleet=fleet)
        _top, during = source.rows(height=30, turn_active=True)
        _top, idle = source.rows(height=30, turn_active=False)
        assert any("Agents" in line.plain for line in during)
        assert idle == []

    def test_plugin_panels_persist_when_idle(self):
        """监控件/桌面宠物按定义常驻——回合结束不该把它撤掉。"""
        source = self._source(panels=_Panels([("pet", "p1", ["pet"])]))
        _top, idle = source.rows(height=30, turn_active=False)
        assert _planes(idle) == ["pet"]

    def test_agents_and_panels_share_the_lower_budget(self):
        fleet = _Fleet([_view(i, status="running", last_tool="t")
                        for i in range(1, 7)])
        panels = _Panels([("big", "p1", [f"l{i}" for i in range(20)])])
        source = self._source(fleet=fleet, panels=panels)
        _top, bottom = source.rows(height=24, turn_active=True)
        assert len(bottom) <= deck_budget(24)

    def test_provider_failures_degrade_to_empty(self):
        def boom():
            raise RuntimeError("坏 todos")

        class BadFleet:
            def snapshot(self):
                raise RuntimeError("坏快照")

        class BadPanels:
            def panels(self):
                raise RuntimeError("坏收集器")

        source = self._source(todos=boom, fleet=BadFleet(), panels=BadPanels())
        assert source.rows(height=30, turn_active=True) == ([], [])

    def test_configure_rewires(self):
        source = self._source()
        assert source.attached is False
        source.configure(panels=_Panels([("pet", "p1", ["hi"])]))
        assert source.attached is True
        assert _planes(source.rows(height=30, turn_active=False)[1]) == ["hi"]
        source.configure()
        assert source.attached is False


# ── 4. DeckView：承载与不变量 ──────────────────────────────────────


class TestDeckView:
    async def test_hidden_until_it_has_rows(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            for selector in ("#plan-deck", "#agents-deck"):
                view = app.query_one(selector, DeckView)
                assert view.display is False, "空 deck 连 1 行都不该占"
                assert view.size.height == 0

    async def test_height_equals_row_count(self):
        """1 deck 行 ≡ 1 终端行：高度显式等于行数（不赌 auto-height）。"""
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            view = app.query_one("#plan-deck", DeckView)
            app.attach_decks(todos=lambda: [
                {"content": f"task {i}", "status": "pending"} for i in range(3)
            ])
            await pilot.pause()
            assert view.display is True
            assert view.rows_count > 0
            assert view.size.height == view.rows_count

    async def test_long_lines_never_wrap(self):
        """行内 no_wrap + ellipsis：折行会同时破坏两端的高度记账。"""
        app = OpenXTui()
        async with app.run_test(size=(60, 24)) as pilot:
            view = app.query_one("#plan-deck", DeckView)
            app.attach_decks(todos=lambda: [
                {"content": "长" * 400, "status": "pending"}])
            await pilot.pause()
            rows = view.rows()
            assert len(rows) == view.size.height
            assert all(row.no_wrap for row in rows)

    async def test_rerender_is_skipped_when_nothing_changed(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 24)):
            view = app.query_one("#plan-deck", DeckView)
            app.attach_decks(todos=lambda: [{"content": "x",
                                             "status": "pending"}])
            before = view.rows()
            assert app.query_one("#plan-deck", DeckView).set_rows(before) is False

    async def test_click_on_deck_keeps_input_focus(self):
        """deck 不可聚焦：点它之后照样打字（与转录容器同一条纪律）。"""
        app = OpenXTui()
        async with app.run_test(size=(80, 30)) as pilot:
            app.attach_decks(todos=lambda: [{"content": "x",
                                             "status": "in_progress"}])
            await pilot.pause()
            await pilot.click("#plan-deck")
            await pilot.pause()
            assert isinstance(app.focused, PromptEditor)


class TestFleetNavigation:
    """``Ctrl+O`` / ``Alt+0..9``：主回答 ⇄ 子代理详情（顶掉转录，同行内语义）。"""

    async def _nav(self, app, pilot, views) -> tuple[_Fleet, TuiStream]:
        """起一个真回合的接线：``start()`` 会 reset 舰队，登记必须在它之后。"""
        console = type("_C", (), {"_input_queue": []})()
        app.attach_console(console)
        fleet = _Fleet([])
        stream = TuiStream(console, app=app, fleet=fleet)
        stream.start()
        fleet.views = list(views)
        _pump(app)
        await pilot.pause()
        return fleet, stream

    async def test_ctrl_o_opens_detail_and_hides_transcript(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 30)) as pilot:
            await self._nav(app, pilot, [_view(1, lines=["● grep", "找到了"])])
            await pilot.press("ctrl+o")
            await pilot.pause()
            detail = app.query_one("#detail", DetailView)
            assert app._fleet_focus == 1
            assert detail.showing is True
            assert app.query_one("#transcript").display is False
            assert "Agent 1: sub 1" in detail.head_text()
            assert "^O next" in detail.head_text()    # 全屏的键位提示
            assert detail.body_text() == ["● grep", "找到了"]

    async def test_ctrl_o_cycles_through_agents_and_back(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 30)) as pilot:
            await self._nav(app, pilot, [_view(1), _view(2)])
            seen = []
            for _ in range(3):
                await pilot.press("ctrl+o")
                await pilot.pause()
                seen.append(app._fleet_focus)
            assert seen == [1, 2, 0], "主回答 → 1 → 2 → 主回答"
            detail = app.query_one("#detail", DetailView)
            assert detail.showing is False
            assert app.query_one("#transcript").display is True

    async def test_alt_digit_jumps_directly(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 30)) as pilot:
            await self._nav(app, pilot, [_view(1), _view(2, status="done")])
            await pilot.press("alt+2")
            await pilot.pause()
            assert app._fleet_focus == 2
            assert "Agent 2: sub 2" in app.query_one("#detail", DetailView).head_text()
            await pilot.press("alt+0")
            await pilot.pause()
            assert app._fleet_focus == 0
            assert app.query_one("#transcript").display is True

    async def test_alt_digit_out_of_range_clamps(self):
        """越界钳到最后一个：按键必须总有响应（同行内）。"""
        app = OpenXTui()
        async with app.run_test(size=(80, 30)) as pilot:
            await self._nav(app, pilot, [_view(1)])
            app.action_fleet_select(7)
            await pilot.pause()
            assert app._fleet_focus == 1

    async def test_deck_marks_the_view_you_are_in(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 30)) as pilot:
            await self._nav(app, pilot, [_view(1), _view(2)])
            await pilot.press("ctrl+o")
            await pilot.pause()
            rows = _planes(app.query_one("#agents-deck", DeckView).rows())
            assert any(r.startswith("❯1") for r in rows), rows
            assert any(r.startswith(" 0 main") for r in rows), "主条目不再被标记"

    async def test_ctrl_o_without_agents_is_a_noop(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 30)) as pilot:
            await self._nav(app, pilot, [])
            await pilot.press("ctrl+o")
            await pilot.pause()
            assert app._fleet_focus == 0
            assert app.query_one("#detail", DetailView).showing is False

    async def test_focus_resets_on_new_turn(self):
        """新回合的干净起点：绝不还停在上一轮某个子代理的详情上。"""
        app = OpenXTui()
        async with app.run_test(size=(80, 30)) as pilot:
            _fleet, _stream = await self._nav(app, pilot, [_view(1)])
            await pilot.press("ctrl+o")
            await pilot.pause()
            assert app._fleet_focus == 1

            app.attach_decks(fleet=_Fleet([]))   # = 下一回合的 start()
            await pilot.pause()
            assert app._fleet_focus == 0
            assert app.query_one("#detail", DetailView).showing is False
            assert app.query_one("#transcript").display is True

    async def test_empty_fleet_clamps_focus_back_to_main(self):
        """舰队被清空（reset）而焦点还在子代理上 → 回主视图，不留悬空详情。"""
        app = OpenXTui()
        async with app.run_test(size=(80, 30)) as pilot:
            fleet, _stream = await self._nav(app, pilot, [_view(1)])
            await pilot.press("ctrl+o")
            await pilot.pause()
            assert app.query_one("#detail", DetailView).showing is True

            fleet.views = []
            app._tick_decks()
            await pilot.pause()
            assert app._fleet_focus == 0
            assert app.query_one("#detail", DetailView).showing is False
            assert app.query_one("#transcript").display is True

    async def test_body_rewrites_only_on_new_output(self):
        """头行逐拍刷新（耗时/转帧），正文按签名去重——不白烧布局。"""
        app = OpenXTui()
        async with app.run_test(size=(80, 30)) as pilot:
            fleet, _stream = await self._nav(app, pilot, [_view(1, lines=["a"])])
            await pilot.press("ctrl+o")
            await pilot.pause()
            assert app._sync_detail() is False, "内容没变：不该重写正文"

            fleet.views[0]["lines"] = ["a", "b"]
            assert app._sync_detail() is True
            assert app.query_one("#detail", DetailView).body_text() == ["a", "b"]

    async def test_header_follows_elapsed_time(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 30)) as pilot:
            fleet, _stream = await self._nav(app, pilot, [_view(1)])
            await pilot.press("ctrl+o")
            await pilot.pause()
            fleet.views[0]["elapsed"] = 42.0
            app._tick_decks()
            await pilot.pause()
            assert "42s" in app.query_one("#detail", DetailView).head_text()

    async def test_click_on_detail_keeps_input_focus(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 30)) as pilot:
            await self._nav(app, pilot, [_view(1, lines=["x"])])
            await pilot.press("ctrl+o")
            await pilot.pause()
            await pilot.click("#detail")
            await pilot.pause()
            assert isinstance(app.focused, PromptEditor)
            # 点击不该把详情关掉（它不是"点外面返回"的浮层）
            assert app.query_one("#detail", DetailView).showing is True

    async def test_detail_leaves_room_on_short_terminal(self):
        """12 行终端上详情接管转录的位置，两个 deck 仍守预算、不炸布局。"""
        app = OpenXTui()
        async with app.run_test(size=(60, 12)) as pilot:
            todos = [{"content": f"t{i}", "status": "pending"} for i in range(9)]
            console = type("_C", (), {"_input_queue": []})()
            app.attach_console(console)
            fleet = _Fleet([])
            stream = TuiStream(console, app=app, todos_provider=lambda: todos,
                               fleet=fleet)
            stream.start()
            fleet.views = [_view(i, last_tool="t") for i in range(1, 9)]
            _pump(app)
            await pilot.pause()
            await pilot.press("ctrl+o")
            await pilot.pause()
            detail = app.query_one("#detail", DetailView)
            assert detail.showing is True
            assert detail.size.height >= 1, "详情接管了转录的空间"
            assert app.query_one("#transcript").size.height == 0
            assert app.query_one("#plan-deck", DeckView).size.height \
                <= deck_budget(12)
            assert app.query_one("#agents-deck", DeckView).size.height \
                <= deck_budget(12)
            assert app.query_one("#prompt").size.height >= 1


# ── 5. 接线：布局顺序、回合生命周期、TuiStream ──────────────────────


def _make_stream(app, *, console=None, todos=None, fleet=None, panels=None):
    console = console or type("_Console", (), {"_input_queue": []})()
    app.attach_console(console)
    return TuiStream(
        console, app=app,
        todos_provider=todos,
        fleet=fleet,
        panels=panels,
    )


class TestDeckLayout:
    async def test_compose_order(self):
        """计划/队列在输入框**之上**、Agents 与插件面板在**之下**（同行内）。"""
        app = OpenXTui()
        async with app.run_test(size=(80, 30)):
            order = [w.id for w in app.screen.children if getattr(w, "id", None)]
            assert order.index("plan-deck") < order.index("spinner")
            assert order.index("spinner") < order.index("prompt")
            assert order.index("prompt") < order.index("agents-deck")

    async def test_decks_do_not_starve_the_transcript(self):
        """矮终端上两个 deck 都被预算压住，转录仍有可见行数。"""
        app = OpenXTui()
        async with app.run_test(size=(60, 12)) as pilot:
            fleet = _Fleet([_view(i, status="running", last_tool="t")
                            for i in range(1, 9)])
            todos = [{"content": f"t{i}", "status": "pending"} for i in range(9)]
            stream = _make_stream(app, todos=lambda: todos, fleet=fleet,
                                  panels=_Panels([("p", "x",
                                                   [f"l{i}" for i in range(9)])]))
            stream.start()
            _pump(app)
            await pilot.pause()
            top = app.query_one("#plan-deck", DeckView)
            bottom = app.query_one("#agents-deck", DeckView)
            assert top.size.height <= deck_budget(12)
            assert bottom.size.height <= deck_budget(12)
            assert app.query_one("#transcript").size.height >= 3


class TestDeckLifecycle:
    async def test_start_renders_all_four_decks(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 30)) as pilot:
            fleet = _Fleet()
            todos = [{"content": "接 deck", "status": "in_progress",
                      "activeForm": "接 deck"}]
            stream = _make_stream(
                app, console=type("_C", (), {"_input_queue": ["排队中"]})(),
                todos=lambda: todos, fleet=fleet,
                panels=_Panels([("pet", "p1", ["pet 在"] )]))
            fleet.views = [_view(1, status="running", last_tool="grep(x)")]
            stream.start()
            _pump(app)
            await pilot.pause()

            top = _planes(app.query_one("#plan-deck", DeckView).rows())
            bottom = _planes(app.query_one("#agents-deck", DeckView).rows())
            assert any("Plan 0/1" in line for line in top)
            assert any("Queue (1)" in line for line in top)
            assert any("Agents (1)" in line for line in bottom)
            assert any("pet 在" in line for line in bottom)

    async def test_turn_end_hides_agents_but_keeps_plan(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 30)) as pilot:
            fleet = _Fleet([_view(1, status="done")])
            todos = [{"content": "留着", "status": "in_progress"}]
            stream = _make_stream(app, todos=lambda: todos, fleet=fleet,
                                  panels=_Panels([("pet", "p1", ["pet"])]))
            stream.start()
            _pump(app)
            await pilot.pause()
            assert app.query_one("#agents-deck", DeckView).rows_count > 0

            stream.feed("答案")
            stream.done()
            _pump(app)
            await pilot.pause()
            bottom = _planes(app.query_one("#agents-deck", DeckView).rows())
            assert bottom == ["pet"], "Agents 退场，插件面板留存"
            assert app.query_one("#plan-deck", DeckView).rows_count > 0

    async def test_todos_mutation_shows_up_next_tick(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 30)) as pilot:
            todos = [{"content": "a", "status": "pending"}]
            app.attach_decks(todos=lambda: todos)
            await pilot.pause()
            assert "Plan 0/1" in app.query_one("#plan-deck", DeckView).rows()[0].plain

            # 整体替换（ToolHost 共享同一 list，切片即原子）
            todos[:] = [{"content": "a", "status": "completed"},
                        {"content": "b", "status": "in_progress"}]
            app._tick_decks()
            await pilot.pause()
            assert "Plan 1/2" in app.query_one("#plan-deck", DeckView).rows()[0].plain

    async def test_fleet_is_reset_on_each_turn(self):
        """构造即新一轮：上一轮的子代理不能赖在面板上。"""
        app = OpenXTui()
        async with app.run_test(size=(80, 30)):
            fleet = _Fleet([_view(1, status="done")])
            _make_stream(app, fleet=fleet)
            assert fleet.resets == 1
            assert fleet.views == []

    async def test_closing_detaches(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 30)) as pilot:
            app.attach_decks(todos=lambda: [{"content": "x",
                                             "status": "pending"}])
            await pilot.pause()
            assert app._decks.attached is True
            app._close()
            assert app._decks.attached is False
            # 收摊后迟到的定时器拍子是 no-op（不写已拆除的控件树）
            app._tick_decks()

    async def test_no_source_means_no_decoration(self):
        """还没跑过回合：两 deck 隐藏，屏幕与阶段 3 完全一致。"""
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            _pump(app)
            await pilot.pause()
            assert app.query_one("#plan-deck", DeckView).rows_count == 0
            assert app.query_one("#agents-deck", DeckView).rows_count == 0
