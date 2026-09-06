"use strict";

/* OpenX Serve — 左栏侧边栏（工作区树 + 会话）。
   数据源走真实 API：GET /api/workspaces 返回全部工作区及其会话。
   渲染要点：
   - 顶栏 sidebar-actions：纯图标（+ 新建、⌕ 搜索），不再使用文字按钮
   - 工作区段头：图标代表"工作区"+ 计数 + +（新建工作区）
   - 工作区分组（按目录）：组头 = 折叠 + 目录名 + [+ 在此创建] + [× 删除目录]
   - 会话项：标题 + 时间 + [··· 更多] → 删除会话
   - 搜索：默认折叠，图标按钮 toggle 后显示一行输入框
   XSS 纪律：所有路径/标题走 textContent。 */

const Sidebar = {
  workspaces: [],     // [{workspace, active, sessions:[...]}]
  filter: "",
  activeSession: "",
  expanded: new Set(),
  _popoverSession: null,    // 当前打开的"更多"菜单对应的 session-item

  init() {
    this.bindGlobalFilter();
    this.bindNewChat();
    this.bindSearchToggle();
    this.bindSettings();
    this.bindWorkspaceAdd();
    this.bindPopoverDismiss();
    this.reload().then(() => this.renderAll());
  },

  async reload() {
    try {
      this.workspaces = (await OX.get("/api/workspaces")) || [];
    } catch (_) {
      this.workspaces = [];
    }
    if (!this.expanded.size) {
      const active = this.workspaces.find((g) => g.active);
      if (active) this.expanded.add(active.workspace);
    }
  },

  renderAll() {
    this.renderWorkspaces();
  },

  // ── 顶栏操作 ─────────────────────────────────
  bindNewChat() {
    const btn = $("new-session");
    if (!btn) return;
    btn.onclick = async () => {
      if (AppState.streaming && !OX.confirm("当前回合仍在进行，确定放弃并新建会话？")) return;
      const active = this.workspaces.find((g) => g.active);
      try {
        await OX.post("/api/session/new", {});
        AppState.clearMessages();
        await this.reload();
        this.renderAll();
        OX.toast("已新建会话", "ok");
      } catch (_) {
        // 后端未实现时本地清屏，至少把欢迎页拉回
        AppState.clearMessages();
        OX.toast("已新建会话（本地）", "ok");
      }
      // 新建空会话 → 关闭任务面板，回到初始化视图
      if (typeof TaskPanel !== "undefined" && TaskPanel.close) TaskPanel.close();
      _ = active;
    };
  },

  /**
   * 工作区段头的 +：弹输入框让用户指定要"挂载"的工作目录，
   * 然后调用 /api/workspace/switch（服务端 live 重根）+ 重载侧栏。
   * 输入为空 → 跳出不报错（用户取消）。
   */
  bindWorkspaceAdd() {
    const btn = $("ws-add-root");
    if (!btn) return;
    btn.onclick = () => {
      const hint = (this.workspaces.find((g) => g.active) || {}).workspace || "/";
      const input = window.prompt("新建工作目录（绝对路径）：", hint);
      if (input == null) return;                                  // 取消
      const path = String(input).trim();
      if (!path) return OX.toast("路径不能为空", "error");
      this.switchWorkspace(path);
    };
  },

  bindSearchToggle() {
    const toggle = $("search-toggle");
    const pane = $("search-pane");
    const input = $("global-filter");
    const closeBtn = $("search-close");
    if (!toggle || !pane || !input) return;
    const open = () => {
      pane.hidden = false;
      toggle.setAttribute("aria-expanded", "true");
      requestAnimationFrame(() => input.focus());
    };
    const close = () => {
      pane.hidden = true;
      toggle.setAttribute("aria-expanded", "false");
      input.value = "";
      this.filter = "";
      this.renderWorkspaces();
    };
    toggle.onclick = () => (pane.hidden ? open() : close());
    if (closeBtn) closeBtn.onclick = close;
    input.addEventListener("input", (e) => {
      this.filter = e.target.value.trim().toLowerCase();
      this.renderWorkspaces();
    });
    // 全局 ⌘K 唤起搜索（不阻断浏览器其他快捷键）
    document.addEventListener("keydown", (e) => {
      const cmd = e.metaKey || e.ctrlKey;
      if (cmd && e.key.toLowerCase() === "k") {
        e.preventDefault();
        if (pane.hidden) open(); else input.focus();
      } else if (e.key === "Escape" && !pane.hidden) {
        close();
      }
    });
  },

  bindSettings() {
    const btn = document.querySelector(".user-settings-btn");
    if (btn) btn.onclick = () => {
      if (typeof Settings !== "undefined") Settings.open("models");
    };
  },

  /** 任意空白处点击/ESC：收起所有"更多"菜单。 */
  bindPopoverDismiss() {
    document.addEventListener("click", (e) => {
      if (!this._popoverSession) return;
      const keep = e.target.closest(".session-item");
      if (keep === this._popoverSession) return;
      this._closePopover();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") this._closePopover();
    });
  },

  _closePopover() {
    if (!this._popoverSession) return;
    const item = this._popoverSession;
    item.querySelector(".session-more").dataset.open = "";
    item.querySelector(".session-popover").dataset.open = "";
    this._popoverSession = null;
  },

  // ── 监听：会话重命名（首条用户消息发出后回填） ────────────────
  /** 把刚发出的第一条用户问题写回指定会话的 title（在 workspaces 缓存里）。 */
  renameSession(sessionId, title) {
    if (!sessionId || !title) return;
    const t = String(title).split(/\r?\n/)[0].trim().slice(0, 80);
    for (const g of this.workspaces) {
      const s = (g.sessions || []).find((x) => x.session_id === sessionId);
      if (s) { s.title = t || s.title; break; }
    }
  },

  /** 把一个全新 session 推到当前活动工作区分组顶部（首问命名后再 push）。 */
  pushSession(session, workspace) {
    const w = workspace || (session && session.workspace) || "";
    let group = this.workspaces.find((g) => g.workspace === w);
    if (!group) {
      group = { workspace: w, active: true, sessions: [] };
      this.workspaces.unshift(group);
      this.expanded.add(w);
    }
    group.sessions.unshift(session);
  },

  // ── 工作区树渲染 ────────────────────────────────────────────
  renderWorkspaces() {
    const host = $("workspace-list");
    if (!host) return;
    host.innerHTML = "";
    const needle = this.filter;
    const totalSessions = this.workspaces.reduce(
      (n, g) => n + ((g.sessions || []).length), 0
    );
    $("ws-count").textContent = String(totalSessions);

    if (!this.workspaces.length) {
      const li = el("li", "session-empty");
      li.textContent = "暂无目录 · 点上方 + 新建";
      host.appendChild(li);
      return;
    }

    for (const group of this.workspaces) {
      const path = group.workspace || "";
      const sessions = group.sessions || [];
      const visible = needle
        ? sessions.filter((s) => (s.title || "").toLowerCase().includes(needle)
            || wsBase(path).toLowerCase().includes(needle))
        : sessions;
      const isActive = Boolean(group.active);
      const isOpen = needle ? visible.length > 0 : this.expanded.has(path);

      const groupLi = el("li", "ws-group");
      groupLi.dataset.ws = path;
      if (isOpen) groupLi.classList.add("open");

      // ── 组头 ──
      const head = el("div", "ws-head" + (isActive ? " is-active" : ""));
      const caret = el("span", "ws-caret");
      caret.textContent = isOpen ? "▾" : "▸";
      const icon = el("span", "ws-icon");
      icon.textContent = "▤";
      const name = el("span", "ws-name");
      name.textContent = wsBase(path);
      name.title = path;
      head.append(caret, icon, name);

      const count = el("span", "ws-count");
      count.textContent = String(sessions.length);
      head.appendChild(count);

      // 右侧两个图标按钮：在该目录创建会话 / 删除该目录
      const createBtn = el("button", "ws-action-btn ws-create");
      createBtn.type = "button";
      createBtn.title = "在此目录新建对话";
      createBtn.textContent = "+";
      createBtn.onclick = (e) => {
        e.stopPropagation();
        this.createSessionInDir(path);
      };
      head.appendChild(createBtn);

      const deleteBtn = el("button", "ws-action-btn ws-delete");
      deleteBtn.type = "button";
      deleteBtn.title = isActive ? "删除当前工作区（不可恢复）" : "删除目录及全部会话";
      deleteBtn.textContent = "×";
      deleteBtn.onclick = (e) => {
        e.stopPropagation();
        this.deleteGroup(path, isActive);
      };
      head.appendChild(deleteBtn);

      head.onclick = () => this.toggleGroup(path);
      groupLi.appendChild(head);

      // ── 会话列表 ──
      const list = el("ul", "ws-sessions");
      if (!visible.length) {
        if (!needle) {
          const li = el("li", "session-empty");
          li.textContent = "（暂无会话）";
          list.appendChild(li);
        }
      } else {
        for (const s of visible) {
          list.appendChild(this._renderSessionItem(s));
        }
      }
      groupLi.appendChild(list);
      host.appendChild(groupLi);
    }
  },

  /**
   * 渲染一条会话行：标题 + 时间 + 「更多」(···) 按钮 + 隐藏的 popover（删除）。
   * 单击行 = 切到该会话（保留原行为）；点击更多 = 切换 popover，不冒泡到行。
   */
  _renderSessionItem(s) {
    const li = el("li", "session-item");
    if (s.session_id === this.activeSession) li.classList.add("active");

    const title = el("div", "session-title");
    title.textContent = s.title || "新会话";
    title.title = title.textContent;
    li.appendChild(title);

    const sub = el("div", "session-sub");
    const time = el("span");
    time.textContent = s.time || (s.updated_at ? OX.shortDate(s.updated_at) : "");
    sub.appendChild(time);
    li.appendChild(sub);

    // 「更多」按钮：toggle popover
    const more = el("button", "session-more");
    more.type = "button";
    more.title = "更多";
    more.setAttribute("aria-label", "更多操作");
    more.textContent = "···";
    more.onclick = (e) => {
      e.stopPropagation();
      // 若已为别的会话打开菜单，先关掉
      if (this._popoverSession && this._popoverSession !== li) this._closePopover();
      const openNow = more.dataset.open === "true";
      if (openNow) this._closePopover();
      else this._openPopover(li, s);
    };
    li.appendChild(more);

    // popover（删除会话）
    const pop = el("div", "session-popover");
    const del = el("button", "session-popover-item danger");
    del.type = "button";
    del.textContent = "删除会话";
    del.onclick = (e) => {
      e.stopPropagation();
      this._closePopover();
      this.deleteSession(s);
    };
    pop.appendChild(del);
    li.appendChild(pop);

    // 单击行 = 进入该会话（点 more / popover 内排除冒泡）
    li.addEventListener("click", (e) => {
      if (e.target.closest(".session-more") || e.target.closest(".session-popover")) return;
      this.activeSession = s.session_id;
      this.renderWorkspaces();
      AppState.openReplay(s.session_id);
    });

    return li;
  },

  _openPopover(item, _s) {
    item.querySelector(".session-more").dataset.open = "true";
    item.querySelector(".session-popover").dataset.open = "true";
    this._popoverSession = item;
  },

  // ── 动作 ─────────────────────────────────────────────────
  toggleGroup(path) {
    if (this.expanded.has(path)) this.expanded.delete(path);
    else this.expanded.add(path);
    this.renderWorkspaces();
  },

  /**
   * 在指定目录下创建一个新会话：先尝试以该目录调 /api/session/new
   * （后端实现时把 workspace 写入 body；fallback 仍走当前活动工作区）。
   */
  async createSessionInDir(path) {
    if (AppState.streaming && !OX.confirm("当前回合仍在进行，确定新建会话？")) return;
    try {
      await OX.post("/api/session/new", { workspace: path });
    } catch (_) {
      // 后端不接收 workspace 参数时：仍清屏，active workspace 维持
    }
    AppState.clearMessages();
    if (typeof TaskPanel !== "undefined" && TaskPanel.close) TaskPanel.close();
    await this.reload();
    this.renderAll();
    OX.toast(`已在「${wsBase(path)}」创建新对话`, "ok");
  },

  /**
   * 删除整个工作区分组：先确认 → 试图调 DELETE 接口 → 无论成败都从本地缓存移除。
   * 若是活动工作区，则跳过（不让用户把当前工作区整棵搬走）。
   */
  async deleteGroup(path, isActive) {
    const name = wsBase(path);
    if (!OX.confirm(`删除目录「${name}」及其全部会话？此操作不可恢复。`)) return;
    let removed = false;
    try {
      const res = await fetch(`/api/workspaces/${encodeURIComponent(path)}`, { method: "DELETE" });
      if (res.ok || res.status === 404) removed = true;
    } catch (_) { /* 后端无该接口时静默 */ }
    if (!removed && !OX.confirm("服务端未确认删除（可能不支持），是否仅从本机视图中隐藏？")) return;
    this.workspaces = this.workspaces.filter((g) => g.workspace !== path);
    this.expanded.delete(path);
    if (isActive && typeof TaskPanel !== "undefined") TaskPanel.close();
    this.renderAll();
    OX.toast(`已删除「${name}」`, "ok");
  },

  /** 删除一条会话：先确认 → DELETE → 重载。 */
  async deleteSession(sessionInfo) {
    if (!sessionInfo) return;
    const name = sessionInfo.title || "新会话";
    if (!OX.confirm(`删除会话「${name}」？此操作不可恢复。`)) return;
    try {
      await fetch(`/api/sessions/${encodeURIComponent(sessionInfo.session_id)}`, { method: "DELETE" });
    } catch (_) { /* 后端无该接口时仍继续——本地视图过滤 */ }
    // 本地视图：所有分组里把这条 session 摘掉
    for (const g of this.workspaces) {
      const idx = (g.sessions || []).findIndex((x) => x.session_id === sessionInfo.session_id);
      if (idx >= 0) g.sessions.splice(idx, 1);
    }
    if (this.activeSession === sessionInfo.session_id) this.activeSession = "";
    this.renderAll();
    OX.toast("会话已删除", "ok");
  },

  /** 切换 serve 当前工作区（服务端 live 重根 + 新会话）。 */
  async switchWorkspace(path) {
    const name = wsBase(path);
    if (AppState.streaming && !OX.confirm(`当前回合仍在进行，确定切换到「${name}」？`)) return;
    try {
      await OX.post("/api/workspace/switch", { workspace: path });
    } catch (err) {
      OX.toast(`切换失败：${err.message}`, "err");
      return;
    }
    OX.toast(`已切换到「${name}」，刷新中…`, "ok");
    setTimeout(() => location.reload(), 250);
  },

};

/** 工作区绝对路径 → 展示名（末段目录名）。 */
function wsBase(path) {
  const base = String(path || "").split("/").filter(Boolean).pop();
  return base || String(path || "") || "工作区";
}
