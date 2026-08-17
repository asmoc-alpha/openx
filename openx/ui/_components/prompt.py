from __future__ import annotations

"""Chat input prompt — an inline frame that travels with the conversation.

The frame (top rule, input line, bottom rule, status) is drawn at the
current cursor position — right below the startup logo on the first turn,
and below the latest response thereafter — so it is never pinned to a
fixed screen row; it moves down as the conversation grows and scrolls
naturally once the viewport fills.

During streaming the frame is part of the Rich ``Live`` region
(:meth:`_frame_renderable`): the streamed response renders above it and,
as it grows, the frame is pushed down — so the box stays visible and
continuously moves down while the model answers, rather than vanishing.
When streaming finishes the frame is left on screen as the next input;
the next prompt reads into it directly (no redraw), and on send it is
cleared so it never lingers in the conversation history — the user's
message is re-shown as a styled banner above the response instead.

Relative cursor movement (``\\033[3A`` …) is used rather than absolute
save/restore: relative moves stay correct even when drawing or scrolling
shifts content, whereas an absolute saved position would be invalidated.
"""

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

import select
import sys
import unicodedata
from shutil import get_terminal_size

from rich.console import Group
from rich.text import Text as RichText

from .._style import (
    ACCENT,
    ACCENT_BOLD,
    CHROME,
    DIM,
    MARK_CURSOR,
    PROMPT_STYLE,
    USER_BANNER_BG,
    USER_BANNER_TEXT,
)
from ..input_capture import (
    PASTE_END,
    PASTE_START,
    SHIFT_ENTER,
    read_unicode_char,
)

# 框线裸 ANSI（与 _style.CHROME = "grey35" 同为 256 色 240 号）：
# 重绘路径不经 Rich 渲染（防宽度缓存滞后折行），颜色须手写序列。
_RULE_ANSI = "\033[38;5;240m"


class PromptMixin:
    """Inline input frame that moves down with the conversation."""

    _console: object
    _terminal_width: int
    _mode: str
    _frame_on_screen: bool

    # ── size + helpers ───────────────────────────────────────────

    def _refresh_terminal_size(self) -> None:
        """Refresh cached terminal width from the live TTY."""
        try:
            size = get_terminal_size()
        except OSError:
            return
        if size.columns:
            self._terminal_width = size.columns

    def _render(self, renderable) -> str:
        """Render a Rich object to an ANSI string with no trailing newline."""
        with self._console.capture() as capture:
            self._console.print(renderable, end="")
        return capture.get()

    def _status_text(self, input_tokens: int, output_tokens: int) -> RichText:
        i_tok = (
            f"{input_tokens / 1000:.1f}k"
            if input_tokens >= 1000
            else str(input_tokens)
        )
        o_tok = (
            f"{output_tokens / 1000:.1f}k"
            if output_tokens >= 1000
            else str(output_tokens)
        )
        # 模式名是唯一着色点（强调色），其余 dim 退居背景；未知模式
        # 黄色提示异常。单行 no_wrap/ellipsis 不变量（框恒四行，SDD §6）。
        mode_style = ACCENT if self._mode in ("auto", "plan", "manual") \
            else "yellow"
        text = RichText.from_markup(
            f"  [{mode_style}]{self._mode}[/{mode_style}]"
            f"  [{DIM}]·[/{DIM}]"
            f"  [{DIM}]{i_tok} in[/{DIM}]"
            f"  [{DIM}]·[/{DIM}]"
            f"  [{DIM}]{o_tok} out[/{DIM}]"
        )
        # Keep the status on a single line (truncated with an ellipsis
        # when the terminal is too narrow) so the frame stays exactly
        # four rows.  Inside a Rich Group this Text-level no_wrap is
        # honoured (no console.capture() — that would race with the Live
        # display thread and leak the streamed response into the status).
        text.no_wrap = True
        text.overflow = "ellipsis"
        return text

    def _print_status_line(
        self, input_tokens: int = 0, output_tokens: int = 0
    ) -> None:
        """Print mode + cumulative token counts below the bottom rule."""
        self._console.print(
            self._status_text(input_tokens, output_tokens),
            no_wrap=True,
            overflow="ellipsis",
        )

    def _frame_renderable(self, input_tokens: int, output_tokens: int):
        """Build the pinned frame as a Rich renderable (4 rows).

        Used by the streaming ``Live`` region so the frame sits at the
        bottom of the streamed response and moves down as it grows.  While
        :attr:`_input_capture` is active, the user's in-progress typed text
        is shown on the input line (truncated to one row so the frame stays
        exactly four rows and the cursor math in ``cancel`` stays valid).
        """
        self._refresh_terminal_size()
        tw = self._terminal_width
        self._frame_width = tw  # 记录留屏框绘制宽度（resize 复用守卫用）
        rule = RichText("─" * tw, style="dim")

        typed = ""
        cap = self._input_capture
        if cap is not None and cap.active:
            typed = cap.current
        # 多行粘贴（字面 \n）：glyph 含换行会撑成多行破坏框 4 行不变量
        # ——首行 + 行数提示预览，全文随 Enter 排队。
        if "\n" in typed:
            parts = typed.split("\n")
            typed = f"{parts[0]}  (+{len(parts) - 1} more lines)"
        # Keep the input on a single row: leave room for "❯ ".
        typed = typed[: max(0, tw - 2)]

        glyph = RichText()
        glyph.append("❯", style=PROMPT_STYLE)
        glyph.append(" ")
        glyph.append(typed)
        # _status_text is no_wrap/ellipsis, so the frame stays exactly
        # four rows even on narrow terminals.
        return Group(rule, glyph, rule, self._status_text(input_tokens, output_tokens))

    # ── 自绘行编辑器 ────────────────────────────────────────────

    @staticmethod
    def _char_width(c: str) -> int:
        """一个字符在终端占用的列数（中日韩 / emoji 等宽字符为 2）。"""
        return 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1

    def _read_line_interactive(self) -> str | None:
        """用 cbreak 自绘编辑器读取一行（UTF-8 感知、宽字符干净擦除）。

        内核的 cooked 行编辑在部分平台（尤其 macOS）上**不是 UTF-8 感知**的：
        退格只擦掉多字节字符的**一个字节**，屏幕上留下残影（"删不干净"），
        残缺的字节序列还会让 ``readline()`` 解码失败、直接崩掉 REPL。
        改为自己接管行编辑：逐字符累积、退格删一个完整字符、按字符宽度擦除。

        返回值区分 EOF 与空行 / Return values distinguish EOF from an empty
        line：``None`` = EOF（stdin 已关闭/耗尽），``""`` = 空回车。
        ``None`` on EOF (stdin closed/exhausted), ``""`` on an empty Enter.
        """
        if not sys.stdin.isatty():  # 非终端（测试 / 管道）走原生 readline
            line = sys.stdin.readline()
            if not line:  # EOF（如 `openx </dev/null`）→ None，供调用方干净退出
                return None
            return line.rstrip("\r\n")

        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        out = sys.stdout
        # 进入行编辑器前确保光标可见：流式 Live 会隐藏光标（?25l），正常
        # 路径在 done()/cancel() 已恢复；这里是兜底——任何泄漏路径之后，
        # 只要轮到用户输入，光标必然可见。?25h 幂等，重复发无副作用。
        # Guarantee a visible cursor whenever the user is expected to type.
        out.write("\033[?25h")
        out.flush()
        buf: list[str] = []
        cur = 0  # 光标在 buf 中的下标（0..len(buf)）；←/→ 移动，退格
                 # 删光标前一字符，可打印字符在光标处插入。
        # 斜杠补全菜单状态（v0.4.2）： menu=None 为关闭；menu_sel 为选中
        # 下标（↑↓ 移动，输入变化归零）
        menu: list | None = None
        menu_sel = 0
        try:
            tty.setcbreak(fd, termios.TCSANOW)
            # 括号粘贴（?2004h）：终端把粘贴内容包在 \033[200~ … \033[201~
            # 里，其间换行按字面收入——多行粘贴不再被首行截断、余行泄漏
            # 成后续输入。扩展键协议（kitty \x1b[=1u + modifyOtherKeys
            # \x1b[>4;2m）让 Shift+Enter 可区分（→ 插入字面换行；Enter
            # 仍是提交）。不支持的终端忽略序列 → 行为同旧版。
            out.write("\033[?2004h\033[=1u\033[>4;2m")
            out.flush()
            while True:
                # 0.2s 超时 select 取代阻塞 read：超时时检查终端 resize
                # （信号事件 + 宽度漂移双通道）并按新宽整框重绘。PEP 475：
                # SIGWINCH 不会提前唤醒 select（带剩余超时自动重试），
                # 超时轮询才是实际检测机制——信号只为降低延迟。
                try:
                    ready, _, _ = select.select([fd], [], [], 0.2)
                except (OSError, ValueError):
                    ready = [fd]  # 降级为直接读（保持原行为）
                if not ready:
                    if self._resize_pending():
                        self._redraw_frame(buf, menu, menu_sel)
                    continue
                ch = read_unicode_char(fd)
                if ch is None:  # EOF
                    raise EOFError

                if ch == PASTE_START:
                    # 括号粘贴：整块读到 \033[201~，换行字面保留为 \n；
                    # 多行内容在框内全展开（所见即所得），Enter 提交全文。
                    buf.extend(self._read_paste_content(fd))
                    cur = len(buf)
                    self._redraw_frame(buf, menu, menu_sel, cur)
                    continue
                if ch == SHIFT_ENTER:
                    # Shift+Enter（或 Alt+Enter）：插入字面换行——多行
                    # 消息编辑；提交仍走 Enter（返回含 \n 的全文）。
                    buf.append("\n")
                    cur = len(buf)
                    self._redraw_frame(buf, menu, menu_sel, cur)
                    continue

                # ── 菜单导航键（仅菜单打开时接管，否则落回原语义）──
                if menu is not None and ch == "\x1b[A":      # ↑
                    menu_sel = (menu_sel - 1) % len(menu)
                    self._redraw_frame(buf, menu, menu_sel)
                    continue
                if menu is not None and ch == "\x1b[B":      # ↓
                    menu_sel = (menu_sel + 1) % len(menu)
                    self._redraw_frame(buf, menu, menu_sel)
                    continue
                if menu is not None and ch == "\t":
                    # Tab：补全选中项 + 尾随空格 → 命令名完整，菜单自然
                    # 关闭，光标留在参数位置继续编辑
                    buf = list("/" + menu[menu_sel][0] + " ")
                    cur = len(buf)
                    self._input_cells_on_screen = 2 + sum(
                        self._char_width(c) for c in buf)
                    self._sync_input_rows()
                    menu, menu_sel = None, 0
                    self._redraw_frame(buf, None, 0, cur)
                    continue
                if menu is not None and ch in ("\r", "\n"):
                    # Enter：按选中命令提交（部分输入如 /he → /help）
                    buf = list("/" + menu[menu_sel][0])
                    cur = len(buf)
                    self._input_cells_on_screen = 2 + sum(
                        self._char_width(c) for c in buf)
                    self._sync_input_rows()
                    menu = None
                    self._redraw_frame(buf, None, 0, cur)  # 收菜单/框再提交
                    out.write("\r\n")
                    out.flush()
                    break
                if ch == "\x1b":  # 单独 Esc：有菜单关菜单，无则忽略
                    if menu is not None:
                        menu, menu_sel = None, 0
                        self._redraw_frame(buf)
                    continue

                # ── 原有按键语义 ──
                if ch in ("\r", "\n"):
                    # \r 先取消 pending-wrap（写满行末时光标悬停在右边界，
                    # 裸 \n 的行进距离终端间有二义性）；ONLCR 下 \n 自带 \r，
                    # 多一个 \r 无副作用。
                    out.write("\r\n")
                    out.flush()
                    break
                if ch == "\x03":  # Ctrl-C
                    raise KeyboardInterrupt
                if ch == "\x04":  # Ctrl-D：空行→重新提示（与 cooked 一致），否则忽略
                    if not buf:
                        break
                    continue

                # ── 编辑键：光标移动 / 退格 / 插入（恒整框重绘）──
                if ch == "\x1b[D":  # ← 光标左移
                    if cur > 0:
                        cur -= 1
                        self._redraw_frame(buf, menu, menu_sel, cur)
                    continue
                if ch == "\x1b[C":  # → 光标右移
                    if cur < len(buf):
                        cur += 1
                        self._redraw_frame(buf, menu, menu_sel, cur)
                    continue
                if ch in ("\x7f", "\b"):  # 退格：删光标前一字符
                    if cur > 0:
                        buf.pop(cur - 1)
                        cur -= 1
                        self._redraw_frame(buf, menu, menu_sel, cur)
                    continue
                if ch == "" or not (ch.isprintable() or ch == " "):
                    continue  # 其他序列（↑↓ 已被菜单/粘贴以外路径忽略）
                buf.insert(cur, ch)  # 可打印：在光标处插入
                cur += 1

                new_menu = self._slash_menu(buf)
                # 任何内容变化恒整框重绘（选中项归零）：逐字节增量写在
                # 光标中途移动 / 宽字符 / 行边界回退时无法正确擦除（用户
                # 报告"删干净了屏幕还有残影"），从 buf 全量重建帧面无此
                # 可能；单键重绘成本可忽略。
                menu, menu_sel = new_menu, 0
                self._redraw_frame(buf, menu, menu_sel, cur)
        finally:
            # 关括号粘贴 + 弹 kitty 协议 + 关 modifyOtherKeys（幂等，
            # 不支持的终端忽略）
            out.write("\033[?2004l\033[<u\033[>4m")
            out.flush()
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        return "".join(buf)

    @staticmethod
    def _read_paste_content(fd) -> list[str]:
        """读括号粘贴体至 ``\\033[201~``（PASTE_END），按字符列表返回。

        换行（``\\r``/``\\n``）字面保留为 ``"\\n"``；Esc 系记号按原文逐字
        追加（粘贴内容恰含转义样文本的罕见情形）；EOF 视为粘贴结束
        （防御——粘贴标记不成对时绝不挂死）。
        """
        chars: list[str] = []
        while True:
            ch = read_unicode_char(fd)
            if ch is None or ch == PASTE_END:
                break
            if ch in ("\r", "\n"):
                chars.append("\n")
            elif ch.startswith("\x1b"):
                chars.extend(ch)
            elif ch:
                chars.append(ch)
        return chars

    # ── resize 支持（SDD 终端交互 §4.5）────────────────────────────

    def _sync_input_rows(self) -> None:
        """由当前格数与（缓存）宽度推导输入区物理行数。

        pending-wrap（格数恰为宽度整倍数）不增加物理行——(c−1)//tw+1
        对此正确。仅在无 resize 时与屏面一致；resize 后由重绘接管并
        以新宽刷新簿记。
        """
        tw = max(1, self._terminal_width)
        self._input_rows_on_screen = (self._input_cells_on_screen - 1) // tw + 1

    def _resize_pending(self) -> bool:
        """自上次绘制以来是否发生过 resize（信号事件 + 宽度漂移双通道）。

        漂移轮询兜底 Windows（无 SIGWINCH）、事件丢失与非主线程 Console；
        事件只为降低延迟。宽度 0（某些 pty 瞬态）视为无效、不触发重绘。
        """
        watcher = getattr(self, "_resize", None)
        if watcher is not None and watcher.check():
            return True
        try:
            cols = get_terminal_size().columns
        except OSError:
            return False
        return cols not in (0, self._terminal_width)

    def _redraw_input_frame(self, buf: list[str]) -> None:
        """resize 后按新宽整框重绘（不带菜单；等价 ``_redraw_frame(buf)``）。"""
        self._redraw_frame(buf)

    def _redraw_frame(
        self, buf: list[str], menu: list | None = None, menu_sel: int = 0,
        cursor: int | None = None,
    ) -> None:
        """整框重绘 + 可选斜杠补全菜单（v0.4.2），光标归位到输入文本末尾。

        锚点公式 ``up = min(K_old, K_new)``——K_old 为屏上观测行数、
        K_new = ⌈c/新宽⌉（c = "❯ " 前缀 2 格 + 输入格数，宽字符按
        :meth:`_char_width` 计格）。终端 reflow 只会**增加**（缩窄）
        或**减少**（加宽）光标上方的行数，故 min 在任何终端类下
        **永不越界上移**（绝不吞没上方对话），至多在新框之上留有界
        装饰残行（随后续滚动/重绘消失）。自锚点 ``\\033[J`` 向下
        精确清除——菜单渲染在状态行之下，同属擦除区（输入期间框下
        绝无他物）。

        整除宽度（c ≡ 0 mod tw）时补一尾空格：pending-wrap 态的光标
        位置不可由 ``\\033[C`` 寻址（停在行首会覆盖内容），补空格提交
        换行使光标落新行可寻址处；``_input_cells_on_screen`` 记 c+1
        使后续清框/再重绘算术自洽。

        光标回程计入菜单高 menu_h：``(k_new − 光标行偏移) + 2 + menu_h``
        行上移（无菜单时 ≡ 旧式 3A）。菜单行绘制在近屏底时可能滚屏——
        滚屏保持框/菜单相对布局不变，相对回程算术仍精确。
        """
        old_rows = self._input_rows_on_screen
        self._refresh_terminal_size()
        tw = max(1, self._terminal_width)
        typed = "".join(buf)
        cursor_idx = len(buf) if cursor is None else min(cursor, len(buf))
        # ── 多行格位模型 ─────────────────────────────────────────
        # 逻辑行按 \n 切分，各行独立按 tw 折行：首行含 "❯ " 前缀 2 格。
        # 光标在末尾且末行恰整除宽度 → pending-wrap 不可寻址 → 补尾
        # 空格使光标落新行（与单行语义一致；渲染串与光标下标同步 +1）。
        pre_lines = typed.split("\n")
        last_cells = (
            sum(self._char_width(ch) for ch in pre_lines[-1])
            + (2 if len(pre_lines) == 1 else 0)
        )
        trail = 1 if (
            cursor_idx == len(buf)
            and last_cells > 0 and last_cells % tw == 0
        ) else 0
        typed_r = typed + (" " if trail else "")
        cursor_r = cursor_idx + trail
        logical = typed_r.split("\n")
        cells = [
            sum(self._char_width(ch) for ch in ln) + (2 if i == 0 else 0)
            for i, ln in enumerate(logical)
        ]
        line_rows = [max(1, (c_i + tw - 1) // tw) for c_i in cells]
        k_new = sum(line_rows)
        up = min(old_rows, k_new)  # 永不越界上移
        out = sys.stdout
        out.write("\r")
        if up > 0:
            out.write(f"\033[{up}A")  # 尽力锚点（≥ 顶框线行）
        out.write("\033[J")  # 锚点行 → 屏末：向下精确
        # 框线按终端实际新宽输出（裸 dim ANSI）——不经 Rich 渲染，避免
        # Rich console 缓存宽度滞后于终端时把框线折行
        out.write(_RULE_ANSI + "─" * tw + "\033[0m\n")  # 顶框线
        # 输入区：多行全文展开（\n 经终端 ONLCR 成真换行，各逻辑行
        # 独立折行）——Shift+Enter / 多行粘贴后所输即所见。
        out.write("❯ " + typed_r)
        out.write("\n")
        out.write(_RULE_ANSI + "─" * tw + "\033[0m\n")  # 底框线
        self._console.print(
            self._status_text(self._input_tokens_view, self._output_tokens_view),
            no_wrap=True, overflow="ellipsis",
        )
        # ── 斜杠补全菜单（状态行之下；候选单行、选中反白）────────
        menu_h = 0
        if menu:
            rows, above, below = self._menu_window(menu, menu_sel)
            if above:
                out.write(f"\033[2m   ↑ +{above} more\033[0m\n")
                menu_h += 1
            for i, (name, desc, aliases) in enumerate(rows):
                out.write(self._format_menu_line(
                    name, desc, aliases,
                    selected=(i == menu_sel - above), width=tw,
                ) + "\n")
                menu_h += 1
            if below:
                out.write(f"\033[2m   ↓ +{below} more\033[0m\n")
                menu_h += 1
        # 光标归位：菜单/状态行之下 → 回到光标所在格，\r 列 0，按格
        # 右移（宽字符正确）。光标下标 → 逻辑行号（前缀 \n 数）+ 行内
        # 前缀格数：行偏移 = 之前逻辑行行数之和 + 行内折行偏移；列 =
        # 行内格数 mod tw（首行前缀 2 格；行首无内容时列 0，仅首行落
        # "❯ " 之后 = 列 2，与单行旧行为一致）。
        prefix = typed_r[:cursor_r]
        idx = prefix.count("\n")
        rows_before = sum(line_rows[:idx])
        pre = prefix.rsplit("\n", 1)[-1]  # 光标所在逻辑行的光标前文本
        cells_before = (2 if idx == 0 else 0) + sum(
            self._char_width(ch) for ch in pre)
        if cells_before == 0:
            cur_row_off = rows_before
            col = 0
        else:
            cur_row_off = rows_before + (cells_before - 1) // tw
            col = cells_before % tw
        back = (k_new - cur_row_off) + 2 + menu_h
        out.write(f"\033[{back}A\r\033[{col}C")
        out.flush()
        self._frame_width = tw
        self._input_rows_on_screen = k_new
        self._input_cells_on_screen = cells[-1] if cells else 2

    # ── 斜杠命令补全菜单（v0.4.2）────────────────────────────────

    _MENU_MAX_ROWS = 10  # 候选行上限；超出折叠为 ↑/↓ +N more 滚动提示

    def _slash_menu(self, buf: list[str]) -> list | None:
        """由当前输入计算斜杠命令候选；``None`` = 不开菜单。

        仅当**整行以 ``/`` 起头且命令名未写完**（不含空白）时开启；
        前缀同时匹配主名与别名。命令数据经 cli.commands 注册表取用——
        **延迟导入**断开 ui→cli 顶层环（cli.interactive 顶层导入
        ui.console，顶层互导会在 ui.console 未完成初始化时炸）。
        """
        text = "".join(buf)
        if not text.startswith("/"):
            return None
        body = text[1:]
        if any(ch.isspace() for ch in body):
            return None  # 命令名已完整（正在写参数）→ 关菜单
        from ...app.cli.commands import menu_entries
        q = body.lower()
        items = [
            (name, desc, aliases)
            for name, desc, aliases in menu_entries()
            if name.startswith(q) or any(a.startswith(q) for a in aliases)
        ]
        return items or None

    def _menu_window(self, items: list, sel: int) -> tuple:
        """以 sel 为中心取 ≤ _MENU_MAX_ROWS 的候选切片。

        → ``(切片, 上方折叠数, 下方折叠数)``；上方折叠数即切片起点
        下标，选中行在切片内索引 = ``sel − 上方折叠数``。
        """
        n = len(items)
        if n <= self._MENU_MAX_ROWS:
            return items, 0, 0
        half = self._MENU_MAX_ROWS // 2
        start = max(0, min(sel - half, n - self._MENU_MAX_ROWS))
        end = start + self._MENU_MAX_ROWS
        return items[start:end], start, n - end

    def _format_menu_line(
        self, name: str, desc: str, aliases: list, selected: bool, width: int
    ) -> str:
        """单条候选行（裸 ANSI，截断至 width 内单行）：选中项反白。"""
        marker = "❯" if selected else " "
        alias_hint = f" ({','.join(aliases)})" if aliases else ""
        left = f" {marker} {name}{alias_hint} "
        used = sum(self._char_width(ch) for ch in left)
        d = ""
        budget = width - used - 1
        if desc and budget > 3:
            d = desc
            while d and sum(self._char_width(ch) for ch in d) > budget - 1:
                d = d[:-1]
            d = " " + d
        if selected:
            return f"\033[7m{left}\033[0m\033[2m{d}\033[0m"
        return f"\033[1m{left}\033[0m\033[2m{d}\033[0m"

    def reset_scroll_region(self) -> None:
        """No-op; the terminal always uses normal (full-screen) scrolling."""
        return

    def clear_input_frame(self) -> None:
        """Clear a frame left on screen by a finished stream.

        Used when a queued follow-up is sent without a fresh prompt: the
        cursor sits just below the frame (left by ``Live``), so move up
        four rows to the top rule and clear to the end of the screen.
        """
        if self._frame_on_screen:
            sys.stdout.write("\033[4A\033[J")
            sys.stdout.flush()
            self._frame_on_screen = False

    # ── public ───────────────────────────────────────────────────

    def print_user_prompt(
        self, input_tokens: int = 0, output_tokens: int = 0
    ) -> str | None:
        """Show the input frame and read one line; clear it on send.

        If a streaming ``Live`` region just left a frame on screen
        (``_frame_on_screen``), read into that frame directly — otherwise
        draw a fresh frame at the cursor.  After Enter the frame is
        cleared (it isn't history); the caller re-shows the message as a
        banner above the streamed response.

        返回 ``None`` 表示 EOF（非 TTY 的 stdin 已耗尽，如 ``openx
        </dev/null``）——调用方应像 /quit 一样干净退出，而非把 ``None``
        当作空行重新提示（那会死循环）。TTY 下的 EOF 仍抛 EOFError。
        Returns ``None`` on EOF (non-TTY stdin exhausted); callers should
        break cleanly instead of re-prompting.  TTY EOF still raises
        EOFError.
        """
        self._refresh_terminal_size()
        out = sys.stdout
        tw = self._terminal_width
        rule = "─" * tw
        # 暂存 token 数：resize 重绘时渲染准确的状态行
        self._input_tokens_view = input_tokens
        self._output_tokens_view = output_tokens

        if self._frame_on_screen and tw != self._frame_width:
            # 上一轮留屏的框是旧宽绘制的（done 后发生过 resize）：尽力擦除
            # （框恒 4 行 → 4A 精确；缩窄 reflow 终端可能在新框之上留有界
            # 装饰残行，随滚动消失），然后落回新绘分支按新宽重绘。
            out.write("\033[4A\033[J")
            out.flush()
            self._frame_on_screen = False

        if self._frame_on_screen:
            # A frame is already on screen (left by the previous stream).
            # The cursor is below it; move up to the input line, clear it,
            # and write the prompt glyph fresh before reading.
            out.write("\033[3A\033[2K")
            out.write(self._render(RichText.from_markup(
                f"[{PROMPT_STYLE}]❯[/{PROMPT_STYLE}] "
            )))
            out.flush()
            # 留屏框的输入行是空的 "❯ "（捕获已停）：刷新簿记
            self._input_rows_on_screen = 1
            self._input_cells_on_screen = 2
        else:
            # Draw the full frame (4 lines), then move up to the input line.
            self._console.print(f"[{CHROME}]{rule}[/{CHROME}]")     # top rule
            self._console.print()                          # input line (blank)
            self._console.print(f"[{CHROME}]{rule}[/{CHROME}]")     # bottom rule
            self._print_status_line(input_tokens, output_tokens)
            out.write("\033[3A")
            out.write(self._render(RichText.from_markup(
                f"[{PROMPT_STYLE}]❯[/{PROMPT_STYLE}] "
            )))
            out.flush()
            # 新绘簿记：框宽 + 空输入行（"❯ " = 2 格 1 行）
            self._frame_width = tw
            self._input_rows_on_screen = 1
            self._input_cells_on_screen = 2

        result = self._read_line_interactive()

        # Clear the transient frame so it isn't left in history.  Enter left
        # the cursor on the bottom-rule row (\r\n 取消了 pending-wrap，位置
        # 确定)；move up to the top-rule line — accounting for wrapped input
        # rows — and clear to the end of the screen, erasing the whole frame.
        #
        # 读实例态（= 屏上**实际绘出**的布局），绝不在清框前重新探测宽度：
        # <200ms 内的 resize 若未触发重绘，缓存宽度才与屏面一致。
        # 上移行数以**物理行数**计（输入区可多行：Shift+Enter / 多行
        # 粘贴 / 折行）：extra = 输入物理行 − 1（光标所在行不计），+2 =
        # 底框线 + 状态行。单行时 ≡ 旧式 (c_render−1)//tw（等价替换）。
        extra_rows = max(0, self._input_rows_on_screen - 1)
        out.write(f"\033[{2 + extra_rows}A\033[J")
        out.flush()
        self._frame_on_screen = False

        return result

    def print_sent_message(self, text: str) -> None:
        """Slate-background banner for a message the user just sent.

        Printed where the input frame was (cleared on send), so the user's
        turn appears as a distinct block above the model's response — the
        slate background separates user content from the model's
        (un-backgrounded) Markdown output.

        **配色定稿 = OpenClaw TUI 深色主题**（2026-08-12 用户指定参考）：
        深石板灰底 ``#2B2F36`` + 暖白字 ``#F3EEE0`` + 左竖条 ``▎``
        bold cyan（无背景版遗留，用户要求保留——石板块上的唯一强调点）。
        史：初版 ``table.style = "on …"`` 在 rich 14 下从未着色
        （Table.style 只喂 border_style）→ 2026-08-11 行级 style 真上色
        的 7 种浅色块候选被用户否决回退无背景 → 2026-08-12 按 OpenClaw
        深色块定稿。教训：往背景色方向改前先对齐参考对象。

        背景必须挂**行级 style**（``add_row(style=...)``）——rich 14 的
        ``Table.style`` 只喂 border_style（box=None 无边框 → 完全不上色）；
        行级 style 连左右 padding 一并铺满，整行一条完整色块。正文经
        ``RichText.append`` 字面追加（绝不用 ``from_markup`` 拼用户输入）
        ——输入可含 ``[x]`` 一类方括号，markup 解析会误当样式标签轻则
        渲染错乱、重则 MarkupError。
        """
        from rich.table import Table

        tw = self._terminal_width
        table = Table(show_header=False, box=None, padding=(0, 1), width=tw)
        table.add_column(ratio=1)
        banner = RichText()
        banner.append("▎ ", style=ACCENT_BOLD)
        banner.append(text, style=USER_BANNER_TEXT)
        table.add_row(banner, style=f"on {USER_BANNER_BG}")
        self._console.print(table)


def paste_aware_input(console, prompt_markup: str = "") -> str:
    """支持括号粘贴的行读取器——对话框/设置等自由输入框用。

    原 rich ``console.input`` 走内核 cooked readline：粘贴的多行文本被
    换行切断——只返回首行，**余行泄漏进后续输入**（下一个输入框莫名
    收到内容，极难排查）。本函数自接 cbreak + 括号粘贴（?2004h）：
    粘贴内容被终端包在 ``\\033[200~ … \\033[201~`` 内，换行按字面读入、
    以 ``\\n`` 连接整体返回，Enter 提交；退格删一个完整字符（UTF-8 感知）。

    非 TTY（管道/测试）退回原生 ``input()``；终端不支持括号粘贴时无
    标记 → 单行行为同旧版。EOF 抛 ``EOFError``（与 rich 一致）。
    """
    console.print(prompt_markup, end="")
    if not sys.stdin.isatty():
        return input()
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    out = sys.stdout
    try:
        tty.setcbreak(fd, termios.TCSANOW)
        out.write("\033[?2004h\033[=1u\033[>4;2m")
        out.flush()
        chars: list[str] = []
        in_paste = False
        while True:
            ch = read_unicode_char(fd)
            if ch is None:
                raise EOFError
            if ch == PASTE_START:
                in_paste = True
                continue
            if in_paste:
                # 字面收入 + 即时回显——旧版只收不显，粘贴内容在屏上
                # 完全不可见（用户报告 /config 粘贴"复制了但不展示"）。
                # 换行按终端原生换行回显（多行粘贴自然分行呈现）。
                if ch == PASTE_END:
                    in_paste = False
                elif ch in ("\r", "\n"):
                    chars.append("\n")
                    out.write("\r\n")
                elif ch.startswith("\x1b"):
                    chars.extend(ch)
                    out.write(ch)
                elif ch:
                    chars.append(ch)
                    out.write(ch)
                out.flush()
                continue
            if ch == SHIFT_ENTER:
                chars.append("\n")  # Shift/Alt+Enter → 字面换行
                continue
            if ch in ("\r", "\n"):
                out.write("\r\n")
                out.flush()
                break
            if ch == "\x03":  # Ctrl-C
                raise KeyboardInterrupt
            if ch == "\x04":  # Ctrl-D：空行即返回（cooked 语义），否则忽略
                if not chars:
                    break
                continue
            if ch in ("\x7f", "\b"):
                if chars:
                    w = (2 if unicodedata.east_asian_width(chars[-1]) in ("W", "F")
                         else 1)
                    chars.pop()
                    out.write("\b" * w + " " * w + "\b" * w)
                    out.flush()
                continue
            if ch == "" or not (ch.isprintable() or ch == " "):
                continue  # 方向键/其他序列 → 忽略
            chars.append(ch)
            out.write(ch)
            out.flush()
        return "".join(chars)
    finally:
        out.write("\033[?2004l\033[<u\033[>4m")
        out.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


if __name__ == "__main__":
    import io
    from rich.console import Console
    _buf = io.StringIO()
    _m = PromptMixin()
    _m._console = Console(file=_buf, width=100, force_terminal=False, color_system=None)
    _m._terminal_width, _m._mode = 100, "auto"
    _m._frame_on_screen, _m._input_capture = False, None
    with _m._console.capture() as _cap:  # 只渲染输入框，绝不读 stdin
        _m._console.print(_m._frame_renderable(120, 340))
    _frame = _cap.get()
    _input_line = _frame.splitlines()[1]          # 第 2 行是输入行
    assert _input_line.startswith("❯ "), _input_line  # 标记为 ❯ 且顶格左对齐
    assert "▸" not in _frame
    _m.print_sent_message("hello from self-check")
    # 宽字符宽度：中文/emoji 占 2 列，ASCII 占 1 列
    assert _m._char_width("a") == 1 and _m._char_width("中") == 2
    # 斜杠补全菜单离线渲染（v0.4.2）：候选行、选中反白、折叠提示
    _m._input_rows_on_screen = 1
    _m._input_cells_on_screen = 2
    _items = [(f"cmd{i:02d}", f"desc {i}", []) for i in range(15)]
    _rows, _above, _below = _m._menu_window(_items, 12)
    assert len(_rows) == _m._MENU_MAX_ROWS and _below == 0 and _above == 5
    with _m._console.capture() as _cap2:
        _m._console.print(_m._format_menu_line("help", "Show all", [], True, 80))
    assert "help" in _cap2.get() and "\033[7m" in _cap2.get()  # 选中反白
    assert _m._slash_menu is not None  # 方法就位（过滤逻辑见 pytest）
    print(f"frame input line: {_input_line!r} | sent-banner: {len(_buf.getvalue())} chars")
    print("openx/ui/_components/prompt.py OK ✓")
