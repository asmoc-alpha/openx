"use strict";

/* OpenX Serve — 主控制器：三栏布局接线层。
   职责边界：本文件只做「事件 → DOM」的编排 + 全局 WS / 启动序。
   - common.js：$ / el / escapeHtml / renderMarkdown / OX API / toast
   - sidebar.js：左栏（工作区树 / 会话 / 用户）
   - chat.js：中栏（消息 + 输入）
   - task-panel.js：右栏（任务流 / 上下文 / 产物）
   - trace.js：右栏「路径」标签（任务路径，REST 拉取）
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
  // 发件箱（至少一次投递）：[{id, text, timer}]。message 意图经
  // sendMessage 入箱，收到服务端 message_ack 才出箱；断连期间留在箱里，
  // 重连 onopen 冲刷重发（服务端按 msg_id 去重，回合不跑两遍）。
  outbox: [],
  // 是否有独立视觉模型（modal）承接图片回合；由 GET /api/info 填充。
  // false 时附图片发送前给提示（仍允许发送）。
  hasVision: true,
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

/* ── 布局：宽度 + 左右栏收缩 ─────────────────────────────────────
   两份状态一起持久化：拖拽得到的栏宽、以及两栏的展开/收起。
   收起态写在 #layout 的 data-* 上（CSS 据此把宽度压到 0），
   inline style.width 不动，展开即恢复上次宽度。 */
const LAYOUT_KEY = "openx.serve.layout.v2";

const PANES = {
  sidebar:   { el: "sidebar",    width: 280, min: 180, max: 420 },
  taskpanel: { el: "task-panel", width: 360, min: 260, max: 560 },
};

// 开关按钮字形的方向随状态换向（与 title 同步）：展开态显示收拢箭头。
const PANE_GLYPH = {
  sidebar:   { open: "«", collapsed: "»" },
  taskpanel: { open: "»", collapsed: "«" },
};

// 开关按钮的 title：按当前展开/收起切换文案，让「点了会发生什么」一目了然
const PANE_TOGGLE_TITLE = {
  sidebar:   { collapsed: "展开侧栏 (⌘B)",     open: "收起侧栏 (⌘B)" },
  taskpanel: { collapsed: "展开任务面板 (⌘J)", open: "收起任务面板 (⌘J)" },
};

function readLayout() {
  const def = {
    sidebar: PANES.sidebar.width,
    taskpanel: PANES.taskpanel.width,
    sidebarCollapsed: false,
    taskpanelCollapsed: false,
  };
  try {
    const s = JSON.parse(localStorage.getItem(LAYOUT_KEY) || "{}");
    if (Number(s.sidebar) >= PANES.sidebar.min) def.sidebar = Number(s.sidebar);
    if (Number(s.taskpanel) >= PANES.taskpanel.min) def.taskpanel = Number(s.taskpanel);
    def.sidebarCollapsed = s.sidebarCollapsed === true;
    def.taskpanelCollapsed = s.taskpanelCollapsed === true;
  } catch (_) { /* 坏值回落默认 */ }
  return def;
}

function saveLayout() {
  const layout = $("layout");
  if (!layout) return;
  try {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify({
      sidebar: parseInt($("sidebar").style.width, 10) || PANES.sidebar.width,
      taskpanel: parseInt($("task-panel").style.width, 10) || PANES.taskpanel.width,
      sidebarCollapsed: layout.dataset.sidebar === "collapsed",
      taskpanelCollapsed: layout.dataset.taskpanel === "collapsed",
    }));
  } catch (_) { /* 写入失败忽略 */ }
}

function restoreLayout() {
  const s = readLayout();
  $("sidebar").style.width = s.sidebar + "px";
  $("task-panel").style.width = s.taskpanel + "px";
  setPaneCollapsed("sidebar", s.sidebarCollapsed, false);
  setPaneCollapsed("taskpanel", s.taskpanelCollapsed, false);
}

function isCollapsed(name) {
  const layout = $("layout");
  return Boolean(layout) && layout.dataset[name] === "collapsed";
}

function setPaneCollapsed(name, collapsed, persist = true) {
  const layout = $("layout");
  if (!layout || !PANES[name]) return;
  layout.dataset[name] = collapsed ? "collapsed" : "open";
  syncPaneToggles();
  if (persist) saveLayout();
  // 左栏收成 rail 时品牌钮退化为「展开侧栏」入口
  if (name === "sidebar") {
    const b = $("brand");
    if (b) b.title = collapsed ? "展开侧栏 (⌘B)" : "新建对话";
  }
  // 中栏宽度随之变化：画板 / 代码高亮之类依赖宽度的组件可据此重排
  window.dispatchEvent(new CustomEvent("openx:layout", {
    detail: { pane: name, collapsed },
  }));
}

function togglePane(name) {
  setPaneCollapsed(name, !isCollapsed(name));
}

/** 所有 data-pane-toggle 按钮（顶栏两个 + 栏内两个）同步到当前状态 */
function syncPaneToggles() {
  document.querySelectorAll("[data-pane-toggle]").forEach((btn) => {
    const name = btn.dataset.paneToggle;
    if (!PANES[name]) return;
    const collapsed = isCollapsed(name);
    btn.classList.toggle("is-collapsed", collapsed);
    btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
    const t = PANE_TOGGLE_TITLE[name];
    if (t) btn.title = collapsed ? t.collapsed : t.open;
    const g = PANE_GLYPH[name];
    if (g) btn.textContent = collapsed ? g.collapsed : g.open;
  });
}

function bindPaneToggles() {
  document.querySelectorAll("[data-pane-toggle]").forEach((btn) => {
    btn.addEventListener("click", () => togglePane(btn.dataset.paneToggle));
  });
  // ⌘B / ⌘J（Windows 上为 Ctrl）：与主流编辑器一致的双栏快捷键
  document.addEventListener("keydown", (e) => {
    if (!(e.metaKey || e.ctrlKey) || e.altKey) return;
    const k = e.key.toLowerCase();
    if (k === "b") { e.preventDefault(); togglePane("sidebar"); }
    else if (k === "j") { e.preventDefault(); togglePane("taskpanel"); }
  });
}

function bindResizers() {
  document.querySelectorAll(".resizer").forEach((r) => {
    const name = r.dataset.resize;
    const spec = PANES[name];
    if (!spec) return;
    const pane = $(spec.el);
    if (!pane) return;
    let dragging = false;
    r.addEventListener("mousedown", (e) => {
      // 收起态没有分隔条（display:none），这里只是防御
      if (isCollapsed(name)) return;
      dragging = true;
      r.classList.add("dragging");
      document.body.classList.add("is-resizing");
      document.body.style.cursor = "col-resize";
      e.preventDefault();
    });
    document.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      const raw = name === "sidebar"
        ? e.clientX
        : window.innerWidth - e.clientX;
      const w = Math.min(spec.max, Math.max(spec.min, raw));
      pane.style.width = w + "px";
    });
    document.addEventListener("mouseup", () => {
      if (!dragging) return;
      dragging = false;
      r.classList.remove("dragging");
      document.body.classList.remove("is-resizing");
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
        // 新会话/切区/删除当前会话都经 init 广播——任务面板状态是
        // 会话级的（执行计划/上下文引用/统计），一并归位（见 onSessionReset）
        TaskPanel.onSessionReset();
      }
      break;
    case "history":
      renderHistory(ev.messages || []);
      break;
    case "user_message":
      // 带附件/多模态时 ev.content 为 parts 列表（缩略图/文件 chip）；纯文本
      // 事件无 content，仍走 text（与旧版一致）
      Chat.appendUser(ev);
      AppState.streaming = true;
      $("messages").classList.add("streaming");
      Chat.startTurn();   // 正文/思考/工具缓冲按回合隔离（见 startTurn）
      showTurnBar(true);
      TaskPanel.onTurnStart(
        ev.text || (Array.isArray(ev.content) && ev.content.length ? "📎 附带内容" : "")
      );
      updateBreadcrumb();
      break;
    case "text_delta":
      // 非会话态（!streaming）的 text_delta 是越带外提示（如模型切换的
      // “⟳”元提示）——不进正文，也不该在空态误建一条助手消息。
      if (!AppState.streaming) break;
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
    case "message_ack":
      // 服务端收到即回执（不经回合队列）：从发件箱出箱 + 停看门狗
      AppState.ackOutbox(ev.msg_id || "");
      break;
    case "todos":
      // 执行计划快照（todo_write 全量；attach 补发）→ 任务流 tab
      TaskPanel.onTodos(ev.todos || []);
      break;
    case "fleet":
      // 子 agent 快照（task 委派；变化才广播）→ 任务流 tab
      TaskPanel.onFleet(ev.agents || []);
      break;
    case "result":
      AppState.streaming = false;
      $("messages").classList.remove("streaming");
      Chat.commitStream();   // 同步提交最终正文（见 commitStream 竞态说明）
      showTurnBar(false);
      Chat.appendMeta(doneLabel(ev));
      Chat.finalizeTurn();
      TaskPanel.onResult(ev);
      Sidebar.reload().then(() => Sidebar.renderAll());
      // 回合结束 = 路径多了一轮：标签可见时才拉（不可见等切标签时拉）
      if (typeof Trace !== "undefined" && Trace.visible()) Trace.load(Trace.sessionId);
      break;
    case "interrupted":
      AppState.streaming = false;
      $("messages").classList.remove("streaming");
      Chat.commitStream();   // 保留已生成的半截回复，不清 buffer
      showTurnBar(false);
      Chat.appendMeta("⏹ Interrupted");
      Chat.finalizeTurn();
      TaskPanel.onInterrupted();
      if (typeof Trace !== "undefined" && Trace.visible()) Trace.load(Trace.sessionId);
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
    if (m.role === "user") Chat.appendUser(m.content);
    else if (m.role === "assistant") {
      Chat.createAssistantCard({});
      Chat.setAssistantContent(textOf(m.content));
    } else if (m.role === "tool") {
      Chat.appendToolResult(m.name || "tool", false, textOf(m.content));
    }
  }
}

function showTurnBar(show) {
  // 回答状态不再显示在对话框上方：改由发送钮表达（空闲 "→" 发送；
  // 回答中变 "■" 停止 + 脉冲动画），点击发送钮即中断。turn-bar 保持隐藏。
  if (typeof Chat !== "undefined" && Chat.setStreaming) Chat.setStreaming(Boolean(show));
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
    AppState.flushOutbox();   // 断连期间积压的消息：重连即补发（服务端按 msg_id 去重）
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

// 网络恢复（如 DevTools Offline → Online）：不等 1s 重试计时，立即重连
window.addEventListener("online", () => {
  if (AppState.ws && AppState.ws.readyState !== WebSocket.OPEN) {
    clearTimeout(AppState.reconnectTimer);
    connect();
  }
});

// ── AppState public API ────────────────────────────────────────────
AppState.send = function (obj) {
  // 瞬态意图（interrupt / permission_response / ...）直发，不排队：
  // 断连后 request_id 已失效，权限桥 fail-closed 已兜底。
  if (AppState.ws && AppState.ws.readyState === WebSocket.OPEN) {
    AppState.ws.send(JSON.stringify(obj));
  }
};

/* ── 用户消息的至少一次投递（outbox + message_ack）────────────────
   只覆盖 message 意图：入箱 → 发送 → 收到回执出箱。断连时留在箱里
   （WS 非 OPEN 发不出去），重连 onopen 冲刷重发；半开连接（表面 OPEN
   实际已死）靠看门狗发现：回执是服务端收到即回、不经回合队列，迟迟
   无回执 = 消息根本没到 → 主动断开触发重连。重复发送由服务端按
   msg_id 去重（回合不跑两遍），所以客户端可以放心重发。 */
function newMsgId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return "m-" + Date.now() + "-" + Math.random().toString(36).slice(2, 10);
}

const ACK_WATCHDOG_MS = 10000;

AppState.sendMessage = function (text, attachments) {
  // attachments：随消息引用的上传 id 列表（/api/upload 预传；服务端解析成
  // 图片 data-url / 文件 part）。断连重发时 id 不变，服务端按 msg_id 去重。
  const entry = { id: newMsgId(), text, timer: 0, attachments: attachments || [] };
  AppState.outbox.push(entry);
  if (!AppState.flushOutbox()) {
    OX.toast("连接断开，消息将在重连后自动发送", "err");
  }
};

AppState.flushOutbox = function () {
  const ws = AppState.ws;
  if (!ws || ws.readyState !== WebSocket.OPEN) return false;
  let sent = false;
  for (const m of AppState.outbox) {
    try {
      const frame = { type: "message", text: m.text, msg_id: m.id };
      if (m.attachments && m.attachments.length) frame.attachments = m.attachments;
      ws.send(JSON.stringify(frame));
      sent = true;
      // 发出即武装看门狗：ACK_WATCHDOG_MS 内无回执 → 判半开，断线重连
      clearTimeout(m.timer);
      m.timer = setTimeout(() => AppState._ackTimeout(m), ACK_WATCHDOG_MS);
    } catch (_) {
      return sent;   // 发送竞态抛错：后续条目留在箱里，等 onclose→重连→onopen
    }
  }
  return sent;
};

AppState.ackOutbox = function (msgId) {
  if (!msgId) return;
  AppState.outbox = AppState.outbox.filter((m) => {
    if (m.id !== msgId) return true;
    clearTimeout(m.timer);
    return false;
  });
};

AppState._ackTimeout = function (entry) {
  // 仍在箱里且未回执 = 服务端从未收到（回执不经回合队列，10s 足够宽）：
  // 半开连接。主动断开 → onclose 走重连 → onopen 冲刷重发（服务端去重）。
  if (AppState.outbox.indexOf(entry) === -1) return;
  if (AppState.ws && AppState.ws.readyState === WebSocket.OPEN) {
    OX.toast("连接无响应，正在重连…", "err");
    AppState.ws.close();
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
  // 路径面板随之切到被回放的会话（系统 prompt 如实显示"未记录"）
  if (typeof Trace !== "undefined") Trace.load(sessionId);
};

function applyReplayEvent(ev) {
  if (!ev || typeof ev !== "object") return;
  if (ev.type === "message") {
    const m = ev.message || {};
    if (m.role === "user") Chat.appendUser(m.content);
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
    AppState.hasVision = Boolean(info.has_vision);
    if (info.session_id) AppState.sessionId = info.session_id;
    Sidebar.activeSession = AppState.sessionId;
  } catch (_) { /* 服务未就绪：chip 留空 */ }
  updateBreadcrumb();
  Sidebar.renderAll();    // 工作区树 + 环境卡（同一批真实事实）
  TaskPanel.renderStats();
  // 启动即空态：输入框居中 + 填充目录/模型选择（进入会话态时会自动贴底）
  if (typeof Chat !== "undefined" && Chat.enterEmpty) Chat.enterEmpty();
}

async function boot() {
  bindResizers();
  bindPaneToggles();
  restoreLayout();
  Chat.init();
  Modals.init();
  TaskPanel.init();
  if (typeof Trace !== "undefined") Trace.init();
  if (typeof Artifacts !== "undefined") Artifacts.init();
  if (typeof Settings !== "undefined") Settings.init();
  if (typeof WebPlugins !== "undefined") WebPlugins.init();
  await Sidebar.init();
  await loadInfo();
  connect();
}

document.addEventListener("DOMContentLoaded", boot);