"use strict";

/* OpenX Serve — 主控制器：三栏布局接线层。
   职责边界：本文件只做「事件 → DOM」的编排 + 全局 WS / 启动序。
   - common.js：$ / el / escapeHtml / renderMarkdown / OX API / toast
   - sidebar.js：左栏（工作区树 / 会话 / 用户）
   - chat.js：中栏（消息 + 输入）
   - task-panel.js：右栏（任务流 / 上下文 / 产物）
   - modals.js：弹窗（permission / ask_user / plan）+ 插件面板
   - artifacts.js：产物标签内容（沿用旧版）
   - settings.js：设置弹窗（沿用旧版）

   XSS 纪律：模型/工具/路径文本走 textContent 或先 renderMarkdown。 */

const AppState = {
  ws: null,
  reconnectTimer: null,
  sessionId: "",
  workspace: "",
  model: "",
  streaming: false,
  streamBuf: "",
  replaying: false,
};

function setConn(stateName, label) {
  const el2 = $("conn");
  if (!el2) return;
  el2.dataset.state = stateName;
  el2.textContent = label || stateName;
  // 欢迎页的状态点已移除（首屏极简），仍保留对 Chat.setWelcomeHint 的
  // 调用以兼容潜在第三方 hook；该 API 在新版 chat.js 是 no-op。
  if (typeof Chat !== "undefined" && Chat.setWelcomeHint) {
    const hintText =
      stateName === "connecting" ? "正在连接服务…" :
      stateName === "disconnected" ? "连接断开 · 自动重试中" :
      stateName === "replay" ? "回放历史会话（不可发送新消息）" :
      "就绪";
    Chat.setWelcomeHint(hintText, stateName);
  }
}

const LAYOUT_KEY = "openx.serve.layout.v2";

function restoreLayout() {
  try {
    const saved = JSON.parse(localStorage.getItem(LAYOUT_KEY) || "{}");
    if (saved.sidebar) $("sidebar").style.width = saved.sidebar + "px";
    if (saved.taskpanel) $("task-panel").style.width = saved.taskpanel + "px";
  } catch (_) { /* 坏值忽略 */ }
}

function saveLayout() {
  try {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify({
      sidebar: parseInt($("sidebar").style.width, 10) || 260,
      taskpanel: parseInt($("task-panel").style.width, 10) || 340,
    }));
  } catch (_) { /* 写入失败忽略 */ }
}

function bindResizers() {
  document.querySelectorAll(".resizer").forEach((r) => {
    const target = r.dataset.resize;
    const pane = $(target === "taskpanel" ? "task-panel" : target);
    if (!pane) return;
    let dragging = false;
    r.addEventListener("mousedown", (e) => {
      dragging = true;
      r.classList.add("dragging");
      document.body.style.cursor = "col-resize";
      e.preventDefault();
    });
    document.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      let w;
      if (target === "sidebar") {
        w = Math.min(400, Math.max(180, e.clientX));
      } else {
        // 折叠态（width:0）的任务面板拖不动；跳过让用户先点开
        if (pane.dataset.visible === "false") return;
        w = Math.min(560, Math.max(260, window.innerWidth - e.clientX));
      }
      pane.style.width = w + "px";
    });
    document.addEventListener("mouseup", () => {
      if (!dragging) return;
      dragging = false;
      r.classList.remove("dragging");
      document.body.style.cursor = "";
      saveLayout();
    });
  });
}

// ── 事件 reducer（WS → DOM） ───────────────────────────────────────
function textOf(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.map((p) => (p && p.text) || "").join("\n");
  return "";
}

function doneLabel(ev) {
  const parts = [];
  if (ev.is_error) parts.push("✗ error");
  parts.push(`${ev.num_turns ?? 0} tool turns`);
  const u = ev.usage || {};
  parts.push(`${u.input_tokens ?? 0}/${u.output_tokens ?? 0} tokens`);
  return "✓ Done · " + parts.join(" · ");
}

function applyEvent(ev) {
  switch (ev.type) {
    case "system":
      if (ev.subtype === "init") {
        AppState.sessionId = ev.session_id || "";
        AppState.model = ev.model || "";
        updateBreadcrumb();
        Sidebar.activeSession = AppState.sessionId;
        Sidebar.renderAll();
      }
      break;
    case "history":
      renderHistory(ev.messages || []);
      break;
    case "user_message":
      Chat.appendUser(ev.text || "");
      AppState.streaming = true;
      $("messages").classList.add("streaming");
      Chat.streamBuf = "";
      Chat.lastAssistant = null;
      showTurnBar(true, "working…");
      TaskPanel.onTurnStart(ev.text || "");
      updateBreadcrumb();
      break;
    case "text_delta":
      Chat.streamBuf += ev.text || "";
      Chat.scheduleFlush();
      TaskPanel.onTextDelta();
      break;
    case "thinking_delta":
      Chat.appendThinking(ev.text || "");
      TaskPanel.onThinking();
      break;
    case "tool_use":
      // target 由服务端从工具入参派生（绝不原样回传入参）；无目标时退回摘要
      Chat.appendToolStart(ev.name || "tool", ev.target || ev.args_summary || "");
      TaskPanel.onToolUse(ev.name || "tool", ev.target || "");
      break;
    case "tool_result":
      Chat.appendToolResult(ev.name || "tool", Boolean(ev.is_error), ev.output || "");
      TaskPanel.onToolResult(ev.name || "tool", Boolean(ev.is_error));
      break;
    case "artifact":
      if (typeof Artifacts !== "undefined") Artifacts.push(ev.path || "", ev.tool || "");
      break;
    case "result":
      AppState.streaming = false;
      $("messages").classList.remove("streaming");
      Chat.streamBuf = "";
      showTurnBar(false);
      Chat.appendMeta(doneLabel(ev));
      TaskPanel.onResult(ev);
      Sidebar.reload().then(() => Sidebar.renderAll());
      break;
    case "interrupted":
      AppState.streaming = false;
      $("messages").classList.remove("streaming");
      Chat.streamBuf = "";
      showTurnBar(false);
      Chat.appendMeta("⏹ Interrupted");
      TaskPanel.onInterrupted();
      break;
    case "permission_request":
      Modals.showPermission(ev);
      break;
    case "ask_user":
      Modals.showAsk(ev);
      break;
    case "plan_request":
      Modals.showPlan(ev);
      break;
    case "panels":
      Modals.renderPanels(ev.panels || []);
      break;
    default:
      break; // 未知事件容忍（前向兼容）
  }
}

function renderHistory(messages) {
  // 若首屏已用示例对话填充（seedDemoChat），且服务端回放为空，
  // 保留示例数据，给用户先看到布局再被覆盖也来得及。
  // 若回放非空（真实历史），永远以历史为准——清空并重渲染。
  if (!messages || !messages.length) {
    return; // 保留示例 / 空状态，避免覆盖首屏
  }
  Chat.clearAll();
  for (const m of messages) {
    if (!m || typeof m !== "object") continue;
    if (m.role === "user") Chat.appendUser(textOf(m.content));
    else if (m.role === "assistant") {
      Chat.createAssistantCard({});
      Chat.setAssistantContent(textOf(m.content));
    } else if (m.role === "tool") {
      Chat.appendToolResult(m.name || "tool", false, textOf(m.content));
    }
  }
}

function showTurnBar(show, status) {
  const bar = $("turn-bar");
  bar.hidden = !show;
  if (status) $("turn-status").textContent = status;
}

// ── 顶栏 / 面包屑 ──────────────────────────────────────────────────
// 三级全部是真实会话事实：模型 / 工作区目录 / 当前回合标题。
function updateBreadcrumb() {
  const crumb = (key) => $("breadcrumb").querySelector(`[data-key="${key}"]`);
  const model = AppState.model || "";
  const ws = AppState.workspace || "";
  if (crumb("agent")) crumb("agent").textContent = model || "OpenX";
  if (crumb("mode")) crumb("mode").textContent = ws ? ws.split("/").filter(Boolean).pop() : "未挂载工作区";
  if (crumb("task")) {
    crumb("task").textContent = (TaskPanel.turn && TaskPanel.turn.title) || "新会话";
  }
}

// ── WS 连接 ────────────────────────────────────────────────────────
function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  AppState.ws = new WebSocket(`${proto}//${location.host}/ws`);
  AppState.ws.onopen = () => {
    if (!AppState.replaying) setConn("connected", "connected");
  };
  AppState.ws.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch (_) { return; }
    applyEvent(ev);
  };
  AppState.ws.onclose = () => {
    if (!AppState.replaying) setConn("disconnected", "disconnected · retrying");
    clearTimeout(AppState.reconnectTimer);
    AppState.reconnectTimer = setTimeout(connect, 1000);
  };
}

// ── AppState public API ────────────────────────────────────────────
AppState.send = function (obj) {
  if (AppState.ws && AppState.ws.readyState === WebSocket.OPEN) {
    AppState.ws.send(JSON.stringify(obj));
  }
};

AppState.clearMessages = function () {
  Chat.clearAll();
};

AppState.openReplay = async function (sessionId) {
  AppState.replaying = true;
  setConn("replay", `replay · ${String(sessionId).slice(0, 8)}（点击返回实时）`);
  $("conn").title = "点击返回实时视图";
  Chat.clearAll();
  try {
    const data = await OX.get(`/api/sessions/${encodeURIComponent(sessionId)}/events`);
    Chat.appendMeta(`↻ Replaying session ${sessionId}`);
    for (const ev of (data && data.events) || []) applyReplayEvent(ev);
    Chat.appendMeta("— end of replay —");
  } catch (err) {
    Chat.appendMeta("Failed to load replay: " + err.message);
  }
};

function applyReplayEvent(ev) {
  if (!ev || typeof ev !== "object") return;
  if (ev.type === "message") {
    const m = ev.message || {};
    if (m.role === "user") Chat.appendUser(textOf(m.content));
    else if (m.role === "assistant") {
      Chat.createAssistantCard({});
      Chat.setAssistantContent(textOf(m.content));
    } else if (m.role === "tool") Chat.appendToolResult(m.name || "tool", false, textOf(m.content));
  } else if (ev.type === "permission_decision") {
    Chat.appendMeta(`🔒 ${ev.tool} → ${ev.approved ? "allowed" : "denied"} (${ev.verdict})`);
  } else if (ev.type === "provider_selected") {
    Chat.appendMeta(`🤖 provider: ${ev.provider} · ${ev.model}`);
  } else if (ev.type === "plugin_loaded" || ev.type === "plugin_failed") {
    Chat.appendMeta(`🧩 ${ev.type}: ${ev.plugin}`);
  }
}

function exitReplay() {
  if (!AppState.replaying) return;
  location.reload();
}

$("conn").onclick = exitReplay;

// ── 启动 ─────────────────────────────────────────
async function loadInfo() {
  try {
    const info = await OX.get("/api/info");
    AppState.workspace = info.workspace || "";
    AppState.model = info.model || "";
    if (info.session_id) AppState.sessionId = info.session_id;
    Sidebar.activeSession = AppState.sessionId;
  } catch (_) { /* 服务未就绪：chip 留空 */ }
  updateBreadcrumb();
  Sidebar.renderAll();    // 工作区树 + 环境卡（同一批真实事实）
  TaskPanel.renderStats();
}

async function boot() {
  bindResizers();
  restoreLayout();
  Chat.init();
  Modals.init();
  TaskPanel.init();
  if (typeof Artifacts !== "undefined") Artifacts.init();
  if (typeof Settings !== "undefined") Settings.init();
  await Sidebar.init();
  await loadInfo();
  connect();
}

document.addEventListener("DOMContentLoaded", boot);