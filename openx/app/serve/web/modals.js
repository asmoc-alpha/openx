"use strict";

/* OpenX Serve — 弹窗（权限 / 交互提问 / 计划审批）+ 插件面板快照。
   把原 app.js 中分散的弹窗逻辑收口到此，按请求 id 配对客户端应答。 */

const Modals = {
  permission: null,
  ask: null,
  plan: null,

  init() {
    this.initPermission();
    this.initAsk();
    this.initPlan();
  },

  // ── 权限 ───────────────────────────────────────
  initPermission() {
    const overlay = $("perm-overlay");
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) this.respondPermission(false, false);
    });
    overlay.querySelectorAll("[data-perm]").forEach((btn) => {
      btn.onclick = () => {
        const k = btn.dataset.perm;
        if (k === "allow-once") this.respondPermission(true, false);
        else if (k === "allow-remember") this.respondPermission(true, true);
        else this.respondPermission(false, false);
      };
    });
  },

  showPermission(ev) {
    this.permission = ev;
    $("perm-tool").textContent = ev.tool || "";
    $("perm-reason").textContent = ev.reason || "";
    $("perm-details").textContent = ev.details || ev.args_summary || "";
    const rememberBtn = document.querySelector('#perm-overlay [data-perm="allow-remember"]');
    if (rememberBtn) rememberBtn.hidden = ev.can_remember !== true;
    $("perm-overlay").hidden = false;
  },

  respondPermission(allowed, remember) {
    const p = this.permission;
    $("perm-overlay").hidden = true;
    this.permission = null;
    if (!p) return;
    AppState.send({ type: "permission_response", request_id: p.request_id, allowed, remember });
  },

  // ── ask_user ──────────────────────────────────────
  initAsk() {
    const overlay = $("ask-overlay");
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) this.respondAsk([]); // 落保守默认
    });
    $("ask-skip-btn").onclick = () => this.respondAsk([]);
    $("ask-other-btn").onclick = () => {
      const s = this.ask;
      if (!s) return;
      const cust = $("ask-custom");
      const show = cust.hidden;
      cust.hidden = !show;
      if (show) $("ask-custom-input").focus();
      else {
        if (s) s.otherText = "";
        $("ask-custom-input").value = "";
      }
      this._syncAskSubmit();
    };
    $("ask-custom-input").addEventListener("input", (e) => {
      if (this.ask) this.ask.otherText = e.target.value;
      this._syncAskSubmit();
    });
    $("ask-submit-btn").onclick = () => {
      const s = this.ask;
      if (!s) return;
      const custom = (s.otherText || "").trim();
      this.respondAsk(custom ? [custom] : Array.from(s.selected));
    };
    $("ask-options").addEventListener("change", (e) => {
      const s = this.ask;
      if (!s) return;
      const input = e.target;
      if (input.type === "checkbox") {
        if (input.checked) s.selected.add(input.value);
        else s.selected.delete(input.value);
      } else {
        s.selected.clear();
        if (input.checked) s.selected.add(input.value);
      }
      this._syncAskSubmit();
    });
  },

  _syncAskSubmit() {
    const s = this.ask;
    const hasCustom = Boolean(s && s.otherText.trim());
    $("ask-submit-btn").disabled = !(hasCustom || (s && s.selected.size > 0));
  },

  showAsk(ev) {
    this.ask = {
      request_id: ev.request_id,
      multi_select: Boolean(ev.multi_select),
      selected: new Set(),
      otherText: "",
    };
    $("ask-question").textContent = ev.question || "";
    this._renderAskOptions(ev.options || []);
    $("ask-custom").hidden = true;
    $("ask-custom-input").value = "";
    this._syncAskSubmit();
    $("ask-overlay").hidden = false;
  },

  _renderAskOptions(options, multiSelect) {
    const box = $("ask-options");
    box.innerHTML = "";
    const ms = this.ask ? this.ask.multi_select : multiSelect;
    for (const opt of options) {
      const row = el("label", "ask-option");
      const input = el("input", "ask-choice");
      input.type = ms ? "checkbox" : "radio";
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
  },

  respondAsk(answers) {
    const s = this.ask;
    $("ask-overlay").hidden = true;
    this.ask = null;
    if (!s) return;
    AppState.send({ type: "ask_user_response", request_id: s.request_id, answers });
  },

  // ── plan_request ─────────────────────────────────
  initPlan() {
    const overlay = $("plan-overlay");
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) this.respondPlan(false);
    });
    $("plan-approve-btn").onclick = () => this.respondPlan(true);
    $("plan-reject-btn").onclick = () => this.respondPlan(false);
  },

  showPlan(ev) {
    this.plan = { request_id: ev.request_id };
    $("plan-details").innerHTML = renderMarkdown(ev.plan || "");
    $("plan-overlay").hidden = false;
  },

  respondPlan(approved) {
    const s = this.plan;
    $("plan-overlay").hidden = true;
    this.plan = null;
    if (!s) return;
    AppState.send({ type: "plan_response", request_id: s.request_id, approved });
  },

  // ── 插件 UI 面板 ───────────────────────────────
  renderPanels(panels) {
    const host = $("panels");
    if (!host) return;
    host.innerHTML = "";
    if (!panels.length) {
      host.hidden = true;
      return;
    }
    for (const p of panels) {
      const node = el("div", "panel");
      for (const line of (p.lines || [])) {
        const row = el("div", "panel-line");
        row.textContent = String(line); // 纯文本渲染
        node.appendChild(row);
      }
      host.appendChild(node);
    }
    host.hidden = false;
  },
};