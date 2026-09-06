"use strict";

/* OpenX Serve — 共享工具（最先加载，供 artifacts/settings/app 复用）。
   XSS 纪律：任何模型/工具/文件名文本先 escapeHtml 再进 innerHTML；
   纯文本一律 textContent；链接只放行 http/https/相对路径。 */

const $ = (id) => document.getElementById(id);

function el(tag, cls) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  return n;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// ── 迷你 markdown（先转义后变换）────────────────────────────────
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

// ── 命名空间：API / 提示 ───────────────────────────────────────
const OX = {
  /** 调服务端 REST；返回 data，失败抛 Error（reason 作为 message）。 */
  async api(method, path, body) {
    const opt = { method, headers: {} };
    if (body !== undefined) {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, opt);
    } catch (err) {
      throw new Error("网络错误：无法连接服务");
    }
    let payload = null;
    try { payload = await res.json(); } catch (_) { /* 非 JSON 响应 */ }
    if (!res.ok || (payload && payload.ok === false)) {
      const reason = (payload && payload.reason) || `HTTP ${res.status}`;
      throw new Error(reason);
    }
    // 兼容两种后端返回：{ok:true, data:...} 与原始数组/对象（如 /api/sessions）
    if (payload && typeof payload === "object" && "data" in payload) {
      return payload.data;
    }
    return payload;
  },

  get(path) { return OX.api("GET", path); },
  post(path, body) { return OX.api("POST", path, body); },
  del(path) { return OX.api("DELETE", path); },

  _toastTimer: null,
  toast(msg, kind) {
    const node = $("toast");
    if (!node) return;
    node.textContent = msg;
    node.className = "toast" + (kind ? " " + kind : "");
    node.hidden = false;
    clearTimeout(OX._toastTimer);
    OX._toastTimer = setTimeout(() => { node.hidden = true; }, 2600);
  },

  confirm(msg) {
    return window.confirm(msg);
  },

  /** 相对时间/短日期：会话列表的时间戳展示。 */
  shortDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d)) return "";
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    if (sameDay) {
      return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    }
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  },

  humanSize(bytes) {
    if (!bytes) return "";
    if (bytes < 1024) return bytes + "B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + "K";
    return (bytes / 1024 / 1024).toFixed(1) + "M";
  },
};
