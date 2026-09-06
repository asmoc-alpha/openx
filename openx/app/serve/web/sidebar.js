"use strict";

/* OpenX Serve — 左栏侧边栏（工作区树 + 会话）。
   数据源走真实 API：GET /api/workspaces 返回全部工作区及其会话。
   - 目录组：组头 = 折叠箭头 + 图标 + 目录名 + 会话数（活动组带"进行中"角标）；
     点组头展开/收起，首载全部展开——目录下的会话直接可见，不用翻折叠。
   - 会话项：标题 + 时间 + 悬停显形的删除按钮（DELETE /api/sessions/{id}）。
     XSS 纪律：所有路径/标题走 textContent。 */

const Sidebar = {
  workspaces: [],     // [{workspace, active, sessions:[...]}]
  filter: "",
  activeSession: "",
  expanded: new Set(),   // 展开的目录绝对路径

  init() {
    this.bindNewChat();
    this.bindSearch();
    this.bindSettings();
    this.reload().then(() => this.renderAll());
  },

  async reload() {
    try {
      this.workspaces = (await OX.get("/api/workspaces")) || [];
    } catch (_) {
      this.workspaces = [];
    }
    // 首次加载：全部工作区默认展开——目录下的会话要能被直接看到；
    // 折叠只由用户手动点组头发起（expanded 非空后不再强制展开）。
    if (!this.expanded.size) {
      for (const g of this.workspaces) {
        if (g.workspace) this.expanded.add(g.workspace);
      }
    }
  },

  renderAll() {
    this.renderWorkspaces();
  },

  /** 当前 serve 正在写的会话 id（live）；回放别的会话时不随之改变。 */
  liveId() {
    return (typeof AppState !== "undefined" && AppState.sessionId) || "";
  },

  // ── 新建会话（品牌钮与「+新建对话」共用） ─────────────────
  async startNewSession() {
    if (AppState.streaming && !OX.confirm("当前回合仍在进行，确定放弃并新建会话？")) return;
    try {
      const data = await OX.post("/api/session/new", {});
      // 新会话 id 立即生效（"进行中"角标与高亮跟着走，不等 init 事件）
      if (data && data.session_id) {
        AppState.sessionId = data.session_id;
        this.activeSession = data.session_id;
      }
    } catch (_) { /* 后端未实现时本地清屏兜底 */ }
    AppState.clearMessages();
    await this.reload();
    this.renderAll();
    OX.toast("已新建会话", "ok");
  },

  bindNewChat() {
    const btn = $("new-session");
    if (btn) btn.onclick = () => this.startNewSession();
    // 品牌行点击 = 新建对话（同 DSH 点 mark）；左栏收成 rail 时先展开
    const brand = $("brand");
    if (brand) {
      brand.onclick = () => {
        const layout = $("layout");
        if (layout && layout.dataset.sidebar === "collapsed") {
          if (typeof togglePane === "function") togglePane("sidebar");
          return;
        }
        this.startNewSession();
      };
    }
  },

  // ── 会话搜索（DSH 工作区浏览器内联搜索） ────────────────
  bindSearch() {
    const box = $("sb-search");
    const inp = $("ws-filter");
    if (!inp) return;
    const apply = () => {
      this.filter = inp.value.trim().toLowerCase();
      if (box) box.classList.toggle("has-text", Boolean(this.filter));
      this.renderWorkspaces();
    };
    inp.addEventListener("input", apply);
    inp.addEventListener("search", apply);   // 原生 ✕ 清除也触发
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { inp.value = ""; apply(); }
    });
    const clear = $("ws-filter-clear");
    if (clear) clear.onclick = () => { inp.value = ""; apply(); inp.focus(); };
  },

  bindSettings() {
    const btn = document.querySelector(".user-settings-btn");
    if (btn) btn.onclick = () => {
      if (typeof Settings !== "undefined") Settings.open("models");
    };
  },

  // ── 工作区树渲染（按目录分组；组头折叠，组内平铺会话） ──
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
      li.textContent = "暂无工作区";
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
      // 搜索态：有命中就强制展开（否则筛出来的会话仍藏在折叠里）
      const isOpen = needle ? visible.length > 0 : this.expanded.has(path);

      const groupLi = el("li", "ws-group" + (group.active ? " is-active" : ""));
      if (isOpen) groupLi.classList.add("open");
      groupLi.dataset.ws = path;

      // ── 组头：折叠箭头 + 图标 + 目录名 + 会话数 ──
      const head = el("div", "ws-head" + (group.active ? " is-active" : ""));
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

      if (group.active) {
        const live = el("span", "ws-live");
        live.textContent = "进行中";
        head.appendChild(live);
      }

      head.onclick = () => this.toggleGroup(path);
      groupLi.appendChild(head);

      // ── 会话列表（展开时才可见：CSS 用 .ws-group.open 控制） ──
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
   * 渲染一条会话行：标题（+ 进行中角标）+ 删除按钮 + 时间。
   * 单击行 = 切到该会话（回放）；点删除按钮不冒泡到行。
   */
  _renderSessionItem(s) {
    const li = el("li", "session-item");
    if (s.session_id === this.activeSession) li.classList.add("active");

    const row = el("div", "session-row");
    const title = el("div", "session-title");
    title.textContent = s.title || "新会话";
    title.title = title.textContent;
    row.appendChild(title);

    if (s.session_id && s.session_id === this.liveId()) {
      const tag = el("span", "session-live");
      tag.textContent = "进行中";
      row.appendChild(tag);
    }

    const del = el("button", "session-del");
    del.type = "button";
    del.title = "删除会话";
    del.setAttribute("aria-label", "删除会话");
    del.textContent = "✕";
    del.onclick = (e) => {
      e.stopPropagation();
      this.deleteSession(s);
    };
    row.appendChild(del);
    li.appendChild(row);

    const sub = el("div", "session-sub");
    const time = el("span");
    time.textContent = s.time || (s.updated_at ? OX.shortDate(s.updated_at) : "");
    sub.appendChild(time);
    li.appendChild(sub);

    // 单击行 = 进入该会话
    li.addEventListener("click", () => {
      this.activeSession = s.session_id;
      this.renderWorkspaces();
      AppState.openReplay(s.session_id);
    });

    return li;
  },

  // ── 动作 ───────────────────────────────────────────────────
  toggleGroup(path) {
    if (this.expanded.has(path)) this.expanded.delete(path);
    else this.expanded.add(path);
    this.renderWorkspaces();
  },

  /**
   * 删除一条会话：确认 → DELETE /api/sessions/{id} → 重载侧栏。
   * 删的是当前打开的会话（实时或正在回放）→ 刷新回到干净的实时视图。
   */
  async deleteSession(s) {
    if (!s || !s.session_id) return;
    const name = String(s.title || "新会话").slice(0, 40);
    if (!OX.confirm(`删除会话「${name}」？此操作不可恢复。`)) return;

    const sid = s.session_id;
    try {
      await OX.del(`/api/sessions/${encodeURIComponent(sid)}`);
    } catch (err) {
      OX.toast(`删除失败：${err.message}`, "err");
      return;
    }

    const wasOpen = this.activeSession === sid;
    // 本地视图先摘掉（避免重载抖动）；随后以服务端为准再拉一次
    for (const g of this.workspaces) {
      g.sessions = (g.sessions || []).filter((x) => x.session_id !== sid);
    }
    if (this.activeSession === sid) this.activeSession = "";
    OX.toast("会话已删除", "ok");

    if (wasOpen) {
      location.reload();   // 删掉的正是当前视图 → 回到实时会话
      return;
    }
    await this.reload();
    this.renderAll();
  },
};

/** 工作区绝对路径 → 展示名（末段目录名）。 */
function wsBase(path) {
  const base = String(path || "").split("/").filter(Boolean).pop();
  return base || String(path || "") || "工作区";
}
