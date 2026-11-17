"""输入行：多行编辑控件（``tui/prompt.py``）+ 历史存储（``tui/history.py``）。

替代 ``Input`` 的理由只有一条，但很硬：``Input`` 的文档模型里没有 ``\\n``，
多行粘贴会被压成一行、光标进不了第二行。换成 ``TextArea`` 之后每条键位语义
都是自己定的，所以每一条都得钉住——这些东西一旦回退，用户看到的是"敲不进去
字"或"回车没反应"，而单靠手测发现不了。

本文件覆盖四层：

1. **历史格式**（纯函数，无 textual）：与行内 ptk ``FileHistory`` 逐字兼容
   （``# 时间戳`` 头 + ``+`` 行前缀，多行条目天然可存）；空行/相邻重复不入史；
   失败静默降级；``↑``/``↓`` 的草稿语义。
2. **编辑控件**：``Enter`` 提交整段（含换行）、``Ctrl+J``/``Alt+Enter`` 换行、
   多行粘贴原样进入、高度随软折行增长并封顶、``↑``/``↓`` 只在首/末行翻历史
   （多行中间仍是光标移动）。
3. **空格分工**：菜单关时 ``Tab`` 走焦点链、``Esc`` 冒泡成打断——既不吞键，
   也不让绑定在无菜单时挡路。
4. **接线**：提交即清空 + 记历史、空行不记、相邻重复不记；``TextScreen``
   用的是同一个控件，多行 instructions 因此在一个弹窗里写完。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("textual", reason="TUI 依赖 textual")

from textual.widgets import Button                                   # noqa: E402

from openx.tui import history as history_mod                         # noqa: E402
from openx.tui.app import OpenXTui                                   # noqa: E402
from openx.tui.history import PromptHistory, append_entry, load_entries  # noqa: E402
from openx.tui.modals import TextScreen                              # noqa: E402
from openx.tui.prompt import MAX_ROWS, PromptEditor                   # noqa: E402


def _editor(app) -> PromptEditor:
    return app.query_one("#prompt", PromptEditor)


async def _type(pilot, text: str) -> None:
    for ch in text:
        await pilot.press(*ch)
    await pilot.pause()


# ── 1. 历史存储（与行内 ptk 共用一个文件）───────────────────────────


class TestHistoryStore:
    def test_shared_file_matches_inline_editor(self, monkeypatch):
        """行内与全屏共用 ``~/.openx/history``——同一份记忆，不是两份。

        conftest 的 autouse 夹具把模块常量指到了 tmp 隔离文件（连同本用例
        收到的 ``monkeypatch`` 是同一个实例，故可 ``undo()`` 还原），这里要
        比的正是**默认落点**。
        """
        from openx.ui._components.editor import HISTORY_PATH as inline_path

        monkeypatch.undo()
        assert history_mod.HISTORY_PATH == inline_path
        assert inline_path == Path.home() / ".openx" / "history"

    def test_missing_file_is_empty_not_an_error(self, tmp_path):
        assert load_entries(tmp_path / "nope" / "history") == []

    def test_roundtrip_single_and_multiline(self, tmp_path):
        target = tmp_path / "history"
        assert append_entry("hello", target) is True
        assert append_entry("line1\nline2", target) is True
        # 旧 → 新；多行条目是一条而不是两条
        assert load_entries(target) == ["hello", "line1\nline2"]

    def test_format_is_prompt_toolkit_compatible(self, tmp_path):
        """落盘格式必须是 ptk ``FileHistory`` 认得的那种（``+`` 前缀）。"""
        target = tmp_path / "history"
        append_entry("two\nlines", target)
        raw = target.read_text(encoding="utf-8")
        assert raw.startswith("\n# ")          # 条目前的 ``# <时间戳>`` 头
        assert "+two\n+lines\n" in raw

    def test_append_creates_parent_directory(self, tmp_path):
        target = tmp_path / "deep" / "dir" / "history"
        assert append_entry("x", target) is True
        assert load_entries(target) == ["x"]

    def test_append_failure_is_silent(self, tmp_path):
        """父路径是文件 → OSError；历史是锦上添花，绝不炸界面。"""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a dir")
        assert append_entry("x", blocker / "history") is False

    def test_reads_tolerate_garbage_and_old_formats(self, tmp_path):
        target = tmp_path / "history"
        target.write_bytes(
            b"\n# 2020-01-01\n+first\n\n# 2020-01-02\n+multi\n+line\n"
            b"garbage line\n+orphan\n"
        )
        # 认不出的行一律当分隔符：最多少一条，绝不抛异常
        assert load_entries(target) == ["first", "multi\nline", "orphan"]

    def test_broken_encoding_does_not_raise(self, tmp_path):
        target = tmp_path / "history"
        target.write_bytes(b"\n# x\n+\xff\xfe bad bytes\n")
        assert isinstance(load_entries(target), list)


class TestHistoryCursor:
    def _history(self, tmp_path, *entries: str) -> PromptHistory:
        history = PromptHistory(tmp_path / "history")
        for entry in entries:
            history.add(entry)
        return history

    def test_empty_and_whitespace_not_recorded(self, tmp_path):
        history = PromptHistory(tmp_path / "history")
        assert history.add("") is False
        assert history.add("   \n ") is False
        assert history.entries() == []

    def test_adjacent_duplicate_not_recorded(self, tmp_path):
        history = PromptHistory(tmp_path / "history")
        assert history.add("/help") is True
        assert history.add("/help") is False
        assert history.entries() == ["/help"]
        # 隔一条仍可重复（shell 同款：只有**相邻**重复被折叠）
        assert history.add("gpt-5") is True
        assert history.add("/help") is True
        assert history.entries() == ["/help", "gpt-5", "/help"]

    def test_entries_are_persisted_once_each(self, tmp_path):
        target = tmp_path / "history"
        history = PromptHistory(target)
        history.add("one")
        history.add("one")          # 重复不落盘
        history.add("two")
        assert load_entries(target) == ["one", "two"]

    def test_max_entries_trims_memory_not_file(self, tmp_path):
        target = tmp_path / "history"
        history = PromptHistory(target, max_entries=3)
        for i in range(5):
            history.add(f"cmd{i}")
        assert history.entries() == ["cmd2", "cmd3", "cmd4"]
        # 文件归用户：内存只保留最近 N 条，磁盘不截断
        assert load_entries(target) == [f"cmd{i}" for i in range(5)]

    def test_prev_walks_backwards_and_stops(self, tmp_path):
        history = self._history(tmp_path, "a", "b", "c")
        assert history.prev("") == "c"
        assert history.prev() == "b"
        assert history.prev() == "a"
        assert history.prev() is None      # 最旧一条不再循环

    def test_next_restores_draft_past_newest(self, tmp_path):
        history = self._history(tmp_path, "a", "b")
        assert history.prev("half-typed") == "b"
        assert history.prev() == "a"
        assert history.next() == "b"
        assert history.next() == "half-typed"   # 草稿还回去
        assert history.next() is None           # 已退出回放态

    def test_reset_drops_replay_state(self, tmp_path):
        history = self._history(tmp_path, "a", "b")
        history.prev("draft")
        history.reset()
        assert history.next() is None


# ── 2. 编辑控件（run_test + Pilot）─────────────────────────────────


class TestPromptEditorEditing:
    async def test_mounts_focused_with_frame_only(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = _editor(app)
            await pilot.pause()
            assert editor.has_focus
            # 内高 1 行 + 上下两条框线 = 3（行内的框同款）
            assert editor.size.height == 1
            assert editor.outer_size.height == 3

    async def test_ctrl_j_inserts_newline_and_grows_frame(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            await _type(pilot, "hi")
            editor = _editor(app)
            assert editor.size.height == 1

            await pilot.press("ctrl+j")
            await _type(pilot, "there")
            assert editor.value == "hi\nthere"
            assert editor.size.height == 2, "两行内容 → 框长到两行"
            assert editor.outer_size.height == 4, "内容两行 + 上下框线各一行"

    async def test_alt_enter_and_shift_enter_also_break_the_line(self):
        """``Alt+Enter`` 是终端最可靠的换行键（``ESC CR`` 可区分）；``Shift+Enter``
        只有 kitty 键盘协议等终端才真报——三个都接。"""
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            await _type(pilot, "a")
            await pilot.press("alt+enter")
            await _type(pilot, "b")
            await pilot.press("shift+enter")
            await _type(pilot, "c")
            assert _editor(app).value == "a\nb\nc"

    async def test_enter_submits_whole_multiline_content(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            _editor(app).load_text("first\nsecond")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert app._input_q.get_nowait() == "first\nsecond"
            assert _editor(app).value == "", "提交即清空（内容已作为回合重绘）"
            assert _editor(app).size.height == 1, "清空后框缩回一行"

    async def test_empty_enter_submits_nothing(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("enter")
            await pilot.pause()
            assert app._input_q.empty()
            assert app._history.entries() == []

    async def test_multiline_paste_arrives_intact(self):
        """``Input`` 做不到的那件事：多行粘贴不被压成一行。"""
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            from textual import events

            _editor(app).focus()
            await pilot.pause()
            app.post_message(events.Paste("a\nb\nc"))
            await pilot.pause()
            assert _editor(app).value == "a\nb\nc"
            assert _editor(app).size.height == 3

    async def test_height_follows_soft_wrap(self):
        """长单行按宽度软折行 —— 高度看的是折行后的行数，不是 ``\\n`` 数。"""
        app = OpenXTui()
        async with app.run_test(size=(60, 24)) as pilot:
            editor = _editor(app)
            editor.load_text("x" * 400)
            await pilot.pause()
            assert editor.wrapped_document.height > 1
            assert editor.size.height == editor.wrapped_document.height

    async def test_height_recomputes_after_terminal_resize(self):
        """窗口变窄 → 同一段文字折成更多行 → 框得跟着长。

        ``_on_resize`` 里推迟到下一帧算（要在 ``TextArea`` 重新折行之后取
        ``wrapped_document``），这条路径只有真 resize 才走得到。
        """
        app = OpenXTui()
        async with app.run_test(size=(120, 24)) as pilot:
            editor = _editor(app)
            editor.load_text("y" * 120)
            await pilot.pause()
            wide = editor.wrapped_document.height

            await pilot.resize_terminal(40, 24)
            await pilot.pause()
            assert editor.wrapped_document.height > wide
            assert editor.size.height == editor.wrapped_document.height

    async def test_height_is_capped(self):
        """超出上限后内部滚动：再高就把转录挤没了。"""
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = _editor(app)
            editor.load_text("\n".join(str(i) for i in range(30)))
            await pilot.pause()
            assert editor.size.height == MAX_ROWS
            assert editor.outer_size.height == MAX_ROWS + 2

    async def test_value_property_is_input_compatible(self):
        """``Input.value`` 的对位——其余代码只认这个面。"""
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = _editor(app)
            editor.value = "/help"
            await pilot.pause()
            assert editor.value == "/help"
            assert editor.text == "/help"
            editor.value = ""
            await pilot.pause()
            assert editor.value == ""

    async def test_placeholder_can_be_changed_at_runtime(self):
        """流式期改成"回车 = 排队"；空内容时 placeholder 才可见。"""
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            app.set_placeholder("Queue a message…")
            await pilot.pause()
            assert _editor(app).placeholder == "Queue a message…"


class TestPromptEditorHistory:
    async def test_up_recalls_newest_first(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            app._history.add("first")
            app._history.add("second")
            editor = _editor(app)
            await pilot.press("up")
            await pilot.pause()
            assert editor.value == "second"
            await pilot.press("up")
            await pilot.pause()
            assert editor.value == "first"
            # 最旧一条处再按 ↑ 不动（不循环），也不吞字
            await pilot.press("up")
            await pilot.pause()
            assert editor.value == "first"

    async def test_down_walks_forward_and_returns_the_draft(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            app._history.add("cmd")
            editor = _editor(app)
            await _type(pilot, "half")
            await pilot.press("up")
            await pilot.pause()
            assert editor.value == "cmd"
            await pilot.press("down")
            await pilot.pause()
            assert editor.value == "half", "翻历史翻了一半按 ↓ 不该吞掉刚打的字"

    async def test_arrows_move_the_caret_inside_multiline_text(self):
        """多行内容里方向键的第一含义仍是光标移动，历史只在边界接管。"""
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            app._history.add("history-entry")
            editor = _editor(app)
            editor.load_text("aa\nbb")
            editor.cursor_location = (1, 0)
            await pilot.pause()
            await pilot.press("up")
            await pilot.pause()
            assert editor.value == "aa\nbb"
            assert editor.cursor_location == (0, 0)

    async def test_empty_history_leaves_the_key_alone(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = _editor(app)
            await _type(pilot, "typed")
            await pilot.press("up")
            await pilot.pause()
            assert editor.value == "typed"

    async def test_editor_without_history_ignores_arrows(self):
        """弹窗里的输入行不接历史：一次性输入，翻历史只会让人莫名其妙。"""
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            app._history.add("main-entry")
            editor = PromptEditor(placeholder="type", id="readonly-hist")
            await app.mount(editor)
            editor.focus()
            await pilot.pause()
            await _type(pilot, "aa")
            await pilot.press("ctrl+j")
            await _type(pilot, "bb")
            await pilot.press("up")
            await pilot.pause()
            assert editor.value == "aa\nbb"       # 光标上移，没被历史顶替
            await pilot.press("down")
            await pilot.pause()
            assert editor.value == "aa\nbb"

    async def test_submitted_lines_are_recorded_and_persisted(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            await _type(pilot, "hi")
            await pilot.press("enter")
            await pilot.pause()
            assert app._history.entries() == ["hi"]
            assert load_entries(history_mod.HISTORY_PATH) == ["hi"]

    async def test_adjacent_duplicate_submission_not_recorded(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            for _ in range(3):
                await _type(pilot, "again")
                await pilot.press("enter")
                await pilot.pause()
            assert app._history.entries() == ["again"]


# ── 3. 键位分界：菜单关时 Tab 走焦点链、Esc 冒泡成打断 ────────────────


class _Focusable(Button):
    """占位可聚焦控件（只为把焦点链走出来）。"""


class TestKeyLayering:
    async def test_tab_walks_focus_when_menu_is_closed(self):
        app = OpenXTui()
        async with app.run_test(size=(80, 24)) as pilot:
            await app.mount(_Focusable("x", id="btn"))
            _editor(app).focus()
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            assert app.focused is not _editor(app)
            await pilot.press("shift+tab")
            await pilot.pause()
            assert app.focused is _editor(app)

    async def test_escape_bubbles_to_the_app_when_menu_is_closed(self):
        """输入行聚焦时 ``Esc`` 仍是打断——这是流式期唯一能按下它的地方。"""
        app = OpenXTui()
        calls: list[int] = []
        async with app.run_test(size=(80, 24)) as pilot:
            app.set_interrupt(lambda: calls.append(1))
            editor = _editor(app)
            await _type(pilot, "keep me")
            await pilot.press("escape")
            await pilot.pause()
            assert calls == [1], "菜单关时 Esc 必须冒泡到 app 的 interrupt"
            assert editor.value == "keep me", "Esc 不清输入内容"


# ── 4. 弹窗自由输入：同一个控件，多行一次写完 ────────────────────────


class TestTextScreenUsesPromptEditor:
    async def _open(self, app, pilot) -> tuple[PromptEditor, dict]:
        result: dict = {}
        app.push_screen(
            TextScreen("Skill instructions", placeholder="Type here…"),
            lambda value: result.setdefault("value", value),
        )
        await pilot.pause()
        return app.screen.query_one("#modal-input", PromptEditor), result

    async def test_modal_input_is_a_prompt_editor(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            editor, _ = await self._open(app, pilot)
            assert editor.has_focus
            assert editor._prompt_history is None

    async def test_multiline_text_confirmed_in_one_screen(self):
        """``/skill add`` 的 instructions：一个弹窗写完，不再一行一个弹窗。"""
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            editor, result = await self._open(app, pilot)
            await _type(pilot, "line1")
            await pilot.press("ctrl+j")
            await _type(pilot, "line2")
            await pilot.pause()
            assert editor.size.height == 2
            await pilot.press("enter")
            await pilot.pause()
            assert result["value"] == "line1\nline2"

    async def test_blank_input_still_means_unanswered(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            _, result = await self._open(app, pilot)
            await pilot.press("enter")
            await pilot.pause()
            assert result["value"] is None

    async def test_escape_cancels_with_none(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            _, result = await self._open(app, pilot)
            await _type(pilot, "discard")
            await pilot.press("escape")
            await pilot.pause()
            assert result["value"] is None
