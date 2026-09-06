"""Web 前端结构性回归测试（防权限弹窗/回合条常显 bug）。

背景：``.overlay`` / ``#turn-bar`` 用 ``display:flex`` 展示，CSS 声明优先级
高于 UA 的 ``[hidden]{display:none}``——若 HTML 的 ``hidden`` 被覆盖，权限
弹窗在页面加载即常显、点按钮也关不掉（display 覆盖 hidden）。本测试锁定
三处不变量：HTML 起始 hidden、CSS 有 ``[hidden]{display:none!important}``
兜底、JS 仍用 hidden 控制显隐。

P4.5 重构：弹窗（permission / ask / plan / 插件面板）已抽到 ``modals.js``；
``app.js`` 只负责事件分发。测试既检查入口（app.js 的 case 分支），也检查
实现（modals.js / chat.js 等模块）的关键不变量。
"""

from __future__ import annotations

import re
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parents[2] / "openx" / "app" / "serve" / "web"


def _read(name: str) -> str:
    return (WEB_DIR / name).read_text(encoding="utf-8")


# ── HTML / CSS 结构性不变量 ────────────────────────────────────────


def test_overlay_and_turnbar_start_hidden():
    html = _read("index.html")
    assert 'id="perm-overlay" class="overlay" hidden' in html, \
        "权限弹窗必须起始 hidden（否则 CSS display:flex 会覆盖 hidden）"
    assert '<div id="turn-bar" hidden>' in html, "回合条必须起始 hidden"


def test_css_forces_hidden_to_win():
    """style.css 必须有 `[hidden]{display:none!important}` 兜底。

    精确匹配规则本体（避免命中注释里的 `[hidden]{display:none}` 字样）：
    不带 !important 的规则压不过 .overlay / / #turn-bar 的 display:flex。
    """
    css = _read("style.css")
    assert re.search(r"\[hidden\]\s*\{[^}]*!important[^}]*\}", css), \
        "style.css 缺少 [hidden]{display:none!important} 兜底"


# ── 权限弹窗（实现已搬到 modals.js，入口仍在 app.js） ──────────


def test_modal_handlers_toggle_hidden():
    """权限弹窗在 modals.js 实现：showPermission / respondPermission 控制 hidden。

    测试不锁变量名，只锁语义：modals.js 必须能找到 ``$("perm-overlay").hidden =``
    两次（一次 true 一次 false），以及 ``#perm-overlay [data-perm]`` 选择器。
    """
    js = _read("modals.js")
    assert 'overlayEl.hidden = false' not in js, \
        "旧约定 overlayEl 已不存在"
    # modals.js 内显隐切换（顺序不固定，所以分别断言至少一次 =true / =false）
    assert js.count('$("perm-overlay").hidden = false') >= 1
    assert js.count('$("perm-overlay").hidden = true') >= 1
    # 三个权限按钮的接线（querySelectorAll 字符串 / 选择器作用域都可以）
    assert (
        'querySelectorAll("#perm-overlay [data-perm]")' in js
        or 'querySelectorAll("[data-perm]")' in js  # 相对于 overlay 元素
        or '"#perm-overlay [data-perm]"' in js
    )


def test_appjs_dispatches_to_modals():
    """app.js 把 WS 事件分发给 Modals 对象（不再内联 showPermission）。"""
    js = _read("app.js")
    assert 'Modals.showPermission(ev)' in js
    assert 'Modals.showAsk(ev)' in js
    assert 'Modals.showPlan(ev)' in js


# ── 插件 UI 面板（实现搬到 modals.js） ──────────────────────────────


def test_panels_container_starts_hidden():
    """面板区起始 hidden（同权限弹窗不变量：CSS display 不得覆盖 hidden）。"""
    html = _read("index.html")
    assert '<div id="panels" class="panels" hidden>' in html


def test_panels_event_dispatch_and_render():
    """reducer 有 panels 分支（app.js）；渲染走 textContent（modals.js）。"""
    app = _read("app.js")
    modals = _read("modals.js")
    assert 'case "panels":' in app
    assert "renderPanels" in modals
    assert "row.textContent = String(line)" in modals  # XSS 纪律
    # 空面板 → 隐藏面板区
    assert "host.hidden = true" in modals
    assert "host.hidden = false" in modals


def test_css_has_panel_styles():
    css = _read("style.css")
    assert ".panels {" in css
    assert ".panel {" in css
    assert ".panel-line {" in css


# ── P4.1 交互弹窗：ask_user / plan_request ───────────────────────────────


def test_ask_and_plan_overlays_start_hidden():
    """两个交互弹窗必须起始 hidden（同权限弹窗不变量）。"""
    html = _read("index.html")
    assert 'id="ask-overlay" class="overlay" hidden' in html
    assert 'id="plan-overlay" class="overlay" hidden' in html


def test_modals_renders_ask_and_plan_interactively():
    """弹窗实现搬到 modals.js：showAsk / showPlan / respondAsk / respondPlan。"""
    app = _read("app.js")
    modals = _read("modals.js")
    assert 'case "plan_request":' in app
    assert 'case "ask_user":' in app
    assert "function showAsk(ev)" in modals or "showAsk(ev)" in modals
    assert "function showPlan(ev)" in modals or "showPlan(ev)" in modals
    assert "respondAsk" in modals
    assert "respondPlan" in modals
    # 交互通道：上送 ask_user_response / plan_response
    assert 'type: "ask_user_response"' in modals or 'type: "ask_user_response"' in app
    assert 'type: "plan_response"' in modals or 'type: "plan_response"' in app


def test_modals_ask_options_use_textcontent():
    """选项/问题/自定义答案是模型产物——必须 textContent，绝不 innerHTML。"""
    modals = _read("modals.js")
    assert "lab.textContent = opt.label" in modals
    assert "desc.textContent = opt.description" in modals
    assert '$("ask-question").textContent = ev.question' in modals
    # plan 是唯一走 renderMarkdown 的（先转义后渲染，XSS-safe）
    assert '$("plan-details").innerHTML = renderMarkdown(ev.plan' in modals


def test_modals_handles_skip_and_other():
    """Skip 发空答（服务端落保守默认）；Other 走自由文本输入。"""
    html = _read("index.html")
    modals = _read("modals.js")
    assert 'id="ask-skip-btn"' in html
    assert "respondAsk([])" in modals
    assert "ask-custom-input" in modals
    assert "custom ? [custom] : Array.from(s.selected)" in modals


# ── P4.5 新增：三栏布局的最小不变量 ────────────────────────────────


def test_three_pane_layout_ids_present():
    """三栏关键 id 必须在 index.html 中存在（防布局骨架被破坏）。"""
    html = _read("index.html")
    for pid in ("sidebar", "chat", "task-panel"):
        assert f'id="{pid}"' in html, f"缺失 #{pid}"


def test_three_pane_resizers_present():
    """两个 resizer 拖拽分隔条都必须在 DOM 中（拖拽功能依赖）。"""
    html = _read("index.html")
    assert 'data-resize="sidebar"' in html
    assert 'data-resize="taskpanel"' in html


def test_task_panel_three_tabs():
    """右栏任务面板三个标签都存在：任务流 / 上下文 / 产物。"""
    html = _read("index.html")
    assert 'data-tab="flow"' in html
    assert 'data-tab="context"' in html
    assert 'data-tab="artifacts"' in html


def test_chat_input_and_send_present():
    """中栏输入区关键控件：textarea、send-btn、turn-bar。"""
    html = _read("index.html")
    assert 'id="input"' in html
    assert 'id="send-btn"' in html
    assert 'id="turn-bar"' in html
    assert 'id="interrupt-btn"' in html


def test_no_build_artifacts_in_static():
    """vanilla JS 部署：禁止打包产物（map / min / 哈希后缀）。"""
    for f in WEB_DIR.iterdir():
        if f.is_file():
            n = f.name
            assert not n.endswith(".map"), f"禁止源码映射：{n}"
            assert not n.endswith(".min.js"), f"禁止压缩产物：{n}"
            assert not re.search(r"\.[a-f0-9]{8}\.(js|css)$", n), f"禁止哈希后缀：{n}"