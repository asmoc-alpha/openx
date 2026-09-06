"use strict";

/* OpenX Serve — 右侧产物区（上半「本次改动」· 下半「文件」）+ 预览抽屉。

   产物是**读侧派生**概念：内核只存消息与账本，没有 artifact。两路来源——
   - 进行中：WS 的 ``artifact`` 事件（serve 从写工具入参抽 path 后广播）
   - 复盘：GET /api/artifacts?session=<id>

   XSS 纪律：路径/工具名是模型可影响的文本，一律 textContent。 */

const Artifacts = {
  items: [],          // [{path, tool, exists, size, isImage}]
  seen: new Set(),
  sessionId: "",
  // 文件树：path -> {loading, entries:[{name,path,isDir,size}]}
  dirs: new Map(),
  expanded: new Set(),
  filter: "",

  init() {
    $("refresh-artifacts").onclick = () => Artifacts.load(Artifacts.sessionId);
    $("refresh-files").onclick = () => Artifacts.reloadTree();
    $("file-filter").addEventListener("input", (e) => {
      Artifacts.filter = e.target.value.trim().toLowerCase();
      Artifacts.renderTree();
    });
    $("preview-close").onclick = () => { $("preview").hidden = true; };
    $("preview-copy").onclick = () => {
      const text = $("preview-body").dataset.raw || "";
      navigator.clipboard.writeText(text).then(
        () => OX.toast("已复制", "ok"),
        () => OX.toast("复制失败")
      );
    };
    Artifacts.loadDir("");
  },

  // ── 本次改动 ────────────────────────────────────────────────
  /** WS artifact 事件：增量加一条（去重；已存在则刷新）。 */
  push(path, tool) {
    if (!path || Artifacts.seen.has(path)) return;
    Artifacts.seen.add(path);
    Artifacts.items.push({ path, tool, exists: true, size: 0, isImage: false });
    Artifacts.render();
    // 补全是否存在/大小（列表端点有真实文件系统信息）
    Artifacts.load(Artifacts.sessionId);
  },

  /** 从 REST 加载（切换会话 / 手动刷新）。 */
  async load(sessionId) {
    Artifacts.sessionId = sessionId || "";
    const q = sessionId ? `?session=${encodeURIComponent(sessionId)}` : "";
    try {
      const data = await OX.get("/api/artifacts" + q);
      const list = (data && data.artifacts) || [];
      Artifacts.items = list;
      Artifacts.seen = new Set(list.map((a) => a.path));
    } catch (_) {
      // 服务未就绪时静默（首屏 connect 前会先调一次）
    }
    Artifacts.render();
  },

  render() {
    const host = $("artifact-list");
    host.innerHTML = "";
    $("art-count").textContent = String(Artifacts.items.length);
    if (!Artifacts.items.length) {
      const empty = el("li", "art-empty");
      empty.textContent = "本会话还没有文件改动";
      host.appendChild(empty);
      return;
    }
    for (const a of Artifacts.items) {
      const li = el("li", "artifact-item");
      li.title = `${a.path} · ${a.tool}`;
      const name = el("span", "artifact-path" + (a.exists ? "" : " artifact-missing"));
      // 只显示文件名，目录部分淡出（面板窄，全路径挤不下）
      const parts = String(a.path).split("/");
      name.textContent = parts[parts.length - 1] || a.path;
      const tool = el("span", "artifact-tool");
      tool.textContent = a.tool;
      li.append(name, tool);
      li.onclick = () => Artifacts.preview(a.path);
      host.appendChild(li);
    }
  },

  // ── 文件树（懒加载）─────────────────────────────────────────
  async loadDir(path) {
    Artifacts.dirs.set(path, { loading: true, entries: [] });
    try {
      const data = await OX.get("/api/files?path=" + encodeURIComponent(path));
      Artifacts.dirs.set(path, { loading: false, entries: (data && data.entries) || [] });
    } catch (err) {
      Artifacts.dirs.set(path, { loading: false, entries: [], error: err.message });
    }
    Artifacts.renderTree();
  },

  reloadTree() {
    const open = Array.from(Artifacts.expanded);
    Artifacts.dirs.clear();
    Artifacts.loadDir("").then(() => {
      // 重新展开此前展开的目录（保持用户视图）
      (async () => {
        for (const p of open) {
          if (p === "") continue;
          await Artifacts.loadDir(p);
          Artifacts.renderTree();
        }
      })();
    });
  },

  toggleDir(path) {
    if (Artifacts.expanded.has(path)) {
      Artifacts.expanded.delete(path);
      Artifacts.renderTree();
      return;
    }
    Artifacts.expanded.add(path);
    if (!Artifacts.dirs.has(path)) Artifacts.loadDir(path);
    else Artifacts.renderTree();
  },

  renderTree() {
    const host = $("file-tree");
    host.innerHTML = "";
    Artifacts._renderLevel(host, "", 0);
  },

  _renderLevel(host, path, depth) {
    const node = Artifacts.dirs.get(path);
    if (!node) return;
    if (node.loading) {
      const row = el("div", "tree-row");
      row.textContent = "加载中…";
      host.appendChild(row);
      return;
    }
    let entries = node.entries || [];
    if (Artifacts.filter) {
      // 过滤：文件名命中即显示（目录不过滤掉，否则子项无处可达）
      entries = entries.filter(
        (e) => e.isDir || e.name.toLowerCase().includes(Artifacts.filter)
      );
    }
    if (!entries.length && depth === 0) {
      const row = el("div", "art-empty");
      row.textContent = Artifacts.filter ? "没有匹配的文件" : "（空目录）";
      host.appendChild(row);
      return;
    }
    for (const e of entries) {
      const row = el("div", "tree-row");
      row.style.paddingLeft = 6 + depth * 12 + "px";

      const caret = el("span", "tree-caret");
      caret.textContent = e.isDir ? (Artifacts.expanded.has(e.path) ? "▾" : "▸") : "";
      row.appendChild(caret);

      const name = el("span", "tree-name " + (e.isDir ? "tree-dir" : "tree-file"));
      name.textContent = e.name;
      name.title = e.path;
      row.appendChild(name);

      if (!e.isDir && e.size) {
        const size = el("span", "tree-size");
        size.textContent = OX.humanSize(e.size);
        row.appendChild(size);
      }

      row.onclick = () => {
        if (e.isDir) Artifacts.toggleDir(e.path);
        else Artifacts.preview(e.path);
      };
      host.appendChild(row);

      if (e.isDir && Artifacts.expanded.has(e.path)) {
        Artifacts._renderLevel(host, e.path, depth + 1);
      }
    }
  },

  // ── 预览抽屉 ────────────────────────────────────────────────
  async preview(path) {
    const drawer = $("preview");
    const body = $("preview-body");
    $("preview-path").textContent = path;
    body.innerHTML = "";
    body.dataset.raw = "";
    drawer.hidden = false;

    const isImage = /\.(png|jpe?g|gif|webp|svg|bmp|ico)$/i.test(path);
    if (isImage) {
      const img = el("img");
      img.src = "/api/files/raw?path=" + encodeURIComponent(path);
      img.alt = path;
      body.appendChild(img);
      return;
    }

    try {
      const data = await OX.get("/api/files/content?path=" + encodeURIComponent(path));
      if (data && data.isImage) {
        const img = el("img");
        img.src = "/api/files/raw?path=" + encodeURIComponent(path);
        body.appendChild(img);
        return;
      }
      if (data && data.binary) {
        const note = el("div", "drawer-note");
        note.textContent = "二进制文件，无法以文本预览。";
        body.appendChild(note);
        return;
      }
      const pre = el("pre");
      pre.textContent = (data && data.text) || "";
      body.appendChild(pre);
      body.dataset.raw = (data && data.text) || "";
      if (data && data.truncated) {
        const note = el("div", "drawer-note");
        note.textContent = "（内容过大，已截断显示）";
        body.appendChild(note);
      }
    } catch (err) {
      const note = el("div", "drawer-note");
      note.textContent = "读取失败：" + err.message;
      body.appendChild(note);
    }
  },
};
