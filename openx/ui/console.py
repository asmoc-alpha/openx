"""Rich console output for OpenX.

Delegates to mixin classes in ``_components/`` for layout, prompt,
dialogs, display, messages, setup, and miscellaneous output.
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

from dataclasses import dataclass

from rich.console import Console as RichConsole

from ..config import OpenXConfig

# Shared helpers and legacy streaming display
from ._helpers import shorten_path as _shorten_path, trunc as _trunc  # re-export for tests
from ._stream_display import StreamDisplay  # backward compatibility
from .resize import ResizeWatcher

# Mixin components — each adds a logical group of methods to Console
from ._components.layout import LayoutMixin
from ._components.prompt import PromptMixin
from ._components.dialogs import DialogsMixin
from ._components.display import DisplayMixin
from ._components.messages import MessagesMixin
from ._components.setup import SetupMixin
from ._components.misc import MiscMixin


@dataclass
class Console(
    LayoutMixin,
    PromptMixin,
    DialogsMixin,
    DisplayMixin,
    MessagesMixin,
    SetupMixin,
    MiscMixin,
):
    """Terminal output manager — Claude Code-inspired UI.

    Methods are organised by concern across mixin classes:

    * ``LayoutMixin`` — header bar, startup panel, project overview, status line
    * ``PromptMixin`` — chat input prompt with status bar
    * ``DialogsMixin`` — permission prompts, trust screen, AskUser questions
    * ``DisplayMixin`` — streaming status, tool calls, assistant output, code
    * ``MessagesMixin`` — error/warning/info/success/goodbye, help, tips, release notes
    * ``SetupMixin`` — first-run setup wizard
    * ``MiscMixin`` — todos, cost, images, file diffs
    """

    config: OpenXConfig

    def __post_init__(self):
        self._console = RichConsole(highlight=False)
        self._terminal_width = self._console.width or 80
        self._conversation_topic: str = "chat"
        self._mode: str = "auto"  # "auto" or "plan"
        # True when a streaming Live region has just left an input frame on
        # screen; the next prompt reads into it instead of redrawing.
        self._frame_on_screen: bool = False
        # Captures keystrokes concurrently while streaming so the user can
        # queue follow-up messages; rendered into the frame's input line.
        self._input_capture = None  # InputCapture, set when streaming
        # Messages queued during streaming, sent after the answer finishes.
        self._input_queue: list[str] = []
        # 弹窗钩子：交互式弹窗（ask_user / 计划审批 / 权限询问 / 信任提示）
        # 开启与结束时触发，供流式服务整体暂停 Live 重绘与 InputCapture。
        # 缺省 None → 零行为变化。不加类型注解：避免成为 dataclass 字段
        # （config 无默认值，带默认的字段排在它前面会在类创建时抛 TypeError）。
        # Dialog hooks (None = inactive); unannotated to stay off the
        # dataclass field list.
        self.on_dialog_start = None
        self.on_dialog_end = None
        # ── 终端 resize 支持（SDD 终端交互 §4.5）────────────────────
        # 信号处理器只置标志（绝不写屏）；非 TTY/Windows/非主线程下非
        # 活动，消费方另有宽度漂移轮询兜底。同样不加类型注解（避 dataclass）。
        self._resize = ResizeWatcher()
        self._resize.install()
        # 屏上输入框的簿记——记录"实际绘出"的布局，供 resize 重绘锚点
        # （min(K_old, K_new)，永不越界上移）与提交清框公式使用：
        self._frame_width = self._terminal_width  # 留屏框的绘制宽度
        self._input_rows_on_screen = 1   # 输入区（含 ❯ 前缀换行）占的物理行数
        self._input_cells_on_screen = 2  # 输入区格数（"❯ " 前缀；整除宽度补尾空格时 +1）
        # print_user_prompt 暂存 token 数，供 resize 重绘渲染准确状态行
        self._input_tokens_view = 0
        self._output_tokens_view = 0

    # ── state accessors ─────────────────────────────────────────

    def set_topic(self, text: str) -> None:
        """Set the conversation topic shown in the input-frame label."""
        topic = text.strip().split("\n")[0][:60]
        self._conversation_topic = topic if topic else "chat"

    @property
    def mode(self) -> str:
        """Current permission mode (``"auto"`` or ``"plan"``)."""
        return self._mode

    @mode.setter
    def mode(self, value: str) -> None:
        self._mode = value

    @property
    def raw(self) -> RichConsole:
        """Access the underlying Rich console."""
        return self._console


if __name__ == "__main__":
    import io, tempfile
    from rich.console import Console as _RC
    from openx.config import OpenXConfig
    with tempfile.TemporaryDirectory() as _ws:
        c = Console(config=OpenXConfig(workspace=_ws))
    _buf = io.StringIO()
    c._console = _RC(file=_buf, width=100, highlight=False)  # 重定向到缓冲区
    c.set_topic("self-check")
    c.print_info("console wrapper alive")
    c.print_success("mixins wired")
    print(f"captured {len(_buf.getvalue())} chars: {_buf.getvalue().strip()[:70]!r}")
    print("openx/ui/console.py OK ✓")
