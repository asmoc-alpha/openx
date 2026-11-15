"""斜杠命令菜单（``openx/tui/slash.py`` + ``PromptInput`` 键位路由）。

对齐行内 ptk 补全菜单（``ui/_components/editor.py``）的承重语义：

1. **匹配规则**：``/`` 起头且命令名无空白才出候选；前缀同命中主名与
   别名；大小写不敏感。
2. **键位**：↑/↓ 移光标、Tab 应用 + 空格、Enter 应用规范名并提交、
   Esc 只收菜单（无菜单时冒泡打断——那层在 test_streaming 的 Esc 契约里）。
3. **联动**：输入变化即时重渲；提交后菜单即收。
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual", reason="TUI 依赖 textual")

from openx.app.cli import commands as commands_mod
from openx.tui.app import OpenXTui
from openx.tui.slash import SlashMenu, slash_matches


def _menu(app) -> SlashMenu:
    return app.query_one("#slash-menu", SlashMenu)


async def _type(pilot, text: str) -> None:
    """逐字符打进输入行（走 Input.Changed → 菜单联动真路径）。"""
    for ch in text:
        await pilot.press(*ch)
    await pilot.pause()


# ── 1. 匹配规则（纯函数）──────────────────────────────────────────


class TestSlashMatches:
    def test_no_slash_no_candidates(self):
        assert slash_matches("hello") == []
        assert slash_matches("") == []

    def test_args_position_no_candidates(self):
        assert slash_matches("/model ") == []
        assert slash_matches("/mcp add") == []

    def test_bare_slash_lists_commands(self):
        matches = slash_matches("/")
        assert matches, "裸 / 应列出命令"
        names = [n for n, _, _ in matches]
        assert "assembly" in names  # 排序后首项（注册表稳定）
        assert all(" " not in name for name, _, _ in matches)
        # 截断上限：候选多于 _MAX_ROWS 时只展示前 N（行内菜单滚动位）
        assert len(matches) <= 10

    def test_prefix_filters_and_is_case_insensitive(self):
        assert any(n == "model" for n, _, _ in slash_matches("/mo"))
        assert any(n == "model" for n, _, _ in slash_matches("/MO"))

    def test_alias_hit_by_prefix(self):
        """/ex（quit 别名）前缀也应命中 quit 的候选。"""
        # 确认 quit 确有别名（否则找别的带别名命令验证）
        quit_aliases = [
            a for n, _, al in commands_mod.menu_entries() if n == "quit" for a in al
        ]
        if not quit_aliases:  # pragma: no cover - 注册表变了就换目标
            pytest.skip("quit 无别名，别名语义由注册表保证")
        assert any(n == "quit" for n, _, _ in slash_matches("/" + quit_aliases[0]))


# ── 2. 菜单弹出与键位（run_test + Pilot）───────────────────────────


class TestMenuInteraction:
    async def test_slash_pops_menu_with_candidates(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            await _type(pilot, "/")
            menu = _menu(app)
            assert menu.matches, "裸 / 应有候选"
            assert menu.has_class("visible")

    async def test_typing_filters_live(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            await _type(pilot, "/he")
            menu = _menu(app)
            assert [n for n, _, _ in menu.matches] == ["help"]

    async def test_arrow_moves_cursor(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            await _type(pilot, "/")
            menu = _menu(app)
            first = menu.cursor
            await pilot.press("down")
            await pilot.pause()
            assert menu.cursor == first + 1
            await pilot.press("up")
            await pilot.pause()
            assert menu.cursor == first

    async def test_tab_applies_with_trailing_space(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            await _type(pilot, "/he")
            await pilot.press("tab")
            await pilot.pause()
            prompt = app.query_one("#prompt")
            assert prompt.value == "/help "
            assert not _menu(app).matches, "应用后菜单应收起"

    async def test_enter_applies_canonical_and_submits(self):
        """/he + Enter → 提交 ``/help``（规范名，非原文）。"""
        app = OpenXTui()
        submitted: list[str] = []

        async def fake_ask():
            submitted.append(await app._input_q.get())

        async with app.run_test(size=(100, 30)) as pilot:
            task = asyncio.create_task(fake_ask())
            await _type(pilot, "/he")
            await pilot.press("enter")
            await pilot.pause()
            assert submitted == ["/help"]
            assert not _menu(app).matches
            task.cancel()

    async def test_esc_closes_menu_only(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            await _type(pilot, "/he")
            assert _menu(app).matches
            await pilot.press("escape")
            await pilot.pause()
            assert not _menu(app).matches
            assert not _menu(app).has_class("visible")
            # 输入行内容保留（Esc 只收菜单，不清输入）
            assert app.query_one("#prompt").value == "/he"

    async def test_backspace_to_non_slash_hides_menu(self):
        app = OpenXTui()
        async with app.run_test(size=(100, 30)) as pilot:
            await _type(pilot, "/h")
            assert _menu(app).matches
            await pilot.press("backspace", "backspace")
            await pilot.pause()
            assert not _menu(app).matches
