"use strict";

/* OpenX Serve — 聊天视图：消息渲染 + 输入 + 流式响应。
   与 AppState 配合：reducer 负责 WS 事件 → DOM，本模块提供可复用渲染函数。
   XSS 纪律：所有模型/工具名走 textContent；助手正文走 renderMarkdown。 */

const Chat = {
  // 渲染缓冲（仅前端展示，不持会话语义）
  streamBuf: "",
  lastAssistant: null,
  lastTool: null,
  // 本回合 thinking 折叠块（wrap 供收尾折叠/改标签；start 计耗时）
  thinkingWrap: null,
  thinkingBody: null,
  thinkingStart: 0,

  init() {
    this.bindInput();
    this.bindSend();
    this.bindInterrupt();
    this.bindWelcomeChips();
    this.bindPickers();
    this.setStreaming(false);
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
    // 离开空态 = 进入会话态：先把输入框从问候区挪回 #chat 底部贴底
    this._dockComposer();
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

  // ── 空态 / 会话态：居中大输入框 <-> 贴底输入框 ─────────────────
  // #composer 元素随状态在两个父容器间物理搬移：
  //   空态 → #welcome-hero（问候区，居中大输入框）
  //   会话 → #chat（消息之下、贴底）
  _heroEl() { return document.getElementById("welcome-hero"); },

  /** 把 #composer 从问候区挪回 #chat 底部（贴底；#panels 之后）。 */
  _dockComposer() {
    const comp = $("composer");
    const chat = $("chat");
    if (comp && chat && comp.parentNode !== chat) chat.appendChild(comp);
  },

  /** 把 #composer 放进问候区（插到副标题与能力芯片之间，保持居中）。 */
  _placeComposerInHero() {
    const hero = this._heroEl();
    const comp = $("composer");
    if (!hero || !comp || comp.parentNode === hero) return;
    const chips = hero.querySelector(".welcome-chips");
    hero.insertBefore(comp, chips);
  },

  /**
   * 进入空态：问候区可见 + 输入框居中 + 重置输入区 + 刷新目录/模型选择。
   * 新建对话 / 切工作区 / 启动空会话时调用（幂等）。
   */
  enterEmpty() {
    const hero = this._heroEl();
    if (!hero) return;
    this._placeComposerInHero();
    const inp = $("input");
    if (inp) inp.value = "";
    const att = $("composer-attachments");
    if (att) { att.hidden = true; att.innerHTML = ""; }
    const bar = $("turn-bar");
    if (bar) bar.hidden = true;
    this._dirClose();
    hero.classList.remove("is-hidden", "is-fading");
    hero.style.display = "";
    this.refreshPickers();
  },

  // ── 空态输入框内的目录/模型组选择 ─────────────────────────────
  bindPickers() {
    const ws = $("ws-picker");
    if (ws) ws.addEventListener("change", () => this.onPickWorkspace());
    const mp = $("model-picker");
    if (mp) mp.addEventListener("change", () => this.onPickModel());

    // 本地目录浏览器（弹窗内导航；绑定一次）
    const up = $("dir-up");
    if (up) up.addEventListener("click", () => this._dirUp());
    const close = $("dir-close");
    if (close) close.addEventListener("click", () => this._dirClose());
    const cancel = $("dir-cancel");
    if (cancel) cancel.addEventListener("click", () => this._dirClose());
    const choose = $("dir-choose");
    if (choose) choose.addEventListener("click", () => this._dirChoose());
    const list = $("dir-list");
    if (list) list.addEventListener("click", (e) => {
      const item = e.target.closest("[data-dir]");
      if (item && item.dataset.dir) this._dirEnter(item.dataset.dir);
    });
  },

  _busyGuard() {
    if (typeof AppState === "undefined") return false;
    if (AppState.streaming) { OX.toast("当前回合仍在进行，先打断或等待完成", "err"); return true; }
    return false;
  },

  async refreshPickers() {
    const ws = $("ws-picker");
    const mp = $("model-picker");
    if (!ws && !mp) return;

    if (ws) {
      try {
        const groups = (await OX.get("/api/workspaces")) || [];
        const curWs = (typeof AppState !== "undefined" && AppState.workspace) || "";
        const activePath = (groups.find((g) => g.active) || {}).workspace || curWs || "";
        let html = `<option value="" disabled ${activePath ? "" : "selected"}>选择工作目录…</option>`;
        for (const g of groups) {
          const p = g.workspace || "";
          if (!p) continue;
          const base = String(p).split("/").filter(Boolean).pop() || p;
          const sel = p === activePath ? "selected" : "";
          html += `<option value="${escapeHtml(p)}" ${sel} title="${escapeHtml(p)}">${escapeHtml(base)}${p === activePath ? " · 当前" : ""}</option>`;
        }
        html += `<option value="__browse__">📁 浏览本地目录…</option>`;
        ws.innerHTML = html;
      } catch (_) { /* 服务未就绪：空选项，稍后重进空态会再刷 */ }
    }

    if (mp) {
      try {
        const data = (await OX.get("/api/models")) || {};
        const cur = ((data.current || {}).group) || data.active || "";
        let html = `<option value="" disabled ${cur ? "" : "selected"}>选择模型组…</option>`;
        for (const g of (data.groups || [])) {
          const label = g.name + (g.main ? ` · ${g.main}` : "");
          html += `<option value="${escapeHtml(g.name)}" ${g.name === cur ? "selected" : ""}>${escapeHtml(label)}</option>`;
        }
        mp.innerHTML = html;
      } catch (_) { /* 同上 */ }
    }
  },

  onPickWorkspace() {
    const sel = $("ws-picker");
    if (!sel || !sel.value) return;
    if (this._busyGuard()) { sel.value = ""; return; }
    const v = sel.value;
    if (v === "__browse__") { sel.value = ""; this.openDirPicker(); return; }
    this.switchWorkspace(v);
  },

  // ── 本地目录浏览器（serve 枚举子目录，联动选择而非手填） ─────
  _dirStartPath() {
    return (typeof AppState !== "undefined" && AppState.workspace) || "/";
  },

  async openDirPicker() {
    const ov = $("dir-overlay");
    if (!ov) return;
    ov.hidden = false;
    await this._dirOpen(this._dirStartPath());
  },

  _dirClose() {
    const ov = $("dir-overlay");
    if (ov) ov.hidden = true;
    this._dirData = null;
  },

  async _dirOpen(path) {
    const ov = $("dir-overlay");
    if (ov) ov.hidden = false;
    let data;
    try {
      data = await OX.get("/api/dirs?path=" + encodeURIComponent(path || ""));
    } catch (err) {
      OX.toast("无法读取目录：" + err.message, "err");
      this._dirClose();
      return;
    }
    if (!data) return;
    this._dirData = data;
    this._dirRender(data);
  },

  async _dirEnter(path) {
    await this._dirOpen(path);
  },

  async _dirUp() {
    const d = this._dirData;
    if (d && d.parent) await this._dirOpen(d.parent);
  },

  async _dirChoose() {
    const d = this._dirData;
    if (!d) return;
    if (this._busyGuard()) return;
    const path = d.path;
    this._dirClose();
    await this.switchWorkspace(path);
  },

  /** 面包屑：/a/b/c → [{/,/},{a,/a},{b,/a/b},{c,/a/b/c}]（点击跳转）。 */
  _dirCrumbs(path) {
    const p = String(path);
    const segs = p.split("/").filter(Boolean);
    const out = [];
    if (p.startsWith("/")) out.push({ label: "/", path: "/" });
    let acc = p.startsWith("/") ? "/" : "";
    for (const s of segs) {
      acc = acc.endsWith("/") ? acc + s : acc + "/" + s;
      out.push({ label: s, path: acc });
    }
    return out;
  },

  _dirRender(d) {
    const pathEl = $("dir-path");
    const cur = $("dir-current");
    const choose = $("dir-choose");
    const up = $("dir-up");
    if (pathEl) {
      pathEl.innerHTML = "";
      pathEl.title = d.path || "";
      const crumbs = this._dirCrumbs(d.path);
      crumbs.forEach((c, i) => {
        if (i) {
          const sep = el("span", "seg-sep");
          sep.textContent = "/";
          pathEl.appendChild(sep);
        }
        const seg = el("span", "seg");
        seg.textContent = c.label;
        seg.title = c.path;
        seg.onclick = () => this._dirOpen(c.path);
        pathEl.appendChild(seg);
      });
    }
    const list = $("dir-list");
    const empty = $("dir-empty");
    if (list) {
      list.innerHTML = "";
      for (const item of (d.dirs || [])) {
        const li = el("li", "dir-item");
        const ic = el("span", "di-ic");
        ic.textContent = "▸";
        const nm = el("span", "di-name");
        nm.textContent = item.name;
        nm.title = item.path;
        li.append(ic, nm);
        li.dataset.dir = item.path;
        list.appendChild(li);
      }
      if (empty) empty.hidden = (d.dirs || []).length > 0;
    }
    if (cur) cur.textContent = d.path || "";
    if (choose) choose.disabled = !d.path;
    if (up) up.hidden = !d.parent;
  },

  /** 切工作目录：后端会在该目录重开新会话并重根 tools。 */
  async switchWorkspace(path) {
    try {
      await OX.post("/api/workspace/switch", { workspace: path });
    } catch (err) {
      OX.toast("切换目录失败：" + err.message, "err");
      return;
    }
    try {
      const info = await OX.get("/api/info");
      if (typeof AppState !== "undefined" && info) {
        AppState.workspace = info.workspace || "";
        AppState.sessionId = info.session_id || "";
        AppState.model = info.model || "";
      }
    } catch (_) { /* info 读失败不阻断 */ }
    if (typeof Sidebar !== "undefined") {
      Sidebar.activeSession = (typeof AppState !== "undefined" && AppState.sessionId) || "";
      await Sidebar.reload();
      Sidebar.renderAll();
    }
    if (typeof updateBreadcrumb === "function") updateBreadcrumb();
    this.enterEmpty();
    OX.toast("已切换到工作区：" + path, "ok");
  },

  /** 切模型组：影响后续消息（当前空会话保留）。 */
  async onPickModel() {
    const sel = $("model-picker");
    if (!sel || !sel.value) return;
    if (this._busyGuard()) { sel.value = ""; return; }
    const group = sel.value;
    try {
      const data = await OX.post("/api/models/switch", { group });
      if (data && data.current && typeof AppState !== "undefined") {
        AppState.model = data.current.model || "";
      }
    } catch (err) {
      OX.toast("切换模型组失败：" + err.message, "err");
      this.refreshPickers();
      return;
    }
    if (typeof updateBreadcrumb === "function") updateBreadcrumb();
    OX.toast("已切换到模型组 → " + group, "ok");
  },

  /**
   * setWelcomeHint / bindWelcomeChips：首屏底部状态点和能力芯片（撤回请求 4 恢复）。
   * setWelcomeHint 更新 #welcome-hint-text 文案 + #hint-dot 颜色，
   * bindWelcomeChips 给 4 个芯片绑定点击 → 写入输入框。
   */
  setWelcomeHint(text, connState) {
    const hint = document.getElementById("welcome-hint-text");
    if (hint) hint.textContent = text || "就绪";
    const dot = document.querySelector(".hint-dot");
    if (dot) dot.dataset.state = connState || "";
  },
  bindWelcomeChips() {
    const chips = document.querySelectorAll(".welcome-chip");
    const inp = $("input");
    if (!inp) return;
    chips.forEach((chip) => {
      chip.addEventListener("click", () => {
        const label = chip.textContent.trim();
        if (inp.value && !OX.confirm("替换当前输入？")) return;
        inp.value = label;
        inp.focus();
        inp.dispatchEvent(new Event("input"));
      });
    });
  },

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
    $("send-btn").onclick = () => {
      if (typeof AppState !== "undefined" && AppState.streaming) {
        AppState.send({ type: "interrupt" });   // 回答中点击 = 停止
      } else {
        this.submit();
      }
    };
  },

  /**
   * 流式状态 → 发送钮：空闲显示 "→"（发送）；回答中变 "■"（停止）并脉冲动画。
   * 由 app.js 的 showTurnBar(true|false) 驱动（见 showTurnBar 委托）。
   */
  setStreaming(on) {
    const btn = $("send-btn");
    if (!btn) return;
    btn.classList.toggle("streaming", Boolean(on));
    btn.title = on ? "停止生成" : "发送 (Enter)";
    btn.setAttribute("aria-label", on ? "停止生成" : "发送");
    btn.textContent = on ? "■" : "→";
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
  /**
   * 新回合开始：清渲染缓冲。正文 / 思考块 / 工具配对都按回合隔离——
   * 尤其 thinkingBody 不清的话，本回合思考会追加进上一回合的折叠块
   * （在滚动历史很上方，看起来就是"这次回答没有思考"）。
   * 由 app.js 的 user_message 分支调用。
   */
  startTurn() {
    this.streamBuf = "";
    this.lastAssistant = null;
    this.lastTool = null;
    this.thinkingWrap = null;
    this.thinkingBody = null;
    this.thinkingStart = 0;
  },

  clearAll() {
    const host = $("messages");
    // 只移除消息 / 元 / 思考节点；#day-divider 与 #welcome-hero 常驻，
    // 让「新建对话」能稳定回到空态（hero 隐藏只靠 .is-hidden）。
    for (const c of Array.from(host.children)) {
      if (c.id === "welcome-hero" || c.id === "day-divider") continue;
      c.remove();
    }
    host.classList.remove("streaming");
    this.startTurn();
    // 清屏 = 进入「无消息」空态：输入框回居中、问候可见。
    this.enterEmpty();
  },

  appendUser(text) {
    this.hideWelcome();
    const row = el("div", "msg user");
    row.textContent = text;
    $("messages").appendChild(row);
    this.autoscroll();
  },

  appendMeta(text) {
    this.hideWelcome();
    const row = el("div", "meta-note");
    row.textContent = text;
    $("messages").appendChild(row);
    this.autoscroll();
  },

  /**
   * 追加一段思考增量。回合进行中默认展开（流式可见，对标 DeepSeek/
   * Claude 的 reasoning 区）；用户手动折叠后不强迫展开。收尾由
   * finalizeTurn 折叠并标注耗时。
   */
  appendThinking(text) {
    this.hideWelcome();
    if (!this.thinkingBody) {
      const wrap = el("div", "thinking");
      wrap.classList.add("open");
      this.thinkingStart = performance.now();
      const toggle = el("button", "thinking-toggle");
      toggle.type = "button";
      const caret = el("span", "caret");
      caret.textContent = "▸";
      const dot = el("span", "t-dot");
      const label = el("span", "t-label");
      label.textContent = "思考中";
      toggle.append(caret, dot, label);
      toggle.onclick = () => wrap.classList.toggle("open");
      const body = el("div", "thinking-body");
      wrap.append(toggle, body);
      this.thinkingWrap = wrap;
      this.thinkingBody = body;
      $("messages").appendChild(wrap);
    }
    this.thinkingBody.textContent += text;
    this.autoscroll();
  },

  /**
   * 创建一个助手卡片（标签行 + 正文 + 附件）。
   * 后续 text_delta 注入到 .assistant-body；
   * tool_use / tool_result 进入「本轮工具调用」折叠区（.turn-fold），
   * 折叠区在首个工具出现时才动态插到正文之前（叙事=先工具、后回答）；
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
    const attachments = el("div", "attachments");

    card.append(head, body, attachments);

    const msg = el("div", "msg assistant");
    msg.appendChild(card);
    $("messages").appendChild(msg);
    // fold / tools 惰性创建（见 _ensureToolFold）
    this.lastAssistant = { card, body, attachments, fold: null, tools: null };
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

  /**
   * 惰性建「本轮工具调用」折叠区，插到正文前。运行中保持展开，
   * 回合收尾由 finalizeTurn 收起成一行汇总。
   */
  _ensureToolFold() {
    const a = this.lastAssistant;
    if (!a || a.fold) return;
    const fold = el("div", "turn-fold");
    const headBtn = el("button", "turn-fold-head");
    headBtn.type = "button";
    const caret = el("span", "fold-caret");
    caret.textContent = "▸";
    const dot = el("span", "fold-dot");
    const label = el("span", "fold-label");
    label.textContent = "工具调用";
    const status = el("span", "fold-status");
    status.textContent = "运行中";
    headBtn.append(caret, dot, label, status);
    const tools = el("div", "tool-cards");
    fold.append(headBtn, tools);
    headBtn.onclick = () => fold.classList.toggle("closed");
    a.card.insertBefore(fold, a.body);
    a.fold = fold;
    a.tools = tools;
    this.autoscroll();
  },

  appendToolStart(name, desc) {
    if (!this.lastAssistant) this.createAssistantCard();
    this._ensureToolFold();
    const tools = this.lastAssistant.tools;
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
    tools.appendChild(wrap);
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

  /**
   * 回合收尾：思考块折叠并标注耗时（对标 CLI 的 "Thought for Ns"），
   * 工具折叠区收成「N 次工具调用」汇总行（点击可再展开）。
   * 由 app.js 在 result / interrupted 后调用；不清内容。
   */
  finalizeTurn() {
    if (this.thinkingWrap && this.thinkingBody && this.thinkingBody.textContent.trim()) {
      const secs = ((performance.now() - this.thinkingStart) / 1000).toFixed(1);
      const label = this.thinkingWrap.querySelector(".t-label");
      if (label) label.textContent = `思考了 ${secs}s`;
      this.thinkingWrap.classList.remove("open");
    }
    const a = this.lastAssistant;
    if (!a || !a.fold) return;
    const n = a.tools ? a.tools.children.length : 0;
    const label = a.fold.querySelector(".fold-label");
    const status = a.fold.querySelector(".fold-status");
    if (label) label.textContent = n ? `${n} 次工具调用` : "工具调用";
    if (status) status.textContent = "";
    a.fold.classList.add("closed");
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

  /**
   * 回合收尾：把累积的 streamBuf 同步渲染进正文，并取消挂起的 rAF 刷新。
   *
   * 竞态说明：若最后一个 text_delta 与 result 在同一帧内到达，result 处理
   * 若先把 streamBuf 清空，随后挂起的 rAF 会用空 buffer 覆写 innerHTML，
   * 已显示的回复就会在结束时消失（interrupted 同理会抹掉半截回复）。
   * 因此收尾一律走这里同步提交；buffer 交给下一次 user_message / clearAll 重置。
   */
  commitStream() {
    if (this._flushPending) this._flushPending = false;
    if (!this.lastAssistant && this.streamBuf) this.createAssistantCard();
    const a = this.lastAssistant;
    if (!a) return;
    a.body.innerHTML = renderMarkdown(this.streamBuf);
    this.autoscroll();
  },

  autoscroll() {
    const m = $("messages");
    const near = m.scrollHeight - m.scrollTop - m.clientHeight < 80;
    if (near) m.scrollTop = m.scrollHeight;
  },
};