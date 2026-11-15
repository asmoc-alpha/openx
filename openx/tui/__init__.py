"""OpenX 全屏 TUI（Textual）——默认界面。

``openx``（无参）与 ``openx --tui``（兼容别名）都进本包；``openx --inline``
走旧行内 REPL，完全不 import 这里（迁移期逃生门）。textual 缺失时默认路径
警告后回退行内（见 ``main._run_tui``），因此本包仍保持惰性导入的纪律。

组成：

- :mod:`openx.tui.sink` —— 输出缝：``Console._console`` 的替身，``print()``
  投递 renderable，由 app 挂成 widget。
- :mod:`openx.tui.stream` —— 流式事件与原语 + ``TuiStream``（与行内
  ``StreamingService`` 同面的显示服务，只入队、不碰终端）。
- :mod:`openx.tui.view` —— 事件 → widget 的管线（只在 app 定时器里驱动）。
- :mod:`openx.tui.blocks` —— 可折叠块：thinking / 工具调用 / 状态行。
- :mod:`openx.tui.app` —— app 主体：排空、输入缝、按键、演示脚本。

已落地：输出/输入/会话三缝、可折叠块、流式答案与思考、Esc 打断、排队输入。
仍未迁移（均以行内实现为准）：**弹窗**（权限 ASK / 计划批准 / ``ask_user`` /
设置浮层，直写 tty 的 raw 模式弹窗与 Textual 不兼容——在 modal screen 落地
前用 ``-y`` / auto 模式跑）、**输入行历史与多行编辑**、**fleet 面板与插件
``ui.panel`` 的 widget 化**。

设计纪律（沿既有架构约定）：

1. **textual 只在需要时导入**：本 ``__init__`` 不 import textual，
   :func:`available` 才会探测——非 TUI 路径的 import 成本与失败面为零。
2. **调用方永不触碰 widget**：所有写屏都经 ``SinkQueue`` / ``stream_queue``
   投递，由 app 在主事件循环的定时器里排空（跨线程安全，且改 widget 的
   永远只有单一消费者）。
"""

from __future__ import annotations

__all__ = ["available"]


def available() -> bool:
    """textual 是否已安装（缺失时调用方给安装指引，而非抛 ImportError）。"""
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True
