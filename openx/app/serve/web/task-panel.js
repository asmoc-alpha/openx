"use strict";

/* OpenX Serve — 右栏任务面板。
   四标签：任务流 / 路径 / 上下文 / 产物（路径见 trace.js，REST 拉取）。

   **数据全部来自真实会话事件，没有 mock**：
   - 任务流：一次回合 = 一个任务。步骤 = 本轮**真实发生过**的工具调用
     （tool_use 开一步、tool_result 收一步）；状态与耗时来自
     user_message / result / interrupted。执行计划（``todos`` 快照）与
     子 agent（``fleet`` 快照）是**状态型下行**，单独渲染为两块清单。
   - 上下文：本会话**真实读/写过**的文件路径。路径由服务端从工具入参
     派生后随 tool_use 的 ``target`` 字段下发（读侧派生，绝不原样回传
     入参——write_file 的 content 可能含整个文件）。
   - 产物：沿用 Artifacts 模块（artifact 事件，同样派生自真实写工具）。

   两条诚实性纪律：
   1. **不猜百分比**。回合结束前总步数未知，编一个「60%」「预计还需 2
      分钟」就是撒谎。故进度条只在回合收尾时定格 100%，进行中走不确定
      态动画，文案只报已知事实（第几步 / 已耗时）。
   2. **没有数据就显示空态**。不拿示例数据填充，避免用户把假流程当成
      真实执行状态。

   XSS 纪律：工具名 / 路径 / 用户消息都是模型或产物文本，一律走
   textContent，绝不拼 innerHTML。 */

const TaskPanel = {
  // 本轮回合：title 取用户消息；status 见 STATUS 表
  turn: { title: "", status: "idle", startedAt: 0, endedAt: 0, error: "" },
  steps: [],     // 本轮步骤（回合开始清空）
  todos: [],     // 执行计划（todo_write 全量快照：跨回合保持，新会话清空）
  agents: [],    // 子 agent（task 委派快照：本轮委派，回合开始清空）
  context: [],   // 本会话文件引用（跨回合累积，最近使用的在前）
  usage: null,   // 最近一次 result 事件的累计 token 用量
  lastTurn: null,
  _ticker: null,

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
  },

  // ── 事件入口（app.js 的 reducer 调用）——────────────────────────
  // 只收真实 WS 事件；没有任何 mock 分支。

  onTurnStart(text) {
    this.steps = [];
    this.agents = [];   // 服务端回合级 fleet.reset：本轮委派从空起算
    this.turn = {
      title: firstLine(text) || "新会话",
      status: "thinking",
      startedAt: Date.now(),
      endedAt: 0,
      error: "",
    };
    this._startTicker();
    this.render();
  },

  onThinking() {
    if (this._active()) this.turn.status = "thinking";
    this.renderCurrentTask();
  },

  onToolUse(name, target) {
    if (!this._active()) return;
    const step = {
      num: this.steps.length + 1,
      name: name || "tool",
      target: target || "",
      status: "running",
      startedAt: Date.now(),
      durationMs: 0,
      isError: false,
      ctx: null,
    };
    // 步骤与上下文条目是同一件事的两面：步骤收尾时顺带把上下文标成已读/已改
    if (target) step.ctx = this._touchContext(target, name || "tool");
    this.steps.push(step);
    this.turn.status = "running";
    this.render();
  },

  onToolResult(name, isError) {
    // 收最近一个同名且仍在跑的步骤（并行调用可能重名，后进先出最贴近真实配对）
    for (let i = this.steps.length - 1; i >= 0; i--) {
      const s = this.steps[i];
      if (s.status !== "running" || s.name !== name) continue;
      s.status = isError ? "error" : "done";
      s.isError = Boolean(isError);
      s.durationMs = Date.now() - s.startedAt;
      if (s.ctx) s.ctx.state = isError ? "error" : "done";
      break;
    }
    if (this._active() && !this.steps.some((s) => s.status === "running")) {
      this.turn.status = "answering";
    }
    this.render();
  },

  onTextDelta() {
    // 文本到达时若还有工具在跑，状态仍归「执行工具」
    if (this._active() && !this.steps.some((s) => s.status === "running")) {
      this.turn.status = "answering";
    }
    this.renderCurrentTask();
  },

  onResult(ev) {
    if (!this._active()) return;
    const e = ev || {};
    this.turn.status = e.is_error ? "error" : "done";
    this.turn.endedAt = Date.now();
    this.turn.error = e.error || "";
    if (e.usage) this.usage = e.usage;
    this.lastTurn = {
      toolCalls: this.steps.length,
      durationMs: e.duration_ms || 0,
      rounds: e.num_turns || 0,
    };
    this._stopTicker();
    this.render();
  },

  onInterrupted() {
    if (!this._active()) return;
    this.turn.status = "interrupted";
    this.turn.endedAt = Date.now();
    for (const s of this.steps) {
      if (s.status === "running") s.status = "error";
    }
    this._stopTicker();
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
    this.todos = [];
    this.agents = [];
    this.context = [];
    this.turn = { title: "", status: "idle", startedAt: 0, endedAt: 0, error: "" };
    this.usage = null;
    this.lastTurn = null;
    this.render();
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
    if (title) title.textContent = t.title || "尚未开始对话";

    const step = $("current-task-step");
    if (step) step.textContent = this._elapsedText();

    const fill = $("progress-fill");
    if (fill) {
      const settled = t.status === "done" || t.status === "error" || t.status === "interrupted";
      fill.style.width = settled ? "100%" : meta.bar || "0%";
      fill.classList.toggle("indeterminate", !settled && t.status !== "idle");
    }
    const pct = $("progress-percent");
    if (pct) pct.textContent = this._progressText();
    const eta = $("progress-eta");
    if (eta) eta.textContent = this._etaText();
  },

  renderSteps() {
    const host = $("workflow-steps");
    const empty = $("steps-empty");
    const meta = $("steps-meta");
    const count = $("flow-tab-count");
    if (!host) return;

    host.innerHTML = "";
    const done = this.steps.filter((s) => s.status === "done").length;
    if (meta) meta.textContent = `${this.steps.length} 步` + (done ? ` · 完成 ${done}` : "");
    if (count) {
      count.textContent = String(this.steps.length);
      count.hidden = this.steps.length === 0;
    }
    if (empty) empty.hidden = this.steps.length > 0;

    for (const s of this.steps) {
      const li = el("li", "step " + s.status);
      const num = el("span", "step-num");
      num.textContent = s.num;
      const content = el("div", "step-content");
      const name = el("div", "step-name");
      name.textContent = s.name;                    // 工具名：textContent
      const line = el("div", "step-meta");
      line.textContent = this._stepMeta(s);
      content.append(name, line);
      li.append(num, content);
      host.appendChild(li);
    }
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
    }
    if (this.lastTurn) {
      rows.push([
        "上轮",
        `${this.lastTurn.toolCalls} 次工具 · ${fmtDuration(this.lastTurn.durationMs)}`,
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

  _stepMeta(s) {
    const parts = [];
    if (s.target) parts.push(s.target);
    if (s.status === "done") parts.push(fmtDuration(s.durationMs));
    else if (s.status === "error") parts.push("失败");
    else parts.push("执行中");
    return parts.join(" · ");
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

  _progressText() {
    const t = this.turn;
    if (t.status === "idle") return "等待第一条消息";
    if (t.status === "done") return "已完成";
    if (t.status === "error") return "出错";
    if (t.status === "interrupted") return "已打断";
    return this.steps.length ? `第 ${this.steps.length} 步` : "思考中";
  },

  _etaText() {
    const t = this.turn;
    if (t.status === "error") return t.error || "见对话详情";
    if (t.status === "done" || t.status === "interrupted") {
      return `耗时 ${fmtDuration((t.endedAt || Date.now()) - t.startedAt)}`;
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

  _startTicker() {
    this._stopTicker();
    // 进行中让「已耗时」走字；回合收尾即停（不空转）
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

// 回合状态 →（状态点色 / 文案 / 进行中进度条宽度）
const STATUS = {
  idle:        { dot: "pending", label: "空闲",     bar: "0%" },
  thinking:    { dot: "running", label: "思考中",   bar: "35%" },
  running:     { dot: "running", label: "执行工具", bar: "60%" },
  answering:   { dot: "running", label: "生成回复", bar: "85%" },
  done:        { dot: "done",    label: "已完成",   bar: "100%" },
  error:       { dot: "error",   label: "出错",     bar: "100%" },
  interrupted: { dot: "error",   label: "已打断",   bar: "100%" },
};

// 写类工具（与后端 api.WRITE_TOOLS 同义）：上下文里区分「已读 / 已改」
const WRITE_TOOLS = new Set(["write_file", "edit_file", "write_plugin"]);

function firstLine(text) {
  const s = String(text || "").trim();
  if (!s) return "";
  const line = s.split("\n")[0].trim();
  return line.length > 80 ? line.slice(0, 79) + "…" : line;
}

function fmtDuration(ms) {
  const n = Math.max(0, Number(ms) || 0);
  if (n < 1000) return `${n}ms`;
  const s = n / 1000;
  return s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m${Math.round(s % 60)}s`;
}

function fmtClock(sec) {
  const s = Math.max(0, Number(sec) || 0);
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m${s % 60}s`;
}
