"""Concurrent, visible input capture for streaming turns.

While the model is answering, the terminal is switched to cbreak mode
(echo off, character-at-a-time, signals kept) and a background thread
reads keystrokes.  The in-progress line is exposed via :attr:`current` so
the streaming ``Live`` region can render it in the input frame — the user
sees what they type.  Pressing Enter queues the line (it is not sent until
the current answer finishes); Backspace edits.  Everything is restored on
stop, guarded so the terminal is never left in cbreak mode.

The capture only touches ``stdin`` (reads); it never writes to the screen,
so it cannot clash with the ``Live`` refresh thread's writes to stdout.
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

import os
import select
import sys
import termios
import threading
import tty
from collections import deque


def _consume_escape(fd: int) -> str | None:
    """读完 ESC(0x1b) 后，识别并消费转义序列的剩余部分。

    返回值约定：

    - ``None`` —— 20ms 内无任何后续字节：**单独一个 ESC**（v0.4.1 的
      "打断"热键识别基础；终端转义序列字节同包到达，远程 SSH 亦不
      例外，该窗口可靠区分两者）；
    - ``"up" / "down" / "right" / "left"`` —— **无参数**的 CSI
      （``ESC [ A-D``）或 SS3（``ESC O A-D``）方向键（v0.4.2 补全
      菜单导航）；
    - ``"0".."9"`` —— **Alt+数字**组合键（终端把 Alt+key 发成
      ESC+key；macOS Terminal/iTerm2 默认如此）：舰队视图直选子代理。
      仅识别数字，其余 Alt 组合仍按未知序列丢弃；
    - ``""`` —— 其他序列（带参数的 CSI 如 ``ESC [ 1 ; 5 C``、Home/
      End 等），已整体读走，调用方忽略即可。避免 ``[A`` 这类字节被
      当成普通输入插入。
    """
    r, _, _ = select.select([fd], [], [], 0.02)
    if not r:
        return None  # 无后续 → 单独 Esc
    b = os.read(fd, 1)
    if not b:
        return None
    if b[0] in (0x0d, 0x0a):
        # ESC + Enter（Alt+Enter）→ 换行插入（同 Shift+Enter 语义）。
        # \r 与 \n 皆收：行规程 ICRNL 把输入 CR 映射成 NL。
        return "shift_enter"
    if 0x30 <= b[0] <= 0x39:  # ESC + 数字 → Alt+数字组合键（舰队直选）
        return chr(b[0])
    if b[0] in (0x5b, 0x4f):  # '[' (CSI) 或 'O' (SS3)
        r, _, _ = select.select([fd], [], [], 0.02)
        if r:
            c2 = os.read(fd, 1)
            if c2 and 0x41 <= c2[0] <= 0x44:  # A-D 直接终止 → 方向键
                return {0x41: "up", 0x42: "down",
                        0x43: "right", 0x44: "left"}[c2[0]]
            if c2 and 0x30 <= c2[0] <= 0x39:
                # CSI 数字参数序列：5~/6~ = PgUp/PgDn，200~/201~ =
                # 括号粘贴起止（?2004h）；13;2u = Shift+Enter（kitty 键盘
                # 协议）；27;2;13~ = Shift+Enter（modifyOtherKeys）。带参
                # 序列（ESC[1;5C 等）必须**消费完整**——中间字节（';'）
                # 不当终止处理，但**保留进参数字串**供分号参数解析。
                digits = chr(c2[0])
                term = None
                for _ in range(15):
                    r2, _, _ = select.select([fd], [], [], 0.02)
                    if not r2:
                        break
                    cb = os.read(fd, 1)
                    if not cb:
                        break
                    if cb[0] == 0x7e:  # '~' 终止
                        term = "~"
                        break
                    if 0x40 <= cb[0] <= 0x7e:  # 其他终止字节（CSI 尾）
                        term = chr(cb[0])
                        break
                    if 0x30 <= cb[0] <= 0x39:
                        digits += chr(cb[0])
                    elif cb[0] == 0x3b:  # ';' 参数分隔符 → 保留结构
                        digits += ";"
                    # 其他中间字节 → 继续消费
                if term == "~":
                    if digits == "200":
                        return "paste_start"
                    if digits == "201":
                        return "paste_end"
                    if digits == "5":
                        return "pageup"
                    if digits == "6":
                        return "pagedown"
                    # modifyOtherKeys：ESC[27;mods;code~ —— 带修饰的 Enter
                    parts = digits.split(";")
                    if len(parts) == 3 and parts[0] == "27" and parts[2] == "13":
                        return "shift_enter"
                    return ""
                if term == "u":
                    # kitty 键盘协议：ESC[code;mods u —— 带修饰的 Enter
                    # （Shift+Enter = 13;2；任何修饰的 Enter 皆视为换行）
                    parts = digits.split(";")
                    if parts[0] == "13" and len(parts) > 1:
                        return "shift_enter"
                    return ""
                return ""
            # 带参数/其他序列：消费至终止字节
            for _ in range(15):
                if c2 and 0x40 <= c2[0] <= 0x7e:
                    break
                r, _, _ = select.select([fd], [], [], 0.02)
                if not r:
                    break
                c2 = os.read(fd, 1)
                if not c2:
                    break
    return ""


# read_unicode_char 对方向键序列的规范化返回值（调用方按此匹配）
ARROW_SEQUENCES = {"up": "\x1b[A", "down": "\x1b[B",
                   "right": "\x1b[C", "left": "\x1b[D"}
# 翻页键：流式响应滚动回看（StreamingService 消费）
PAGE_SEQUENCES = {"pageup": "\x1b[5~", "pagedown": "\x1b[6~"}
# 全部导航序列记号（_handle 据此入热键队列）
NAV_SEQUENCES = frozenset(ARROW_SEQUENCES.values()) | frozenset(PAGE_SEQUENCES.values())
# 括号粘贴（终端 ?2004h 模式）：粘贴内容被 \033[200~ … \033[201~ 包裹，
# 其中的换行是字面内容而非提交——多行粘贴修复的基础记号。
PASTE_SEQUENCES = {"paste_start": "\x1b[200~", "paste_end": "\x1b[201~"}
PASTE_START = PASTE_SEQUENCES["paste_start"]
PASTE_END = PASTE_SEQUENCES["paste_end"]
# Shift+Enter（及一切带修饰的 Enter）→ 字面换行而非提交。终端默认下
# Shift+Enter 与 Enter 同发 \r、不可区分，需应用侧启用扩展键协议：
# kitty 键盘协议（\x1b[=1u）报 \x1b[13;2u；modifyOtherKeys（\x1b[>4;2m）
# 报 \x1b[27;2;13~；Alt+Enter（ESC+\r）任何终端通用（终端开启 Option/
# Alt as Meta 时）。三形统一归一为此记号。
SHIFT_ENTER = "\x1b\r"


def read_unicode_char(fd: int):
    """从处于 cbreak 模式的 *fd* 读取**一个** Unicode 字符。

    把多字节 UTF-8 序列的字节累积完整再解码，因此中文 / emoji 等宽字符能完整到达，
    而不是被逐字节丢弃。返回值约定：

    - 普通字符 → 该字符（``str``）；
    - **单独 Esc**（无后续字节的 ``0x1b``）→ ``"\\x1b"``（v0.4.1：打断热键）；
    - **方向键** → 规范序列串 ``"\\x1b[A"`` … ``"\\x1b[D"``（v0.4.2：
      补全菜单导航；其余转义序列仍返回 ``""`` 表示"已消费的未知序列"）；
    - **Alt+数字** → ``"\\x1b0"`` … ``"\\x1b9"``（舰队视图直选子代理）；
    - **PgUp/PgDn** → ``"\\x1b[5~"`` / ``"\\x1b[6~"``（流式响应滚动回看）；
    - 真正的文件结束（``os.read`` 返回 ``b""``）→ ``None``。
    """
    b = os.read(fd, 1)
    if not b:
        return None  # EOF
    first = b[0]
    if first == 0x1b:
        kind = _consume_escape(fd)
        if kind is None:
            return "\x1b"                    # 单独 Esc → 热键
        if kind in ARROW_SEQUENCES:
            return ARROW_SEQUENCES[kind]     # 方向键规范序列
        if kind in PAGE_SEQUENCES:
            return PAGE_SEQUENCES[kind]      # 翻页键规范序列
        if kind in PASTE_SEQUENCES:
            return PASTE_SEQUENCES[kind]     # 括号粘贴起止记号
        if kind == "shift_enter":
            return SHIFT_ENTER               # Shift/Alt+Enter → 换行插入
        if len(kind) == 1 and kind.isdigit():
            return "\x1b" + kind             # Alt+数字 → 两字节热键记号
        return ""                            # 其他已消费的未知序列
    if first < 0x80:
        return chr(first)
    # 由首字节判断 UTF-8 序列长度
    if 0xC0 <= first <= 0xDF:
        n = 2
    elif 0xE0 <= first <= 0xEF:
        n = 3
    elif 0xF0 <= first <= 0xF7:
        n = 4
    else:
        return ""  # 游离的后续字节 / 非法首字节
    buf = bytearray([first])
    for _ in range(n - 1):
        cb = os.read(fd, 1)
        if not cb:
            break
        buf.append(cb[0])
    try:
        return bytes(buf).decode("utf-8")
    except UnicodeDecodeError:
        return ""


class InputCapture:
    """Read keystrokes concurrently while streaming; queue whole lines."""

    def __init__(self) -> None:
        self._current = ""          # in-progress line, rendered in the frame
        self._queue: list[str] = []  # completed lines, awaiting send
        # 括号粘贴进行中（PASTE_START 与 PASTE_END 之间）：其间一切字符
        # 按字面收入 _current——换行保留为 \n（多行粘贴作为**一条**消息
        # 排队），控制字符不触发提交/热键语义。
        self._in_paste = False
        # 热键队列（v0.4.0：Ctrl-O 切换子代理视图）：捕获线程只入队，
        # Live 刷新线程经 drain_hotkeys() 在 _build_renderable 内消费——
        # 一切屏幕写操作仍只在 Live 线程发生（resize.py:7 不变量）。
        self._hotkeys: deque = deque()
        # 事件回调（v0.4.1）：捕获线程触发，StreamingService 接线。
        # on_interrupt：单独 Esc → 打断当前回合（取消流消费任务）；
        # on_line_queued：Enter 提交一行 → 排队反馈行即时展示。
        # 回调必须线程安全、绝不直接写终端（写屏只在 Live 线程）。
        self.on_interrupt: object = None
        self.on_line_queued: object = None
        self._lock = threading.Lock()
        self._running = False
        self._active = False
        self._thread: threading.Thread | None = None
        self._fd: int | None = None
        self._old = None

    # ── public API ──────────────────────────────────────────────

    @property
    def active(self) -> bool:
        return self._active

    @property
    def current(self) -> str:
        """The in-progress line (safe to read from the Live thread)."""
        with self._lock:
            return self._current

    def start(self) -> None:
        """Begin capturing.  No-op when stdin isn't a real TTY (tests)."""
        if not sys.stdin.isatty():
            return
        try:
            self._fd = sys.stdin.fileno()
            self._old = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd, termios.TCSANOW)  # no echo, char mode
            # **必清 IEXTEN**：macOS/Darwin 行规程在 IEXTEN 开启时把
            # VDISCARD（Ctrl-O）就地消费（翻转输出丢弃标志），字节永
            # 不到达读端——Ctrl-O 舰队切换在真实终端被静默吞掉（用户
            # 报告"子代理无法进入"的根因；此前测试皆直注热键队列绕过
            # tty 层而未暴露）。清 IEXTEN 后全部 Ctrl 组合到达应用层
            # （编辑语义由 _handle 自行实现，VSTATUS 等无关）；ISIG 保留
            # ——Ctrl-C 信号语义不动。stop() 恢复原 termios（含 IEXTEN）。
            attrs = termios.tcgetattr(self._fd)
            attrs[3] = attrs[3] & ~termios.IEXTEN
            termios.tcsetattr(self._fd, termios.TCSANOW, attrs)
            # 扩展键协议：kitty 键盘协议（\x1b[=1u，Shift+Enter →
            # ESC[13;2u）+ modifyOtherKeys（\x1b[>4;2m，→ ESC[27;2;13~）。
            # 支持的终端上报修饰键、不支持的忽略序列（零副作用）——
            # Shift+Enter 插入换行的前提。stop() 成对关闭。
            sys.stdout.write("\x1b[=1u\x1b[>4;2m")
            sys.stdout.flush()
        except (OSError, ValueError, termios.error):
            self._active = False
            return
        self._active = True
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, name="openx-input", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop capturing and restore the terminal to cooked mode."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._fd is not None and self._old is not None:
            try:
                # 成对关闭扩展键协议（pop kitty + modifyOtherKeys off）
                sys.stdout.write("\x1b[<u\x1b[>4m")
                sys.stdout.flush()
            except (OSError, ValueError):
                pass
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)
            except (OSError, termios.error):
                pass
        self._active = False

    def drain(self) -> list[str]:
        """Return queued lines and drop the in-progress line."""
        with self._lock:
            queued = list(self._queue)
            self._queue.clear()
            self._current = ""
            return queued

    def drain_hotkeys(self) -> list[str]:
        """Return and clear queued hotkeys (polled by the Live thread)."""
        with self._lock:
            keys = list(self._hotkeys)
            self._hotkeys.clear()
            return keys

    # ── internals ───────────────────────────────────────────────

    def _loop(self) -> None:
        while self._running:
            try:
                ready, _, _ = select.select([self._fd], [], [], 0.1)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                ch = read_unicode_char(self._fd)
            except OSError:
                break
            if ch is None:  # EOF
                break
            self._handle(ch)

    def _handle(self, ch: str) -> None:
        if ch == PASTE_START:
            self._in_paste = True
            return
        if self._in_paste:
            # 粘贴体内：全部字面化——换行保留为 \n，Esc 系记号按原文
            # 追加（粘贴内容含转义样文本的罕见情形），绝不触发提交/热键
            if ch == PASTE_END:
                self._in_paste = False
            elif ch in ("\r", "\n"):
                with self._lock:
                    self._current += "\n"
            elif ch:
                with self._lock:
                    self._current += ch
            return
        if ch == SHIFT_ENTER:
            # Shift+Enter：字面换行（多行跟进消息），不提交、不排队
            with self._lock:
                self._current += "\n"
            return
        if ch in ("\r", "\n"):
            with self._lock:
                line = self._current
                self._current = ""
            if line.strip():
                with self._lock:
                    self._queue.append(line)
                cb = self.on_line_queued
                if cb is not None:
                    try:
                        cb(line)
                    except Exception:
                        pass  # 回调故障绝不影响输入本身
            else:
                # 空 Enter → 热键（舰队选择确认；无选择时消费方 no-op）。
                # 此前空 Enter 为完全空操作，入队不改变任何既有行为。
                with self._lock:
                    self._hotkeys.append("\r")
        elif ch in ("\x7f", "\b"):  # Backspace / Delete —— 删掉一个完整字符
            with self._lock:
                self._current = self._current[:-1]
        elif ch == "\x04":  # Ctrl-D
            self._running = False
        elif ch == "\x0f":  # Ctrl-O —— 状态层视图切换（主视图 ⇄ 子代理）
            with self._lock:
                self._hotkeys.append(ch)
        elif ch == "\x12":  # Ctrl-R —— thinking 展开/折叠（对标 Claude Code）
            with self._lock:
                self._hotkeys.append(ch)
        elif ch == "\x14":  # Ctrl-T —— 工具块展开/折叠（对标 Claude Code
            # ctrl+o 展开转录；openx 的 ctrl+o 已被舰队切换占用）
            with self._lock:
                self._hotkeys.append(ch)
        elif len(ch) == 2 and ch[0] == "\x1b" and ch[1].isdigit():
            # Alt+0..9 —— 舰队视图：0 回主视图，1..9 直选第 N 个子代理
            # （对标 Claude Code 的代理窗格导航；Ctrl-O 循环保留）
            with self._lock:
                self._hotkeys.append(ch)
        elif ch in ("\x1b[A", "\x1b[B", "\x1b[5~", "\x1b[6~"):
            # ↑/↓/PgUp/PgDn —— 流式响应滚动回看（StreamingService 消费：
            # 上移视窗回看已滚出的内容，回底恢复自动跟随）。左右方向键
            # 流式期无用途，保持丢弃。
            with self._lock:
                self._hotkeys.append(ch)
        elif ch == "\x1b":  # 单独 Esc —— 打断当前回合（v0.4.1）
            cb = self.on_interrupt
            if cb is not None:
                try:
                    cb()
                except Exception:
                    pass
        elif ch == "":  # 完整转义序列（方向键等）—— 忽略
            return
        elif ch.isprintable() or ch == " ":
            with self._lock:
                self._current += ch


if __name__ == "__main__":
    # 只检查 API，绝不真正捕获键盘（不调用 start()）。
    ic = InputCapture()
    print("class:", InputCapture.__name__, "| active:", ic.active, "| drain():", ic.drain())
    # 热键分发离线验证：直接调 _handle（不启动捕获线程）
    assert ic.drain_hotkeys() == []
    ic._handle("\x0f")  # Ctrl-O → 入队
    ic._handle("\x12")  # Ctrl-R → 入队
    ic._handle("\x14")  # Ctrl-T → 入队（工具块展开/折叠）
    ic._handle("\x1b2")  # Alt+2 → 入队（舰队直选）
    ic._handle("\x1b[A")  # ↑ → 入队（滚动回看）
    ic._handle("\x1b[5~")  # PgUp → 入队（滚动回看）
    ic._handle("a")     # 普通字符 → 进 _current，非热键
    assert ic.drain_hotkeys() == [
        "\x0f", "\x12", "\x14", "\x1b2", "\x1b[A", "\x1b[5~"
    ]
    assert ic.drain_hotkeys() == []
    # Esc 打断回调 + Enter 排队回调（v0.4.1）
    fired = {"esc": 0, "queued": []}
    ic.on_interrupt = lambda: fired.__setitem__("esc", fired["esc"] + 1)
    ic.on_line_queued = lambda line: fired["queued"].append(line)
    ic._handle("\x1b")
    ic._current = "hello"
    ic._handle("\r")
    assert fired["esc"] == 1 and fired["queued"] == ["hello"]
    assert ic.drain() == ["hello"]
    # 括号粘贴：多行内容字面保留（换行 \n），作为单条消息提交；
    # 粘贴体内的 \r 不触发提交、Esc 系记号不触发热键
    ic._handle("\x1b[200~")
    ic._handle("l1")
    ic._handle("\r")
    ic._handle("l2")
    ic._handle("\x1b[A")  # 粘贴体内的方向键记号 → 字面追加
    ic._handle("\x1b[201~")
    assert ic.current == "l1\nl2\x1b[A", ic.current
    assert ic.drain_hotkeys() == []  # 粘贴体内无热键
    ic._handle("\r")
    assert fired["queued"] == ["hello", "l1\nl2\x1b[A"]
    # Shift+Enter → 字面换行（多行编辑），Enter 提交全文
    ic._handle("m1")
    ic._handle(SHIFT_ENTER)
    ic._handle("m2")
    assert ic.current == "m1\nm2", ic.current
    ic._handle("\r")
    assert fired["queued"][-1] == "m1\nm2"
    print("public API:", sorted(n for n in dir(InputCapture) if not n.startswith("_")))
    print("openx/ui/input_capture.py OK ✓")
