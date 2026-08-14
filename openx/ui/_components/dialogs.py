from __future__ import annotations

"""Dialog components: permission prompts, trust screen, AskUser questions."""

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


# 模块级 sys：_interactive_select/_raw_select 读写 sys.stdin/sys.stdout。
# 此前 _sys 只存在于 __main__ 引导分支，作为包导入时任何交互弹窗都会 NameError。
import os
import select as _select
import shutil as _shutil
import sys as _sys
import unicodedata as _unicodedata
from datetime import datetime

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .._helpers import box_rounded, mask_key
from .._style import DIM, PROMPT_STYLE
from .prompt import paste_aware_input

# _raw_select 的取消哨兵：默认不启用（其他弹窗保持旧行为）；
# pick_session 显式传入 cancel=None 启用 Esc/q 取消。
_NO_CANCEL = object()


def _cell_len(text: str) -> int:
    """终端显示宽度：CJK 全角字符占 2 列。"""
    return sum(
        2 if _unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text
    )


def _wrap_by_cells(text: str, width: int) -> list[str]:
    """按终端列宽折行（全角字符按 2 列计）。

    词优先、超长单词字符级兜底。菜单选项必须折到物理行 ≡ 逻辑行——
    否则 ``_re_render`` 的上移行数（按逻辑行计）欠移，每次重渲块体下移，
    按住方向键时选项内容无限重复打印（用户报告的 bug 根因）。
    """
    words = text.split(" ")
    lines: list[str] = []
    cur = ""
    for w in words:
        cand = f"{cur} {w}" if cur else w
        if _cell_len(cand) <= width:
            cur = cand
            continue
        if cur:
            lines.append(cur)
        # 单词自身超宽 → 字符级切断（CJK 天然任意位可断）
        while _cell_len(w) > width:
            acc, used = "", 0
            for ch in w:
                cw = 2 if _unicodedata.east_asian_width(ch) in "WF" else 1
                if used + cw > width:
                    break
                acc += ch
                used += cw
            lines.append(acc)
            w = w[len(acc):]
        cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def _human_stamp(iso: str) -> str:
    """ISO 时间戳 → 紧凑本地时间（如 "07-24 15:32"）；解析失败退回原串。"""
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        return (iso or "?")[:16]


def _fire_dialog_hook(console: object, attr: str) -> None:
    """触发控制台级弹窗钩子（``on_dialog_start`` / ``on_dialog_end``）。

    交互式弹窗在工具执行期间发生时，流式 Live 重绘与 InputCapture 读键
    必须整体暂停（否则屏幕疯狂打印、按键被偷）——经 Console 上这对可调用
    属性通知 StreamingService。钩子缺省 None（零行为变化）；回调异常一律
    吞掉：弹窗本身绝不能被通知钩子拖垮。
    Console-level dialog hooks (None-safe, errors swallowed).
    """
    cb = getattr(console, attr, None)
    if cb is None:
        return
    try:
        cb()
    except Exception:
        pass


class DialogsMixin:
    """Permission dialogs, trust prompt, and interactive ask-user questions."""

    _console: object

    # ── permission ──────────────────────────────────────────────

    async def ask_permission(
        self,
        tool_name: str,
        reason: str,
        details: str = "",
        args_summary: str = "",
        can_remember: bool = True,
        diff: tuple[str, str, str] | None = None,
    ) -> tuple[bool, bool]:
        """Ask user for permission. Returns ``(approved, remember)``.

        **流式期**（console 上注册了活动的 StreamingService）：委托
        ``ask_permission_bridge``——选择面板嵌在输入框下方，上方内容
        照常展示、Live 不暂停（用户报告：全屏弹窗占满屏影响体验）。
        **非流式**：传统全屏箭头菜单（↑/↓ 选择、Enter 确认，非 TTY
        退回数字菜单）。

        ``diff``：``(path, old_content, new_content)`` 三元组——write/edit
        类工具的变更预览；传统路径经 ``print_file_diff`` 渲染彩色 unified
        diff，桥接路径压缩为摘要行。缺省 None → 不渲染。

        ``can_remember=False``（manual 模式传入）隐藏"不再询问"选项——
        手动模式语义为逐项授权，绝不产生持久化放行规则。
        """
        svc = getattr(self, "_streaming_service", None)
        if svc is not None and svc.is_live_active():
            return await svc.ask_permission_bridge(
                tool_name, reason, details=details,
                args_summary=args_summary,
                can_remember=can_remember, diff=diff,
            )
        # 传统全屏弹窗：期间整体暂停流式（Live + 捕获），
        # try/finally 保证钩子成对触发。
        _fire_dialog_hook(self, "on_dialog_start")
        try:
            self._console.print()
            text = Text()
            text.append("Allow ", style="bold yellow")
            text.append(tool_name, style="bold white")
            text.append("?", style="bold yellow")
            if reason:
                text.append(f" ({reason})", style=DIM)
            self._console.print(text)
            if diff is not None:
                try:
                    # print_file_diff 来自 MiscMixin（组合类运行期可见）；
                    # 渲染失败回退静默——审批菜单本身绝不被拖垮
                    self.print_file_diff(*diff)
                except Exception:
                    pass
            if details:
                self._console.print(
                    Panel(details[:500], title="Details", border_style=DIM)
                )

            # Build the menu. "Don't ask again" only makes sense when we have
            # an args summary to persist a rule against, and the caller allows
            # persistent rules (manual mode passes can_remember=False).
            options: list[tuple[str, tuple[bool, bool]]] = [
                ("Yes, allow once", (True, False)),
                ("No, don't run", (False, False)),
            ]
            if args_summary and can_remember:
                # Insert the "always allow" entry above the reject option.
                options.insert(
                    1, ("Yes, and don't ask again for this", (True, True))
                )

            return self._interactive_select(
                options=options,
                default_index=0,
                prompt="Choose:",
            )
        finally:
            _fire_dialog_hook(self, "on_dialog_end")

    # ── plan approval ───────────────────────────────────────────

    def confirm_plan(self) -> bool:
        """Ask the user to approve or reject the plan just rendered.

        Returns True when the plan is approved. Mirrors ``ask_permission``:
        arrow-key selection (↑/↓, Enter) with a numbered fallback when stdin
        isn't a TTY. 计划审批弹窗：↑/↓ 选择、Enter 确认；非 TTY 退化为数字菜单。
        """
        _fire_dialog_hook(self, "on_dialog_start")
        try:
            self._console.print()
            text = Text()
            text.append("Plan approval ", style="bold yellow")
            text.append("计划审批", style=DIM)
            self._console.print(text)
            return self._interactive_select(
                options=[
                    ("Approve and execute (批准并执行)", True),
                    ("Reject (拒绝)", False),
                ],
                default_index=0,
                prompt="Choose:",
            )
        finally:
            _fire_dialog_hook(self, "on_dialog_end")

    # ── session picker (--resume) ───────────────────────────────

    def pick_session(self, metas: list) -> object | None:
        """Interactive session picker for ``--resume`` (no id given).

        每行：更新时间（本地 "07-24 15:32"）、模型、首条用户消息预览、
        会话 ID。↑/↓ 选择、Enter 确认；Esc/q 或末行取消 → None（调用方
        起新会话并警告）。空列表 → 直接返回 None（调用方警告）。

        Mirrors ``ask_permission``：TTY 走 ``_raw_select``（启用取消键），
        非 TTY 退回数字菜单。返回选中的 meta 对象本身（duck-typed，
        UI 层不依赖 core.sessions 的类型）。
        """
        if not metas:
            return None
        self._console.print()
        header = Text()
        header.append("Resume session ", style="bold yellow")
        header.append("恢复会话", style=DIM)
        self._console.print(header)

        options: list[tuple[str, object]] = []
        for meta in metas:
            stamp = _human_stamp(getattr(meta, "updated_at", ""))
            preview = str(getattr(meta, "first_user_message", "") or "(no messages)")
            preview = " ".join(preview.split())  # 折叠换行/空白
            if len(preview) > 60:
                preview = preview[:57] + "…"
            model = getattr(meta, "model", "") or "?"
            sid = getattr(meta, "session_id", "?")
            options.append(
                (f"{stamp}  {model}  {preview}  ({sid})", meta)
            )
        # 末行取消入口：数字菜单（非 TTY）下的 Esc/q 等价物
        options.append(("── Start a new session instead (取消) ──", None))

        return self._interactive_select(
            options, 0, "Resume:", cancel=None, cancel_chars="q"
        )

    # ── trust prompt ────────────────────────────────────────────

    def ask_trust_directory(self, workspace: str) -> bool:
        """Ask user to trust *workspace*. Returns True if trusted."""
        _fire_dialog_hook(self, "on_dialog_start")
        try:
            return self._ask_trust_body(workspace)
        finally:
            _fire_dialog_hook(self, "on_dialog_end")

    def _ask_trust_body(self, workspace: str) -> bool:
        """``ask_trust_directory`` 的实际渲染 + 选择主体（钩子包裹在外层）。"""
        self._console.print()
        self._console.print(
            Panel(
                Text.from_markup(
                    "\n"
                    f"  [bold white]Workspace:[/bold white] [cyan]{workspace}[/cyan]\n\n"
                    "  OpenX will be able to:\n"
                    "  [green]✓[/green] Read files in this directory\n"
                    "  [green]✓[/green] Write and edit files\n"
                    "  [green]✓[/green] Search code and list files\n"
                    "  [green]✓[/green] Run shell commands\n"
                    "  [green]✓[/green] Access git information\n\n"
                    "  [dim]Trusted directories are remembered in ~/.openx/settings.json[/dim]\n"
                ),
                title="[bold yellow]Trust & safety[/bold yellow]",
                title_align="left",
                border_style="yellow",
                box=box_rounded(),
                padding=(1, 2),
            )
        )
        self._console.print()
        return self._interactive_select(
            options=[
                ("Yes, I trust this directory", True),
                ("No, exit", False),
            ],
            default_index=0,
            prompt="Choose:",
        )

    # ── interactive select (arrow keys) ─────────────────────────

    def _interactive_select(
        self,
        options: list[tuple[str, object]],
        default_index: int = 0,
        prompt: str = "Choose:",
        cancel: object = _NO_CANCEL,
        cancel_chars: str = "",
    ) -> object:
        """Arrow-key selector with fallback to numbered menu.

        ``cancel`` / ``cancel_chars`` 透传给 ``_raw_select``（仅 pick_session
        启用 Esc/q 取消；默认禁用，其他弹窗行为不变）。
        """
        # 弹窗读取输入前确保光标可见：流式 Live 会隐藏光标，正常路径下
        # on_dialog_start 暂停已恢复；此处兜底任何泄漏路径（?25h 幂等）。
        # Guarantee a visible cursor before any dialog reads input.
        _sys.stdout.write("\033[?25h")
        _sys.stdout.flush()
        if _sys.stdin.isatty():
            try:
                return self._raw_select(
                    options, default_index, prompt,
                    cancel=cancel, cancel_chars=cancel_chars,
                )
            except Exception:
                pass
        return self._numbered_select(options, default_index, prompt)

    def _raw_select(
        self,
        options: list[tuple[str, object]],
        selected: int,
        prompt: str,
        cancel: object = _NO_CANCEL,
        cancel_chars: str = "",
    ) -> object:
        """Raw terminal arrow-key selection.

        ``cancel`` 默认 ``_NO_CANCEL``（禁用，保持其他弹窗的旧行为）。
        会话选择器传入 ``cancel=None`` 启用取消：裸 Esc（非方向键序列）
        或 *cancel_chars* 中的任意键（如 ``q``）直接返回 cancel 值。
        """
        import termios, tty

        fd = _sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        cancel_enabled = cancel is not _NO_CANCEL
        # 选项预折行：物理行 ≡ 逻辑行（_re_render 上移行数按物理计）。
        # 终端列宽取 console 簿记（失败退回 shutil）；前缀占 4 列，
        # 再留 1 列余量避免恰好写满触发的终端自动换行歧义。
        tw = getattr(getattr(self, "_console", None), "_terminal_width", 0)
        if not tw:
            tw = _shutil.get_terminal_size((80, 24)).columns
        budget = max(10, tw - 5)
        self._wrapped_options = [
            _wrap_by_cells(label, budget) for label, _ in options
        ]
        # 上移基数 = 选项物理行数 + 1 空行（提示行是光标所在行，不计）
        self._render_lines = sum(len(w) for w in self._wrapped_options) + 1
        # 提示语：Esc 语义随 cancel 开关（取消 vs 打断）
        esc_hint = "Esc to cancel" if cancel_enabled else "Esc to interrupt"
        self._select_hint = f"(↑/↓ to choose, Enter to confirm, {esc_hint})"
        try:
            tty.setraw(fd)
            self._render_options(options, selected, prompt)
            while True:
                # **一切读键走 os.read(fd)**，绝不经过 sys.stdin 的文本
                # 缓冲层：Python 文本缓冲会把 ESC [ B 三字节一次读进用户
                # 态缓冲，select(fd) 却看不到 → 裸 Esc 误判、方向键撕裂。
                # OS 级读取保证 select 所见即所得（InputCapture 同款纪律）。
                b = os.read(fd, 1)
                if not b:
                    raise EOFError("stdin closed during dialog")
                ch = b.decode("utf-8", "ignore")
                if not ch:  # 多字节 UTF-8 的分片/游离字节：忽略
                    continue
                if ch == "\x1b":
                    # 区分裸 Esc 与转义序列：序列字节同包到达，20ms 窗口
                    # 可靠判别（同 InputCapture._consume_escape 手法）。
                    # **绝不阻塞读下一字节**——旧实现直接 read(1) 会挂等
                    # 后续键，Esc 表现为"卡死+吃掉下一个键"（用户报告的
                    # 授权弹窗无法 Esc 打断的根因）。
                    r, _, _ = _select.select([fd], [], [], 0.02)
                    if not r:
                        # 裸 Esc：启用取消语义（pick_session）→ 返回取消
                        # 值；否则打断当前回合——与流式期 Esc 打断同语义
                        # （弹窗期间 InputCapture 已暂停，由弹窗自身上抛）。
                        _sys.stdout.write("\r\n")
                        _sys.stdout.flush()
                        if cancel_enabled:
                            return cancel
                        raise KeyboardInterrupt
                    nb = os.read(fd, 1)
                    nxt = nb.decode("utf-8", "ignore") if nb else ""
                    if nxt == "[":
                        db = os.read(fd, 1)
                        d = db.decode("utf-8", "ignore") if db else ""
                        if d == "A":
                            selected = (selected - 1) % len(options)
                        elif d == "B":
                            selected = (selected + 1) % len(options)
                        self._re_render(options, selected, prompt)
                elif cancel_enabled and cancel_chars and ch in cancel_chars:
                    _sys.stdout.write("\r\n")
                    _sys.stdout.flush()
                    return cancel
                elif ch in ("\r", "\n"):
                    self._re_render(options, selected, prompt, final=True)
                    # **\r\n 而非裸 \n**：raw 模式 ONLCR 关闭，裸 LF 不回列——
                    # 光标会停在末行 "Choose: …" 的行中（~45 列）下移，
                    # resume 后 Live 首渲从此列开写 → 整区错位（用户报告
                    # 的排版错乱根因）。\r\n 与 ONLCR 状态无关，恒回列 0。
                    _sys.stdout.write("\r\n")
                    _sys.stdout.flush()
                    return options[selected][1]
                elif ch == "\x03":
                    _sys.stdout.write("\r\n")
                    _sys.stdout.flush()
                    raise KeyboardInterrupt
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def _re_render(self, options, selected, prompt, final=False):
        lines = getattr(self, "_render_lines", len(options) + 2)
        _sys.stdout.write(f"\033[{lines}A")
        self._render_options(options, selected, prompt, final=final)

    def _render_options(self, options, selected, prompt, final=False):
        wrapped = getattr(self, "_wrapped_options", None)
        hint = getattr(self, "_select_hint", "(↑/↓ to choose, Enter to confirm)")
        for i, (label, _) in enumerate(options):
            wrap_lines = wrapped[i] if wrapped else [label]
            is_sel = i == selected
            for j, ln in enumerate(wrap_lines):
                _sys.stdout.write("\r\033[K")
                if j > 0:
                    # 续行：4 格悬挂缩进对齐首行标签起点
                    if is_sel:
                        style = "\033[1;32m" if final else "\033[1;37m"
                        _sys.stdout.write(f"    {style}{ln}\033[0m\n")
                    else:
                        _sys.stdout.write(f"    \033[2m{ln}\033[0m\n")
                elif is_sel:
                    if final:
                        _sys.stdout.write(f"  \033[1;32m● {ln}\033[0m\n")
                    else:
                        _sys.stdout.write(
                            f"  \033[1;36m●\033[0m \033[1;37m{ln}\033[0m\n"
                        )
                else:
                    _sys.stdout.write(f"  \033[2m  {ln}\033[0m\n")
        _sys.stdout.write("\r\033[K\n")
        _sys.stdout.write(f"\r\033[K  {prompt} ")
        _sys.stdout.write(f"\033[2m{hint}\033[0m")
        _sys.stdout.flush()

    def _numbered_select(self, options, default_index, prompt):
        """Fallback numbered menu."""
        for i, (label, _) in enumerate(options):
            m = "[bold green]" if i == default_index else "[dim]"
            self._console.print(
                f"  {m}[{i + 1}][/{m}] {m}{label}[/{m}]"
            )
        self._console.print()
        default_num = default_index + 1
        choice = (
            paste_aware_input(
                self._console,
                f"  [{PROMPT_STYLE}]▸[/{PROMPT_STYLE}] "
                f"[dim]{prompt} [[bold]{default_num}[/bold]]:[/dim] ",
            )
            .strip()
        )
        if choice == "":
            return options[default_index][1]
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(options):
                return options[idx][1]
        except ValueError:
            pass
        return options[default_index][1]

    # ── ask-user questions ──────────────────────────────────────

    def ask_user_question(
        self, question: str, options: list[dict], multi_select: bool = False
    ) -> list[str] | str:
        """Multi/single-choice question with interactive selection.

        弹窗前后触发 ``on_dialog_start`` / ``on_dialog_end``（try/finally）：
        流式期间该工具在 ``execute()`` 里直接弹窗，executor 的权限钩子够不
        到，只能靠这对 Console 级钩子让 Live 重绘与 InputCapture 整体暂停。
        """
        _fire_dialog_hook(self, "on_dialog_start")
        try:
            return self._ask_user_question_body(question, options, multi_select)
        finally:
            _fire_dialog_hook(self, "on_dialog_end")

    def _ask_user_question_body(
        self, question: str, options: list[dict], multi_select: bool
    ) -> list[str] | str:
        """``ask_user_question`` 的实际渲染 + 选择主体（钩子包裹在外层）。"""
        self._console.print()
        self._console.print(f"[bold white]{question}[/bold white]")
        labels = [
            f"{o.get('label', '')}"
            + (f" — {o['description']}" if o.get("description") else "")
            for o in options
        ]
        if not multi_select:
            pairs = [
                (lbl, options[i].get("label", lbl))
                for i, lbl in enumerate(labels)
            ]
            pairs.append(("Other (type your own)", "__other__"))
            choice = self._interactive_select(
                pairs, default_index=0, prompt="Choose:"
            )
            if choice == "__other__":
                return (
                    paste_aware_input(
                        self._console,
                        f"  [{PROMPT_STYLE}]▸ your answer[/{PROMPT_STYLE}] ",
                    )
                    .strip()
                )
            return choice
        # Multi-select
        self._console.print(
            "[dim]Enter one or more numbers separated by commas.[/dim]"
        )
        for i, lbl in enumerate(labels, 1):
            self._console.print(f"  [cyan]{i}.[/cyan] {lbl}")
        self._console.print("  [cyan]0.[/cyan] Other (type your own)")
        raw = (
            paste_aware_input(
                self._console, f"  [{PROMPT_STYLE}]▸[/{PROMPT_STYLE}] "
            )
            .strip()
        )
        if not raw or raw == "0":
            ans = (
                paste_aware_input(
                    self._console,
                    f"  [{PROMPT_STYLE}]▸ your answer[/{PROMPT_STYLE}] ",
                )
                .strip()
            )
            return [ans] if ans else []
        selected: list[str] = []
        for part in raw.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                idx = int(part) - 1
            except ValueError:
                continue
            if 0 <= idx < len(labels):
                sel = options[idx].get("label", labels[idx])
                if sel not in selected:
                    selected.append(sel)
        return selected


if __name__ == "__main__":
    import io
    from rich.console import Console
    _buf = io.StringIO()
    _m = DialogsMixin()
    _m._console = Console(file=_buf, width=100)
    # 用默认值替代交互式选择，绝不读 stdin / 切 raw 模式。
    _m._interactive_select = (
        lambda options, default_index=0, prompt="Choose:", **_kw:
        options[default_index][1]
    )
    print("ask_trust_directory →", _m.ask_trust_directory("/tmp/openx-ws"))
    print("ask_permission →", _m.ask_permission("Bash", "run tests", details="ls -la", args_summary="ls"))
    print("confirm_plan →", _m.confirm_plan())  # 桩选择器返回首项 → True（批准）

    # pick_session：桩选择器返回首项 → 最新 meta；空列表 → None（不触发选择器）
    from types import SimpleNamespace
    _fake_meta = SimpleNamespace(
        session_id="abc123def456",
        model="gpt-4o",
        updated_at="2026-07-24T15:32:00+00:00",
        first_user_message="fix the login bug",
    )
    assert _m.pick_session([_fake_meta]) is _fake_meta
    assert _m.pick_session([]) is None
    print("pick_session → newest meta selected ✓, empty list → None ✓")
    print(f"captured {len(_buf.getvalue())} chars of dialog rendering")
    print("openx/ui/_components/dialogs.py OK ✓")
