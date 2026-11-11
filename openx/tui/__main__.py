"""离线演示入口：``python -m openx.tui``。

不起模型、不读网络——脚本化喂一遍 thinking / 工具块 / 答案，用来验证
阶段 0 的三件事：

1. 转录滚动与贴底跟随；
2. **thinking 默认折叠、标题栏点击（或聚焦后 Enter）展开**；
3. Rich renderable（Markdown / 语法高亮 / 表格）在 ``Static`` 里的保真度。

Ctrl+Q 退出，Ctrl+T 全量折叠/展开，Ctrl+R 只切 thinking 块。
"""

from __future__ import annotations

import sys


def main() -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        # 全屏界面必须独占一个真终端；管道/CI 下 Textual 的驱动会直接抛错
        # （错误信息对用户毫无意义），这里先给一句人话。
        print(
            "Error: the TUI demo needs an interactive terminal (tty).",
            file=sys.stderr,
        )
        return 2
    try:
        from .app import OpenXTui
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        print(
            "Error: the TUI needs the 'textual' package.\n"
            "  Install with:  pip install 'textual'\n"
            f"  ({exc})",
            file=sys.stderr,
        )
        return 2
    OpenXTui(demo=True).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
