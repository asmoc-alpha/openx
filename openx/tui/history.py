"""输入行历史 —— 与行内 REPL **共用同一份** ``~/.openx/history``。

行内实现（``ui/_components/editor.py``）把历史交给 prompt_toolkit 的
``FileHistory``：文件里每条记录先写一行 ``# <时间戳>`` 头，随后每条逻辑行以
``+`` 前缀落盘（多行条目因此天然可存）。全屏界面要的是**同一份记忆**——用户
在 ``--inline`` 里敲过的指令，回到默认界面按 ↑ 还得能翻到——所以这里直接读写
那个格式，而不是另起一个 json/自己的文件：

    写：``\\n# 2026-01-01 00:00:00\\n+第一行\\n+第二行\\n``
    读：连续 ``+`` 行合并成一条（去掉末行的换行）；其余行（``#`` 头、空行、
        旧格式残留）一律当分隔符 —— 认不出来的字节最多丢一条，绝不抛异常。

三条纪律：

1. **只追加，不重写**：落盘用 ``"ab"`` 追加，多进程（行内 + 全屏同时开）
   也不会互相截断。
2. **失败即降级**：历史是锦上添花，磁盘满 / 权限错 / 编码坏一律吞掉，
   界面照常能用（与 ``ui/_helpers`` 的 fail-open 同纪律）。
3. **空行与相邻重复不入史**：与行内 ``OpenXHistory.store_string`` 的过滤
   逐字一致（连续按 Enter 不该把历史刷满空行；重复发同一句只留一条）。

本模块**不 import textual**：它是纯存储 + 游标逻辑，可在无 GUI 下单测。
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Optional

#: 历史文件落点 —— 与行内 ``ui/_components/editor.HISTORY_PATH`` **同一个文件**
#: （测试钉住这一相等性，见 tests/tui/test_input.py）。
#: 测试 monkeypatch 本模块属性以隔离真实用户数据
#: （同 orchestration/sessions.SESSIONS_DIR 约定）。
HISTORY_PATH = Path.home() / ".openx" / "history"

#: 内存里保留的条目上限（文件本身不截断——历史归用户，界面只取最近的一段）
MAX_ENTRIES = 500


def _flush(out: list[str], lines: list[str]) -> None:
    """把累积的 ``+`` 行合并成一条（去掉最后一个换行，同 ptk 的 join[:-1]）。"""
    if not lines:
        return
    text = "".join(lines)
    if text.endswith("\n"):
        text = text[:-1]
    out.append(text)


def load_entries(path: Optional[Path] = None) -> list[str]:
    """读盘 → 旧到新的条目列表（最新在末尾）。

    格式解析逐字对齐 ``prompt_toolkit.history.FileHistory.load_history_strings``
    （那边返回的 ``reversed(strings)`` 才是「新在前」）。文件不存在、读失败、
    编码坏 → 返回空列表：历史为空不是错误。
    """
    target = Path(path) if path is not None else HISTORY_PATH
    try:
        raw = target.read_bytes()
    except OSError:
        return []
    entries: list[str] = []
    lines: list[str] = []
    for line_bytes in raw.splitlines(keepends=True):
        line = line_bytes.decode("utf-8", errors="replace")
        if line.startswith("+"):
            lines.append(line[1:])
        else:
            _flush(entries, lines)
            lines = []
    _flush(entries, lines)
    return entries


def append_entry(text: str, path: Optional[Path] = None) -> bool:
    """追加一条到文件末尾。成功 → True；任何 IO 失败 → False（静默降级）。"""
    target = Path(path) if path is not None else HISTORY_PATH
    stamp = datetime.datetime.now()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "ab") as handle:
            handle.write(f"\n# {stamp}\n".encode())
            for line in text.split("\n"):
                handle.write(f"+{line}\n".encode())
    except (OSError, UnicodeError):
        return False
    return True


class PromptHistory:
    """输入行历史：内存镜像 + 追加落盘 + ``↑``/``↓`` 回放游标。

    游标三态：``_index is None`` = 不在回放（输入框里是用户自己敲的东西）；
    ``0..len-1`` = 正停在某条历史条目上。第一次 ``↑`` 先把当前输入暂存为
    草稿，``↓`` 越过最新一条时把草稿还回去 —— 与 shell 的语义一致：
    「翻历史翻了一半又按 ↓」不该把用户刚打的东西吞掉。
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        max_entries: int = MAX_ENTRIES,
    ) -> None:
        #: 取模块常量的**当下值**（而非 import 时快照）——测试 monkeypatch
        #: ``HISTORY_PATH`` 后新建的实例必须指向隔离文件。
        self._path = Path(path) if path is not None else HISTORY_PATH
        self._max = max(1, int(max_entries))
        self._entries: Optional[list[str]] = None
        self._index: Optional[int] = None
        self._draft = ""

    # ── 只读面 ──────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        return self._path

    def entries(self) -> list[str]:
        """旧 → 新的条目列表（首次访问才读盘，之后走内存）。"""
        if self._entries is None:
            self._entries = load_entries(self._path)[-self._max:]
        return self._entries

    def __len__(self) -> int:
        return len(self.entries())

    # ── 写入 ────────────────────────────────────────────────────

    def add(self, text: str) -> bool:
        """记一条提交。空行 / 与最新一条相同 → 返回 False 且不落盘。"""
        entry = (text or "").strip()
        if not entry:
            return False
        entries = self.entries()
        if entries and entries[-1] == entry:
            self.reset()
            return False
        entries.append(entry)
        if len(entries) > self._max:
            del entries[: len(entries) - self._max]
        self.reset()
        append_entry(entry, self._path)
        return True

    # ── 回放 ────────────────────────────────────────────────────

    def reset(self) -> None:
        """退出回放态（提交后调用：下一条新输入不该从半路开始翻）。"""
        self._index = None
        self._draft = ""

    def prev(self, current: str = "") -> Optional[str]:
        """``↑``：上一条历史。已在最旧一条（或历史为空）→ None（不循环）。"""
        entries = self.entries()
        if not entries:
            return None
        if self._index is None:
            self._draft = current
            self._index = len(entries) - 1
        elif self._index > 0:
            self._index -= 1
        else:
            return None
        return entries[self._index]

    def next(self, current: str = "") -> Optional[str]:
        """``↓``：下一条历史；越过最新一条 → 还回草稿（无回放态则 None）。"""
        if self._index is None:
            return None
        entries = self.entries()
        if self._index < len(entries) - 1:
            self._index += 1
            return entries[self._index]
        draft = self._draft
        self._index = None
        self._draft = ""
        return draft


__all__ = [
    "HISTORY_PATH",
    "MAX_ENTRIES",
    "PromptHistory",
    "append_entry",
    "load_entries",
]
