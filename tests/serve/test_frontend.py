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


# ── 插件 UI 面板（ui/v1）────────────────────────────────────────


def test_panels_container_starts_hidden():
    """面板区起始 hidden（同权限弹窗不变量：CSS display 不得覆盖 hidden）。"""
    html = _read("index.html")
    assert '<div id="panels" class="panels" hidden>' in html


def test_appjs_handles_panels_event():
    """reducer 有 panels 分支；渲染走 textContent（XSS 纪律：面板行是插件产物）。"""
    js = _read("app.js")
    assert 'case "panels":' in js
    assert "function renderPanels(panels)" in js
    assert "row.textContent = String(line)" in js  # 纯文本渲染，不走 innerHTML
    # 空面板 → 隐藏面板区
    assert "panelsEl.hidden = true" in js
    assert "panelsEl.hidden = false" in js


def test_css_has_panel_styles():
    css = _read("style.css")
    assert ".panels {" in css
    assert ".panel {" in css
    assert ".panel-line {" in css


# ── P4.1 交互弹窗：ask_user / plan_request ───────────────────────


def test_ask_and_plan_overlays_start_hidden():
    """两个交互弹窗必须起始 hidden（同权限弹窗不变量）。"""
    html = _read("index.html")
    assert 'id="ask-overlay" class="overlay" hidden' in html
    assert 'id="plan-overlay" class="overlay" hidden' in html


def test_appjs_renders_ask_and_plan_interactively():
    """reducer 的 ask_user / plan_request 分支改走交互弹窗（不再 appendMeta）。"""
    js = _read("app.js")
    assert 'case "plan_request":' in js
    assert 'case "ask_user":' in js
    assert "function showAsk(ev)" in js
    assert "function showPlan(ev)" in js
    assert "function respondAsk(answers)" in js
    assert "function respondPlan(approved)" in js
    # 交互通道：上送 ask_user_response / plan_response
    assert 'type: "ask_user_response"' in js
    assert 'type: "plan_response"' in js
    # 旧的非交互提示不再出现
    assert "not interactive on the web yet" not in js


def test_appjs_ask_options_use_textcontent():
    """选项/问题/自定义答案是模型产物——必须 textContent，绝不 innerHTML。"""
    js = _read("app.js")
    assert "lab.textContent = opt.label" in js
    assert "desc.textContent = opt.description" in js
    assert '$("ask-question").textContent = ev.question' in js
    # plan 是唯一走 renderMarkdown 的（先转义后渲染，XSS-safe）
    assert '$("plan-details").innerHTML = renderMarkdown(ev.plan' in js


def test_appjs_handles_skip_and_other():
    """Skip 发空答（服务端落保守默认）；Other 走自由文本输入。"""
    js = _read("app.js")
    assert 'id="ask-skip-btn"' in _read("index.html")
    assert "respondAsk([])" in js
    assert "ask-custom-input" in js
    assert "custom ? [custom] : Array.from(s.selected)" in js
