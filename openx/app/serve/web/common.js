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

/** 围栏块分流：mermaid 围栏（或围栏内直接以 graph/flowchart 开头）走
    graph.js 渲染 SVG；渲染失败 / graph.js 缺席回落原代码块路径。 */
function _renderFence(info, code) {
  const wantsGraph =
    info === "mermaid" || /^(graph|flowchart)\b/i.test(code);
  if (wantsGraph && typeof renderGraph === "function") {
    const svg = renderGraph(code);   // 解析失败返回 ""（内部已 catch）
    if (svg) return `<div class="graph-block">${svg}</div>`;
  }
  return `<pre><code>${escapeHtml(code)}</code></pre>`;
}

function renderMarkdown(text) {
  const blocks = [];
  let src = String(text || "");

  // 1. 围栏代码块先抽离（内容只转义，不解析，mermaid 除外）；哨兵格式
  //    正常文本几乎不可能出现，正则整段匹配不残留尾随字符。只匹配已
  //    闭合围栏——流式中的未闭合块按纯文本走，闭合瞬间才成块。
  src = src.replace(/```([^\n]*)\n([\s\S]*?)```/g, (m, info, code) => {
    blocks.push(_renderFence(String(info).trim(), code.replace(/\n$/, "")));
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

  // 4. 行首结构：标题
  html = html.replace(/^#{1,6} (.*)$/gm, (m, body) => {
    const level = m.match(/^#+/)[0].length;
    return `<h${Math.min(level, 6)}>${body}</h${Math.min(level, 6)}>`;
  });

  // 5. 无序 / 有序列表 + 引用（逐行连续分组，行首含转义后的 &gt;）
  html = _blockLists(html);

  // 6. GFM 表格（管道分隔；紧随的表头分隔行触发）
  html = _renderTables(html);

  // 7. 分割线（单独成行的 --- / *** / ___；表格分隔行已被第 6 步消化）
  html = html.replace(/^(\s*)(?:-{3,}|\*{3,}|_{3,})(\s*)$/gm, "<hr>");

  // 8. 恢复代码块
  html = html.replace(PH_RE, (m, i) => blocks[Number(i)]);

  // 9. 段落（空行分段；段内换行 → <br>；块级标签不再套 <p>）
  const parts = html.split(/\n{2,}/).filter((p) => p.trim().length);
  if (!parts.length) return "";
  return parts
    .map((p) => (/^(<h\d|<ul|<pre|<ol|<blockquote|<table|<div)/.test(p.trim()) ? p
      : `<p>${p.replace(/\n/g, "<br>")}</p>`))
    .join("\n");
}

/* ── 迷你 markdown 辅助：列表/引用分组、表格 ─────────────────── */

/** 连续的行首结构：- / 1. / > 各自成组；其余行原样保留。 */
function _blockLists(html) {
  const lines = String(html).split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const ln = lines[i];
    const ulM = /^[-*+] (.*)$/.exec(ln);
    const olM = /^\d{1,3}[.)] (.*)$/.exec(ln);
    const qM = /^&gt;\s?(.*)$/.exec(ln);
    if (ulM || olM || qM) {
      const tag = ulM ? "ul" : olM ? "ol" : "blockquote";
      const items = [];
      const next = tag === "blockquote" ? /^&gt;\s?(.*)$/ : tag === "ol" ? /^\d{1,3}[.)] (.*)$/ : /^[-*+] (.*)$/;
      while (i < lines.length) {
        const m = next.exec(lines[i]);
        if (!m) break;
        items.push(tag === "blockquote" ? m[1] : `<li>${m[1]}</li>`);
        i++;
      }
      out.push(`<${tag}>\n${items.join("\n")}\n</${tag}>`);
    } else {
      out.push(ln);
      i++;
    }
  }
  return out.join("\n");
}

/** 单元格去首尾管道后 split；cell 已是转义+行内化的 HTML。 */
function _tableCells(line) {
  let t = String(line).trim();
  if (t.charAt(0) === "|") t = t.slice(1);
  if (t.charAt(t.length - 1) === "|") t = t.slice(0, -1);
  return t.split("|").map((s) => s.trim());
}

/** 表头分隔行：--- / :--- / :---: / ---:（逐格判定，防单 "---" 误判表格）。 */
function _isDelimiterRow(line) {
  const cells = _tableCells(line);
  if (cells.length === 1 && !/\|/.test(line) && /^-{2,}$/.test(line.trim())) return false;
  return cells.length > 0 && cells.every((c) => /^:?-{1,}:?$/.test(c));
}

function _tableAlign(cell) {
  const c = String(cell).trim();
  const l = c.charAt(0) === ":";
  const r = c.charAt(c.length - 1) === ":";
  return l && r ? "center" : r ? "right" : l ? "left" : "";
}

/** 管道表格 → <table>（含可选对齐）；其它行原样。 */
function _renderTables(text) {
  const lines = String(text).split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const cur = lines[i];
    const nxt = lines[i + 1];
    if (nxt !== undefined && cur.indexOf("|") !== -1 && _isDelimiterRow(nxt)) {
      const header = _tableCells(cur);
      const aligns = _tableCells(nxt).map(_tableAlign);
      const rows = [];
      let j = i + 2;
      while (j < lines.length && lines[j].indexOf("|") !== -1 && !_isDelimiterRow(lines[j])) {
        rows.push(_tableCells(lines[j]));
        j++;
      }
      const attr = (idx) => (aligns[idx] ? ` align="${aligns[idx]}"` : "");
      const head = `<thead><tr>${header
        .map((c, idx) => `<th${attr(idx)}>${c}</th>`)
        .join("")}</tr></thead>`;
      const body = rows.length
        ? `<tbody>${rows
            .map((r) => `<tr>${header
              .map((_, idx) => `<td${attr(idx)}>${r[idx] !== undefined ? r[idx] : ""}</td>`)
              .join("")}</tr>`)
            .join("")}</tbody>`
        : "";
      out.push(`<div class="table-wrap"><table>${head}${body}</table></div>`);
      i = j;
    } else {
      out.push(cur);
      i++;
    }
  }
  return out.join("\n");
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

  /** 上传附件（multipart file）→ 描述符 {id,name,size,kind,mime,relPath}。 */
  async upload(file) {
    const fd = new FormData();
    fd.append("file", file);
    let res;
    try {
      res = await fetch("/api/upload", { method: "POST", body: fd });
    } catch (_) {
      throw new Error("网络错误：无法连接服务");
    }
    let payload = null;
    try { payload = await res.json(); } catch (_) { /* 非 JSON 响应 */ }
    if (!res.ok || (payload && payload.ok === false)) {
      throw new Error((payload && payload.reason) || `HTTP ${res.status}`);
    }
    return payload && payload.data;
  },

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
