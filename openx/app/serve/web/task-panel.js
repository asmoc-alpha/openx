"use strict";

/* OpenX Serve — 右栏任务面板。
   五标签：任务流 / 路径 / 上下文 / 产物 / 插件（路径见 trace.js，REST 拉取）。

   **数据全部来自真实会话事件，没有 mock**：
   - 任务流：一次回合 = 一个任务。步骤 = 本轮**真实发生过**的工具调用
     （tool_use 开一步、tool_result 收一步，按服务端给的 call_id 配对、
     按 round 分轮）；状态与耗时来自 user_message / result / interrupted；
     等待闸门由 interaction 事件（permission / ask_user / plan）驱动。
     执行计划（``todos`` 快照）与子 agent（``fleet`` 快照）是**状态型
     下行**，单独渲染为两块清单。
   - 上下文：本会话**真实读/写过**的文件路径。路径由服务端从工具入参
     派生后随 tool_use 的 ``target`` 字段下发（读侧派生，绝不原样回传
     入参——write_file 的 content 可能含整个文件）。
   - 产物：沿用 Artifacts 模块（artifact 事件，同样派生自真实写工具）。

   三条诚实性纪律：
   1. **不猜百分比**。回合结束前总步数未知，编一个「60%」「预计还需 2
      分钟」就是撒谎。故进度条只在回合收尾时定格 100%，进行中走不确定
      态动画，文案只报已知事实（第几步 / 第几轮 / 已耗时 / 多久没动静）。
   2. **不猜结果**。耗时只用服务端实测值（缺就不显示，不当 0）；失败状态
      只认 tool_result.is_error；历史回合不持久化失败标记就不标成功/失败。
   3. **没有数据就显示空态**。不拿示例数据填充，避免用户把假流程当成
      真实执行状态。

   XSS 纪律：工具名 / 路径 / 输出 / 用户消息都是模型或产物文本，一律走
   textContent，绝不拼 innerHTML（innerHTML 只用于清空）。 */

const TaskPanel = {
  // 本轮回合：title 取用户消息；status 见 STATUS 表
  turn: { title: "", status: "idle", startedAt: 0, endedAt: 0, error: "" },
  steps: [],      // 本轮步骤（回合开始清空）
  notes: [],      // 人工闸门留痕（interaction close：谁批了 / 超时 / 断流）
  awaiting: null, // 当前挂起的人工闸门 {kind, tool, detail, since}
  todos: [],      // 执行计划（todo_write 全量快照：跨回合保持，新会话清空）
  agents: [],     // 子 agent（task 委派快照：本轮委派，回合开始清空）
  context: [],    // 本会话文件引用（跨回合累积，最近使用的在前）
  usage: null,    // 最近一次 result 事件的累计 token 用量
  lastTurn: null,
  archived: null, // /api/flow 的历史回合（刷新/回放后当前无 live 步骤时展示）
  _ticker: null,
  _lastEventAt: 0, // 最近一次进展事件时刻（停滞检测；不猜 ETA，只报事实）

  init() {
    this.bindTabs();
    this.bindHeadCollapse();
    this.render();
  },

  /** 点标题栏空白处收起右栏；右侧的按钮（设置 / 收起）各自处理，不冒泡到这里 */
  bindHeadCollapse() {
    const head = document.querySelector("#task-panel .tp-head");
    if (!head) return;
    head.addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      if (typeof togglePane === "function") togglePane("taskpanel");
    });
  },

  bindTabs() {
    document.querySelectorAll("#tp-tabs .tp-tab").forEach((tab) => {
      tab.onclick = () => this.showTab(tab.dataset.tab);
    });
  },

  showTab(name) {
    document.querySelectorAll("#tp-tabs .tp-tab").forEach((t) => {
      t.classList.toggle("active", t.dataset.tab === name);
    });
    document.querySelectorAll(".tp-panel").forEach((p) => {
      p.classList.toggle("active", p.dataset.panel === name);
    });
    if (name === "artifacts" && typeof Artifacts !== "undefined") {
      Artifacts.load(Artifacts.sessionId);
    }
    if (name === "trace" && typeof Trace !== "undefined") {
      Trace.load(Trace.sessionId);
    }
    if (name === "webplugins" && typeof WebPlugins !== "undefined") {
      WebPlugins.load();   // 幂等：首次激活拉清单，启停/↻ 内部强制重拉
    }
    if (name === "flow") this.loadHistory(this._sessionId());
  },

  // ── 事件入口（app.js 的 reducer 调用）——────────────────────────
  // 只收真实 WS 事件；没有任何 mock 分支。

  onTurnStart(text) {
    this.steps = [];
    this.notes = [];
    this.awaiting = null;
    this.agents = [];   // 服务端回合级 fleet.reset：本轮委派从空起算
    this.archived = null;
    this.turn = {
      title: firstLine(text) || "新会话",
      status: "thinking",
      startedAt: Date.now(),
      endedAt: 0,
      error: "",
    };
    this._touch();
    this._startTicker();
    this.render();
  },

  onThinking() {
    this._touch();
    this._status("thinking");
    this.renderCurrentTask();
  },

  /** tool_use → 开一步。ev 是完整下行事件（call_id / round / args_summary）。 */
  onToolUse(ev) {
    if (!this._active()) return;
    const e = ev || {};
    const target = e.target || "";
    const step = {
      num: this.steps.length + 1,
      name: e.name || "tool",
      summary: e.target || e.args_summary || "",
      target,
      callId: e.call_id || "",
      round: Number(e.round) || 0,
      status: "running",
      startedAt: Date.now(),
      durationMs: null,      // 服务端实测；未知就是 null（不显示耗时）
      isError: false,
      output: "",
      truncated: false,
      open: false,
      ctx: null,
    };
    // 步骤与上下文条目是同一件事的两面：步骤收尾时顺带把上下文标成已读/已改
    if (target) step.ctx = this._touchContext(target, step.name);
    this.steps.push(step);
    this.turn.status = "running";
    this.awaiting = null;   // 工具开跑 = 闸门已放行
    this._touch();
    this.render();
  },

  /** tool_result → 收一步：按 call_id 配对（缺失才退回同名后进先出）。 */
  onToolResult(ev) {
    const e = ev || {};
    const name = e.name || "tool";
    const callId = e.call_id || "";
    let hit = -1;
    if (callId) {
      for (let i = this.steps.length - 1; i >= 0; i--) {
        const s = this.steps[i];
        if (s.callId === callId) { hit = i; break; }
      }
    }
    if (hit < 0) {
      for (let i = this.steps.length - 1; i >= 0; i--) {
        const s = this.steps[i];
        if (s.status === "running" && s.name === name) { hit = i; break; }
      }
    }
    if (hit >= 0) {
      const s = this.steps[hit];
      s.status = e.is_error ? "error" : "done";
      s.isError = Boolean(e.is_error);
      s.durationMs = typeof e.duration_ms === "number" ? e.duration_ms : null;
      s.output = e.output || "";
      s.truncated = Boolean(e.truncated);
      // 截断前的原始长度（服务端给）——展开后也能如实说"你少看了多少"
      s.outputChars = typeof e.output_chars === "number" ? e.output_chars : 0;
      // 失败默认展开：错误正文是你唯一想立刻看到的东西
      if (s.isError) s.open = true;
      if (s.ctx) s.ctx.state = s.isError ? "error" : "done";
    }
    if (!this.steps.some((s) => s.status === "running")) this._status("answering");
    this._touch();
    this.render();
  },

  onTextDelta() {
    this._touch();
    // 文本到达时若还有工具在跑，状态仍归「执行工具」
    if (!this.steps.some((s) => s.status === "running")) this._status("answering");
    this.renderCurrentTask();
  },

  onResult(ev) {
    if (!this._active()) return;
    const e = ev || {};
    this.turn.status = e.is_error ? "error" : "done";
    this.turn.endedAt = Date.now();
    this.turn.error = e.error || "";
    this.awaiting = null;
    if (e.usage) this.usage = e.usage;
    this.lastTurn = {
      toolCalls: this.steps.length,
      durationMs: e.duration_ms || 0,
      rounds: e.num_turns || this._roundsUsed(),
    };
    this._stopTicker();
    this.render();
  },

  onInterrupted(ev) {
    if (!this._active()) return;
    const e = ev || {};
    this.turn.status = "interrupted";
    this.turn.kind = e.kind || "client";
    this.turn.endedAt = Date.now();
    this.awaiting = null;
    for (const s of this.steps) {
      if (s.status === "running") s.status = "error";
    }
    this._stopTicker();
    this.render();
  },

  // ── 人工闸门（permission / ask_user / plan）─────────────────────
  // 服务端在弹窗广播后发 interaction(open)、裁决后发 interaction(close)。
  // 面板据此把回合标成「等待你处理」并在步骤流里留一条夹注——"卡在哪"
  // 与"谁批的 / 超时 / 断流"都是事实，不能靠猜。

  /** 兜底：raw 请求到达即标等待（服务端 close 事件才是权威解除信号）。 */
  onAwait(kind, ev) {
    if (!this._active()) return;
    const e = ev || {};
    // 两种来源：raw 请求事件（reason/details/question）/ interaction open（detail）
    const detail = kind === "permission"
      ? (e.reason || e.details || e.args_summary || e.detail || "")
      : (e.question || e.detail || "");
    this.awaiting = {
      kind: kind || "permission",
      tool: e.tool || "",
      detail: detail,
      since: Date.now(),
    };
    this.turn.status = "blocked";
    this.render();
  },

  onInteraction(ev) {
    const e = ev || {};
    if (e.phase === "open") {
      this.onAwait(e.kind, e);
      return;
    }
    if (e.phase !== "close") return;
    this.awaiting = null;
    this.notes.push({
      kind: e.kind || "permission",
      outcome: e.outcome || "",
      tool: e.tool || "",
      at: Date.now(),
    });
    if (this._active()) {
      this.turn.status = this.steps.some((s) => s.status === "running")
        ? "running" : "answering";
    }
    this._touch();
    this.render();
  },

  // ── 状态型快照：执行计划 / 子 agent（todos / fleet 下行）───────
  // 数据是"状态"不是增量——收到全量即整体替换（后端已按变化才广播），
  // 渲染与重复事件天然幂等。

  onTodos(list) {
    this.todos = Array.isArray(list) ? list : [];
    this.renderPlan();
  },

  onFleet(list) {
    this.agents = Array.isArray(list) ? list : [];
    this.renderAgents();
  },

  /** 新会话（init 下行，经"新建对话"/切工作区/删当前会话）→ 全量归位。
   *  服务端已清 agent.todos / fleet 并重置上下文；端侧把执行计划、
   *  子 agent、步骤、上下文引用一并清空，避免旧会话状态残留。 */
  onSessionReset() {
    this._stopTicker();
    this.steps = [];
    this.notes = [];
    this.awaiting = null;
    this.todos = [];
    this.agents = [];
    this.context = [];
    this.turn = { title: "", status: "idle", startedAt: 0, endedAt: 0, error: "" };
    this.usage = null;
    this.lastTurn = null;
    this.archived = null;
    this.render();
  },

  // ── 历史上轮（/api/flow）：刷新、回放、切会话后不再是空面板 ──────

  _sessionId() {
    return (typeof AppState !== "undefined" && AppState.sessionId) || "";
  },

  /** 拉取会话任务流并记住**最后一轮**（当前正在跑的一轮由 WS 实时喂）。 */
  async loadHistory(sessionId) {
    try {
      const q = sessionId ? `?session=${encodeURIComponent(sessionId)}` : "";
      const data = await OX.get("/api/flow" + q);
      const turns = (data && data.turns) || [];
      this.archived = turns.length ? turns[turns.length - 1] : null;
    } catch (_) {
      this.archived = null;   // 拉取失败就保持空态，绝不拿旧数据充数
    }
    // 只在没有 live 回合时刷新渲染：进行中不被历史覆盖
    if (!this._active() && this.steps.length === 0) this.render();
  },

  // ── 内部：上下文引用表 ────────────────────────────────────────

  _touchContext(path, tool) {
    const hit = this.context.findIndex((c) => c.path === path);
    if (hit >= 0) this.context.splice(hit, 1);      // 重复引用：移到最前
    const entry = { path, tool, state: "pending" };
    this.context.unshift(entry);
    return entry;                                    // 交回给步骤，供收尾时改状态
  },

  // ── 渲染 ────────────────────────────────────────────────────

  render() {
    this.renderCurrentTask();
    this.renderSteps();
    this.renderPlan();
    this.renderAgents();
    this.renderContext();
    this.renderStats();
  },

  renderCurrentTask() {
    const t = this.turn;
    const meta = STATUS[t.status] || STATUS.idle;

    const dot = $("current-task-dot");
    if (dot) dot.className = "status-dot " + meta.dot;

    const status = $("current-task-status");
    if (status) status.textContent = meta.label;

    const title = $("current-task-title");
    if (title) {
      title.textContent = t.title
        || (this.archived ? firstLine(this.archived.user) : "")
        || "尚未开始对话";
    }

    const step = $("current-task-step");
    if (step) step.textContent = this._elapsedText();

    const fill = $("progress-fill");
    if (fill) {
      const settled = t.status === "done" || t.status === "error"
        || t.status === "interrupted";
      // 不猜进度：只有收尾才定格 100%，进行中一律不确定态
      fill.style.width = settled ? "100%" : "0%";
      fill.classList.toggle("indeterminate", !settled && t.status !== "idle");
    }
    const pct = $("progress-percent");
    if (pct) pct.textContent = this._progressText();
    const eta = $("progress-eta");
    if (eta) eta.textContent = this._etaText();
    this.renderAwait();
  },

  /** 等待闸门横幅（`.ct-await`）：只在有挂起交互时出现，显示在等谁批什么。 */
  renderAwait() {
    const host = $("current-task");
    if (!host) return;
    let banner = host.querySelector(".ct-await");
    if (!this.awaiting) {
      if (banner) banner.remove();
      return;
    }
    if (!banner) {
      banner = el("div", "ct-await");
      host.appendChild(banner);
    }
    const a = this.awaiting;
    const who = a.kind === "permission" ? "批准"
      : a.kind === "ask_user" ? "回答" : "审批计划";
    const bits = [`⏸ 等待你${who}`];
    if (a.tool) bits.push(a.tool);
    if (a.detail) bits.push(a.detail);
    banner.textContent = bits.join(" · ");
    banner.title = a.detail || a.tool || "";
  },

  /** 当前要渲染的步骤来源：有 live 步骤就用它，否则退回历史最后一轮。 */
  _stepSource() {
    if (this.steps.length) return { steps: this.steps, archived: false };
    const a = this.archived;
    if (!a || !Array.isArray(a.steps) || !a.steps.length) {
      return { steps: [], archived: Boolean(a) };
    }
    return {
      steps: a.steps.map((s, i) => ({
        num: i + 1,
        name: s.name || "tool",
        summary: s.target || s.args_summary || "",
        target: s.target || "",
        callId: s.call_id || "",
        round: Number(s.round) || 0,
        // 失败标记 `is_error`：true/false 才敢下结论；null（历史未记录）
        // 一律进 unknown——绝不把"不知道"渲染成"完成了"
        status: s.pending ? "pending" : (s.is_error === true ? "error"
          : s.is_error === false ? "done" : "unknown"),
        durationMs: null,          // 历史不落耗时——不编
        isError: Boolean(s.is_error),
        output: s.output || "",
        truncated: Boolean(s.truncated),
        open: false,
        ctx: null,
      })),
      archived: true,
    };
  },

  renderSteps() {
    const host = $("workflow-steps");
    const empty = $("steps-empty");
    const meta = $("steps-meta");
    const count = $("flow-tab-count");
    if (!host) return;

    const source = this._stepSource();
    const steps = source.steps;
    host.innerHTML = "";
    const done = steps.filter((s) => s.status === "done").length;
    const failed = steps.filter((s) => s.status === "error").length;
    if (meta) {
      const bits = [`${steps.length} 步`];
      if (done) bits.push(`完成 ${done}`);
      if (failed) bits.push(`失败 ${failed}`);
      if (source.archived) bits.push("上一轮");
      meta.textContent = bits.join(" · ");
    }
    if (count) {
      count.textContent = String(steps.length);
      count.hidden = steps.length === 0;
    }
    if (empty) {
      empty.hidden = steps.length > 0;
      // 空态文案随来源走（别把"上一轮"写死——新回合开始后要能翻回来）
      const text = empty.querySelector(".tp-empty-text");
      if (text) {
        text.textContent = source.archived
          ? "上一轮没有工具调用"
          : "本轮还没有工具调用";
      }
    }

    const rows = [];
    let shownRound = 0;
    for (const s of steps) {
      // 轮次分组：同一轮模型请求产出的调用聚在一起（round 由服务端给）
      if (s.round && s.round !== shownRound) {
        shownRound = s.round;
        rows.push({ at: s.startedAt || 0, round: s.round });
      }
      rows.push({ at: s.startedAt || 0, step: s });
    }
    if (!source.archived) {
      for (const n of this.notes) rows.push({ at: n.at || 0, note: n });
    }
    // 步骤与闸门夹注按发生时间交错（同一毫秒按插入序，稳定）
    rows.forEach((row, idx) => { row.idx = idx; });
    rows.sort((a, b) => (a.at - b.at) || (a.idx - b.idx));

    for (const row of rows) {
      if (row.round) {
        const head = el("li", "round-head");
        head.textContent = `第 ${row.round} 轮`;
        host.appendChild(head);
      } else if (row.note) {
        host.appendChild(this._noteRow(row.note));
      } else {
        host.appendChild(this._stepRow(row.step));
      }
    }
  },

  /** 一步：工具名 + 摘要/目标；点击展开输出（失败默认展开）。 */
  _stepRow(s) {
    const li = el("li", "step " + s.status + (s.open ? " open" : ""));
    const num = el("span", "step-num");
    num.textContent = s.num;
    const content = el("div", "step-content");

    const head = el("div", "step-head");
    const name = el("div", "step-name");
    name.textContent = s.name;                    // 工具名：textContent
    const meta = el("div", "step-meta");
    meta.textContent = this._stepMeta(s);
    head.append(name, meta);
    content.appendChild(head);

    if (s.summary && s.summary !== s.name) {
      const sub = el("div", "step-sub");
      sub.textContent = s.summary;                // 命令/路径：textContent
      sub.title = s.target || s.summary;
      content.appendChild(sub);
    }
    if (s.isError && s.output) {
      const err = el("div", "step-err");
      err.textContent = firstMeaningfulLine(s.output);   // 失败首行：textContent
      content.appendChild(err);
    }
    if (s.output) {
      if (s.truncated) {
        const note = el("span", "step-trunc");
        // 只说事实：传输上限截了多少（原始字符数是服务端给的，缺就不说）
        note.textContent = s.outputChars
          ? `已截断 · 原始 ${s.outputChars} 字符`
          : "已截断";
        content.appendChild(note);
      }
      const out = el("pre", "step-out");
      out.hidden = !s.open;
      out.textContent = s.output;                 // 输出：textContent
      content.appendChild(out);
      li.classList.add("clickable");
      li.onclick = () => {
        s.open = !s.open;
        li.classList.toggle("open", s.open);
        out.hidden = !s.open;
      };
    }
    li.append(num, content);
    return li;
  },

  /** 人工闸门夹注：把"谁批的 / 超时 / 断流"变成时间线上的一行。 */
  _noteRow(n) {
    const li = el("li", "step-note " + (n.outcome || ""));
    const icon = el("span", "note-icon");
    icon.textContent = n.outcome === "approved" || n.outcome === "answered"
      ? "✓" : (n.outcome === "timeout" || n.outcome === "disconnected"
        ? "!" : "✕");
    const text = el("span", "note-text");
    const who = n.kind === "permission" ? "权限"
      : n.kind === "ask_user" ? "提问" : "计划";
    text.textContent = `${who}${n.tool ? " · " + n.tool : ""}`
      + ` · ${this._outcomeLabel(n.outcome)}`;
    li.append(icon, text);
    return li;
  },

  renderPlan() {
    const host = $("plan-list");
    const empty = $("plan-empty");
    const meta = $("plan-meta");
    if (!host) return;
    host.innerHTML = "";
    const todos = this.todos;
    const done = todos.filter((t) => t.status === "completed").length;
    if (meta) meta.textContent = todos.length ? `${done}/${todos.length}` : "";
    if (empty) empty.hidden = todos.length > 0;

    for (const t of todos) {
      const status = String(t.status || "pending");
      const li = el("li", "plan-item" + (status === "completed" ? " done"
        : status === "in_progress" ? " active" : ""));
      const dot = el("span", "status-dot " + (status === "completed" ? "done"
        : status === "in_progress" ? "running" : "pending"));
      const body = el("div", "plan-body");
      const title = el("div", "plan-title");
      title.textContent = t.content || t.activeForm || "未命名任务";   // 模型文本：textContent
      const sub = el("div", "plan-sub");
      const bits = [this._planStatusLabel(status)];
      if (status === "in_progress" && t.activeForm && t.activeForm !== (t.content || "")) {
        bits.push(t.activeForm);          // 进行中一项显示"正在做什么"
      }
      sub.textContent = bits.join(" · ");
      body.append(title, sub);
      li.append(dot, body);
      host.appendChild(li);
    }
  },

  renderAgents() {
    const host = $("agent-list");
    const empty = $("agent-empty");
    const meta = $("agent-meta");
    if (!host) return;
    host.innerHTML = "";
    const agents = this.agents;
    const running = agents.filter((a) => a.status === "running").length;
    if (meta) meta.textContent = agents.length
      ? (running ? `${running} 运行中` : "已结束") : "";
    if (empty) empty.hidden = agents.length > 0;

    for (const a of agents) {
      const status = String(a.status || "running");
      const li = el("li", "agent-item " + (status === "done" ? "done"
        : status === "error" ? "error" : "running"));
      const dot = el("span", "status-dot " + (status === "running" ? "running"
        : status === "error" ? "error" : "done"));
      const body = el("div", "agent-body");
      const name = el("div", "agent-name");
      const label = a.label || a.subagent_type || "";
      name.textContent = label;                          // 子代理摘要：textContent
      name.title = label;
      const sub = el("div", "agent-sub");
      const bits = [];
      if (a.subagent_type) bits.push(a.subagent_type);
      if (a.tools_count) bits.push(`${a.tools_count} 次工具`);
      bits.push(fmtClock(a.elapsed));
      bits.push(this._agentStatusLabel(status));
      sub.textContent = bits.join(" · ");
      body.append(name, sub);
      li.append(dot, body);
      host.appendChild(li);
    }
  },

  renderContext() {
    const host = $("context-list");
    const empty = $("context-empty");
    const count = $("context-count");
    const root = $("ctx-root");
    if (!host) return;

    if (root) {
      root.textContent = (typeof AppState !== "undefined" && AppState.workspace) || "—";
    }
    if (count) count.textContent = String(this.context.length);
    if (empty) empty.hidden = this.context.length > 0;

    host.innerHTML = "";
    for (const c of this.context) {
      const li = el("li", "ctx-item " + c.state);
      const icon = el("span", "ctx-icon");
      icon.textContent = c.state === "error" ? "!" : (WRITE_TOOLS.has(c.tool) ? "✎" : "◦");
      const body = el("div", "ctx-body");
      const path = el("div", "ctx-path");
      path.textContent = c.path;                    // 路径：textContent
      path.title = c.path;
      const sub = el("div", "ctx-sub");
      sub.textContent = `${c.tool} · ${this._ctxStateLabel(c)}`;
      body.append(path, sub);
      li.append(icon, body);
      host.appendChild(li);
    }
  },

  renderStats() {
    const host = $("session-stats");
    if (!host) return;
    host.innerHTML = "";
    const rows = [];
    const model = (typeof AppState !== "undefined" && AppState.model) || "";
    const sid = (typeof AppState !== "undefined" && AppState.sessionId) || "";
    rows.push(["模型", model || "—"]);
    rows.push(["会话", sid ? sid.slice(0, 8) : "—"]);
    if (this.usage) {
      rows.push([
        "累计 tokens",
        `${this.usage.input_tokens ?? 0} / ${this.usage.output_tokens ?? 0}`,
      ]);
      // 缓存与插件预算是**累计**口径（与 CLI 的 result 同源），标明以免误读
      if (this.usage.cached_tokens) {
        rows.push(["缓存命中", String(this.usage.cached_tokens)]);
      }
      if (this.usage.plugin_tokens) {
        rows.push(["插件预算", String(this.usage.plugin_tokens)]);
      }
    }
    if (this.lastTurn) {
      rows.push([
        "上轮",
        `${this.lastTurn.toolCalls} 次工具`
          + (this.lastTurn.rounds ? ` · ${this.lastTurn.rounds} 轮` : "")
          + ` · ${fmtDuration(this.lastTurn.durationMs)}`,
      ]);
    }
    for (const [k, v] of rows) {
      const li = el("li", "stat-row");
      const kk = el("span", "stat-key");
      kk.textContent = k;
      const vv = el("span", "stat-val");
      vv.textContent = v;
      li.append(kk, vv);
      host.appendChild(li);
    }
  },

  // ── 文案 ────────────────────────────────────────────────────

  /** 步骤副标题：只报已知事实（耗时为服务端实测，缺则不写）。 */
  _stepMeta(s) {
    if (s.status === "done") {
      return s.durationMs == null ? "完成" : `完成 · ${fmtDuration(s.durationMs)}`;
    }
    if (s.status === "error") {
      return s.durationMs == null ? "失败" : `失败 · ${fmtDuration(s.durationMs)}`;
    }
    if (s.status === "pending") return "未返回结果";
    // 历史未持久化失败标记：如实说"不知道"，不写"完成"
    if (s.status === "unknown") return "已返回 · 成败未记录";
    return "执行中";
  },

  _outcomeLabel(outcome) {
    return {
      approved: "已允许",
      denied: "已拒绝",
      timeout: "超时（按拒绝处理）",
      answered: "已回答",
      skipped: "已跳过（取保守默认）",
      rejected: "未通过",
      cancelled: "随回合取消",
      disconnected: "连接断开（按拒绝处理）",
    }[outcome] || outcome || "已处理";
  },

  _ctxStateLabel(c) {
    if (c.state === "error") return "失败";
    if (c.state === "done") return WRITE_TOOLS.has(c.tool) ? "已改" : "已读";
    return WRITE_TOOLS.has(c.tool) ? "写入中" : "读取中";
  },

  _planStatusLabel(status) {
    if (status === "completed") return "已完成";
    if (status === "in_progress") return "进行中";
    return "待开始";
  },

  _agentStatusLabel(status) {
    if (status === "done") return "已完成";
    if (status === "error") return "失败";
    return "运行中";
  },

  _roundsUsed() {
    const set = new Set();
    for (const s of this.steps) if (s.round) set.add(s.round);
    return set.size;
  },

  _progressText() {
    const t = this.turn;
    if (t.status === "idle") return this.archived ? "上一轮已结束" : "等待第一条消息";
    if (t.status === "done") return "已完成";
    if (t.status === "error") return "出错";
    if (t.status === "interrupted") return "已打断";
    if (t.status === "blocked") return "等待你处理";
    const bits = [];
    if (this.steps.length) bits.push(`第 ${this.steps.length} 步`);
    const rounds = this._roundsUsed();
    if (rounds) bits.push(`第 ${rounds} 轮`);
    return bits.length ? bits.join(" · ") : "思考中";
  },

  /** 副行只报事实：出错原因 / 收尾耗时 / 停滞时长——**不编 ETA**。 */
  _etaText() {
    const t = this.turn;
    if (t.status === "error") return t.error || "见对话详情";
    if (t.status === "done") {
      return `耗时 ${fmtDuration((t.endedAt || Date.now()) - t.startedAt)}`;
    }
    if (t.status === "interrupted") {
      return `已打断${t.kind === "client" ? "（客户端）" : ""}`
        + ` · 已跑 ${fmtDuration((t.endedAt || Date.now()) - t.startedAt)}`;
    }
    if (t.status === "blocked" && this.awaiting) {
      return `已等 ${fmtClock((Date.now() - this.awaiting.since) / 1000)}`;
    }
    if (this._active() && this._lastEventAt) {
      const idleSec = (Date.now() - this._lastEventAt) / 1000;
      // 只报"多久没新事件"这个**事实**；不推断它在干什么
      if (idleSec >= 15) return `${fmtClock(idleSec)} 无新事件`;
    }
    return "";
  },

  _elapsedText() {
    const t = this.turn;
    if (t.status === "idle" || !t.startedAt) return "";
    const end = t.endedAt || Date.now();
    return fmtDuration(end - t.startedAt);
  },

  _active() {
    return !["idle", "done", "error", "interrupted"].includes(this.turn.status);
  },

  /** 任何"进展"都刷新停滞计时（工具开/收、文本、闸门变化）。 */
  _touch() {
    this._lastEventAt = Date.now();
  },

  /** 状态迁移：等待人工期间是**闸门说了算**——文本/思考/收步都不得顺手改掉。 */
  _status(status) {
    if (this.awaiting) return;
    if (this._active()) this.turn.status = status;
  },

  _startTicker() {
    this._stopTicker();
    // 进行中让「已耗时 / 等待时长 / 停滞」走字；回合收尾即停（不空转）
    this._ticker = setInterval(() => {
      if (!this._active()) return this._stopTicker();
      this.renderCurrentTask();
    }, 250);
  },

  _stopTicker() {
    if (this._ticker) {
      clearInterval(this._ticker);
      this._ticker = null;
    }
  },
};

// 回合状态 →（状态点色 / 文案）。**不含进度百分比**：总步数未知时
// 任何百分比都是编的（见文件头纪律 1）。
const STATUS = {
  idle:        { dot: "pending", label: "空闲" },
  thinking:    { dot: "running", label: "思考中" },
  running:     { dot: "running", label: "执行工具" },
  answering:   { dot: "running", label: "生成回复" },
  blocked:     { dot: "waiting", label: "等待你处理" },
  done:        { dot: "done",    label: "已完成" },
  error:       { dot: "error",   label: "出错" },
  interrupted: { dot: "error",   label: "已打断" },
};

// 写类工具（与后端 api.WRITE_TOOLS 同义）：上下文里区分「已读 / 已改」
const WRITE_TOOLS = new Set(["write_file", "edit_file", "write_plugin"]);

function firstLine(text) {
  const s = String(text || "").trim();
  if (!s) return "";
  const line = s.split("\n")[0].trim();
  return line.length > 80 ? line.slice(0, 79) + "…" : line;
}

/** 输出的第一行"有内容"的行（跳过空行 / 纯分隔线），用作失败摘要。 */
function firstMeaningfulLine(text) {
  const lines = String(text || "").split("\n");
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) continue;
    return line.length > 120 ? line.slice(0, 119) + "…" : line;
  }
  return "";
}

// fmtDuration 移驻 common.js（chat.js「思考了」也要用，common 最先加载）。
function fmtClock(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m${s % 60}s`;
  return `${Math.floor(m / 60)}h${m % 60}m`;
}
