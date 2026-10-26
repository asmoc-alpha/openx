"""editor.py（prompt_toolkit 输入行）单元与键路测试。

覆盖：
- OpenXHistory：空行 / 相邻重复不入史、多行 roundtrip（隔离 HISTORY_PATH）；
- OpenXCompleter：过滤语义（/ 起头、命令名完整后关闭、前缀匹配主名与
  别名、命中别名提交规范名）；
- 键路（PipeInput 真按键）：菜单开 Enter=应用+提交、Tab=应用+空格、
  补全竞态回退原文提交、Esc 关菜单保输入、Alt+Enter / kitty Shift+Enter
  字面换行、Ctrl+D 空行 ""、Ctrl+C KeyboardInterrupt、Alt+R replay 回调、
  历史 ↑ 召回、反搜一次 Enter 提交；
- toolbar：两行、宽度受限、token 换算与 rich 侧逐字一致。

补全竞态：complete_while_typing 在后台线程算候选，键入与 Enter 间隔
需 >0（测试用 settle 睡眠模拟真人节奏）；原子喂键时菜单未及打开 →
原文提交，是合法降级路径（同样断言）。

运行：``python -m pytest tests/ui/test_editor.py -q``
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

import openx.ui._components.editor as editor
from openx.ui._components.editor import (
    OpenXCompleter,
    OpenXHistory,
    build_key_bindings,
    status_toolbar,
)

SETTLE = 0.2  # 补全线程落地窗口（模拟真人键入间隔）


class FakeConsole:
    _terminal_width = 80
    _mode = "auto"
    _input_tokens_view = 1500
    _output_tokens_view = 999

    def __init__(self):
        self.replays = []

    def _replay_toggle(self, text):
        self.replays.append(text)


@pytest.fixture
def isolated_history(monkeypatch, tmp_path):
    """隔离真实 ~/.openx/history——测试绝不触碰用户数据。"""
    path = tmp_path / "history"
    monkeypatch.setattr(editor, "HISTORY_PATH", path)
    return path


async def _run(keys, console=None, parts=False, settle=SETTLE,
               esc_settle=None):
    """PipeInput 驱动 read_input_async。``parts``=分段喂键（等补全落地）；
    ``esc_settle``=Escape 键后的专用间隔（默认同 settle）。"""
    console = console or FakeConsole()
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=DummyOutput()):
            task = asyncio.ensure_future(editor.read_input_async(console))
            await asyncio.sleep(0.05)
            if parts:
                for p in keys:
                    pipe.send_text(p)
                    wait = esc_settle if (
                        esc_settle is not None and p == "\x1b") else settle
                    await asyncio.sleep(wait)
            else:
                pipe.send_text(keys)
            text, session = await asyncio.wait_for(task, timeout=5)
    return console, text, session


# ── OpenXHistory ─────────────────────────────────────────────────


class TestOpenXHistory:
    def test_roundtrip_dedup_and_multiline(self, isolated_history):
        h = OpenXHistory(isolated_history)
        h.store_string("hello")
        h.store_string("   ")          # 空白 → 不入史
        h.store_string("hello")        # 相邻重复 → 不入史
        h.store_string("a\nb")         # 多行（FileHistory "+" 转义）
        h.store_string("hello")        # 不相邻 → 保留
        # load_history_strings 最新在前；get_strings() 惰性（无人开
        # prompt 前恒空），不能用于断言。
        loaded = list(OpenXHistory(isolated_history).load_history_strings())
        assert loaded == ["hello", "a\nb", "hello"], loaded

    def test_whitespace_only_line_not_stored(self, isolated_history):
        h = OpenXHistory(isolated_history)
        h.store_string("  \n\t")
        assert isolated_history.exists() is False or \
            list(OpenXHistory(isolated_history).load_history_strings()) == []


# ── OpenXCompleter ───────────────────────────────────────────────


def _names(text: str) -> list[str]:
    c = OpenXCompleter()
    return [x.text for x in c.get_completions(Document(text), None)]


class TestOpenXCompleter:
    def test_non_slash_no_candidates(self):
        assert _names("hello") == []
        assert _names("") == []

    def test_complete_command_name_closed(self):
        # 命令名已写完（含空白，正在写参数）→ 不出候选
        assert _names("/model g") == []

    def test_prefix_matches_menu_entries(self):
        from openx.app.cli.commands import menu_entries
        names = [n for n, _, _ in menu_entries()]
        assert _names("/") == names          # 裸 / → 全量候选
        assert _names("/he") == [n for n in names if n.startswith("he")]

    def test_unknown_prefix_no_candidates(self):
        assert _names("/zzzzz") == []

    def test_alias_matches_to_canonical(self):
        # 别名命中 → 提交文本是规范名（同旧版菜单语义）
        from openx.app.cli.commands import menu_entries
        names = {n for n, _, _ in menu_entries()}
        for text in _names("/ex"):
            assert text in names

    def test_meta_carries_description(self):
        c = OpenXCompleter()
        comps = list(c.get_completions(Document("/hel"), None))
        assert comps and all(c.display_meta for c in comps)


# ── 键位表组装 ───────────────────────────────────────────────────


class TestKeyBindingsTable:
    def test_custom_bindings_registered(self):
        from prompt_toolkit.keys import Keys
        kb = build_key_bindings(lambda text: None)
        keysets = [tuple(b.keys or ()) for b in kb.bindings]
        assert any(ks == (Keys.ControlM,) for ks in keysets)              # enter
        assert any(ks == (Keys.Tab,) for ks in keysets)                   # tab
        assert any(ks == (Keys.Escape, "r") for ks in keysets)            # alt+r
        assert any(ks == (Keys.Escape, Keys.ControlM) for ks in keysets)  # alt+enter

    def test_kitty_shift_enter_in_ansi_table(self):
        # 模块导入即注册（导入本模块即已生效）
        from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
        from prompt_toolkit.keys import Keys
        assert ANSI_SEQUENCES["\x1b[13;2u"] == (Keys.Escape, Keys.ControlM)


# ── 键路（PipeInput 真按键）──────────────────────────────────────


class TestKeyPaths:
    async def test_enter_with_menu_applies_and_submits(self, isolated_history):
        _, text, _ = await _run(["/he", "\r"], parts=True)
        assert text == "/help"

    async def test_tab_applies_with_trailing_space(self, isolated_history):
        _, text, _ = await _run(["/he", "\t", "\r"], parts=True)
        assert text == "/help "

    async def test_race_fallback_submits_raw(self, isolated_history):
        # 原子喂键：菜单未及打开（后台补全线程竞态）→ 原文提交
        _, text, _ = await _run("/he\r")
        assert text == "/he"

    async def test_escape_enter_pairing_is_alt_enter(self, isolated_history):
        # 无补全菜单时，\x1b 与 \r 相继到达（无论间隔）会被 KeyProcessor
        # 配成 (Escape, Enter) 对 = 我们的 Alt+Enter 绑定（插字面换行）——
        # 这正是 Alt+Enter 在真终端的到达形式。菜单开时 Esc 被 eager 绑定
        # 立即收菜单（见 editor._esc_closes_menu），配对不发生。
        _, text, _ = await _run(["a\x1b\r", "b\r"], parts=True)
        assert text == "a\nb"

    async def test_esc_closes_menu_then_enter_submits(self, isolated_history):
        # eager Esc：菜单开时 Esc 立即收菜单（不悬在 meta 配对等待里），
        # 紧随的 Enter 提交原文——旧版"Esc 关菜单保留输入"语义。
        _, text, _ = await _run(["/he", "\x1b\r"], parts=True)
        assert text == "/he"

    async def test_esc_aborts_reverse_search_restores_buffer(
        self, isolated_history,
    ):
        # 反搜中 Esc：abort 搜索、还原进入搜索前的缓冲（ptk 默认语义），
        # 随后 Enter 提交原缓冲。
        _, text, _ = await _run(["abc", "\x12xyz", "\x1b", "\r"],
                                parts=True, esc_settle=0.6)
        assert text == "abc"

    async def test_alt_enter_inserts_newline(self, isolated_history):
        _, text, _ = await _run(["a\x1b\r", "b\r"], parts=True)
        assert text == "a\nb"

    async def test_kitty_shift_enter_inserts_newline(self, isolated_history):
        _, text, _ = await _run(["a\x1b[13;2u", "b\r"], parts=True)
        assert text == "a\nb"

    async def test_ctrl_d_empty_returns_empty_string(self, isolated_history):
        _, text, _ = await _run("\x04")
        assert text == ""

    async def test_ctrl_c_raises_keyboard_interrupt(self, isolated_history):
        # 任务内逃逸的 KI 不经 await 点返回（asyncio 特殊处理）——与
        # interactive.py 同款手法：协程内捕获 KI 转哨兵异常，await 点可断言。
        class _KiProxy(Exception):
            pass

        console = FakeConsole()

        async def runner():
            try:
                await editor.read_input_async(console)
            except KeyboardInterrupt:
                raise _KiProxy()

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.ensure_future(runner())
                await asyncio.sleep(0.05)
                pipe.send_text("\x03")
                with pytest.raises(_KiProxy):
                    await asyncio.wait_for(task, timeout=5)

    async def test_plain_text_submits_verbatim(self, isolated_history):
        _, text, _ = await _run("fix the bug\r")
        assert text == "fix the bug"

    async def test_alt_r_fires_replay_callback(self, isolated_history):
        console = FakeConsole()
        _, text, _ = await _run(["hi", "\x1br", "\r"], console=console,
                                parts=True)
        assert console.replays == ["hi"]
        assert text == "hi"

    async def test_history_up_recalls_previous_line(self, isolated_history):
        console = FakeConsole()
        await _run("recalled-line\r", console=console)
        _, text, _ = await _run("\x1b[A\r", console=console)
        assert text == "recalled-line"

    async def test_reverse_search_one_enter_submits(self, isolated_history):
        console = FakeConsole()
        await _run("searchable-unique-line\r", console=console)
        # Ctrl+R → 输入查询词 → 一次 Enter 直提命中行（accept_search_and_
        # accept_input 绑定；ptk 默认要两次）
        _, text, _ = await _run("\x12searchable\r", console=console)
        assert text == "searchable-unique-line"


# ── status_toolbar ───────────────────────────────────────────────


class TestStatusToolbar:
    def test_two_lines_within_width(self):
        ft = status_toolbar(FakeConsole())
        rendered = ft() if callable(ft) else ft
        plain = "".join(t for _, t in rendered)
        lines = plain.split("\n")
        assert len(lines) == 2
        assert all(len(ln) <= 80 for ln in lines)
        assert lines[0] == "─" * 80
        assert "auto" in lines[1]

    def test_token_format_matches_rich_side(self):
        # 与 rich 路径（prompt._status_text）共用 fmt_tokens：逐字一致
        from openx.ui._helpers import fmt_tokens
        assert fmt_tokens(999) == "999"
        assert fmt_tokens(1500) == "1.5k"
        ft = status_toolbar(FakeConsole())
        rendered = ft() if callable(ft) else ft
        plain = "".join(t for _, t in rendered)
        assert "1.5k in" in plain and "999 out" in plain

    def test_unknown_mode_uses_warn_style(self):
        c = FakeConsole()
        c._mode = "weird"
        ft = status_toolbar(c)
        rendered = ft() if callable(ft) else ft
        styles = [s for s, _ in rendered]
        assert any("warn" in s for s in styles)
