"""Terminal resize (SIGWINCH) detection for OpenX.

终端窗口变化检测。设计原则（SDD 终端交互 §4.5/§6）：

- **信号处理器绝不写屏**——只 ``Event.set()`` 置标志。写屏统一发生在
  各消费检查点（流式 ``_ResizeAwareLive.refresh``、编辑器 select 超时
  轮询），与既有"仅 Live 线程 + 主线程写屏"不变量一致。
- **信号只降延迟，不承担正确性**：PEP 475 下 SIGWINCH 不会提前唤醒
  ``select``（带剩余超时自动重试），消费方另有宽度漂移轮询兜底——
  Windows（无 SIGWINCH）、事件丢失、非主线程构造的 Console 皆能感知。
- 安装守卫：``hasattr(signal, "SIGWINCH")`` / ``stdout.isatty()`` /
  主线程（``signal.signal`` 在其他线程抛 ValueError）；幂等；链式调用
  前一处理器（兼容多 Console 遗留路径）。
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

import signal
import sys
import threading


class ResizeWatcher:
    """SIGWINCH → Event；消费方以 :meth:`check` 读后清。

    非 TTY（测试/管道）、Windows、非主线程构造 → 保持非活动，
    ``check()`` 恒 False，``install()`` 静默空操作。
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._installed = False
        self._prev = None  # 前一处理器（链式调用，多为 SIG_DFL=0）

    @property
    def active(self) -> bool:
        """信号处理已安装（仅此时事件通道有效；漂移轮询不依赖它）。"""
        return self._installed

    def install(self) -> None:
        """安装 SIGWINCH 处理器；不满足条件时静默跳过，幂等。"""
        if self._installed:
            return
        if not hasattr(signal, "SIGWINCH"):  # Windows
            return
        try:
            if not sys.stdout.isatty():  # 测试 / 管道
                return
            if threading.current_thread() is not threading.main_thread():
                return  # signal.signal 仅主线程可调
            self._prev = signal.signal(signal.SIGWINCH, self._handle)
            self._installed = True
        except (ValueError, OSError):
            self._installed = False

    def _handle(self, signum, frame) -> None:
        # 只置标志，绝不写屏（主线程字节码间隙执行，Event.set 安全）。
        self._event.set()
        prev = self._prev
        if callable(prev):  # 链式前一处理器（SIG_DFL/SIG_IGN 是 int，跳过）
            prev(signum, frame)

    def check(self) -> bool:
        """自上次 check 以来是否发生过 resize（读后清）。

        is_set 与 clear 之间（纳秒窗）可能丢一次事件——无碍：消费方
        另有宽度漂移检测兜底，事件只为降低重绘延迟而存在。
        """
        if self._event.is_set():
            self._event.clear()
            return True
        return False


if __name__ == "__main__":
    # 独立调试：绝不真装信号（自检进程 stdout 通常非 TTY → 非活动路径）
    w = ResizeWatcher()
    assert w.active is False and w.check() is False
    w._event.set()  # 直接模拟信号到达
    assert w.check() is True and w.check() is False  # 读后清
    w.install()  # 非 TTY → 仍非活动；TTY 终端下手动跑可验证安装
    print(f"active={w.active} (非 TTY 环境下应为 False)")
    print("openx/ui/resize.py OK ✓")
