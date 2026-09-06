"use strict";

/* OpenX Serve — 聊天视图：消息渲染 + 输入 + 流式响应。
   与 AppState 配合：reducer 负责 WS 事件 → DOM，本模块提供可复用渲染函数。
   XSS 纪律：所有模型/工具名走 textContent；助手正文走 renderMarkdown。 */

const Chat = {
  // 渲染缓冲（仅前端展示，不持会话语义）
  streamBuf: "",
  lastAssistant: null,
  lastTool: null,
  thinkingBody: null,
  thinkingOpen: false,
  // 标记这一会话是否已经触发过「自动开任务面板」——避免每次 user_message 都重开
  _panelOpenedFor: "",

  init() {
    this.bindInput();
    this.bindSend();
    this.bindInterrupt();
  },

  /**
   * 把 #welcome-hero 暴露给 app.js 的可读状态。
   * 真实状态由 DOM 决定（模块隐藏状态以 DOM 为单一事实源）。
   */
  welcomeVisible() {
    const hero = document.getElementById("welcome-hero");
    return Boolean(hero && !hero.classList.contains("is-hidden") && !hero.classList.contains("is-fading"));
  },

  /**
   * 首次会话有内容时隐藏欢迎页；保留显隐过渡，结束后从布局中摘除。
   * 任何 user/assistant 渲染都会触发——首条消息起即出局。
   */
  hideWelcome() {
    const hero = document.getElementById("welcome-hero");
    if (!hero || hero.classList.contains("is-hidden")) return;
    hero.classList.add("is-fading");
    const finalize = () => {
      hero.classList.add("is-hidden");
      hero.removeEventListener("transitionend", finalize);
    };
    hero.addEventListener("transitionend", finalize);
    // 兜底：若 transitionend 没触发（prefers-reduced-motion 或被截断），
    // 240ms 后强制清理。
    setTimeout(finalize, 240);
  },

  /** 把欢迎页重新展示（如清空历史再次进入空状态时调用）。 */
  showWelcome() {
    const hero = document.getElementById("welcome-hero");
    if (!hero) return;
    hero.classList.remove("is-hidden", "is-fading");
  },

  /** 把欢迎页彻底隐藏（用户已发过第一条消息，再回到空状态也不再展示首屏 logo）。 */
  hideWelcomePermanent() {
    const hero = document.getElementById("welcome-hero");
    if (!hero) return;
    hero.classList.add("is-hidden", "is-fading");
    hero.style.display = "none";
  },

  /**
   * setWelcomeHint / bindWelcomeChips：旧的连接状态提示 + 能力芯片已不在
   * 首屏暴露给用户，这两个 API 保留为 no-op 以兼容 history 调用方。
   */
  setWelcomeHint(_text, _connState) { /* no-op: 首屏已极简，不再展示 */ },
  bindWelcomeChips() { /* no-op: 4 个能力芯片已移除 */ },

  // ── 输入 ─────────────────────────────────────────
  bindInput() {
    const inp = $("input");
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        this.submit();
      }
    });
    // 自动调高
    inp.addEventListener("input", () => {
      inp.style.height = "auto";
      inp.style.height = Math.min(200, inp.scrollHeight) + "px";
    });
  },

  bindSend() {
    $("send-btn").onclick = () => this.submit();
  },

  bindInterrupt() {
    $("interrupt-btn").onclick = () => AppState.send({ type: "interrupt" });
  },

  submit() {
    const inp = $("input");
    const text = inp.value.trim();
    if (!text) return;
    inp.value = "";
    inp.style.height = "auto";
    AppState.send({ type: "message", text });
  },

  // ── 渲染：单条消息 ────────────────────────────────
  clearAll() {
    $("messages").innerHTML = "";
    $("messages").classList.remove("streaming");
    this.streamBuf = "";
    this.lastAssistant = null;
    this.lastTool = null;
    this.thinkingBody = null;
    // 清屏 = 进入「无消息」状态：拉回欢迎页（多用于新建会话、清空、刷新当前会话）。
    // replay 路径会立刻 appendUser，所以 hidden 状态不会持续太久。
    this.showWelcome();
  },

  appendUser(text) {
    this.hideWelcome();
    const row = el("div", "msg user");
    row.textContent = text;
    $("messages").appendChild(row);

    // 首条用户消息：自动打开任务面板——只有「这才刚开始的会话」才需要它，
    // 已打开过的不重复触发；并把首条问题作为会话标题回填（轻量本地回填）。
    if (this._panelOpenedFor !== AppState.sessionId) {
      this._panelOpenedFor = AppState.sessionId || "";
      if (typeof TaskPanel !== "undefined" && TaskPanel.open) TaskPanel.open();
      if (typeof Sidebar !== "undefined" && Sidebar.renameSession) {
        Sidebar.renameSession(AppState.sessionId, text);
      }
    }
    this.autoscroll();
  },

  appendMeta(text) {
    this.hideWelcome();
    const row = el("div", "meta-note");
    row.textContent = text;
    $("messages").appendChild(row);
    this.autoscroll();
  },

  appendThinking(text) {
    this.hideWelcome();
    if (!this.thinkingBody) {
      const wrap = el("div", "thinking");
      const toggle = el("button", "thinking-toggle");
      toggle.textContent = "💭 Thinking…";
      toggle.onclick = () => wrap.classList.toggle("open");
      const body = el("div", "thinking-body");
      wrap.append(toggle, body);
      this.thinkingBody = body;
      $("messages").appendChild(wrap);
    }
    this.thinkingBody.textContent += text;
  },

  /**
   * 创建一个助手卡片（标签行 + 正文 + 工具区 + 附件）。
   * 后续 text_delta 注入到 .assistant-body；
   * tool_use / tool_result 注入到 .tool-cards 容器；
   * 流结束不删除任何内容，由 reducer 标记状态。
   */
  createAssistantCard(opts = {}) {
    this.hideWelcome();
    const card = el("div", "assistant-card");
    const head = el("div", "assistant-header");

    if (opts.agent) {
      const pill = el("span", "agent-pill");
      const dot = el("span", "ai-dot");
      pill.append(dot, document.createTextNode(opts.agent));
      head.appendChild(pill);
    }
    if (opts.model) {
      const pill = el("span", "model-pill");
      pill.textContent = opts.model;
      head.appendChild(pill);
    }
    if (opts.enhance) {
      const pill = el("span", "model-pill");
      pill.textContent = opts.enhance;
      head.appendChild(pill);
    }
    const meta = el("span", "msg-meta-row");
    if (opts.time) meta.appendChild(this._metaSpan(opts.time));
    if (opts.tokens) meta.appendChild(this._metaSpan(opts.tokens));
    if (opts.duration) meta.appendChild(this._metaSpan(opts.duration));
    head.appendChild(meta);

    const body = el("div", "assistant-body");
    const tools = el("div", "tool-cards");
    const attachments = el("div", "attachments");

    card.append(head, body, tools, attachments);

    const msg = el("div", "msg assistant");
    msg.appendChild(card);
    $("messages").appendChild(msg);
    this.lastAssistant = { card, body, tools, attachments };
    this.autoscroll();
    return card;
  },

  _metaSpan(text) {
    const s = el("span");
    s.textContent = text;
    return s;
  },

  /**
   * 在助手卡片顶部填入一次性内容（用于 history 回放，复盘态已知完整内容）。
   */
  setAssistantContent(html) {
    if (!this.lastAssistant) this.createAssistantCard();
    this.lastAssistant.body.innerHTML = renderMarkdown(html);
    this.streamBuf = html || "";
    this.autoscroll();
  },

  appendToolStart(name, desc) {
    if (!this.lastAssistant) this.createAssistantCard();
    const wrap = el("div", "tool-card");
    const head = el("div", "tool-card-head");
    const dot = el("span", "dot running");
    const nm = el("span", "tool-name");
    nm.textContent = name;
    const ds = el("span", "tool-desc");
    ds.textContent = desc || "执行中…";
    head.append(dot, nm, ds);
    const status = el("span", "tool-status");
    status.textContent = "运行中";
    head.appendChild(status);
    head.onclick = () => wrap.classList.toggle("open");
    const out = el("pre", "tool-output");
    out.hidden = true;
    wrap.append(head, out);
    this.lastAssistant.tools.appendChild(wrap);
    this.lastTool = { name, wrap, status, out };
    this.autoscroll();
  },

  appendToolResult(name, isError, output) {
    // 配对到上次起始；找不到就现场合成一条
    let t = this.lastTool && this.lastTool.name === name ? this.lastTool : null;
    if (!t) {
      this.appendToolStart(name);
      t = this.lastTool;
    }
    const dot = t.wrap.querySelector(".dot");
    dot.className = "dot " + (isError ? "error" : "done");
    t.status.textContent = isError ? "失败" : "完成";
    t.status.className = "tool-status " + (isError ? "error" : "done");
    if (output) {
      t.out.textContent = output;
      t.out.hidden = false;
    }
    t.wrap.classList.toggle("error", isError);
  },

  appendAttachment(icon, name) {
    if (!this.lastAssistant) this.createAssistantCard();
    const a = el("span", "attachment");
    if (icon) {
      const i = el("span");
      i.textContent = icon;
      a.append(i, document.createTextNode(" " + name));
    } else {
      a.textContent = name;
    }
    this.lastAssistant.attachments.appendChild(a);
  },

  // ── 流式增量 ─────────────────────────────────────
  scheduleFlush() {
    if (this._flushPending) return;
    this._flushPending = true;
    requestAnimationFrame(() => {
      this._flushPending = false;
      if (!this.lastAssistant) this.createAssistantCard();
      this.lastAssistant.body.innerHTML = renderMarkdown(this.streamBuf);
      this.autoscroll();
    });
  },

  autoscroll() {
    const m = $("messages");
    const near = m.scrollHeight - m.scrollTop - m.clientHeight < 80;
    if (near) m.scrollTop = m.scrollHeight;
  },
};