"""Web 前端结构性回归测试（防权限弹窗/回合条常显 bug）。

背景：``.overlay`` / ``#turn-bar`` 用 ``display:flex`` 展示，CSS 声明优先级
高于 UA 的 ``[hidden]{display:none}``——若 HTML 的 ``hidden`` 被覆盖，权限
弹窗在页面加载即常显、点按钮也关不掉（display 覆盖 hidden）。本测试锁定
三处不变量：HTML 起始 hidden、CSS 有 ``[hidden]{display:none!important}``
兜底、app.js 仍用 hidden 控制显隐。
"""

from __future__ import annotations

import re
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parents[2] / "openx" / "app" / "serve" / "web"


def _read(name: str) -> str:
    return (WEB_DIR / name).read_text(encoding="utf-8")


def test_overlay_and_turnbar_start_hidden():
    html = _read("index.html")
    assert 'id="perm-overlay" class="overlay" hidden' in html, \
        "权限弹窗必须起始 hidden（否则 CSS display:flex 会覆盖 hidden）"
    assert '<div id="turn-bar" hidden>' in html, "回合条必须起始 hidden"


def test_css_forces_hidden_to_win():
    """style.css 必须有 `[hidden]{display:none!important}` 兜底。

    精确匹配规则本体（避免命中注释里的 `[hidden]{display:none}` 字样）：
    不带 !important 的规则压不过 .overlay / #turn-bar 的 display:flex。
    """
    css = _read("style.css")
    assert re.search(r"\[hidden\]\s*\{[^}]*!important[^}]*\}", css), \
        "style.css 缺少 [hidden]{display:none!important} 兜底"


def test_appjs_toggles_hidden_for_modal():
    js = _read("app.js")
    assert "overlayEl.hidden = false" in js   # showPermission 显示
    assert "overlayEl.hidden = true" in js    # respondPermission 隐藏
    # 权限按钮监听已接线（querySelectorAll + data-perm）
    assert 'querySelectorAll("#perm-overlay [data-perm]")' in js
