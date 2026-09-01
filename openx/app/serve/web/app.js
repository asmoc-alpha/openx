"use strict";

/* OpenX Serve — 薄客户端：渲染事件流、上送意图流（架构详设 §5）。
   纯函数 reducer：不持会话状态语义，事件驱动渲染。
   XSS 纪律：任何模型/工具文本先 escapeHtml 再进 innerHTML；工具输出走
   textContent；链接只放行 http/https/相对路径。 */

// ── DOM ────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const messagesEl = $("messages");
const inputEl = $("input");
const connEl = $("conn");
const metaEl = $("meta");
const overlayEl = $("perm-overlay");
const askOverlayEl = $("ask-overlay");
const planOverlayEl = $("plan-overlay");
const sessionListEl = $("session-list");
const turnBarEl = $("turn-bar");
const turnStatusEl = $("turn-status");
const liveBtn = $("live-btn");
const panelsEl = $("panels");

function el(tag, cls) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  return n;
}

function setConn(state, label) {
  connEl.dataset.state = state;
  connEl.textContent = label || state;
}

function autoscroll() {
  const nearBottom = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 80;
  if (nearBottom) messagesEl.scrollTop = messagesEl.scrollHeight;
}

// ── 状态 ────────────────────────────────────────────────────────
const state = {
  sessionId: null,
  model: "",
  streaming: false,
  streamBuf: "",
  lastAssistant: null,
  thinkingBody: null,
  lastTool: null,
  permission: null,
  ask: null,   // 交互提问待决：{ request_id, multi_select, selected:Set, otherText }
  plan: null,  // 计划审批待决：{ request_id }
};

function clearMessages() {
  messagesEl.innerHTML = "";
  messagesEl.classList.remove("streaming");
  state.streamBuf = "";
  state.lastAssistant = null;
  state.thinkingBody = null;
  state.lastTool = null;
}

// ── XSS-safe 迷你 markdown ─────────────────────────────────────
function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

const PH_PREFIX = "@@OPENX_BLOCK_";
const PH_RE = /@@OPENX_BLOCK_(\d+)@@/g;

function renderMarkdown(text) {
  const blocks = [];
  let src = String(text || "");

  // 1. 围栏代码块先抽离（内容只转义，不解析）；哨兵格式正常文本几乎
  //    不可能出现，正则整段匹配不残留尾随字符。
  src = src.replace(/```[^\n]*\n([\s\S]*?)```/g, (m, code) => {
    blocks.push(`<pre><code>${escapeHtml(code.replace(/\n$/, ""))}</code></pre>`);
    return PH_PREFIX + (blocks.length - 1) + "@@";
  });

  // 2. 其余整体转义后再变换
  let html = escapeHtml(src);

  // 3. 行内：行内代码 / 链接（协议白名单）/ 粗体 / 斜体
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, label, url) => {
    const u = url.replace(/&amp;/g, "&");
    if (/^javascript:/i.test(u)) return m;                 // 拒 javascript:
    if (!/^(https?:|#|\/?\.?[a-zA-Z0-9_.\-/]+$)/.test(u)) return m;
    return `<a href="${escapeHtml(u)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  });
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>");

  // 4. 行首结构：标题 / 无序列表
  html = html.replace(/^#{1,6} (.*)$/gm, (m, body) => {
    const level = m.match(/^#+/)[0].length;
    return `<h${Math.min(level, 6)}>${body}</h${Math.min(level, 6)}>`;
  });
  html = html.replace(/^[-*+] (.*)$/gm, "<li>$1</li>");
  html = html.replace(/(?:<li>.*<\/li>\n?)+/g, (m) => `<ul>${m}</ul>`);

  // 5. 恢复代码块
  html = html.replace(PH_RE, (m, i) => blocks[Number(i)]);

  // 6. 段落（空行分段；段内换行 → <br>；块级标签不再套 <p>）
  const parts = html.split(/\n{2,}/).filter((p) => p.trim().length);
  if (!parts.length) return "";
  return parts
    .map((p) => (/^(<h\d|<ul|<pre|<ol|<blockquote)/.test(p.trim()) ? p
      : `<p>${p.replace(/\n/g, "<br>")}</p>`))
    .join("\n");
}

function textOf(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.map((p) => (p && p.text) || "").join("\n");
  return "";
}

// ── 消息渲染 ───────────────────────────────────────────────────
function addToChat(node) {
  messagesEl.appendChild(node);
  autoscroll();
}

function appendUser(text) {
  const row = el("div", "msg user");
  row.textContent = text;
  addToChat(row);
}

function appendAssistant(text) {
  const row = el("div", "msg assistant");
  row.innerHTML = renderMarkdown(text);
  addToChat(row);
  state.lastAssistant = row;
  return row;
}

function appendMeta(text) {
  const row = el("div", "meta-note");
  row.textContent = text;
  addToChat(row);
}

function appendThinking(text) {
  if (!state.thinkingBody) {
    const wrap = el("div", "thinking");
    const toggle = el("button", "thinking-toggle");
    toggle.textContent = "💭 Thinking…";
    toggle.onclick = () => wrap.classList.toggle("open");
    const body = el("div", "thinking-body");
    wrap.append(toggle, body);
    state.thinkingBody = body;
    addToChat(wrap);
  }
  state.thinkingBody.textContent += text;
}

function appendToolStart(name) {
  const wrap = el("div", "tool");
  const head = el("div", "tool-head");
  const dot = el("span", "dot running");
  const label = el("span", "tool-name");
  label.textContent = name;
  head.append(dot, label);
  const body = el("pre", "tool-out");
  body.hidden = true;
  head.onclick = () => { body.hidden = !body.hidden; };
  wrap.append(head, body);
  state.lastTool = { name, wrap, body };
  addToChat(wrap);
}

function appendToolResult(name, isError, output) {
  let t = (state.lastTool && state.lastTool.name === name) ? state.lastTool : null;
  if (!t) {
    // 无配对起始（历史/复盘回放）：合成一条
    appendToolStart(name);
    t = state.lastTool;
  }
  const dot = t.wrap.querySelector(".dot");
  dot.className = "dot " + (isError ? "error" : "done");
  t.body.textContent = output;
  t.body.hidden = !output;
  t.wrap.classList.toggle("error", isError);
}

// 流式节流：text_delta 累积，rAF 一次重渲染（防逐 token 重排）
let flushPending = false;
function scheduleStreamFlush() {
  if (flushPending) return;
  flushPending = true;
  requestAnimationFrame(() => {
    flushPending = false;
    if (!state.lastAssistant) appendAssistant("");
    state.lastAssistant.innerHTML = renderMarkdown(state.streamBuf);
    autoscroll();
  });
}

// ── 事件 reducer ───────────────────────────────────────────────
function applyEvent(ev) {
  switch (ev.type) {
    case "system":
      if (ev.subtype === "init") {
        state.sessionId = ev.session_id;
        state.model = ev.model;
        metaEl.textContent = `${ev.model} · ${String(ev.session_id).slice(0, 12)} · ${(ev.tools || []).length} tools`;
      }
      break;
    case "history":
      renderHistory(ev.messages || []);
      break;
    case "user_message":
      appendUser(ev.text || "");
      state.streaming = true;
      messagesEl.classList.add("streaming");  // 流式光标：末条气泡尾部 ▍
      showTurnBar(true, "working…");
      break;
    case "text_delta":
      state.streamBuf += ev.text || "";
      scheduleStreamFlush();
      break;
    case "thinking_delta":
      appendThinking(ev.text || "");
      break;
    case "tool_use":
      appendToolStart(ev.name || "tool");
      break;
    case "tool_result":
      appendToolResult(ev.name || "tool", Boolean(ev.is_error), ev.output || "");
      break;
    case "result":
      state.streaming = false;
      messagesEl.classList.remove("streaming");
      state.streamBuf = "";
      showTurnBar(false);
      appendMeta(doneLabel(ev));
      break;
    case "interrupted":
      state.streaming = false;
      messagesEl.classList.remove("streaming");
      state.streamBuf = "";
      showTurnBar(false);
      appendMeta("⏹ Interrupted");
      break;
    case "permission_request":
      showPermission(ev);
      break;
    case "panels":
      // 插件 UI 面板（ui/v1）：服务端已剥 rich 标签、变化才广播——
      // 端只做整区重绘（面板小、整绘成本可忽略，天然幂等）
      renderPanels(ev.panels || []);
      break;
    case "plan_request":
      showPlan(ev);
      break;
    case "ask_user":
      showAsk(ev);
      break;
    default:
      break; // 未知事件容忍（前向兼容）
  }
}

function doneLabel(ev) {
  const parts = [];
  if (ev.is_error) parts.push("✗ error");
  parts.push(`${ev.num_turns ?? 0} tool turns`);
  const u = ev.usage || {};
  parts.push(`${u.input_tokens ?? 0}/${u.output_tokens ?? 0} tokens`);
  return "✓ Done · " + parts.join(" · ");
}

function renderHistory(messages) {
  clearMessages();
  for (const m of messages) {
    if (!m || typeof m !== "object") continue;
    if (m.role === "user") appendUser(textOf(m.content));
    else if (m.role === "assistant") appendAssistant(textOf(m.content));
    else if (m.role === "tool") appendToolResult(m.name || "tool", false, textOf(m.content));
  }
}

// ── 插件 UI 面板（ui/v1）────────────────────────────────────────
function renderPanels(panels) {
  panelsEl.innerHTML = "";
  if (!panels.length) {
    panelsEl.hidden = true;
    return;
  }
  for (const p of panels) {
    if (!p || typeof p !== "object") continue;
    const node = el("div", "panel");
    for (const line of (p.lines || [])) {
      const row = el("div", "panel-line");
      row.textContent = String(line); // 纯文本（服务端已剥 rich 标签）
      node.appendChild(row);
    }
    panelsEl.appendChild(node);
  }
  panelsEl.hidden = false;
}

// ── turn bar / interrupt ───────────────────────────────────────
function showTurnBar(show, status) {
  turnBarEl.hidden = !show;
  if (status) turnStatusEl.textContent = status;
}

// ── 权限弹窗 ───────────────────────────────────────────────────
function showPermission(ev) {
  state.permission = ev;
  $("perm-tool").textContent = ev.tool || "";
  $("perm-reason").textContent = ev.reason || "";
  $("perm-details").textContent = ev.details || "";
  $('button[data-perm="allow-remember"]').hidden = ev.can_remember !== true;
  overlayEl.hidden = false;
}

function respondPermission(allowed, remember) {
  const p = state.permission;
  overlayEl.hidden = true;
  state.permission = null;
  if (!p) return;
  send({ type: "permission_response", request_id: p.request_id, allowed, remember });
}

document.querySelectorAll("#perm-overlay [data-perm]").forEach((btn) => {
  btn.onclick = () => {
    const k = btn.dataset.perm;
    if (k === "allow-once") respondPermission(true, false);
    else if (k === "allow-remember") respondPermission(true, true);
    else respondPermission(false, false);
  };
});

// ── 交互弹窗：ask_user / plan_request（P4.1 交互化）──────────────
// 服务端广播后按 request_id 等待应答；任一客户端应答即唤醒（首个赢，
// 其余因 future 已决被忽略——多标签页天然竞速，同权限弹窗纪律）。
// XSS 纪律：选项/问题是模型产物，一律 textContent；plan 走 renderMarkdown
//（先转义后渲染）。

function renderAskOptions(options, multiSelect) {
  const box = $("ask-options");
  box.innerHTML = "";
  for (const opt of options || []) {
    const row = el("label", "ask-option");
    const input = el("input", "ask-choice");
    input.type = multiSelect ? "checkbox" : "radio";
    input.name = "ask-choice";
    input.value = opt.label;
    const lab = el("span", "ask-opt-label");
    lab.textContent = opt.label;
    row.append(input, lab);
    if (opt.description) {
      const desc = el("div", "ask-opt-desc");
      desc.textContent = opt.description;
      row.appendChild(desc);
    }
    box.appendChild(row);
  }
}

function syncAskSubmit() {
  const s = state.ask;
  const hasCustom = Boolean(s && s.otherText.trim());
  $("ask-submit-btn").disabled = !(hasCustom || (s && s.selected.size > 0));
}

function showAsk(ev) {
  state.ask = {
    request_id: ev.request_id,
    multi_select: Boolean(ev.multi_select),
    selected: new Set(),
    otherText: "",
  };
  $("ask-question").textContent = ev.question || "";
  renderAskOptions(ev.options || [], state.ask.multi_select);
  $("ask-custom").hidden = true;
  $("ask-custom-input").value = "";
  syncAskSubmit();
  askOverlayEl.hidden = false;
}

function respondAsk(answers) {
  const s = state.ask;
  askOverlayEl.hidden = true;
  state.ask = null;
  if (!s) return;
  send({ type: "ask_user_response", request_id: s.request_id, answers });
}

$("ask-options").addEventListener("change", (e) => {
  const s = state.ask;
  if (!s) return;
  const input = e.target;
  if (input.type === "checkbox") {
    if (input.checked) s.selected.add(input.value);
    else s.selected.delete(input.value);
  } else {  // radio：互斥选择
    s.selected.clear();
    if (input.checked) s.selected.add(input.value);
  }
  syncAskSubmit();
});

$("ask-other-btn").onclick = () => {
  const s = state.ask;
  if (!s) return;
  const showCustom = $("ask-custom").hidden;
  $("ask-custom").hidden = !showCustom;
  if (showCustom) {
    $("ask-custom-input").focus();
  } else {
    s.otherText = "";              // 收起 Other 时清空草稿
    $("ask-custom-input").value = "";
  }
  syncAskSubmit();
};

$("ask-custom-input").addEventListener("input", (e) => {
  if (state.ask) state.ask.otherText = e.target.value;
  syncAskSubmit();
});

$("ask-submit-btn").onclick = () => {
  const s = state.ask;
  if (!s) return;
  const custom = s.otherText.trim();
  respondAsk(custom ? [custom] : Array.from(s.selected));
};

$("ask-skip-btn").onclick = () => {
  respondAsk([]);  // 空答 → 服务端落保守默认（不等待超时）
};

function showPlan(ev) {
  state.plan = { request_id: ev.request_id };
  $("plan-details").innerHTML = renderMarkdown(ev.plan || "");
  planOverlayEl.hidden = false;
}

function respondPlan(approved) {
  const s = state.plan;
  planOverlayEl.hidden = true;
  state.plan = null;
  if (!s) return;
  send({ type: "plan_response", request_id: s.request_id, approved });
}

$("plan-approve-btn").onclick = () => respondPlan(true);
$("plan-reject-btn").onclick = () => respondPlan(false);

// ── WS 连接 ────────────────────────────────────────────────────
let ws = null;
let reconnectTimer = null;

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.onopen = () => { setConn("connected", "connected"); };
  ws.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch (_) { return; }
    applyEvent(ev);
  };
  ws.onclose = () => {
    setConn("disconnected", "disconnected · retrying");
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, 1000);
  };
}

// ── 输入 ───────────────────────────────────────────────────────
inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    const text = inputEl.value.trim();
    if (!text) return;
    inputEl.value = "";
    send({ type: "message", text });
  }
});

$("interrupt-btn").onclick = () => send({ type: "interrupt" });

// ── 会话列表 / 复盘 ────────────────────────────────────────────
async function loadSessions() {
  try {
    const res = await fetch("/api/sessions");
    const list = await res.json();
    sessionListEl.innerHTML = "";
    if (!list.length) {
      const li = el("li", "session-item");
      li.textContent = "(no sessions yet)";
      li.style.cursor = "default";
      sessionListEl.append(li);
      return;
    }
    for (const s of list) {
      const li = el("li", "session-item");
      const title = document.createElement("div");
      title.textContent = s.first_user_message || s.session_id;
      const when = document.createElement("div");
      when.className = "when";
      when.textContent = `${s.model} · ${shortDate(s.updated_at)}`;
      li.append(title, when);
      li.onclick = () => openReplay(s.session_id);
      sessionListEl.append(li);
    }
  } catch (_) { /* 服务未就绪时静默 */ }
}

function shortDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

async function openReplay(sessionId) {
  liveBtn.hidden = false;
  setConn("replay", `replay · ${sessionId.slice(0, 8)}`);
  clearMessages();
  try {
    const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/events`);
    const data = await res.json();
    if (res.status === 404) { appendMeta("Session not found."); return; }
    appendMeta(`↻ Replaying session ${sessionId}`);
    for (const ev of data.events || []) applyReplayEvent(ev);
    appendMeta("— end of replay —");
  } catch (_) {
    appendMeta("Failed to load replay.");
  }
}

function applyReplayEvent(ev) {
  if (!ev || typeof ev !== "object") return;
  if (ev.type === "message") {
    const m = ev.message || {};
    if (m.role === "user") appendUser(textOf(m.content));
    else if (m.role === "assistant") appendAssistant(textOf(m.content));
    else if (m.role === "tool") appendToolResult(m.name || "tool", false, textOf(m.content));
  } else if (ev.type === "permission_decision") {
    appendMeta(`🔒 ${ev.tool} → ${ev.approved ? "allowed" : "denied"} (${ev.verdict})`);
  } else if (ev.type === "provider_selected") {
    appendMeta(`🤖 provider: ${ev.provider} · ${ev.model}`);
  } else if (ev.type === "plugin_loaded" || ev.type === "plugin_failed") {
    appendMeta(`🧩 ${ev.type}: ${ev.plugin}`);
  }
}

$("refresh-sessions").onclick = loadSessions;
$("live-btn").onclick = () => location.reload();

// ── 启动 ───────────────────────────────────────────────────────
connect();
loadSessions();
