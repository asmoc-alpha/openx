from __future__ import annotations

"""Chat input prompt — an inline frame that travels with the conversation.

The frame travels with the conversation: drawn right below the startup
logo on the first turn and below the latest response thereafter — never
pinned to a fixed screen row; it moves down as the conversation grows and
scrolls naturally once the viewport fills.

分态归属：流式期整个 4 行框（顶框线/输入行/底框线/状态行）都是 Rich
``Live`` region 的一部分（:meth:`_frame_renderable`）——回答在框上方
流式渲染、边长边把框推下去。等待输入期（本模块 + editor.py）：**顶框线
仍是静态打印的 chrome，输入行/底框线/状态行交给 prompt_toolkit**
（editor.read_input_async，:class:`PromptSession` 的 message +
bottom_toolbar）——ptk 的补全菜单是 Float 浮层、bottom_toolbar 自带
``~is_done`` 过滤，静态打印在输入行下方的内容会被浮层覆盖后不还原
（ptk 擦除区只覆盖自身 region），故底 chrome 必须活在 ptk 内（实测
结论，勿回退）。

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

import sys
import unicodedata
from shutil import get_terminal_size

from rich.console import Group
from rich.text import Text as RichText

from .._helpers import fmt_duration, fmt_tokens
from .._style import (
    ACCENT,
    ACCENT_BOLD,
    CHROME,
    DIM,
    MARK_INFO,
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
        # 换算与 editor.status_toolbar（ptk bottom_toolbar 路径）共用
        # fmt_tokens——两侧必须逐字一致，否则流式帧→提示框切换时状态行
        # 数字跳变。
        i_tok = fmt_tokens(input_tokens)
        o_tok = fmt_tokens(output_tokens)
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

    # ── 输入行（prompt_toolkit 路径 + 非 TTY 回退）──────────────

    @staticmethod
    def _char_width(c: str) -> int:
        """一个字符在终端占用的列数（中日韩 / emoji 等宽字符为 2）。"""
        return 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1

    @staticmethod
    def _read_line_fallback() -> str | None:
        """非终端（测试 / 管道）的原生 readline 回退。

        返回值区分 EOF 与空行 / Return values distinguish EOF from an
        empty line：``None`` = EOF（stdin 已关闭/耗尽，如 ``openx
        </dev/null``），``""`` = 空回车。TTY 路径在
        :func:`editor.read_input_async`（Ctrl+D 空行返回 ``""``，Ctrl+C /
        EOF 各有语义）。
        """
        line = sys.stdin.readline()
        if not line:  # EOF → None，供调用方干净退出（不当空行重提示）
            return None
        return line.rstrip("\r\n")

    def _replay_indicator(self, elapsed: float, expanded: bool) -> RichText:
        """重印指示行（与 streaming._thinking_block 冻结态逐字一致）。"""
        t = RichText(
            f"  {MARK_INFO} Thought for {fmt_duration(elapsed)}"
            + (" (alt+r to collapse)" if expanded
               else " (alt+r to expand)"),
            style=DIM,
        )
        t.no_wrap = True
        t.overflow = "ellipsis"
        return t

    def _replay_toggle(self, text: str) -> None:
        """回答结束后 Alt+R：把 thinking 就地重印回 **● Thought for Ns**
        指示行的正下方（用户界面需求：展开位置紧随指示行、不在对话末尾）。

        经 ptk 键位绑定（editor.build_key_bindings）的
        ``run_in_terminal`` 调用：进入时 ptk 已把自身 region（输入行及
        其下 toolbar）整段擦除、光标挪到 **region 顶 = 首个输入行、
        col 0**（``Renderer.erase`` 的光标语义，与 ptk 自家
        ``in_terminal`` 同一方法）——光标位置与输入内容/折行无关，上移
        距离因而是常量：``1（顶框线）+ gap + 屏上块行数``。重印 [指示行
        (+ 思考全文 + 分隔空行) + tail + gap] 后补印顶框线，ptk 在
        ``run_in_terminal`` 退出时重绘自身 region。rich 对已固化行恒写
        硬换行 → 缩窄 reflow 下至多欠冲（留有界装饰残行，与
        _ResizeAwareLive 同类已知局限）、绝不越界吞上方对话。思考全文
        dim 纯文本、绝不走 Markdown（围栏/反引号会被重排乱，与
        streaming._thinking_block 同纪律）。

        **块超一屏（指示行滚出屏顶）走窗口重印**
        （:meth:`_replay_toggle_windowed`）：此时按全量块行数上移会被
        终端钳在行 0——擦除区吞掉屏顶的上方对话、整块重印把已滚入
        scrollback 的行二次追加（用户报告的"输出前半部分重复 + 多轮
        之前的对话看不到"根因）。窗口路径只擦本回合可见区、只重印视口
        放得下的窗口，绝不重打已入 scrollback 的行。
        """
        replay = getattr(self, "_last_replay", None)
        if not replay:
            return
        expand = not self._replay_expanded
        thinking, elapsed = replay["thinking"]
        tail = replay["tail"]
        gap = max(0, int(replay.get("gap", 0)))
        self._refresh_terminal_size()
        tw = max(1, self._terminal_width)
        # k_new 仅用于窗口路径的容量预算（当前输入占几行）；全量路径的
        # 上移距离不需要它（光标恒在 region 顶，见方法 docstring）。
        typed_r, _cursor_r = self._frame_typed_r(list(text), None, tw)
        _line_rows, k_new, _off, _col, _cells = self._frame_layout(
            typed_r, _cursor_r, tw)
        try:
            screen_h = self._console.height
        except Exception:
            screen_h = 24
        # 块可视容量：屏高 − 间距 − ptk 区（输入区 k_new + toolbar 2 行）
        # − 顶框线 1 − 底行余量 1 = −4。块行数 ≤ 容量 ⟺ 指示行必在屏上
        # （块底恒贴间距/框，上方无他物时块顶才可能滚出屏顶）。
        budget = screen_h - gap - k_new - 4
        block = [self._replay_indicator(elapsed, expand)]
        if expand:
            # 思考全文 + 与 tail 首块（自带块间空行前缀）的呼吸空行，
            # 与流式期展开块的 [指示行, 全文, 空行] 结构逐字对齐。
            block.append(RichText(thinking, style=DIM))
            block.append(RichText(""))
        block.extend(tail)
        group = Group(*block)
        try:
            rows = len(self._console.render_lines(group, pad=False))
        except Exception:
            return  # 重印失败宁可不换（屏面保持原状）
        if self._replay_block_rows > budget \
                or getattr(self, "_replay_windowed", False):
            # 指示行已滚出屏顶 → 窗口重印路径（见方法 docstring）。
            # **窗口闩**：本回合一旦走过窗口路径，tail 已部分在
            # scrollback、部分被窗口擦除——此后再走整块重印必二次打印
            # scrollback 行，永久锁定窗口路径。
            self._replay_toggle_windowed(
                expand, thinking, elapsed, tail, gap, screen_h, text)
            return
        # 光标在 ptk region 顶（首输入行）：上移 = 顶框线 1 + gap 空行 +
        # 当前屏上块行数（指示行…tail 末行，_replay_block_rows 记账；
        # 不能用重印后的新行数——擦除发生在重印之前）。
        up = 1 + gap + self._replay_block_rows
        out = sys.stdout
        out.write("\r")
        if up > 0:
            out.write(f"\033[{up}A")
        out.write("\033[J")
        self._console.print(group)
        for _ in range(gap):
            self._console.print(RichText(""))
        out.write(_RULE_ANSI + "─" * tw + "\033[0m\n")  # 顶框线重印
        out.flush()
        self._replay_expanded = expand
        self._replay_block_rows = rows

    @staticmethod
    def _copy_render_line(line) -> RichText:
        """render_lines 行 → Text：只取可见文本段（控制段不得混入重印）。"""
        t = RichText()
        for seg in line:
            if seg.text and not seg.is_control:
                t.append(seg.text, seg.style)
        return t

    def _replay_toggle_windowed(self, expand, thinking, elapsed, tail,
                                gap, screen_h, text) -> None:
        """块超一屏（指示行在屏顶之上）的窗口重印。

        块撑满视口 ⟺ 可见区整段（块可见尾 + 间距 + 顶框线 + ptk 区）
        全属本回合——从行 0 擦除绝不吞上方对话（它们已被块挤入
        scrollback）。**绝不重印已在 scrollback 的行**：整块重印会把
        早已滚出的行二次追加进 scrollback（"输出前半部分重复出现、多
        轮之前的对话看不到"根因）。替代形态：
        - 展开 = 指示行 + 思考**尾部窗口**（推理全文常超屏，最近内容
          才是焦点——同日志滚屏；头部截断以 ↑ … +N more thoughts 标记）
          + 正文末尾几行（上下文连续）；
        - 收起 = 指示行 + 正文末尾窗口（还原 toggle 前的可见形态）。

        光标恒在 ptk region 顶（首输入行，``run_in_terminal`` erase 的
        语义）→ 上移 ``screen_h`` 被终端钳在行 0 = 可见区顶——旧版按
        光标行号精确计算 R 的公式不再需要，借钳位直达行 0。
        簿记落窗口实际行数并置窗口闩：本回合此后恒走窗口路径（整块
        重印会二次打印 scrollback 行，见 _replay_toggle 窗口闩注释）。
        """
        self._refresh_terminal_size()
        tw = max(1, self._terminal_width)
        # 当前输入占几行 → ptk 区高度（预算用；上移距离不需要——见
        # 方法 docstring 的行 0 钳位）。
        typed_r, _cursor_r = self._frame_typed_r(list(text), None, tw)
        _line_rows, k_new, _off, _col, _cells = self._frame_layout(
            typed_r, _cursor_r, tw)
        # 预算：屏高 − 间距 − ptk 区（k_new + toolbar 2）− 顶框线 1 − 底
        # 行余量 1。
        budget = max(3, screen_h - gap - k_new - 4)
        block: list = [self._replay_indicator(elapsed, expand)]
        if expand:
            # 窗口大头给 thinking（用户主动展开就是想读它）；正文末尾
            # 留 ≤3 行上下文（可见尾行被擦后不重印即从屏面丢失）。
            tail_keep = min(3, len(tail), max(0, budget - 3))
            avail = budget - 2 - tail_keep  # 指示行与呼吸空行已占 2 行
            try:
                think_lines = self._console.render_lines(
                    RichText(thinking, style=DIM), pad=False)
            except Exception:
                think_lines = []
            if avail >= 2 and len(think_lines) > avail - 1:
                shown = think_lines[-(avail - 1):]
                block.append(RichText(
                    f"  ↑ … +{len(think_lines) - (avail - 1)} more thoughts",
                    style=DIM))
                block.extend(self._copy_render_line(l) for l in shown)
            elif avail >= 1 and think_lines:
                block.extend(
                    self._copy_render_line(l) for l in think_lines[-avail:])
            block.append(RichText(""))
            if tail_keep:
                block.extend(tail[-tail_keep:])
        else:
            block.extend(tail[-(budget - 1):])
        group = Group(*block)
        try:
            rows = len(self._console.render_lines(group, pad=False))
        except Exception:
            return  # 重印失败宁可不换（屏面保持原状）
        out = sys.stdout
        out.write("\r")
        if screen_h > 0:
            out.write(f"\033[{screen_h}A")  # 过冲上移 → 钳在行 0（可见区顶）
        out.write("\033[J")
        # 底对齐：块不足预算时上方补空行，块尾恒贴间距/框——框永悬空。
        for _ in range(max(0, budget - rows)):
            self._console.print(RichText(""))
        self._console.print(group)
        for _ in range(gap):
            self._console.print(RichText(""))
        out.write(_RULE_ANSI + "─" * tw + "\033[0m\n")  # 顶框线重印
        out.flush()
        self._replay_expanded = expand
        self._replay_block_rows = min(rows, budget)
        self._replay_windowed = True  # 窗口闩：本回合此后恒走窗口路径

    def _frame_typed_r(self, buf: list[str], cursor: int | None,
                       tw: int) -> tuple:
        """渲染串与光标下标（pending-wrap 补尾空格规则，重印预算用）。"""
        typed = "".join(buf)
        cursor_idx = len(buf) if cursor is None else min(cursor, len(buf))
        pre_lines = typed.split("\n")
        last_cells = (
            sum(self._char_width(ch) for ch in pre_lines[-1])
            + (2 if len(pre_lines) == 1 else 0)
        )
        trail = 1 if (
            cursor_idx == len(buf)
            and last_cells > 0 and last_cells % tw == 0
        ) else 0
        return typed + (" " if trail else ""), cursor_idx + trail

    def _frame_layout(self, typed_r: str, cursor_r: int, tw: int) -> tuple:
        """多行格位模型（thinking 重印预算用）：逻辑行按 \\n 切分、各行
        独立按 tw 折行（首行含 "❯ " 前缀 2 格）
        → (各逻辑行物理行数, 输入区总行数, 光标行偏移, 光标列, 各行格数)。"""
        logical = typed_r.split("\n")
        cells = [
            sum(self._char_width(ch) for ch in ln) + (2 if i == 0 else 0)
            for i, ln in enumerate(logical)
        ]
        line_rows = [max(1, (c_i + tw - 1) // tw) for c_i in cells]
        k_new = sum(line_rows)
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
        return line_rows, k_new, cur_row_off, col, cells

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
        # thinking 重印簿记跨提示存活：擦的只有框自身 4 行，屏上展开的
        # 思考块留在原位（_replay_expanded/_replay_block_rows 不改）。

    # ── public ───────────────────────────────────────────────────

    async def print_user_prompt(
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
        当作空行重新提示（那会死循环）。TTY 下 Ctrl+C 抛
        KeyboardInterrupt、Ctrl+D 空行返回 ``""``（重提示）。

        ``async``：ptk 的同步 ``prompt()`` 在运行中的事件循环内会
        RuntimeError（其内部 ``asyncio.run``）——必须走 ``prompt_async``；
        全部调用点（interactive / commands）均在 async 上下文。
        """
        self._refresh_terminal_size()
        out = sys.stdout
        tw = self._terminal_width
        rule = "─" * tw
        # 暂存 token 数：bottom_toolbar 渲染准确的状态行
        self._input_tokens_view = input_tokens
        self._output_tokens_view = output_tokens
        # thinking 重印簿记**不**在此复位：上一轮就地展开的思考块留在
        # 屏上原位（Alt+R 仍可持续 toggle 最近一轮），清框只擦框自身。

        if not sys.stdin.isatty():
            return self._read_line_fallback()

        if self._frame_on_screen and tw != self._frame_width:
            # 上一轮留屏的框是旧宽绘制的（done 后发生过 resize）：尽力擦除
            # （框恒 4 行 → 4A 精确；缩窄 reflow 终端可能在新框之上留有界
            # 装饰残行，随滚动消失），然后落回新绘分支按新宽重绘。
            out.write("\033[4A\r\033[J")
            out.flush()
            self._frame_on_screen = False

        if self._frame_on_screen:
            # 留屏框（4 行）：擦 [字形行, 底框线, 状态行]，**顶框线保留**
            # ——其下清空后 ptk 的浮层/toolbar 可自由生长（底 chrome 在
            # ptk 内，见模块 docstring 的分态归属）。
            out.write("\r\033[3A\033[J")
            out.flush()
        else:
            # 新绘：只有顶框线是静态 chrome；输入行 + 底框线 + 状态行
            # 全在 ptk（message + bottom_toolbar）。
            self._console.print(f"[{CHROME}]{rule}[/{CHROME}]")
            self._frame_width = tw
            out.flush()

        # 惰性导入：非 TTY / 脚本化路径与 openx.ui.console 的顶层导入
        # 完全不触碰 ptk。
        from . import editor
        result, session = await editor.read_input_async(self)

        # 提交清框：banner 将印在框的位置（框不入对话史）。优先 ptk 自身
        # 簿记（renderer.erase 把光标挪回 region 顶并精确下擦——折行/
        # pending-wrap 边界正是旧自绘编辑器的算术事故区，ptk 的内部记账
        # 比重算可靠）；erase 不可用时才按返回文本估算行数兜底。
        cleared = False
        try:
            session.app.renderer.erase()
            cleared = True
        except Exception:
            pass
        if cleared:
            # erase 后光标在输入行首：再上移 1 行吞掉顶框线
            out.write("\033[1A\033[J")
        else:
            typed_r, cursor_r = self._frame_typed_r(list(result), None, tw)
            _lrows, k_new, _off, _col, _cells = self._frame_layout(
                typed_r, cursor_r, tw)
            out.write(f"\r\033[{k_new + 1}A\033[J")
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
        深石板灰底 ``#2B2F36`` + 暖白字 ``#F3EEE0`` + 左标记 ``❯``
        bold cyan（用户指定改用输入提示符同款 ❯——石板块上的唯一强调点）。
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
        banner.append("❯ ", style=ACCENT_BOLD)
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
    with _m._console.capture() as _cap:  # 只渲染流式帧，绝不读 stdin
        _m._console.print(_m._frame_renderable(120, 340))
    _frame = _cap.get()
    _input_line = _frame.splitlines()[1]          # 第 2 行是输入行
    assert _input_line.startswith("❯ "), _input_line  # 标记为 ❯ 且顶格左对齐
    assert "▸" not in _frame
    _m.print_sent_message("hello from self-check")
    # 宽字符宽度：中文/emoji 占 2 列，ASCII 占 1 列
    assert _m._char_width("a") == 1 and _m._char_width("中") == 2
    # 状态行换算与 editor 共用 fmt_tokens：两侧逐字一致
    assert fmt_tokens(999) == "999" and fmt_tokens(1500) == "1.5k"
    # 重印指示行：键位提示已是 Alt+R（Ctrl+R 让位历史反搜）
    _ind = _m._replay_indicator(42.0, expanded=False)
    assert "alt+r to expand" in _ind.plain and "ctrl" not in _ind.plain
    # 多行格位模型（Alt+R 预算用）："ab\n中文" 首行含前缀 2 格
    _lrows, _k, _off, _col, _cells = _m._frame_layout("ab\n中文", 6, 40)
    assert _lrows == [1, 1] and _k == 2 and _cells == [4, 4], (_lrows, _cells)
    print(f"frame input line: {_input_line!r} | sent-banner: {len(_buf.getvalue())} chars")
    print("openx/ui/_components/prompt.py OK ✓")
