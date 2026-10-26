"""prompt_toolkit 输入行编辑器 —— REPL 等待态输入行的全部键路。

分层纪律：本模块只拥有「输入行及其下方区域」——补全浮层（ptk 的
CompletionsMenu 是 Float）与底框线/状态行（bottom_toolbar，ptk 自带
``~is_done`` 过滤，提交后随输入行一起消失）。顶框线与一切对话内容仍由
prompt.py 的 chrome 层打印。**不可**把框线/状态静态打印在输入行下方：
浮层覆盖后不会还原（ptk 擦除区只覆盖自身 region）——这是底 chrome 必须
交给 ptk 管的原因（实测结论，勿回退）。

ptk 依赖只在 TTY 分支被惰性导入（print_user_prompt 内）——非 TTY /
脚本化调用路径与 openx.ui.console 的顶层导入完全不触碰 ptk。

导入即扩展 ptk 的 ANSI 序列表（kitty Shift+Enter）：ptk 3.0.52 的
ANSI_SEQUENCES 无 ``"\\x1b[13;2u"``，kitty 终端下 Shift+Enter 会以未定义
序列抵达 → 实测提交出乱码行。映射为 ``(Escape, ControlM)`` 后与
``("escape", "enter")`` 绑定汇合 → 插入字面 ``\\n``。不支持的终端不发该
序列 → 行为同旧版（Enter 提交）。本模块只启 kitty 协议（``\\x1b[=1u``，
disambiguate 模式：普通字符仍是裸字节，仅修饰键组合变 CSI-u），**不启**
modifyOtherKeys——ptk 把 ``ESC[27;2;13~`` 映射成 c-m（提交），Shift+Enter
会错变成提交。
"""

from __future__ import annotations

# ── 独立调试支持：允许直接运行本文件（python openx/.../xxx.py）──────
if __name__ == "__main__" and not __package__:
    import sys as _sys
    from pathlib import Path as _Path
    _file = _Path(__file__).resolve()
    _root = _file.parent
    while _root != _root.parent and not (_root / "pyproject.toml").exists():
        _root = _root.parent
    _sys.path.insert(0, str(_root))
    __package__ = ".".join(_file.relative_to(_root).parts[:-1])

import sys
from pathlib import Path
from typing import Callable

from prompt_toolkit import search as search_state
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import has_completions, is_searching
from prompt_toolkit.formatted_text import AnyFormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.shortcuts import CompleteStyle, PromptSession
from prompt_toolkit.styles import Style as PtkStyle

from .._helpers import fmt_tokens
from .._style import ACCENT, DIM

# kitty 键盘协议（disambiguate 模式）下的 Shift+Enter。模块导入即注册：
# 晚于首次按键注册就来不及了（序列解析发生在输入线程读字节时）。
ANSI_SEQUENCES["\x1b[13;2u"] = (Keys.Escape, Keys.ControlM)

HISTORY_PATH = Path.home() / ".openx" / "history"
# 测试 monkeypatch 此模块级常量以隔离真实用户数据
# （同 orchestration/sessions.SESSIONS_DIR 约定）。

_KITTY_ON = "\x1b[=1u"
_KITTY_OFF = "\x1b[<u"

# ptk Style：rich 侧调色板的 ANSI 对应。框线 #585858 = 256 色 240 号
# （与 prompt._RULE_ANSI / rich grey35 同一色）。补全菜单沿用 ptk 默认
# 反白选中，只给 meta 补 dim。bottom-toolbar 必须显式 noreverse——ptk
# 默认给工具条窗格 reverse（反色高亮带），底框线/状态行会渲染成一条
# "阴影"横带（用户报告）。
_PTK_STYLE = PtkStyle.from_dict({
    "prompt": "fg:ansicyan",
    "rule": "#585858",
    "dim": "#585858",
    "accent": "fg:ansicyan",
    "warn": "fg:ansiyellow",
    "bottom-toolbar": "noreverse",
    "bottom-toolbar.text": "noreverse",
    "completion-menu.completion": "bg:default fg:default",
    "completion-menu.completion.current": "reverse",
    "completion-menu.meta.completion": "#585858",
    "completion-menu.meta.completion.current": "#585858",
})


class OpenXHistory(FileHistory):
    """空行与相邻重复不入史；多行条目走 FileHistory 原生 ``+`` 转义格式。

    历史文件的加载/保存全部继承 FileHistory（其 store_string 已处理多行
    的 ``+`` 前缀转义，无需自写序列化）——这里只过滤不该入史的行。
    相邻去重**读盘**而非 get_strings()：ptk 的加载是惰性的（没人开
    prompt 前恒空），get_strings() 在 store 时刻不可信。
    """

    def store_string(self, string: str) -> None:
        stripped = string.strip()
        if not stripped:
            return
        latest = next(iter(self.load_history_strings()), None)
        if latest == stripped:
            return
        super().store_string(stripped)


class OpenXCompleter(Completer):
    """斜杠命令补全。数据源 ``cli.commands.menu_entries()``。

    仅整行以 ``/`` 起头且命令名未含空白时出候选（同旧版 _slash_menu）；
    前缀同时匹配主名与别名；命中别名时提交文本仍是规范名（``/ex`` +
    Enter 提交 ``/quit``，同旧版）。命令数据经 cli.commands 注册表取用
    ——**延迟导入**断开 ui→cli 顶层环（cli.interactive 顶层导入
    ui.console，顶层互导会在 ui.console 未完成初始化时炸）。
    """

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        body = text[1:]
        if any(ch.isspace() for ch in body):
            return  # 命令名已完整（正在写参数）→ 不出候选
        from ...app.cli.commands import menu_entries
        q = body.lower()
        for name, desc, aliases in menu_entries():
            if name.startswith(q) or any(a.startswith(q) for a in aliases):
                meta = f" ({','.join(aliases)})" if aliases else ""
                yield Completion(
                    name,
                    start_position=-len(body),
                    display=name,
                    display_meta=desc + meta,
                )


def build_key_bindings(
    on_replay: Callable[[str], None],
) -> KeyBindings:
    """等待态自定义键位。``on_replay(text)`` = thinking 展开/收起（Alt+R）。

    自定义绑定注册在默认绑定之后合并（_MergedKeyBindings → KeyProcessor
    取 matches[-1]）——后注册者赢，故能覆盖 ptk 默认语义。
    """
    kb = KeyBindings()

    @kb.add("enter", filter=has_completions)
    def _enter_with_menu(event):  # noqa: ANN202
        # 旧版菜单打开时 Enter = 提交所选命令（默认选中首项）。ptk 默认在
        # 此场景提交原文（实测 "/he" 原样提交、候选不应用），且
        # complete_while_typing 的自动补全**不预选**首项——未选中时先选
        # 首项再应用。菜单未及打开（后台补全线程竞态，快速连击 Enter）
        # 时 filter 不命中 → 原文提交，与旧版"无候选直接提交"一致。
        b = event.current_buffer
        st = b.complete_state
        if st and st.completions:
            if st.current_completion is None:
                b.go_to_completion(0)
            b.apply_completion(b.complete_state.current_completion)
        b.complete_state = None  # 收菜单再提交：done 渲染残行消除
        b.validate_and_handle()

    @kb.add("tab", filter=has_completions)
    def _tab_with_menu(event):  # noqa: ANN202
        # 旧版 Tab = 应用 + 尾随空格（"/help "，参数位起编）。同上：未
        # 预选时先选首项。
        b = event.current_buffer
        st = b.complete_state
        if st and st.completions:
            if st.current_completion is None:
                b.go_to_completion(0)
            b.apply_completion(b.complete_state.current_completion)
        b.complete_state = None
        b.insert_text(" ")

    @kb.add("enter", filter=is_searching)
    async def _enter_while_searching(event):  # noqa: ANN202
        # bash 语义：反搜中一次 Enter 直接提交命中行（ptk 默认要两次，
        # 第一次只接受搜索结果）。ptk 导出的同名 accept_search_and_
        # accept_input 是 @key_binding 的 Binding 包装（**非可调用**，
        # 直接调用 TypeError 被键处理器吞掉 → 表现为"按了没反应"）——
        # 照抄其实现两行。
        search_state.accept_search()
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "r")
    async def _alt_r_replay(event):  # noqa: ANN202
        # thinking 展开/收起（旧 Ctrl+R 迁此）。run_in_terminal：ptk 先
        # 擦掉自身 region 并把光标挪到 region 顶（col 0），on_replay 里
        # 的就地重印（裸光标移动）在 cooked 态下写屏，返回后 ptk 重绘
        # 输入行。游标数学见 prompt._replay_toggle 的 erase-top 注释。
        doc = event.current_buffer.document
        await run_in_terminal(lambda: on_replay(doc.text))

    @kb.add("escape", filter=has_completions, eager=True)
    def _esc_closes_menu(event):  # noqa: ANN202
        # 收补全菜单（旧版 Esc 语义：有菜单关菜单）。**必须 eager**：
        # escape 是 meta 前缀、此场景下 KeyProcessor 对它无定时 flush
        # （PipeInput 实测 1.2s 不冲刷）——不 eager 则 Esc 永远悬在配对
        # 等待里，与下一键配成 meta 组合，菜单收不到。仅菜单开时接管
        # （filter）：菜单关时 escape 仍是 meta 前缀，Alt+R 与 emacs
        # meta 词操作键位不受影响。
        event.current_buffer.complete_state = None

    @kb.add("escape", "enter")
    def _alt_enter(event):  # noqa: ANN202
        # Alt+Enter（及经 ANSI_SEQUENCES 汇合的 kitty Shift+Enter）→ 字面
        # 换行。multiline=False 下 Enter 仍是提交，与旧版语义一致。
        event.current_buffer.insert_text("\n")

    return kb


def status_toolbar(console) -> AnyFormattedText:
    """bottom_toolbar 回调：底框线 + 状态行（两行）。

    视觉即旧框的第 3/4 行（``───`` / ``  mode · i in · o out``）。数字
    换算与 rich 路径（prompt._status_text）共用 fmt_tokens——两侧必须
    逐字一致，否则流式帧→提示框切换时状态行跳变。单行截断到终端宽：
    toolbar 折行会撑高框、破坏「恒四行」不变量。
    """
    tw = console._terminal_width

    def _toolbar() -> AnyFormattedText:
        mode = console._mode
        mode_cls = "class:accent" if mode in ("auto", "plan", "manual") \
            else "class:warn"
        i_tok = fmt_tokens(console._input_tokens_view)
        o_tok = fmt_tokens(console._output_tokens_view)
        prefix = f"  {mode}"
        # 全 ASCII（模式名 + 数字），len 即列宽；截断到 tw-1 防回折撑高框
        status = f"{prefix}  ·  {i_tok} in  ·  {o_tok} out"
        if len(status) > tw - 1:
            status = status[: tw - 1]
        body = status[len(prefix):] if len(status) > len(prefix) else ""
        return [
            ("class:rule", "─" * tw),
            ("", "\n"),
            (mode_cls, prefix),
            ("class:dim", body),
        ]

    return _toolbar


def _constrain_buffer_window(session: PromptSession) -> None:
    """把 buffer 窗口高度钉到**内容行数**（折行计，首行含 "❯ " 前缀）。

    flow 模式（非全屏）下 ptk 的 buffer 窗口默认 ``Dimension()``——
    weight=1，在 HSplit 里吸干整屏高度：输入内容顶在原位、bottom_toolbar
    （底框线+状态行）被推到**屏幕最底**，中间巨幅空白——框只剩一行线
    （用户报告"行内对话框只展示一行"根因）。改为内容驱动：窗口恰容纳
    当前缓冲（CJK 宽字符按 2 格折行计，空行至少 1 行），toolbar 紧贴
    输入框，紧凑四行框回归。Window.height 支持callable（ptk 自家即传
    ``self._get_default_buffer_control_height``）——每次布局时重算，
    输入增删/折行变化自动跟随。
    """
    from prompt_toolkit.layout.controls import BufferControl
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.utils import get_cwidth

    def _height() -> Dimension:
        try:
            size = session.output.get_size()
            tw = size.columns or 80
            screen_h = size.rows or 24
        except Exception:
            tw, screen_h = 80, 24
        rows = 0
        for i, ln in enumerate(session.default_buffer.document.lines):
            cells = get_cwidth(ln) + (2 if i == 0 else 0)
            rows += max(1, (cells + tw - 1) // tw)
        # 补全菜单开时为浮层让位：COLUMN 浮层一候选一行，flow 模式下
        # Float 不能越出布局高度——不预留则整个菜单被裁掉（实测）。窗口
        # 随菜单长高、toolbar 随之下移（视觉同旧版"菜单在框下"）；收起
        # 即回紧凑四行框。行数受屏高约束（toolbar 2 行 + 底部余量 1）。
        st = session.default_buffer.complete_state
        if st is not None and st.completions:
            menu_max = max(1, screen_h - rows - 3)
            rows += min(len(st.completions), menu_max)
        return Dimension(min=max(1, rows), max=max(1, rows))

    # Layout 不按名暴露 buffer 窗口——按内容匹配（默认缓冲的
    # BufferControl；反搜的 SearchBufferControl 是另一个 buffer，排除）。
    for w in session.app.layout.find_all_windows():
        ctrl = getattr(w, "content", None)
        if isinstance(ctrl, BufferControl) \
                and ctrl.buffer is session.default_buffer:
            w.height = _height
            break


async def read_input_async(console) -> tuple[str, PromptSession]:
    """TTY 专属读行。返回 ``(提交文本, session)``——session 供调用方
    （prompt.print_user_prompt）提交后做 ``renderer.erase()`` 清框。

    Ctrl+D 空行 → ``""``（重提示，不退出——prompt.py:301 旧语义不回退）；
    Ctrl+C → KeyboardInterrupt 原样上抛（interactive 层 goodbye）。
    """
    session = PromptSession(
        history=OpenXHistory(HISTORY_PATH),
        completer=OpenXCompleter(),
        complete_while_typing=True,
        complete_style=CompleteStyle.COLUMN,
        # 关键：0 否则 ptk 常驻预留 8+ 空行给菜单（破坏紧凑四行框）；
        # 菜单以纯浮层出现。
        reserve_space_for_menu=0,
        multiline=False,
        wrap_lines=True,
        message=[("class:prompt", "❯ ")],
        bottom_toolbar=status_toolbar(console),
        key_bindings=build_key_bindings(
            lambda text: console._replay_toggle(text)
        ),
        style=_PTK_STYLE,
        enable_suspend=False,
        enable_open_in_editor=False,
        enable_system_prompt=False,
    )
    _constrain_buffer_window(session)
    out = sys.stdout
    tty_out = out.isatty()
    try:
        if tty_out:
            try:
                out.write(_KITTY_ON)
                out.flush()
            except (OSError, ValueError):
                pass  # 不支持终端忽略序列，零副作用
        try:
            # handle_sigint（默认 True）：ptk 在 prompt 期间接管 SIGINT、
            # 退出时还原——Ctrl+C 经 ptk 转成 KeyboardInterrupt 上抛。
            text = await session.prompt_async(handle_sigint=True)
        except EOFError:
            # Ctrl+D 空行：旧版返回 ""（REPL 重提示）；非空行 ptk 默认
            # 前向删字符、不会到这里。
            return "", session
        return text, session
    finally:
        if tty_out:
            try:
                out.write(_KITTY_OFF)
                out.flush()
            except (OSError, ValueError):
                pass


if __name__ == "__main__":
    import tempfile

    # ── history：空行 / 相邻重复不入史，多行 roundtrip ────────────
    with tempfile.TemporaryDirectory() as td:
        orig = HISTORY_PATH
        try:
            editor_mod = sys.modules[__name__]
            editor_mod.HISTORY_PATH = Path(td) / "history"
            h = OpenXHistory(editor_mod.HISTORY_PATH)
            h.store_string("hello")
            h.store_string("   ")
            h.store_string("hello")        # 相邻重复
            h.store_string("a\nb")         # 多行（+ 转义）
            h.store_string("hello")        # 不相邻 → 保留
            # load_history_strings 最新在前；get_strings() 惰性不可用
            loaded = list(OpenXHistory(editor_mod.HISTORY_PATH)
                          .load_history_strings())
            assert loaded == ["hello", "a\nb", "hello"], loaded
        finally:
            editor_mod.HISTORY_PATH = orig

    # ── completer：过滤与别名→规范名 ─────────────────────────────
    from prompt_toolkit.document import Document

    c = OpenXCompleter()

    def _names(text):
        return [x.text for x in c.get_completions(Document(text), None)]

    assert _names("hello") == []            # 非 / 起头不出候选
    assert _names("/model gpt") == []       # 命令名已完整（含空白）
    from ...app.cli.commands import menu_entries as _me
    names = [n for n, _, _ in _me()]
    assert names, "menu_entries 不能为空"
    sample = [n for n in names if n.startswith("he")]
    got = _names("/he")
    assert got == sample, (got, sample)
    canonical = _names("/ex")
    # 别名命中 → 提交文本是规范名（若 ex 是某命令别名则 ≠ "ex" 本身）
    assert all(x in names for x in canonical)

    # ── 键位表组装：五个自定义绑定都在 ───────────────────────────
    # ptk 把键名规约成 Keys 枚举（"enter" → ControlM），字符键保持字面
    kb = build_key_bindings(lambda text: None)
    keysets = [tuple(b.keys or ()) for b in kb.bindings]
    assert any(ks == (Keys.ControlM,) for ks in keysets)          # enter
    assert any(ks == (Keys.Tab,) for ks in keysets)               # tab
    assert any(ks == (Keys.Escape, "r") for ks in keysets)        # alt+r
    assert any(ks == (Keys.Escape, Keys.ControlM) for ks in keysets)  # alt+enter

    # ── ANSI 表扩展生效 ──────────────────────────────────────────
    assert ANSI_SEQUENCES["\x1b[13;2u"] == (Keys.Escape, Keys.ControlM)

    # ── toolbar：两行、宽度受限 ─────────────────────────────────
    class _FakeConsole:
        _terminal_width = 40
        _mode = "auto"
        _input_tokens_view = 1500
        _output_tokens_view = 999

    ft = status_toolbar(_FakeConsole())
    if callable(ft):
        ft = ft()
    plain = "".join(t for _, t in ft)
    lines = plain.split("\n")
    assert len(lines) == 2, lines
    assert all(len(ln) <= 40 for ln in lines), lines
    assert "1.5k" in lines[1] and "999" in lines[1], lines

    print("openx/ui/_components/editor.py OK ✓")
