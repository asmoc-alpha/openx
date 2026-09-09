"use strict";

/* OpenX Serve — 右栏「路径」标签：任务路径（每轮对话的执行轨迹）。

   数据源 GET /api/trace?session=<id>（REST 拉取，非 WS 推送）：
   - 每轮 = 用户 prompt -> 工具调用（入参派生短串） -> 工具输出 -> 最终回复，
     折叠卡展示，最新一轮展开；
   - 系统 prompt 是会话级块：实时会话展示**当前运行时装配值**（不随会话
     落盘）；历史回放如实显示"未记录"——绝不拿当前 prompt 冒充历史。

   刷新时机：切到本标签 / 回合结束（result · interrupted，标签可见时）/
   手动点刷新 / 进入回放。加载中并发去重（loading 旗标）。

   XSS 纪律：prompt / 工具名 / 输出全是模型可影响的文本，一律
   textContent，绝不拼 innerHTML。 */

const Trace = {
  sessionId: "",   // "" = 实时会话；回放时为被回放的会话 id
  data: null,      // /api/trace 响应（null = 尚未加载 / 加载失败）
  error: "",
  loading: false,

  init() {
    const btn = $("refresh-trace");
    if (btn) btn.onclick = () => Trace.load(Trace.sessionId);
  },

  /** 从 REST 加载（切换会话 / 回合结束 / 手动刷新）；并发调用只跑一次。 */
  async load(sessionId) {
    Trace.sessionId = sessionId || "";
    if (Trace.loading) return;
    Trace.loading = true;
    try {
      const q = Trace.sessionId
        ? `?session=${encodeURIComponent(Trace.sessionId)}` : "";
      Trace.data = await OX.get("/api/trace" + q);
      Trace.error = "";
    } catch (err) {
      Trace.data = null;
      Trace.error = err.message || "加载失败";
    } finally {
      Trace.loading = false;
    }
    Trace.render();
  },

  /** 本标签当前是否可见（回合结束时只在可见时刷新，省一次拉取）。 */
  visible() {
    const panel = document.querySelector('.tp-panel[data-panel="trace"]');
    return Boolean(panel && panel.classList.contains("active"));
  },

  render() {
    Trace.renderRounds();
    Trace.renderSystem();
  },

  renderRounds() {
    const host = $("trace-rounds");
    const empty = $("trace-empty");
    const meta = $("trace-meta");
    if (!host) return;

    const rounds = (Trace.data && Trace.data.rounds) || [];
    host.innerHTML = "";
    if (meta) meta.textContent = Trace.error
      ? "加载失败"
      : `${rounds.length} 轮${Trace.data && Trace.data.session_id && Trace.sessionId ? " · 回放" : ""}`;
    if (empty) {
      empty.hidden = rounds.length > 0;
      if (Trace.error && rounds.length === 0) {
        empty.querySelector(".tp-empty-text").textContent = "路径加载失败";
        empty.querySelector(".tp-empty-hint").textContent = Trace.error;
      } else if (rounds.length === 0) {
        empty.querySelector(".tp-empty-text").textContent = "还没有对话回合";
        empty.querySelector(".tp-empty-hint").textContent =
          "每轮的用户 prompt、工具调用与系统输出会按回合展示在这里";
      }
    }

    // 最新一轮默认展开；历史轮折叠（老回合通常只回看标题）
    for (const r of rounds) {
      host.appendChild(Trace._roundCard(r, r === rounds[rounds.length - 1]));
    }
  },

  /** 一轮 → 折叠卡：<summary> 概要行 + 四块正文（用户 / 工具 / 输出 / 回复）。 */
  _roundCard(r, open) {
    const card = el("details", "trace-round");
    card.open = Boolean(open);

    const summary = el("summary", "trace-round-summary");
    const bits = [`第 ${r.index || "?"} 轮`];
    if (r.steps && r.steps.length) bits.push(`${r.steps.length} 次工具`);
    summary.textContent = bits.join(" · ") + "  " + Trace._firstLine(r.user);
    card.appendChild(summary);

    const body = el("div", "trace-round-body");

    // ① 用户 prompt
    if (r.user) {
      body.appendChild(Trace._block("用户 prompt", r.user, "trace-user"));
    }

    // ② 调用的工具（入参是服务端派生短串，非原样入参）
    if (r.steps && r.steps.length) {
      const toolsBlock = el("div", "trace-block");
      toolsBlock.appendChild(Trace._label(`调用的工具（${r.steps.length}）`));
      const list = el("ul", "trace-steps");
      for (const s of r.steps) {
        const li = el("li", "trace-step");
        const name = el("span", "trace-step-name");
        name.textContent = s.name || "tool";            // 工具名：textContent
        li.appendChild(name);
        if (s.args_summary || s.target) {
          const sub = el("span", "trace-step-sub");
          sub.textContent = s.target || s.args_summary || "";
          li.appendChild(sub);
        }
        list.appendChild(li);
      }
      toolsBlock.appendChild(list);
      body.appendChild(toolsBlock);
    }

    // ③ 系统的输出：工具输出（工具名 + 输出正文）
    if (r.tool_outputs && r.tool_outputs.length) {
      const outBlock = el("div", "trace-block");
      outBlock.appendChild(Trace._label(`系统输出 · 工具返回（${r.tool_outputs.length}）`));
      for (const o of r.tool_outputs) {
        const item = el("div", "trace-out");
        const head = el("div", "trace-out-head");
        const name = el("span", "trace-out-tool");
        name.textContent = o.name || "tool";
        head.appendChild(name);
        if (o.truncated) {
          const note = el("span", "trace-out-trunc");
          note.textContent = "已截断";
          head.appendChild(note);
        }
        const pre = el("pre", "trace-out-body");
        pre.textContent = o.output || "";               // 工具输出：textContent
        item.append(head, pre);
        outBlock.appendChild(item);
      }
      body.appendChild(outBlock);
    }

    // ④ 系统的输出：最终回复
    if (r.assistant) {
      body.appendChild(Trace._block("系统输出 · 最终回复", r.assistant, "trace-reply"));
    }

    if (!body.childElementCount) {
      const none = el("div", "trace-none");
      none.textContent = "（本轮没有留下内容）";
      body.appendChild(none);
    }
    card.appendChild(body);
    return card;
  },

  /** 会话级系统 prompt：实时=当前装配值（折叠，默认收起）；回放=诚实空态。 */
  renderSystem() {
    const box = $("trace-system");
    const body = $("trace-system-body");
    const empty = $("trace-system-empty");
    if (!box || !body || !empty) return;

    const live = Boolean(Trace.data && Trace.data.system_prompt_live);
    const text = (Trace.data && Trace.data.system_prompt) || "";
    box.hidden = !live || !text;
    empty.hidden = live || !Trace.data;   // 尚未加载时不显示空态
    if (live) body.textContent = text;    // 系统 prompt：textContent
  },

  _block(title, text, cls) {
    const block = el("div", "trace-block " + (cls || ""));
    block.appendChild(Trace._label(title));
    const pre = el("pre", "trace-block-body");
    pre.textContent = text || "";
    block.appendChild(pre);
    return block;
  },

  _label(title) {
    const label = el("div", "trace-label");
    label.textContent = title;
    return label;
  },

  _firstLine(text) {
    const s = String(text || "").trim().split("\n")[0].trim();
    return s.length > 60 ? s.slice(0, 59) + "…" : s;
  },
};
