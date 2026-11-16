"""阶段 0 spike：把"骨架能否承重"钉成红灯，而不是手测。

覆盖的四件事（迁移计划 §三 Stage 0 的验收项）：

1. **用户最初的需求**：thinking 块默认折叠、**点击标题栏展开**、
   聚焦后 Enter 也能切换（Textual ``CollapsibleTitle`` 内置行为）；
2. **Rich 兼容**：``Static`` 吃本仓的 Markdown/数学/语法高亮产物；
3. **测试策略**：``App.run_test()`` + ``Pilot`` 能直接断言 widget 状态
   （取代行内实现的 pyte 屏幕级断言）；
4. **两条缝**：``TuiSink`` 的投递→挂载链路，以及 ``Console.attach_tui``
   的换入/还原（迁移期逃生门）。

依赖 textual≥8 与 rich≥14.2（textual 的声明要求）。textual 缺失时整文件
skip——默认（行内）路径不该因缺少可选依赖而红。
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual", reason="--tui 依赖 textual")

from textual.widgets import Input                                      # noqa: E402

from openx.config import OpenXConfig                                   # noqa: E402
from openx.tui.app import OpenXTui
from openx.tui.blocks import ThinkingBlock, ToolBlock, markdown_answer
from openx.tui.sink import TuiSink
from openx.ui.console import Console


def _title_widget(block):
    """Collapsible 的标题栏（``CollapsibleTitle`` 是私有类型，按位置取）。"""
    return block.children[0]


# ── 1. 点击展开（用户最初的需求）────────────────────────────────────


class TestCollapsibleClick:
    async def test_click_title_expands_and_collapses(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            block = ThinkingBlock()
            app.write_widget(block)
            await pilot.pause()

            assert block.collapsed is True, "默认必须折叠"
            await pilot.click(type(_title_widget(block)))
            await pilot.pause()
            assert block.collapsed is False, "点击标题栏应展开"

            await pilot.click(type(_title_widget(block)))
            await pilot.pause()
            assert block.collapsed is True, "再点一次应收起"

    async def test_keyboard_enter_toggles_focused_title(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            block = ToolBlock("grep", "pattern=x")
            app.write_widget(block)
            await pilot.pause()

            _title_widget(block).focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert block.collapsed is False

    async def test_ctrl_t_toggles_every_block(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            blocks = [ThinkingBlock(), ToolBlock("read_file"), ToolBlock("grep")]
            for block in blocks:
                app.write_widget(block)
            await pilot.pause()

            await pilot.press("ctrl+t")
            await pilot.pause()
            assert all(b.collapsed is False for b in blocks)

            await pilot.press("ctrl+t")
            await pilot.pause()
            assert all(b.collapsed is True for b in blocks)


# ── 1b. 点击不抢焦点：任何点击之后输入行都得照常收键 ────────────────


class TestClickKeepsInputFocus:
    """点击转录 / 块标题后输入行必须照常收键（回归：点一下就打不了字）。

    Textual 会把焦点交给被点的可聚焦控件——``VerticalScroll`` 的转录容器
    与 ``CollapsibleTitle`` 都可聚焦。焦点一旦离开输入行，字符、Enter、
    ``/quit`` 全部失效，界面对用户表现为"无法输入"。滚轮滚动与点击展开
    本就不需要焦点（事件按指针位置分发），所以正确语义是焦点永远留在
    输入行；模态屏有自己的焦点语义，不在本类覆盖范围（见 test_dialogs）。
    """

    async def test_transcript_container_is_not_focusable(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 24)):
            transcript = app.query_one("#transcript")
            assert transcript.can_focus is False, (
                "转录容器不可聚焦——可聚焦时点一下转录就抢走输入行的焦点"
            )

    async def test_click_transcript_keeps_input_focus_and_typing(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            app.write_widget(ThinkingBlock())
            await pilot.pause()
            prompt = app.query_one("#prompt", Input)
            assert prompt.has_focus

            await pilot.click("#transcript", offset=(10, 3))
            await pilot.pause()
            assert app.focused is prompt, "点击转录不得移动焦点"

            await pilot.press("h", "i")
            await pilot.pause()
            assert prompt.value == "hi", "点击后按键必须照常到达输入行"

    async def test_click_block_title_returns_focus_to_input(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            block = ThinkingBlock()
            app.write_widget(block)
            await pilot.pause()
            prompt = app.query_one("#prompt", Input)

            await pilot.click(type(_title_widget(block)))
            await pilot.pause()
            assert app.focused is prompt, "点开块之后焦点必须回到输入行"
            await pilot.press("h", "i")
            await pilot.pause()
            assert prompt.value == "hi"


# ── 2. 块内容语义：thinking 纯文本、答案走 Markdown ────────────────


class TestBlocks:
    async def test_thinking_feed_and_freeze(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            block = ThinkingBlock()
            app.write_widget(block)
            block.feed("let me ")
            block.feed("think")
            await pilot.pause()

            assert block.text == "let me think"
            # 纯文本进 body（绝不走 Markdown——围栏/反引号会被重排乱）
            assert block.body.visual.plain == "let me think"
            assert "Thinking" in block.title

            block.freeze(3.4)
            await pilot.pause()
            assert "Thought for" in block.title
            assert block.frozen is True

    async def test_thinking_stays_collapsible_after_freeze(self):
        """行内实现冻结即固化进 scrollback（此后 Ctrl+R 永久失效）；
        widget 树里冻结后照样能点开——这正是迁移换来的能力。"""
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            block = ThinkingBlock()
            app.write_widget(block)
            block.feed("deep thought")
            block.freeze(1.0)
            await pilot.pause()

            await pilot.click(type(_title_widget(block)))
            await pilot.pause()
            assert block.collapsed is False
            assert block.body.visual.plain == "deep thought"

    async def test_tool_block_result_and_error(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            ok = ToolBlock("read_file", "a.py")
            bad = ToolBlock("bash", "exit 1")
            app.write_widget(ok)
            app.write_widget(bad)
            ok.set_result("line 1\nline 2")
            bad.set_result("boom", is_error=True)
            await pilot.pause()

            assert ok.status == "done" and bad.status == "error"
            assert "boom" in bad.body.visual.plain
            assert "read_file" in ok.title and "a.py" in ok.title


# ── 3. Rich 兼容：Static 吃本仓的渲染产物 ──────────────────────────


class TestRichCompat:
    async def test_markdown_with_math_renders_in_static(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            app.write(markdown_answer("**bold** and $x^2$ and `code`"))
            await pilot.pause()
            transcript = app.query_one("#transcript")
            entries = list(transcript.query(".entry"))
            assert len(entries) == 1
            # render_math 已把 LaTeX 转成 Unicode 近似（Markdown 保留源 markup）
            assert "x²" in entries[0].source.markup

    async def test_sink_measure_width_follows_app(self):
        console = Console(config=OpenXConfig(workspace="/tmp"))
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            console.attach_tui(app)
            await pilot.pause()
            assert console._console.width == app.size.width
            assert console._console.height == app.size.height


# ── 4. 两条缝：输出投递 & 输入接管 ─────────────────────────────────


class TestSeams:
    async def test_sink_print_lands_in_transcript(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            console = Console(config=OpenXConfig(workspace="/tmp"))
            console.attach_tui(app)
            console.print_info("hello from the sink")
            await pilot.pause()
            await pilot.pause()  # 排空定时器（1/30s）需第二次泵

            entries = list(app.query_one("#transcript").query(".entry"))
            assert entries, "sink 投递的 renderable 必须出现在转录里"
            assert any("hello from the sink" in str(e.source)
                       for e in entries)

    async def test_attach_detach_restores_console(self):
        console = Console(config=OpenXConfig(workspace="/tmp"))
        original = console._console
        app = OpenXTui()
        async with app.run_test(size=(80, 24)):
            console.attach_tui(app)
            assert isinstance(console._console, TuiSink)
            assert console.raw is console._console   # raw 也走 sink
            console.detach_tui()
            assert console._console is original
            assert console._tui is None

    async def test_ask_returns_submitted_text(self):
        """输入缝：``print_user_prompt`` 在 TUI 下 = ``app.ask()``。"""
        import asyncio

        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            task = asyncio.create_task(app.ask())
            await pilot.pause()
            await pilot.press("h", "i")
            await pilot.press("enter")
            assert await asyncio.wait_for(task, 2) == "hi"

    async def test_quit_wakes_ask_with_none(self):
        """Ctrl+Q 让 ``ask()`` 返回 None——复用行内 REPL 的 EOF 退出路径。"""
        import asyncio

        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            task = asyncio.create_task(app.ask())
            await pilot.pause()
            await pilot.press("ctrl+q")
            assert await asyncio.wait_for(task, 2) is None


# ── 5. 演示脚本：阶段 0 的自证入口 ────────────────────────────────


class TestDemo:
    async def test_demo_mounts_and_feeds_blocks(self):
        app = OpenXTui(demo=True)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause(0.6)
            assert list(app.query(ThinkingBlock)), "演示应产出 thinking 块"

    def test_parse_args_interface_flags(self):
        """入口翻转后的旗标语义：默认 TUI，--inline 逃生门，--tui 兼容别名。

        parse_args 只管解析；"非 TTY 回落行内"的守卫在 main() 里（运行期
        判 tty，单测里改由 test_main_guard* 系列锚定行为语义）。
        """
        from openx.main import parse_args

        # 兼容别名仍在（旧脚本不破）
        assert parse_args(["--tui"]).tui is True
        assert parse_args(["--tui"]).inline is False
        # 逃生门显式选行内
        args = parse_args(["--inline"])
        assert args.inline is True and args.tui is False
        # 默认：两个标志都不亮 → 派发层走 TUI（not args.inline）
        args = parse_args([])
        assert args.inline is False and args.tui is False

    async def test_status_line_shows_label_and_timer(self):
        """状态行：标签 + 耗时同屏（替代行内 spinner 行的转帧语义）。

        ``Static.update`` 需要活跃 app（Textual 的 visualize 要取 console），
        所以必须在 ``run_test`` 里驱动——这也正是 Stage 2 的调用语境。
        """
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            line = app.query_one("#spinner")
            app.set_busy("Thinking…")
            await pilot.pause()
            assert isinstance(line.visual, object)
            text = line.visual.plain
            assert "Thinking" in text and "esc to interrupt" in text

            app.set_busy(None)
            await pilot.pause()
            assert line.visual.plain.strip() == ""
