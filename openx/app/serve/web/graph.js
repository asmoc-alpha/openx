"use strict";

/* OpenX Serve — mermaid 子集流程图渲染器（零依赖 vanilla JS）。
   输入 ```mermaid 围栏块内容，输出自包含 SVG 字符串；解析/布局任何
   异常一律内部捕获返回 ""（common.js 据此回落 <pre><code> 代码块）。

   语法子集（超出即整块回落代码块，永不半渲染）：
   - 头：graph / flowchart + 方向（TD|TB|LR|BT|RL，缺省 TD）
   - 节点：A、A[矩形]、A(圆角)、A((圆))、A{菱形}、A[/平行四边形/]、
     A>旗形]；标签可带引号（A["带空格"]）；A-->B 链式中内联声明
   - 边：--> --- -.-> ==>（线型/箭头区分）与带标签形
     A -- 文字 --> B、A -. 文字 .-> B、A == 文字 ==> B、A -->|文字| B
   - 语句分隔：换行 / ; / 顶层逗号；%% 行注释
   不支持：subgraph / classDef / style / & 多目标 / 嵌套括号标签。

   布局：DFS 破环（回边不参与分层）+ Kahn 最长路径分层；层内
   barycenter 若干趟扫描减交叉。BT/RL 由 TD/LR 坐标镜像得到。

   XSS 纪律：节点 id 与标签一律 escapeHtml（common.js 提供）；SVG 只含
   静态形状与文本，无事件、无 foreignObject。配色全走 CSS 变量
   （.gg-* 类，见 style.css），明暗主题自适应。 */

// 流式期间 renderMarkdown 每帧重跑：按源码串缓存渲染结果（LRU 20 条）
const _gg_cache = new Map();
const _GG_CACHE_MAX = 20;

// 上限护栏：超大图直接回落代码块（防病态布局成本）
const _GG_NODE_LIMIT = 200;

/** mermaid 子集 → SVG 字符串；解析失败返回 ""。 */
function renderGraph(code) {
  const src = String(code || "");
  const hit = _gg_cache.get(src);
  if (hit !== undefined) {
    _gg_cache.delete(src);
    _gg_cache.set(src, hit);          // LRU：命中重插到最新
    return hit;
  }
  let svg = "";
  try {
    const parsed = _gg_parse(src);
    if (parsed && parsed.nodes.size) {
      const layout = _gg_layout(parsed);
      svg = layout ? _gg_svg(layout) : "";
    }
  } catch (_) {
    svg = "";                          // 任何异常 → 回落代码块，绝不抛给调用方
  }
  _gg_cache.set(src, svg);
  if (_gg_cache.size > _GG_CACHE_MAX) {
    _gg_cache.delete(_gg_cache.keys().next().value);
  }
  return svg;
}

// ── 解析 ────────────────────────────────────────────────────────

/** 源码 → {dir, nodes:Map<id,{id,label,shape,order}>, edges:[{from,to,label,style,arrow}]}；失败 null。 */
function _gg_parse(src) {
  const header = /^\s*(?:graph|flowchart)\s+(td|tb|bt|lr|rl)?\s*/i.exec(src);
  if (!header) return null;
  let dir = (header[1] || "td").toUpperCase();
  if (dir === "TB") dir = "TD";
  let body = src.slice(header[0].length);
  // 行注释剔除
  body = body.split("\n").filter((ln) => !/^\s*%%/.test(ln)).join("\n");

  const nodes = new Map();
  const edges = [];
  for (const stmt of _gg_statements(body)) {
    if (!_gg_statement(stmt, nodes, edges)) return null;
  }
  if (!nodes.size || nodes.size > _GG_NODE_LIMIT) return null;
  return { dir, nodes, edges };
}

/** 顶层语句切分：换行 / ; / 顶层逗号（括号深度 0 才切，标签里的逗号保留）。 */
function _gg_statements(body) {
  const out = [];
  let cur = "";
  let depth = 0;
  for (const ch of body) {
    if (ch === "[" || ch === "(" || ch === "{") depth++;
    // 收括号在深度 0 时忽略计数：旗形 `A>label]` 的 `]` 没有开括号，
    // 但仍要闭合（否则整块误判失衡）。真失衡（`A[` 之类）由收尾
    // depth!==0 兜底——中途不抛，避免把合法旗形整块打回代码块。
    else if (ch === "]" || ch === ")" || ch === "}") { if (depth > 0) depth--; }
    if (depth === 0 && (ch === "\n" || ch === ";" || ch === ",")) {
      if (cur.trim()) out.push(cur.trim());
      cur = "";
    } else {
      cur += ch;
    }
  }
  if (depth !== 0) throw new Error("unbalanced bracket");
  if (cur.trim()) out.push(cur.trim());
  return out;
}

/** 从 i 起识别一个完整的边 token（含可选内联标签）。逐字符左→右扫描、
    每次只消费到最近一个合法闭合箭头。不用一次全局正则匹配整句里的所有
    边——那样会在链式 ``A --x--> B --y--> C`` 里把前一箭头头当标签起点
    “桥接”吞并整段（x 前的 ``--`` 直连到 y 的 ``-->``）。失败返回 null
    （调用方整块回落）。len：整段长；label 已去首尾空白；arrow=false =
    ---/=== 无箭头。 */
function _gg_arrow_at(s, i) {
  const c1 = s[i], c2 = s[i + 1];
  let style;
  if (c1 === "=" && c2 === "=") style = "thick";
  else if (c1 === "-" && c2 === "-") style = "solid";
  else if (c1 === "-" && c2 === ".") style = "dashed";
  else return null;
  // 纯箭头：开口即箭头头（--> / ==>）
  if (s[i + 2] === ">") return { len: 3, style, arrow: true, label: "" };
  if (style === "dashed") {
    // 纯点线箭头 -.->（开口 `-.` 后紧跟 `->`）→ 否则必是标签形 -. 文字 .->
    if (s[i + 2] === "-" && s[i + 3] === ">") {
      return { len: 4, style, arrow: true, label: "" };
    }
    const p = _gg_label_close(s, i + 2, ".-");
    return p < 0 ? null : {
      len: p - i + 1, style, arrow: true,
      label: s.slice(i + 2, p - 2).trim(),
    };
  }
  // 实线/粗线：先判无箭头 run（---/=== ≥3 连写）——否则后面箭头会被误当
  // 本 run 的“标签闭合”（`A --- B --> C` 里 B-->C 的箭头头恰好满足）
  let k = i;
  while (s[k] === c1) k++;
  if (k - i >= 3) return { len: k - i, style, arrow: false, label: "" };
  const p = _gg_label_close(s, i + 2, c1 + c1);
  return p < 0 ? null : {
    len: p - i + 1, style, arrow: true,
    label: s.slice(i + 2, p - 2).trim(),
  };
}

/** 标签形闭合：从 from 起找第一个 `>`，其紧前两字符 == tail——闭合箭头头
    前的两个线字符：实线/粗线是 c1+c1（``--``/``==``），点线是 ``.-``。
    标签里出现的裸 `>` 不符 tail 即跳过继续找——模型产物标签宁可按字面
    显示，也不整块打回。返回 `>` 下标或 -1。 */
function _gg_label_close(s, from, tail) {
  for (let p = from + 2; p < s.length; p++) {
    if (s[p] === ">" && s.slice(p - 2, p) === tail) return p;
  }
  return -1;
}

/** 一条语句：裸节点声明，或 node (edge node)+ 链。失败 false。 */
function _gg_statement(stmt, nodes, edges) {
  // 左→右扫描，产出 [node, 边, node, 边, ...] 交替 token；节点标签括号
  // 内（depth>0）的 `-`/`=` 不当作边起点。
  const toks = [];
  let cur = "", depth = 0;
  for (let i = 0; i < stmt.length; i++) {
    const ch = stmt[i];
    if (ch === "[" || ch === "(" || ch === "{") depth++;
    else if (ch === "]" || ch === ")" || ch === "}") { if (depth > 0) depth--; }
    const arrowStart =
      depth === 0 &&
      ((ch === "-" && (stmt[i + 1] === "-" || stmt[i + 1] === ".")) ||
       (ch === "=" && stmt[i + 1] === "="));
    if (arrowStart) {
      const arr = _gg_arrow_at(stmt, i);
      if (!arr) return false;
      if (cur.trim()) toks.push({ node: cur.trim() });
      toks.push({ edge: arr });
      cur = "";
      i += arr.len - 1;
      continue;
    }
    cur += ch;
  }
  if (cur.trim()) toks.push({ node: cur.trim() });

  // 单节点声明
  if (toks.length === 1) {
    const n = _gg_node(toks[0].node);
    if (!n) return false;
    _gg_register(nodes, n);
    return true;
  }
  // 链式须 node edge node ...（首尾为节点，相邻成对）
  if (toks.length % 2 === 0) return false;
  for (let t = 1; t < toks.length; t += 2) {
    if (!toks[t].edge) return false;
    let right = toks[t + 1].node;
    let label = toks[t].edge.label;
    const pipe = /^\|([^|]*)\|\s*/.exec(right);
    if (pipe) {
      label = label || pipe[1].trim();
      right = right.slice(pipe[0].length);
    }
    const left = _gg_node(toks[t - 1].node);
    const rn = _gg_node(right);
    if (!left || !rn) return false;
    _gg_register(nodes, left);
    _gg_register(nodes, rn);
    edges.push({
      from: left.id,
      to: rn.id,
      label: label || "",
      style: toks[t].edge.style,
      arrow: toks[t].edge.arrow,
    });
  }
  return true;
}

/** 节点 token → {id, label, shape}；不合子集返回 null。形状先试复合括号。 */
function _gg_node(tok) {
  const s = String(tok || "").trim();
  if (!s) return null;
  const forms = [
    [/^([\w-]+)\s*\(\((.*)\)\)$/, "circle"],
    [/^([\w-]+)\s*\[\/(.*)\/\]$/, "para"],
    [/^([\w-]+)\s*>(.*)\]$/, "flag"],
    [/^([\w-]+)\s*\[(.*)\]$/, "rect"],
    [/^([\w-]+)\s*\((.*)\)$/, "round"],
    [/^([\w-]+)\s*\{(.*)\}$/, "diamond"],
    [/^([\w-]+)$/, "bare"],
  ];
  for (const [re, shape] of forms) {
    const m = re.exec(s);
    if (m) {
      let label = m[2] !== undefined ? String(m[2]).trim() : "";
      if (label.length > 1 && label.startsWith('"') && label.endsWith('"')) {
        label = label.slice(1, -1);
      }
      return { id: m[1], label: label || m[1], shape };
    }
  }
  return null;
}

/** 注册节点（重复声明时后见标签覆盖，id 保序）。 */
function _gg_register(nodes, n) {
  const prev = nodes.get(n.id);
  if (prev) {
    if (n.shape !== "bare") { prev.shape = n.shape; prev.label = n.label; }
  } else {
    n.order = nodes.size;
    nodes.set(n.id, n);
  }
}

// ── 布局 ─────────────────────────────────────────────────────────

/** 解析结果 → {nodes:[{...形状几何}], edges:[{p0,c1,c2,p3,arrow,dirX,dirY,label,style}], width, height}。 */
function _gg_layout(parsed) {
  const nodes = [...parsed.nodes.values()];
  const ids = nodes.map((n) => n.id);

  // 1) DFS 破环：grey→grey 边即回边，不参与分层
  const adj = new Map(ids.map((id) => [id, []]));
  for (const e of parsed.edges) {
    (adj.get(e.from) || []).push(e);
  }
  const color = new Map(ids.map((id) => [id, 0]));
  const back = new Set();
  const stack = [];
  for (const id of ids) {
    if (color.get(id) !== 0) continue;
    stack.push([id, 0]);
    color.set(id, 1);
    while (stack.length) {
      const [u, ei] = stack[stack.length - 1];
      const outs = adj.get(u) || [];
      if (ei < outs.length) {
        stack[stack.length - 1][1]++;
        const v = outs[ei].to;
        if (color.get(v) === 0) {
          color.set(v, 1);
          stack.push([v, 0]);
        } else if (color.get(v) === 1) {
          back.add(outs[ei]);          // 回边（含自环）
        }
      } else {
        color.set(u, 2);
        stack.pop();
      }
    }
  }

  // 2) Kahn 最长路径分层（回边除外 → 剩余是 DAG，覆盖全部节点）
  const layer = new Map(ids.map((id) => [id, 0]));
  const indeg = new Map(ids.map((id) => [id, 0]));
  for (const e of parsed.edges) {
    if (back.has(e)) continue;
    indeg.set(e.to, (indeg.get(e.to) || 0) + 1);
  }
  const queue = ids.filter((id) => !indeg.get(id));
  const preds = new Map(ids.map((id) => [id, []]));
  for (const e of parsed.edges) {
    if (back.has(e)) continue;
    (preds.get(e.to) || []).push(e.from);
  }
  const succs = new Map(ids.map((id) => [id, []]));
  for (const e of parsed.edges) {
    if (back.has(e)) continue;
    (succs.get(e.from) || []).push(e.to);
  }
  let qi = 0;
  const order = [];
  while (qi < queue.length) {
    const u = queue[qi++];
    order.push(u);
    for (const v of succs.get(u) || []) {
      layer.set(v, Math.max(layer.get(v), layer.get(u) + 1));
      indeg.set(v, indeg.get(v) - 1);
      if (!indeg.get(v)) queue.push(v);
    }
  }
  if (order.length !== ids.length) return null;   // 兜底：破环失败不应发生

  // 3) 层内排序：初始按声明序，barycenter 若干趟扫描减交叉（稳定排序）
  const maxL = Math.max(...[...layer.values()], 0);
  const layers = [];
  for (let i = 0; i <= maxL; i++) layers.push([]);
  for (const id of ids) layers[layer.get(id)].push(id);
  const idx = new Map();           // 当前层内位置（相邻层取邻居均值用）
  for (const L of layers) L.forEach((id, i) => idx.set(id, i));
  for (let sweep = 0; sweep < 4; sweep++) {
    const downward = sweep % 2 === 0;
    // 自上而下 / 自下而上交替；按层号索引改写 layers 本体（reverse 副本无效）
    const seqIdx = layers.map((_, i) => (downward ? i : layers.length - 1 - i));
    for (const li of seqIdx) {
      const L = layers[li];
      const neighbors = downward ? preds : succs;
      const bary = L.map((id, i) => {
        const ns = (neighbors.get(id) || []).filter((x) => idx.has(x));
        if (!ns.length) return i;     // 无邻居：保持原位（稳定排序）
        return ns.reduce((a, x) => a + idx.get(x), 0) / ns.length;
      });
      const paired = L.map((id, i) => [id, bary[i]]);
      paired.sort((a, b) => a[1] - b[1]);   // 稳定排序：同 bary 保原序
      layers[li] = paired.map((p) => p[0]);
      layers[li].forEach((id, i) => idx.set(id, i));
    }
  }

  // 4) 几何：主轴 = 层方向（TD/BT 纵 / LR/RL 横），次轴 = 层内排布
  const vertical = parsed.dir === "TD" || parsed.dir === "BT";
  const GAP_P = vertical ? 52 : 60;      // 层间距（主轴）
  const GAP_S = vertical ? 28 : 14;      // 同层节点间距（次轴）
  const MARGIN = 16;
  for (const n of nodes) _gg_size(n);
  const sizeOf = (n) => (vertical ? { p: n.h, s: n.w } : { p: n.w, s: n.h });
  const layerExt = layers.map((L) => {
    let s = 0;
    for (const id of L) s += sizeOf(parsed.nodes.get(id)).s;
    return s + GAP_S * Math.max(0, L.length - 1);
  });
  const maxSec = Math.max(0, ...layerExt);
  const pos = new Map();               // id → {x, y}
  let pCur = MARGIN;
  layers.forEach((L, li) => {
    const pOff = pCur;
    pCur += Math.max(...L.map((id) => sizeOf(parsed.nodes.get(id)).p), 0) + GAP_P;
    let sCur = MARGIN + (maxSec - layerExt[li]) / 2;
    for (const id of L) {
      const n = parsed.nodes.get(id);
      const sz = sizeOf(n);
      pos.set(id, vertical ? { x: sCur, y: pOff } : { x: pOff, y: sCur });
      sCur += sz.s + GAP_S;
    }
  });
  const primaryTotal = pCur - GAP_P;
  const totalW = (vertical ? maxSec : primaryTotal) + 2 * MARGIN;
  const totalH = (vertical ? primaryTotal : maxSec) + 2 * MARGIN;

  // 节点最终盒（BT/RL 镜像主轴坐标）
  const boxes = nodes.map((n) => {
    const { x, y } = pos.get(n.id);
    let bx = x, by = y;
    if (parsed.dir === "BT") by = totalH - y - n.h;
    if (parsed.dir === "RL") bx = totalW - x - n.w;
    return { ...n, x: bx, y: by, cx: bx + n.w / 2, cy: by + n.h / 2 };
  });
  const boxOf = new Map(boxes.map((b) => [b.id, b]));

  // 边：按两端相对位置取附着点（对镜像/回边天然成立）；自环侧偏
  const routes = parsed.edges.map((e) => {
    const a = boxOf.get(e.from);
    const b = boxOf.get(e.to);
    let p0, p3, verticalEdge, forward;
    if (vertical) {
      forward = b.cy >= a.cy;
      p0 = forward ? { x: a.cx, y: a.y + a.h } : { x: a.cx, y: a.y };
      p3 = forward ? { x: b.cx, y: b.y } : { x: b.cx, y: b.y + b.h };
      verticalEdge = true;
    } else {
      forward = b.cx >= a.cx;
      p0 = forward ? { x: a.x + a.w, y: a.cy } : { x: a.x, y: a.cy };
      p3 = forward ? { x: b.x, y: b.cy } : { x: b.x + b.w, y: b.cy };
      verticalEdge = false;
    }
    let c1, c2;
    if (e.from === e.to) {
      // 自环：控制点向右侧甩出
      c1 = verticalEdge
        ? { x: p0.x + 56, y: p0.y }
        : { x: p0.x, y: p0.y + 56 };
      c2 = verticalEdge
        ? { x: p3.x + 56, y: p3.y }
        : { x: p3.x, y: p3.y + 56 };
    } else if (verticalEdge) {
      const mid = (p0.y + p3.y) / 2;
      c1 = { x: p0.x, y: mid };
      c2 = { x: p3.x, y: mid };
    } else {
      const mid = (p0.x + p3.x) / 2;
      c1 = { x: mid, y: p0.y };
      c2 = { x: mid, y: p3.y };
    }
    return { ...e, p0, p1: c1, p2: c2, p3, verticalEdge, forward };
  });

  return { boxes, routes, width: Math.max(totalW, 60), height: Math.max(totalH, 40) };
}

/** 依标签与形状估算节点尺寸（写入 n.w / n.h）。 */
function _gg_size(n) {
  const tw = _gg_text_w(n.label);
  if (n.shape === "circle") {
    const r = Math.max(18, tw / 2 + 12);
    n.w = n.h = Math.round(r * 2);
  } else if (n.shape === "diamond") {
    n.w = Math.round(Math.max(80, (tw + 22) * 1.45));
    n.h = 46;
  } else {
    n.w = Math.round(Math.max(60, tw + 22));
    n.h = 34;
  }
}

/** 12px 字号下的文本宽度估算：ASCII≈6.8px，全角/CJK≈12px。 */
function _gg_text_w(s) {
  let w = 0;
  for (const ch of String(s || "")) w += ch.charCodeAt(0) > 0xff ? 12 : 6.8;
  return w;
}

// ── SVG 生成 ─────────────────────────────────────────────────────

function _gg_svg(layout) {
  const parts = [];
  parts.push(
    `<svg xmlns="http://www.w3.org/2000/svg" width="${layout.width}" ` +
    `height="${layout.height}" viewBox="0 0 ${layout.width} ${layout.height}" ` +
    `role="img" class="gg-svg">`
  );
  for (const r of layout.routes) parts.push(_gg_edge_svg(r));
  for (const b of layout.boxes) parts.push(_gg_node_svg(b));
  parts.push("</svg>");
  return parts.join("");
}

function _gg_node_svg(b) {
  const x = b.x.toFixed(1), y = b.y.toFixed(1);
  const w = b.w.toFixed(1), h = b.h.toFixed(1);
  let shape = "";
  if (b.shape === "circle") {
    shape = `<circle class="gg-node" cx="${(b.w / 2 + b.x).toFixed(1)}" cy="${(b.h / 2 + b.y).toFixed(1)}" r="${(b.w / 2).toFixed(1)}"/>`;
  } else if (b.shape === "diamond") {
    const pts = [
      [b.cx, b.y], [b.x + b.w, b.cy], [b.cx, b.y + b.h], [b.x, b.cy],
    ].map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
    shape = `<polygon class="gg-node" points="${pts}"/>`;
  } else if (b.shape === "para") {
    const sk = Math.min(16, b.w * 0.2);
    const pts = [
      [b.x + sk, b.y], [b.x + b.w, b.y], [b.x + b.w - sk, b.y + b.h], [b.x, b.y + b.h],
    ].map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
    shape = `<polygon class="gg-node" points="${pts}"/>`;
  } else if (b.shape === "flag") {
    const sk = Math.min(14, b.w * 0.22);
    const pts = [
      [b.x, b.y], [b.x + b.w - sk, b.y], [b.x + b.w, b.cy],
      [b.x + b.w - sk, b.y + b.h], [b.x, b.y + b.h],
    ].map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
    shape = `<polygon class="gg-node" points="${pts}"/>`;
  } else {
    const rx = b.shape === "round" ? 12 : 4;
    shape = `<rect class="gg-node" x="${x}" y="${y}" width="${w}" height="${h}" rx="${rx}"/>`;
  }
  const text =
    `<text class="gg-node-text" x="${b.cx.toFixed(1)}" y="${b.cy.toFixed(1)}" ` +
    `text-anchor="middle" dominant-baseline="middle">${escapeHtml(b.label)}</text>`;
  return shape + text;
}

function _gg_edge_svg(r) {
  const d =
    `M ${r.p0.x.toFixed(1)} ${r.p0.y.toFixed(1)} ` +
    `C ${r.p1.x.toFixed(1)} ${r.p1.y.toFixed(1)}, ` +
    `${r.p2.x.toFixed(1)} ${r.p2.y.toFixed(1)}, ` +
    `${r.p3.x.toFixed(1)} ${r.p3.y.toFixed(1)}`;
  const cls = "gg-edge" + (r.style === "dashed" ? " dashed" : r.style === "thick" ? " thick" : "");
  let out = `<path class="${cls}" d="${d}"/>`;
  if (r.arrow) out += _gg_arrow_svg(r);
  if (r.label) out += _gg_edge_label_svg(r);
  return out;
}

/** 箭头：贝塞尔末端切线在轴对齐控制点下即轴向（垂直边→纵向箭头）。 */
function _gg_arrow_svg(r) {
  const { p3, verticalEdge: v, forward } = r;
  let pts;
  if (v) {
    const dir = forward ? 1 : -1;    // forward=向下箭头
    pts = [
      [p3.x, p3.y],
      [p3.x - 4.5, p3.y - dir * 9],
      [p3.x + 4.5, p3.y - dir * 9],
    ];
  } else {
    const dir = forward ? 1 : -1;    // forward=向右箭头
    pts = [
      [p3.x, p3.y],
      [p3.x - dir * 9, p3.y - 4.5],
      [p3.x - dir * 9, p3.y + 4.5],
    ];
  }
  return `<polygon class="gg-arrow" points="${pts.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ")}"/>`;
}

/** 边标签：贝塞尔中点（(P0+3C1+3C2+P3)/8）+ 底衬矩形。 */
function _gg_edge_label_svg(r) {
  const mx = (r.p0.x + 3 * r.p1.x + 3 * r.p2.x + r.p3.x) / 8;
  const my = (r.p0.y + 3 * r.p1.y + 3 * r.p2.y + r.p3.y) / 8;
  const tw = _gg_text_w(r.label);
  const bw = tw + 10, bh = 16;
  return (
    `<rect class="gg-edge-label-bg" x="${(mx - bw / 2).toFixed(1)}" ` +
    `y="${(my - bh / 2).toFixed(1)}" width="${bw.toFixed(1)}" height="${bh}" rx="3"/>` +
    `<text class="gg-edge-label" x="${mx.toFixed(1)}" y="${my.toFixed(1)}" ` +
    `text-anchor="middle" dominant-baseline="middle">${escapeHtml(r.label)}</text>`
  );
}
