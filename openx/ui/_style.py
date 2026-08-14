"""Shared style constants for OpenX terminal UI.

设计语言（v0.5.0 视觉重做）：克制单色 + 单一强调色
====================================================
- **chrome 灰**：一切结构性线条（框线、分隔线、状态计时）统一暗灰，
  退为背景层；
- **单一强调色**（cyan）：只用于品牌字样、当前模式、进行中标记、
  选中项——出现即"焦点/活动"；
- **语义色只在表意时用**：成功绿 / 错误红 / 警告黄，且去掉 bold 的
  喧哗（错误行本身已够醒目）；
- **层次靠字重**（bold / normal / dim）而非色相数量；
- **标记符号收敛到一个几何家族**：✓ ✕ ▲ ● ○ ▸ ❯——无 emoji（emoji
  字形随平台漂移、与框线字符风格冲突，是旧界面"廉价感"的主因）。
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

# ── 调色板 ────────────────────────────────────────────────────────
CHROME = "grey35"           # 结构线：框线 / 分隔线 / 次要 chrome
ACCENT = "cyan"             # 唯一强调色：品牌 / 焦点 / 活动态
ACCENT_BOLD = "bold cyan"
DIM = "dim"                 # 背景信息层

SUCCESS_STYLE = "green"     # 语义色（去 bold：颜色已足够表意）
ERROR_STYLE = "red"
WARNING_STYLE = "yellow"
INFO_STYLE = "blue"

HEADER_STYLE = "bold white"
PROMPT_STYLE = ACCENT       # 输入提示符 ❯

# 用户消息横幅（print_sent_message）配色，取自 OpenClaw TUI 深色主题
# （src/tui/theme/theme.ts：userBg/userText）——深石板灰底 + 暖白字，
# 融入深色终端、区块感克制（浅色块方案已被用户否决回退，见横幅实现
# 注释；OpenClaw 浅色主题为 #F3F0E8/#1E1E1E，openx 无主题系统不采用）。
USER_BANNER_BG = "#2B2F36"      # 横幅背景（深石板灰）
USER_BANNER_TEXT = "#F3EEE0"    # 横幅正文（暖白）

# ── 标记符号（几何家族，全平台等宽字形稳定）──────────────────────
MARK_OK = "✓"       # 完成 / 成功
MARK_FAIL = "✕"     # 失败 / 错误
MARK_WARN = "▲"     # 警告
MARK_INFO = "●"     # 信息 / 进行中（配 spinner 帧）
MARK_PENDING = "○"  # 待办
MARK_BULLET = "▸"   # 列表项 / 提示条目
MARK_CURSOR = "❯"   # 输入提示符 / 菜单选中


if __name__ == "__main__":
    import io
    from rich.console import Console
    _buf = io.StringIO()
    _c = Console(file=_buf, width=100)
    for _name in ("HEADER_STYLE", "ACCENT", "ACCENT_BOLD", "CHROME", "DIM",
                  "SUCCESS_STYLE", "ERROR_STYLE", "WARNING_STYLE",
                  "INFO_STYLE", "PROMPT_STYLE"):
        _style = globals()[_name]
        print(f"{_name} = {_style!r}")
        _c.print(f"{_name} sample", style=_style)  # 渲染进缓冲区，证明样式可被 rich 解析
    for _m in ("MARK_OK", "MARK_FAIL", "MARK_WARN", "MARK_INFO",
               "MARK_PENDING", "MARK_BULLET", "MARK_CURSOR"):
        assert globals()[_m], _m
    print(f"rendered {len(_buf.getvalue())} chars to buffer")
    print("openx/ui/_style.py OK ✓")
