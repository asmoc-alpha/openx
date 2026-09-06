"use strict";

/* OpenX Serve — 设置弹窗：模型配置 + 插件管理（Plugins / MCP / Skills）。

   所有写操作都落 settings.json（唯一真源，与 CLI 共享），改 MCP 后需重启
   serve 才生效（stdio 连接在启动时建立）——UI 明确提示，不给"改了没反应"
   的错觉。

   XSS 纪律：插件 id / MCP 名 / skill 名都可能是用户或模型产物，一律
   textContent；只有 markdown 正文走 renderMarkdown（先转义后渲染）。 */

const ROLE_KEYS = ["main", "exec", "mini", "modal"];

const Settings = {
  models: { groups: [], active: "", current: {} },
  editingGroup: null,     // 正在编辑的组名（null = 新增模式）

  init() {
    // 防御：旧版有 #settings-btn / #plugins-btn 顶栏按钮，新版三栏布局里
    // 设置入口改到用户卡上的 ⚙ 按钮（由 Sidebar.bindSettings 绑定）。
    // 这里只在元素存在时挂事件，避免无 #settings-btn 时整段 init 抛错。
    const sb = $("settings-btn"); if (sb) sb.onclick = () => Settings.open("models");
    const pb = $("plugins-btn"); if (pb) pb.onclick = () => Settings.open("plugins");
    const sc = $("settings-close"); if (sc) sc.onclick = () => Settings.close();
    const so = $("settings-overlay");
    if (so) so.addEventListener("click", (e) => {
      if (e.target === so) Settings.close();
    });

    // tabs
    document.querySelectorAll("#settings-tabs .tab").forEach((tab) => {
      tab.onclick = () => Settings.showTab(tab.dataset.tab);
    });

    // 通用：主题（深色 / 浅色 / 跟随系统）
    Settings.initTheme();

    // 模型表单
    Settings.buildRoleInputs();
    const mgSave = $("mg-save"); if (mgSave) mgSave.onclick = () => Settings.saveGroup();
    const mgCancel = $("mg-cancel"); if (mgCancel) mgCancel.onclick = () => Settings.resetForm();

    // MCP / Skill 表单
    const mcpSave = $("mcp-save"); if (mcpSave) mcpSave.onclick = () => Settings.saveMcp();
    const skSave = $("sk-save"); if (skSave) skSave.onclick = () => Settings.saveSkill();

    // 顶栏模型选择器
    const ms = $("model-select"); if (ms) ms.onchange = (e) => Settings.switchGroup(e.target.value);
  },

  // ── 弹窗开关 ────────────────────────────────────────────────
  async open(tab) {
    $("settings-overlay").hidden = false;
    Settings.showTab(tab || "models");
    await Settings.loadModels();
    await Settings.loadPlugins();
    await Settings.loadMcp();
    await Settings.loadSkills();
  },

  close() {
    $("settings-overlay").hidden = true;
  },

  showTab(name) {
    document.querySelectorAll("#settings-tabs .tab").forEach((t) => {
      t.classList.toggle("active", t.dataset.tab === name);
    });
    document.querySelectorAll(".tab-panel").forEach((p) => {
      p.classList.toggle("active", p.dataset.panel === name);
    });
    if (name === "general") Settings.syncThemeUI();
  },

  // ── 通用：外观主题（深色 / 浅色 / 跟随系统）────────────────────
  THEME_KEY: "openx.theme",
  _themeMq: null,

  initTheme() {
    document.querySelectorAll("#theme-row .theme-opt").forEach((b) => {
      b.onclick = () => {
        Settings.applyTheme(b.dataset.themeOpt);
        Settings.syncThemeUI();
      };
    });
    // 进页即按偏好就位：pref=system 时挂系统主题变更监听（OS 切换即时跟随）
    Settings.applyTheme(Settings.currentTheme());
  },

  currentTheme() {
    try { return localStorage.getItem(Settings.THEME_KEY) || "system"; }
    catch (_) { return "system"; }
  },

  applyTheme(pref) {
    pref = pref || "system";
    try { localStorage.setItem(Settings.THEME_KEY, pref); } catch (_) {}
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    if (Settings._themeMq) {
      if (Settings._themeMq.removeEventListener) {
        Settings._themeMq.removeEventListener("change", Settings._onSystemTheme);
      }
      Settings._themeMq = null;
    }
    if (pref === "system") {
      Settings._themeMq = mq;
      if (mq.addEventListener) mq.addEventListener("change", Settings._onSystemTheme);
      Settings._setEffective(mq.matches ? "dark" : "light");
    } else {
      Settings._setEffective(pref);
    }
  },

  _setEffective(which) {
    // which ∈ "dark" | "light" —— 落到 <html data-theme>，style.css 据此切色板
    document.documentElement.setAttribute("data-theme", which);
  },

  _onSystemTheme() {
    if (Settings._themeMq) {
      Settings._setEffective(Settings._themeMq.matches ? "dark" : "light");
    }
  },

  syncThemeUI() {
    const pref = Settings.currentTheme();
    document.querySelectorAll("#theme-row .theme-opt").forEach((b) => {
      b.classList.toggle("active", b.dataset.themeOpt === pref);
    });
  },

  // ── 模型 ────────────────────────────────────────────────────
  buildRoleInputs() {
    const host = $("mg-roles");
    host.innerHTML = "";
    // 竖排：field-row 的横排 flex 会把四个输入框挤扁
    Object.assign(host.style, { display: "flex", flexDirection: "column", gap: "7px" });
    Settings._roleInputs = {};
    for (const role of ROLE_KEYS) {
      const input = el("input", "input");
      input.type = "text";
      input.autocomplete = "off";
      input.placeholder = role === "main"
        ? "main 模型（必填，如 deepseek-v4-pro）"
        : `${role} 模型（留空回落到 main）`;
      host.appendChild(input);
      Settings._roleInputs[role] = input;
    }
  },

  async loadModels() {
    try {
      const data = await OX.get("/api/models");
      Settings.models = data || { groups: [], active: "", current: {} };
    } catch (err) {
      OX.toast("模型配置读取失败：" + err.message, "error");
      return;
    }
    Settings.renderModels();
    Settings.renderModelPicker();
  },

  renderModelPicker() {
    const sel = $("model-select");
    const prev = sel.value;
    sel.innerHTML = "";
    const groups = Settings.models.groups || [];
    if (!groups.length) {
      const opt = el("option");
      opt.textContent = "（未配置模型）";
      opt.value = "";
      sel.appendChild(opt);
      return;
    }
    for (const g of groups) {
      const opt = el("option");
      opt.value = g.name;
      opt.textContent = g.main ? `${g.name} · ${g.main}` : g.name;
      opt.title = `${g.name} (${g.kind || "default kind"})`;
      sel.appendChild(opt);
    }
    const current = (Settings.models.current || {}).group;
    sel.value = groups.some((g) => g.name === prev) ? prev : (current || Settings.models.active || groups[0].name);
  },

  renderModels() {
    const host = $("model-groups");
    host.innerHTML = "";
    const groups = Settings.models.groups || [];
    if (!groups.length) {
      const note = el("div", "hint");
      note.textContent = "还没有模型组。在下方新增一组（main 角色必填）。";
      host.appendChild(note);
      return;
    }
    for (const g of groups) {
      const card = el("div", "card group-edit");
      const head = el("div", "card-head");
      const name = el("span", "card-name");
      name.textContent = g.name;
      head.appendChild(name);

      if (g.name === Settings.models.active) {
        const pill = el("span", "pill ok");
        pill.textContent = "当前";
        head.appendChild(pill);
      }
      const kind = el("span", "card-sub");
      kind.textContent = g.kind || "";
      head.appendChild(kind);

      const actions = el("div", "card-actions");
      const useBtn = el("button", "btn small");
      useBtn.textContent = "切换";
      useBtn.onclick = (e) => { e.stopPropagation(); Settings.switchGroup(g.name); };
      const editBtn = el("button", "btn small");
      editBtn.textContent = "编辑";
      editBtn.onclick = (e) => { e.stopPropagation(); Settings.editGroup(g.name); };
      const delBtn = el("button", "btn small danger");
      delBtn.textContent = "删除";
      delBtn.onclick = (e) => { e.stopPropagation(); Settings.deleteGroup(g.name); };
      actions.append(useBtn, editBtn, delBtn);
      head.appendChild(actions);
      card.appendChild(head);

      const roles = el("div", "card-tags");
      for (const r of ROLE_KEYS) {
        const model = (g.roles || {})[r];
        if (!model) continue;
        const tag = el("span", "tag");
        tag.textContent = `${r}: ${model}`;
        roles.appendChild(tag);
      }
      card.appendChild(roles);

      const meta = el("div", "card-body");
      const bits = [];
      if (g.apiBase) bits.push(g.apiBase);
      bits.push(g.apiKey || "（未设置 API Key）");
      meta.textContent = bits.join(" · ");
      card.appendChild(meta);

      card.onclick = () => Settings.editGroup(g.name);
      host.appendChild(card);
    }
  },

  editGroup(name) {
    const g = (Settings.models.groups || []).find((x) => x.name === name);
    if (!g) return;
    Settings.editingGroup = name;
    $("model-form-title").textContent = `编辑模型组：${name}`;
    $("mg-name").value = g.name;
    $("mg-name").disabled = true;          // 组名是主键，编辑时不可改
    $("mg-kind").value = g.kind || "";
    $("mg-base").value = g.apiBase || "";
    $("mg-key").value = "";                // 不回填掩码：留空 = 保持原值
    $("mg-key").placeholder = g.apiKey
      ? `已设置 ${g.apiKey}（留空保持不变）`
      : "API Key（可填 env:OPENAI_API_KEY）";
    for (const r of ROLE_KEYS) {
      Settings._roleInputs[r].value = (g.roles || {})[r] || "";
    }
    $("mg-cancel").hidden = false;
    $("mg-hint").textContent = "";
    Settings.showTab("models");
    $("mg-kind").focus();
  },

  resetForm() {
    Settings.editingGroup = null;
    $("model-form-title").textContent = "新增模型组";
    $("mg-name").value = "";
    $("mg-name").disabled = false;
    $("mg-kind").value = "";
    $("mg-base").value = "";
    $("mg-key").value = "";
    $("mg-key").placeholder = "API Key（可填 env:OPENAI_API_KEY）";
    for (const r of ROLE_KEYS) Settings._roleInputs[r].value = "";
    $("mg-cancel").hidden = true;
    $("mg-hint").textContent = "";
  },

  async saveGroup() {
    const name = ($("mg-name").value || "").trim();
    if (!name) { OX.toast("请填写组名", "error"); return; }
    const roles = {};
    for (const r of ROLE_KEYS) {
      const v = (Settings._roleInputs[r].value || "").trim();
      if (v) roles[r] = v;
    }
    const group = {
      kind: ($("mg-kind").value || "").trim(),
      apiBase: ($("mg-base").value || "").trim(),
      apiKey: ($("mg-key").value || "").trim(),
      roles,
    };
    try {
      await OX.post("/api/models", { name, group });
      OX.toast(`模型组「${name}」已保存`, "ok");
      Settings.resetForm();
      await Settings.loadModels();
    } catch (err) {
      $("mg-hint").textContent = "保存失败：" + err.message;
      OX.toast("保存失败：" + err.message, "error");
    }
  },

  async deleteGroup(name) {
    if (!OX.confirm(`确定删除模型组「${name}」？`)) return;
    try {
      await OX.del("/api/models/" + encodeURIComponent(name));
      OX.toast(`已删除「${name}」`, "ok");
      if (Settings.editingGroup === name) Settings.resetForm();
      await Settings.loadModels();
    } catch (err) {
      OX.toast("删除失败：" + err.message, "error");
    }
  },

  async switchGroup(name) {
    if (!name) return;
    try {
      await OX.post("/api/models/switch", { group: name });
      OX.toast(`已切换到「${name}」`, "ok");
      await Settings.loadModels();
    } catch (err) {
      OX.toast("切换失败：" + err.message, "error");
      Settings.renderModelPicker();     // 复原下拉框显示
    }
  },

  // ── Plugins ─────────────────────────────────────────────────
  async loadPlugins() {
    let data;
    try {
      data = await OX.get("/api/plugins");
    } catch (err) {
      $("plugin-list").innerHTML = "";
      const note = el("div", "hint");
      note.textContent = "插件清单读取失败：" + err.message;
      $("plugin-list").appendChild(note);
      return;
    }
    const host = $("plugin-list");
    host.innerHTML = "";
    const plugins = (data && data.plugins) || [];
    if (!plugins.length) {
      const note = el("div", "hint");
      note.textContent = "内核尚未加载插件（或该工作区无插件）。";
      host.appendChild(note);
      return;
    }
    for (const p of plugins) {
      const card = el("div", "card");
      const head = el("div", "card-head");
      const name = el("span", "card-name");
      name.textContent = p.id;
      head.appendChild(name);

      const phase = el("span", "pill " + (p.phase === "active" ? "ok" : p.phase === "failed" ? "bad" : "warn"));
      phase.textContent = p.disabled ? "disabled" : p.phase;
      head.appendChild(phase);

      if (p.builtin) {
        const b = el("span", "pill");
        b.textContent = "builtin";
        head.appendChild(b);
      }
      const src = el("span", "card-sub");
      src.textContent = p.source || "";
      src.title = p.source || "";
      head.appendChild(src);
      card.appendChild(head);

      if (p.summary) {
        const body = el("div", "card-body");
        body.textContent = p.summary;
        card.appendChild(body);
      }

      const tags = el("div", "card-tags");
      for (const label of ["tools", "commands", "contexts", "lifecycle", "ui_slots"]) {
        for (const v of (p[label] || [])) {
          const tag = el("span", "tag");
          tag.textContent = `${label.replace("_", " ").replace(/s$/, "")}: ${v}`;
          tags.appendChild(tag);
        }
      }
      if (tags.children.length) card.appendChild(tags);

      if (p.error) {
        const err = el("div", "card-error");
        err.textContent = p.error;
        card.appendChild(err);
      }

      if (!p.builtin) {
        const actions = el("div", "card-actions");
        const btn = el("button", "btn small" + (p.disabled ? "" : " danger"));
        btn.textContent = p.disabled ? "启用" : "禁用";
        btn.onclick = () => Settings.togglePlugin(p.id, !p.disabled);
        actions.appendChild(btn);
        const note = el("span", "hint");
        note.textContent = "启停需重启 serve 生效";
        actions.appendChild(note);
        card.appendChild(actions);
      }
      host.appendChild(card);
    }
  },

  async togglePlugin(id, disabled) {
    try {
      await OX.post(`/api/plugins/${encodeURIComponent(id)}/toggle`, { disabled });
      OX.toast(`已${disabled ? "禁用" : "启用"}「${id}」（重启后生效）`, "ok");
      await Settings.loadPlugins();
    } catch (err) {
      OX.toast("操作失败：" + err.message, "error");
    }
  },

  // ── MCP ─────────────────────────────────────────────────────
  async loadMcp() {
    let data;
    try {
      data = await OX.get("/api/mcp");
    } catch (err) {
      const note = el("div", "hint");
      note.textContent = "MCP 配置读取失败：" + err.message;
      $("mcp-list").appendChild(note);
      return;
    }
    const host = $("mcp-list");
    host.innerHTML = "";
    const servers = (data && data.servers) || [];
    if (!servers.length) {
      const note = el("div", "hint");
      note.textContent = "还没有配置 MCP server。";
      host.appendChild(note);
      return;
    }
    for (const s of servers) {
      const card = el("div", "card");
      const head = el("div", "card-head");
      const name = el("span", "card-name");
      name.textContent = s.name;
      head.appendChild(name);

      const connected = /connected/i.test(s.status || "");
      const pill = el("span", "pill " + (connected ? "ok" : "warn"));
      pill.textContent = s.status || "unknown";
      head.appendChild(pill);

      const cmd = el("span", "card-sub");
      cmd.textContent = [s.command, ...(s.args || [])].join(" ");
      cmd.title = [s.command, ...(s.args || [])].join(" ");
      head.appendChild(cmd);

      const del = el("button", "btn small danger");
      del.textContent = "删除";
      del.onclick = () => Settings.deleteMcp(s.name);
      head.appendChild(del);
      card.appendChild(head);

      if ((s.tools || []).length) {
        const tags = el("div", "card-tags");
        for (const t of s.tools) {
          const tag = el("span", "tag");
          tag.textContent = t;
          tags.appendChild(tag);
        }
        card.appendChild(tags);
      }
      host.appendChild(card);
    }
  },

  async saveMcp() {
    const name = ($("mcp-name").value || "").trim();
    const command = ($("mcp-command").value || "").trim();
    const args = ($("mcp-args").value || "").trim().split(/\s+/).filter(Boolean);
    if (!name) { OX.toast("请填写名称", "error"); return; }
    if (!command) { OX.toast("请填写 command", "error"); return; }
    try {
      await OX.post("/api/mcp", { name, config: { command, args } });
      OX.toast(`MCP「${name}」已保存（重启 serve 生效）`, "ok");
      $("mcp-name").value = "";
      $("mcp-command").value = "";
      $("mcp-args").value = "";
      await Settings.loadMcp();
    } catch (err) {
      OX.toast("保存失败：" + err.message, "error");
    }
  },

  async deleteMcp(name) {
    if (!OX.confirm(`确定删除 MCP server「${name}」？`)) return;
    try {
      await OX.del("/api/mcp/" + encodeURIComponent(name));
      OX.toast(`已删除「${name}」`, "ok");
      await Settings.loadMcp();
    } catch (err) {
      OX.toast("删除失败：" + err.message, "error");
    }
  },

  // ── Skills ──────────────────────────────────────────────────
  async loadSkills() {
    let data;
    try {
      data = await OX.get("/api/skills");
    } catch (err) {
      const note = el("div", "hint");
      note.textContent = "Skill 读取失败：" + err.message;
      $("skill-list").appendChild(note);
      return;
    }
    const host = $("skill-list");
    host.innerHTML = "";
    const skills = (data && data.skills) || [];
    if (!skills.length) {
      const note = el("div", "hint");
      note.textContent = "还没有安装 skill。";
      host.appendChild(note);
      return;
    }
    for (const s of skills) {
      const card = el("div", "card");
      const head = el("div", "card-head");
      const name = el("span", "card-name");
      name.textContent = s.name;
      head.appendChild(name);

      const lvl = el("span", "pill");
      lvl.textContent = s.level || "global";
      head.appendChild(lvl);

      const desc = el("span", "card-sub");
      desc.textContent = s.description || "";
      head.appendChild(desc);

      const del = el("button", "btn small danger");
      del.textContent = "卸载";
      del.onclick = () => Settings.deleteSkill(s.name);
      head.appendChild(del);
      card.appendChild(head);

      if ((s.trigger || []).length) {
        const tags = el("div", "card-tags");
        for (const t of s.trigger) {
          const tag = el("span", "tag");
          tag.textContent = t;
          tags.appendChild(tag);
        }
        card.appendChild(tags);
      }
      host.appendChild(card);
    }
  },

  async saveSkill() {
    const name = ($("sk-name").value || "").trim();
    const content = ($("sk-content").value || "").trim();
    if (!name) { OX.toast("请填写名称", "error"); return; }
    if (!content) { OX.toast("请填写 skill 正文", "error"); return; }
    try {
      await OX.post("/api/skills", {
        name,
        description: ($("sk-desc").value || "").trim(),
        content,
        trigger: ($("sk-trigger").value || "").trim(),
        level: $("sk-level").value || "global",
      });
      OX.toast(`Skill「${name}」已安装`, "ok");
      $("sk-name").value = "";
      $("sk-desc").value = "";
      $("sk-trigger").value = "";
      $("sk-content").value = "";
      await Settings.loadSkills();
    } catch (err) {
      OX.toast("安装失败：" + err.message, "error");
    }
  },

  async deleteSkill(name) {
    if (!OX.confirm(`确定卸载 skill「${name}」？`)) return;
    try {
      await OX.del("/api/skills/" + encodeURIComponent(name));
      OX.toast(`已卸载「${name}」`, "ok");
      await Settings.loadSkills();
    } catch (err) {
      OX.toast("卸载失败：" + err.message, "error");
    }
  },
};
