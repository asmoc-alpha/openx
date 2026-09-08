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
    # 回合条必须起始 hidden。元素普遍带 data-page-node-id 尾随属性，故按
    # "div 的 hidden 属性存在"断言而非精确子串（后者对属性顺序/追加过脆）。
    assert re.search(r'<div id="turn-bar"[^>]*\bhidden\b', html), \
        "回合条必须起始 hidden"


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
    assert re.search(r'<div id="panels"[^>]*\bhidden\b', html)


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


def test_flow_tab_plan_and_agent_sections():
    """任务流 tab 含执行计划 / 子 agent 两块清单的 DOM 骨架。"""
    html = _read("index.html")
    for pid in ("plan-list", "plan-meta", "plan-empty",
                "agent-list", "agent-meta", "agent-empty"):
        assert f'id="{pid}"' in html, f"缺失 #{pid}"


def test_composer_decorative_icons_removed():
    """对话框下方装饰图标（附件/代码块/图片/MCP/语音）无副作用，整体移除。

    只留右侧发送钮（真实功能：发送 / 回答中变 ■ 停止）。
    """
    html = _read("index.html")
    css = _read("style.css")
    assert "tool-btn" not in html and "toolbar-left" not in html
    assert "tool-btn" not in css and "toolbar-left" not in css
    assert 'id="send-btn"' in html  # 发送钮仍在


def test_appjs_dispatches_todos_and_fleet_to_taskpanel():
    """reducer 把 todos / fleet 状态快照交给 TaskPanel，init 触发面板归位。"""
    app = _read("app.js")
    assert 'case "todos":' in app
    assert "TaskPanel.onTodos(ev.todos || [])" in app
    assert 'case "fleet":' in app
    assert "TaskPanel.onFleet(ev.agents || [])" in app
    assert "TaskPanel.onSessionReset()" in app


def test_taskpanel_renders_plan_and_agents_with_textcontent():
    """执行计划 / 子 agent 渲染走 textContent（模型文本，XSS 纪律）。"""
    js = _read("task-panel.js")
    assert "onSessionReset() {" in js
    assert "onTodos(list) {" in js and "onFleet(list) {" in js
    assert "renderPlan()" in js and "renderAgents()" in js
    # 计划行主文案 / 子代理名都是模型产物文本
    assert "title.textContent = t.content" in js
    assert "name.textContent = label" in js


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


# ── 侧栏：工作区目录下的会话展示 + 删除 ─────────────────────────


def test_sidebar_lists_sessions_under_each_group():
    """每个目录组都要渲染其下会话，且标题/时间走 textContent（XSS 纪律）。"""
    js = _read("sidebar.js")
    assert "this._renderSessionItem(s)" in js
    assert 'title.textContent = s.title || "新会话"' in js
    assert "time.textContent = s.time ||" in js
    # 首载全部目录默认展开（会话不用翻折叠直接可见）；组头点击可收起
    assert "for (const g of this.workspaces)" in js
    assert "this.expanded.add(g.workspace)" in js
    assert "groupLi.classList.add(\"open\")" in js
    assert "head.onclick = () => this.toggleGroup(path)" in js


def test_sidebar_session_delete_wired_to_api():
    """会话项带删除按钮，删除走 DELETE /api/sessions/{id}。"""
    js = _read("sidebar.js")
    assert "session-del" in js
    assert "this.deleteSession(s)" in js
    assert 'del.title = "删除会话"' in js
    assert "OX.del(`/api/sessions/${encodeURIComponent(sid)}`)" in js
    # 删掉的正是当前打开的会话 → 刷新回实时视图
    assert "location.reload()" in js


def test_css_sessions_visible_only_when_group_open():
    """会话列表默认 display:none，仅 .ws-group.open 时显示（折叠门控在 CSS）。

    防止回归成「目录组渲染了会话却从不加 open → 会话被 CSS 永久藏住」。
    """
    css = _read("style.css")
    assert ".ws-sessions {" in css
    assert ".ws-group.open > .ws-sessions { display: flex; }" in css


# ── 断连恢复：outbox + message_ack 客户端接线 ───────────────────


def test_chat_submit_goes_through_send_message():
    """发送走 AppState.sendMessage（outbox 至少一次投递），不再直发丢消息。"""
    js = _read("chat.js")
    assert "AppState.sendMessage(text, attIds)" in js
    assert "AppState.send({ type: \"message\", text })" not in js


def test_appjs_message_ack_and_outbox_wiring():
    """reducer 有 message_ack 分支；outbox 的入箱/冲刷/出箱/看门狗齐全。"""
    app = _read("app.js")
    assert 'case "message_ack":' in app
    assert 'AppState.ackOutbox(ev.msg_id || "")' in app
    assert "outbox: []," in app
    for fn in ("sendMessage", "flushOutbox", "ackOutbox", "_ackTimeout"):
        assert f"AppState.{fn} =" in app, f"缺 AppState.{fn}"
    # 重连 onopen 冲刷积压；发送帧携带 msg_id（服务端据此去重）
    assert "AppState.flushOutbox()" in app
    assert "msg_id: m.id" in app
    # 半开连接探测：发出即武装看门狗
    assert "ACK_WATCHDOG_MS" in app


# ── 流程图渲染：graph.js + common.js + CSS 接线 ─────────────────


def test_graph_script_loaded_after_common_before_app():
    """graph.js 必须在 common.js 之后（escapeHtml 依赖）、app.js 之前加载。"""
    html = _read("index.html")
    i_common = html.index("/static/common.js")
    i_graph = html.index("/static/graph.js")
    i_app = html.index("/static/app.js")
    assert i_common < i_graph < i_app


def test_graph_js_defines_render_and_escapes():
    """graph.js：导出 renderGraph；标签一律转义；异常回落空串（不抛不半渲染）。"""
    js = _read("graph.js")
    assert "function renderGraph(" in js
    assert "escapeHtml" in js          # 节点/边标签 XSS 纪律
    assert "renderGraph(code)" in js   # 供 common.js 围栏块分流调用
    assert "svg = \"\"" in js          # 解析/布局异常 → 空串回落


def test_common_render_fence_wires_mermaid_to_graph():
    """renderMarkdown 的围栏块：mermaid → renderGraph 渲染，缺席/失败回落代码块。"""
    common = _read("common.js")
    assert "info === \"mermaid\"" in common
    assert "typeof renderGraph === \"function\"" in common
    assert '<div class="graph-block">' in common
    assert "<pre><code>" in common       # 回落路径仍在
    # 只抽已闭合围栏（流式中的未闭合块按纯文本走，闭合瞬间才成块）
    assert "([^\\n]*)\\n([\\s\\S]*?)```" in common


def test_css_has_graph_styles():
    """样式齐全且配色走 CSS 变量（明暗主题自适应）。"""
    css = _read("style.css")
    for sel in (".graph-block", ".gg-node", ".gg-node-text", ".gg-edge",
                ".gg-arrow", ".gg-edge-label"):
        assert sel in css, f"缺 {sel}"
    assert ".graph-block svg" in css

# ── 上传图片/文件：前端接线 ───────────────────────────────────


def test_composer_has_attach_button_and_input():
    """composer 有 📎 钮 + 隐藏的多选文件输入。"""
    html = _read("index.html")
    assert 'id="attach-btn"' in html
    assert 'id="attach-input"' in html
    assert 'type="file"' in html
    assert "multiple" in html


def test_common_js_upload_helper():
    """OX.upload：multipart POST /api/upload，返回描述符。"""
    js = _read("common.js")
    assert "async upload(file)" in js
    assert 'fetch("/api/upload"' in js
    assert 'fd.append("file", file)' in js


def test_chat_pending_attachment_wiring():
    """待发附件生命周期：选择→上传→chips→发送；空态/发送后清空。"""
    js = _read("chat.js")
    assert "pending: []" in js
    for fn in ("handleAttachFiles", "renderPending", "removePending", "clearPending"):
        assert fn + "(" in js, f"缺 {fn}"
    assert "AppState.sendMessage(text, attIds)" in js
    assert "this.clearPending();" in js          # enterEmpty 清待发
    assert "OX.upload(file)" in js


def test_chat_renders_user_content_parts():
    """用户气泡渲染 parts：text / image_url / openx_file，XSS 走 textContent。"""
    js = _read("chat.js")
    assert "appendUser(msg)" in js
    assert '"image_url"' in js
    assert '"openx_file"' in js
    assert "_renderUserParts(row, content)" in js
    assert "Artifacts.preview" in js
    assert "t.textContent = part.text" in js     # 文本不进 innerHTML


def test_appjs_outbox_and_has_vision():
    """发送帧携带附件 id（断连重发服务端去重）；has_vision 供图片提示。"""
    app = _read("app.js")
    assert "function (text, attachments)" in app
    assert "frame.attachments = m.attachments" in app
    assert "hasVision" in app


def test_appjs_renders_user_content_all_paths():
    """live + history + 复盘三条用户消息路径都改走 appendUser(content)。"""
    app = _read("app.js")
    assert "Chat.appendUser(ev);" in app          # live user_message
    assert app.count("Chat.appendUser(m.content)") >= 2   # renderHistory + 复盘


def test_css_has_attachment_styles():
    css = _read("style.css")
    for sel in (".attach-btn", ".attach-chip", ".chip-remove",
                ".msg.user .u-img", ".msg.user .u-file", ".msg.user .u-text"):
        assert sel in css, f"缺 {sel}"
