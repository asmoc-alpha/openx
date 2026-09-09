"use strict";

/* OpenX Serve — 右栏「插件」标签：web 插件（用户自定义静态页面）的
   沙箱 iframe 卡片墙。

   数据源 GET /api/web-plugins（REST 拉取，非 WS 推送）：
   - 每个已发现的插件一张卡：启用 → iframe 实时预览（高度取 manifest
     height）；停用 → 占位卡，点「启用」才挂载 iframe。
   - 启停写 POST /api/web-plugins/{name}/toggle（settings.json 的
     webPlugins.disabled，与内核插件同模式），成功后重拉清单——iframe
     随卡片增删，无需重启 serve。
   - 新插件放进目录 → 手动点 ↻ 重扫（本 tab 只在首次激活 / 手动刷新 /
     启停后拉一次，切换会话不重拉：web 插件是全局的，不属于某条会话）。

   安全纪律（与后端 web_plugins.py 设计对齐）：iframe 只给 allow-scripts，
   **不给 allow-same-origin**——插件页落到唯一 opaque origin，读不到父页
   DOM / localStorage，fetch serve API 因无 CORS 头被浏览器拒绝。插件 =
   自包含展示页，不是扩展 openx 权限的通道。

   XSS 纪律：title / description 是插件目录里的文本（可能被写工具改动），
   一律 textContent；iframe src 的插件名先 encodeURIComponent，不用
   innerHTML 拼任何模型/插件可影响的文本。 */

const WebPlugins = {
  list: [],       // 最近一次清单：[{name,title,description,height,source,enabled}]
  dir: "",        // 用户级插件目录（清单响应回带，空态指引用）
  error: "",
  loaded: false,  // 首次成功拉取过（切 tab 幂等，不重复拉）
  loading: false, // 并发去重：一次在途拉取只跑一遍
  busy: false,    // 有启停在写 settings：挡并发写

  init() {
    const btn = $("refresh-web-plugins");
    if (btn) btn.onclick = () => WebPlugins.load(true);
  },

  /** 拉清单。首次（force=false）幂等；↻ / 启停后 force=true 强制重拉。 */
  async load(force) {
    if (WebPlugins.loading || (WebPlugins.loaded && !force)) return;
    const first = !WebPlugins.loaded;
    WebPlugins.loading = true;
    try {
      const data = await OX.get("/api/web-plugins");
      WebPlugins.dir = (data && data.userDir) || "";
      WebPlugins.list = (data && data.plugins) || [];
      WebPlugins.error = "";
      WebPlugins.loaded = true;
    } catch (err) {
      // 已有数据时的刷新失败：保留现场（live iframe 不因瞬时失败被摘走），
      // 只 toast 提示；首次失败才落错误空态。
      if (first) {
        WebPlugins.list = [];
        WebPlugins.error = err.message || "加载失败";
      } else {
        OX.toast("插件清单刷新失败：" + (err.message || "网络错误"));
      }
    } finally {
      WebPlugins.loading = false;
    }
    WebPlugins.render();
  },

  /** 启停单个插件：写 settings → 强制重拉清单（iframe 随卡片增删）。 */
  async toggle(name) {
    if (WebPlugins.busy) return;
    const p = WebPlugins.list.find((x) => x.name === name);
    if (!p) return;
    const disable = p.enabled;   // 目标态 = 当前态取反
    WebPlugins.busy = true;
    const btn = WebPlugins._toggleBtn(name);
    if (btn) btn.disabled = true;   // 防连点（settings 写是整文件替换）
    try {
      await OX.post(
        `/api/web-plugins/${encodeURIComponent(name)}/toggle`,
        { disabled: disable }
      );
      await WebPlugins.load(true);
    } catch (err) {
      OX.toast((err && err.message) || "启停失败");
    } finally {
      WebPlugins.busy = false;
    }
    WebPlugins.render();   // 失败路径把按钮恢复可用；成功路径无伤（幂等渲染）
  },

  render() {
    const meta = $("wp-meta");
    const empty = $("wp-empty");
    const host = $("wp-cards");
    if (!host) return;
    const n = WebPlugins.list.length;
    const enabled = WebPlugins.list.filter((p) => p.enabled).length;

    if (meta) meta.textContent = WebPlugins.error
      ? "加载失败"
      : `${n} 个 · ${enabled} 启用`;

    // 空态（错误 / 真没插件）；有卡片时隐藏，卡片墙里自然包含停用态
    if (empty) {
      empty.hidden = !(WebPlugins.error || n === 0);
      const text = empty.querySelector(".tp-empty-text");
      const hint = empty.querySelector(".tp-empty-hint");
      if (text && hint) {
        if (WebPlugins.error) {
          text.textContent = "插件清单加载失败";
          hint.textContent = WebPlugins.error;
        } else if (n === 0) {
          text.textContent = WebPlugins.loading && !WebPlugins.loaded
            ? "正在扫描插件目录…"
            : "还没有 web 插件";
          hint.textContent =
            `在 ${WebPlugins.dir || "~/.openx/web-plugins"}（或工作区的 ` +
            `.openx/web-plugins）下建一个子目录即是一个插件：里面放 index.html，` +
            `可选 manifest.json（title / description / height）。放好后点 ↻ 重扫。`;
        }
      }
    }

    // 卡片按名字做 keyed reconcile：已挂载且启停态不变的卡**不重建**——重建
    // iframe 会让该插件重跑、丢页面状态。只有启停态翻转 / 新增 / 消失才动 DOM。
    const seen = new Set();
    const byName = new Map();
    for (const card of host.children) byName.set(card.dataset.name, card);
    for (const p of WebPlugins.list) {
      seen.add(p.name);
      const card = byName.get(p.name);
      if (card && card.dataset.enabled !== (p.enabled ? "1" : "0")) {
        card.replaceWith(WebPlugins._card(p));   // 启停翻转：整卡换（iframe 卸载/挂载）
      } else if (!card) {
        host.appendChild(WebPlugins._card(p));
      } else {
        WebPlugins._refreshMeta(card, p);        // 同态：只轻刷标题/描述，不动 iframe
      }
    }
    for (const card of Array.from(host.children)) {
      if (!seen.has(card.dataset.name)) card.remove();   // 目录删了 → 卡片消失
    }
  },

  /** 一张卡：头（标题 + 来源徽 + 启停钮）+ 启用→iframe / 停用→占位条。 */
  _card(p) {
    const card = el("div", "wp-card" + (p.enabled ? "" : " off"));
    card.dataset.name = p.name;
    card.dataset.enabled = p.enabled ? "1" : "0";

    const head = el("div", "wp-head");
    const main = el("div", "wp-main");
    const titleRow = el("div", "wp-title-row");
    const title = el("span", "wp-title");
    title.textContent = p.title || p.name;         // 标题：textContent
    title.title = p.title || p.name;
    titleRow.appendChild(title);
    const badge = el("span", "wp-src " + p.source);
    badge.textContent = p.source === "project" ? "项目" : "用户";
    titleRow.appendChild(badge);
    main.appendChild(titleRow);
    if (p.description) {
      const desc = el("div", "wp-desc");
      desc.textContent = p.description;            // 描述：textContent
      main.appendChild(desc);
    }
    const toggle = el("button", "wp-toggle" + (p.enabled ? " on" : ""));
    toggle.type = "button";
    toggle.textContent = p.enabled ? "停用" : "启用";
    toggle.title = p.enabled
      ? "停用（卸载沙箱卡片，可随时重新启用）"
      : "启用（加载为沙箱 iframe 卡片）";
    toggle.onclick = () => WebPlugins.toggle(p.name);
    head.append(main, toggle);
    card.appendChild(head);

    if (p.enabled) {
      const wrap = el("div", "wp-frame");
      const h = Number(p.height);
      wrap.style.height = (h >= 80 ? h : 320) + "px";   // 高度：manifest（后端已夹取）
      const frame = el("iframe", "wp-frame-iframe");
      frame.setAttribute("sandbox", "allow-scripts");   // 无 allow-same-origin → opaque origin
      frame.src = `/web-plugins/${encodeURIComponent(p.name)}/index.html`;
      frame.title = `${p.title || p.name}（web 插件沙箱）`;
      frame.setAttribute("loading", "lazy");
      wrap.appendChild(frame);
      card.appendChild(wrap);
    } else {
      const off = el("div", "wp-off");
      off.textContent = "已停用——启用后在此加载沙箱卡片";
      card.appendChild(off);
    }
    return card;
  },

  /** 轻刷标题/描述/徽/按钮（同启停态复用卡片时；绝不重建 iframe）。 */
  _refreshMeta(card, p) {
    const title = card.querySelector(".wp-title");
    const desc = card.querySelector(".wp-desc");
    const badge = card.querySelector(".wp-src");
    const toggle = card.querySelector(".wp-toggle");
    if (title) title.textContent = p.title || p.name;
    if (desc) {
      if (p.description) desc.textContent = p.description;
      else desc.remove();
    }
    if (badge) badge.textContent = p.source === "project" ? "项目" : "用户";
    if (toggle) {
      toggle.textContent = p.enabled ? "停用" : "启用";
      toggle.classList.toggle("on", p.enabled);
      toggle.disabled = false;
    }
  },

  _toggleBtn(name) {
    const host = $("wp-cards");
    if (!host) return null;
    for (const card of host.children) {
      if (card.dataset.name === name) return card.querySelector(".wp-toggle");
    }
    return null;
  },
};
