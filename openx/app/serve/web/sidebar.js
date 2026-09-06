"use strict";

/* OpenX Serve — 左栏侧边栏（工作区树 + 会话）。
   数据源走真实 API：GET /api/workspaces 返回全部工作区及其会话。
   撤回请求 4 后：顶栏文字按钮（"新建对话" / "搜索"）保留，
   工作区按目录分组但每组不再带 [+]/[×]，会话项不再带"更多"按钮。
   XSS 纪律：所有路径/标题走 textContent。 */

const Sidebar = {
  workspaces: [],     // [{workspace, active, sessions:[...]}]
  filter: "",
  activeSession: "",

  init() {
    this.bindNewChat();
    this.bindSettings();
    this.reload().then(() => this.renderAll());
  },

  async reload() {
    try {
      this.workspaces = (await OX.get("/api/workspaces")) || [];
    } catch (_) {
      this.workspaces = [];
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
      try {
        await OX.post("/api/session/new", {});
      } catch (_) { /* 后端未实现时本地清屏兜底 */ }
      AppState.clearMessages();
      await this.reload();
      this.renderAll();
      OX.toast("已新建会话", "ok");
    };
  },

  bindSettings() {
    const btn = document.querySelector(".user-settings-btn");
    if (btn) btn.onclick = () => {
      if (typeof Settings !== "undefined") Settings.open("models");
    };
  },

  // ── 工作区树渲染（按目录分组，每组只展示目录名 + 会话列表） ──
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

      const groupLi = el("li", "ws-group" + (group.active ? " is-active" : ""));
      groupLi.dataset.ws = path;

      // ── 组头：工作区文字 + 计数 ──
      const head = el("div", "ws-head");
      const name = el("span", "ws-name");
      name.textContent = wsBase(path);
      name.title = path;
      head.appendChild(name);

      const count = el("span", "ws-count");
      count.textContent = String(sessions.length);
      head.appendChild(count);

      groupLi.appendChild(head);

      // ── 会话列表（平铺，不折叠） ──
      const list = el("ul", "ws-sessions");
      if (!visible.length) {
        const li = el("li", "session-empty");
        li.textContent = "（暂无会话）";
        list.appendChild(li);
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
   * 渲染一条会话行：标题 + 时间。单击行 = 切到该会话。
   * 撤回请求 4：「更多」按钮 + 删除 popover 移除。
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

    // 单击行 = 进入该会话
    li.addEventListener("click", () => {
      this.activeSession = s.session_id;
      this.renderWorkspaces();
      AppState.openReplay(s.session_id);
    });

    return li;
  },
};

/** 工作区绝对路径 → 展示名（末段目录名）。 */
function wsBase(path) {
  const base = String(path || "").split("/").filter(Boolean).pop();
  return base || String(path || "") || "工作区";
}
