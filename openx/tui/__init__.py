"""OpenX 全屏 TUI（Textual）—— **阶段 0 脚手架**。

本包只在 ``openx --tui`` / ``python -m openx.tui`` 路径被导入；默认的
行内 REPL 完全不经此包（``textual`` 缺失也不影响默认路径）。

阶段分工（见迁移计划）：

- **阶段 0（本包现状）**：输出缝（:mod:`openx.tui.sink`）+ 可折叠块
  （:mod:`openx.tui.blocks`）+ 最小 app（转录 / 状态行 / 输入框）；
  ``--tui`` 下跑**非流式**回合（``config.stream = False``）——流式管线
  在阶段 2 接入，弹窗在阶段 5 接入。
- 阶段 1–6：sink 全量落地 → 流式 widget 管线 → 输入/历史 → 交互块 →
  ModalScreen 弹窗 → 删行内死代码并翻转默认。

设计纪律（沿既有架构约定）：

1. **textual 只在需要时导入**：本 ``__init__`` 不 import textual，
   :func:`available` 才会探测——非 TUI 路径的 import 成本与失败面为零。
2. **调用方永不触碰 widget**：所有写屏都经 :class:`openx.tui.sink.SinkQueue`
   投递，由 app 在主事件循环的定时器里排空（跨线程安全）。
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
