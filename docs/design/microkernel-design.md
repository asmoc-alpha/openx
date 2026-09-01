# 微内核 Agent 架构设计 · 对齐 2026-08 架构图

> 状态：本文对齐**《微内核Agent架构》**（2026-08，架构图）——内核五件套 +
> 模型驱动的动态装配 + 插件自描述/故障隔离 + 插件分类与接入协议。前身为
> "四职责"责任模型（2026-08-24 定稿，映射见 §0.2），沿用其已落地的实现
> 基线（§5）。
>
> 上位文档：`openx-architecture-design.md`（v4.1）、`openx-kernel-design.md`
> （详设，机制章节在新架构下对齐本文件）。实施中机制变更须先改本文再改代码。
>
> 一句话：**内核 = 推理 + 装配 + 安全 + 轨迹 + 沙箱，五者构成信任基座；
> 插件 = 一切能力（包括记忆与规划）；模型经元工具在运行时组装自己**——
> Agent 从"出厂固定的产品"变成"按需自组装的系统"。

---

## 0. 内核五件套（信任基座，不可卸载）

### 0.1 五件套定义

| 内核模块 | 定义 | 不能插件化的原因 |
|---|---|---|
| ① 推理核心 | 模型调用抽象（多 Provider）；路由 / fallback / 重试 / 限流；流式 / 结构化输出约束 | 没有它模型根本不会思考，"由模型决定装配"这句话本身就不成立 |
| ② 插件装配器 | Manifest 解析（能力 / schema / 权限 / token 成本）；生命周期 `discover → load → activate → deactivate → unload`；依赖解析 / 热插拔 / 插件目录索引 | 自举问题——装配器如果是插件，谁来装配它 |
| ③ 安全审计 | 插件调用 + 装配请求的权限闸门；Hook 链（Pre / Post）；审计日志 / injection 防护 | 安全闸门若可被卸载，模型（或被注入的 prompt）就能 `unload_plugin("security")` 绕过一切管控——安全必须不可卸载 |
| ④ 轨迹跟踪 | 全量记录：推理 / 插件调用 / 装配事件 / 成本；Trace 回放 / 导出（eval 数据源） | 审计追踪若可卸载，恶意行为可以"先卸载记录仪再作案"；挂事件总线上天然全量记录，放插件层反而做不到 |
| ⑤ 沙箱执行器 | 进程 / 文件 / 网络隔离；资源限额 / 结果回传 | 插件是"能力"，沙箱是"能力的执行环境"——环境本身不能被环境里的东西替换 |

### 0.2 与四职责（2026-08-24 定稿）的映射

| 四职责 | 五件套 | 变化 |
|---|---|---|
| ① 编排（装配成可执行单元） | ② 插件装配器（装配部分） | 装配显式化为内核模块 |
| ② 沙箱执行 | ③ 安全审计（动态闸门）+ ⑤ 沙箱执行器（静态边界） | 闸门与边界拆成两个模块，职责同构 |
| ③ 插件维护（单一门） | ② 插件装配器（注册门部分） | 并入装配器 |
| ④ 记账（唯一事件出口） | ④ 轨迹跟踪 | **记账升级为轨迹**：在事件账本之上加成本、Trace 回放、eval 导出——"发生过什么"与"为什么这么设计"（离线 eval、装配策略优化）共用同一数据源 |
| （无） | ① 推理核心 | **新增**：模型接入/推理管线入核（重试/限流/路由已在 `kernel/reasoning/retry.py`、provider 形状在 `kernel/reasoning/provider.py`，装配口在 `services/assembly.py`） |

### 0.3 与现状实现的关系：boot 组合 → 运行时装配

现状（已实现）：插件在 **boot 时静态组合**（`ensure_loaded` 装载 base bundle +
用户/项目插件，一次性进注册表）。五件套架构的核心变化是**装配权交给模型**：
插件不再"装了就在"，而是模型按任务经元工具**运行时装配/卸载**（§1）。
boot 组合退化为"出厂默认组合"，运行时装配在其之上增量。

### 0.4 package 与架构模块映射（P-D 落地，2026-08-31）

五件套 → package、协议 → 装配层 → 消费点的实现映射（对齐架构图 ①/④）：

| 架构模块 | package / 文件 | 状态 |
|---|---|---|
| ① 推理核心 | `kernel/reasoning/`（provider / retry） | 路由 / fallback / 限流随 N2 |
| ② 插件装配器 | `kernel/assembly/`（loader / registry / registrations / context / validate / manifest / protocols / plugin_spec） | `protocols.py` 是类别 → 协议 → 装配层路由的唯一真源 |
| ③ 安全审计 | `kernel/audit/guard.py` + 元工具 ASK 闸 | 装配请求闸门 = load/unload/write/promote 的 ASK 弹窗 |
| ④ 轨迹跟踪 | `kernel/ledger.py`（emit / attach_ledger 委托） | 成本字段 / eval 导出随 P-E |
| ⑤ 沙箱执行器 | `kernel/sandbox/`（host / protect） | protect = 调用防护；进程隔离随 D9 |
| 装配层（各协议 Registry） | `registrations.py` 目录：`tools` / `commands` / `contexts` / `lifecycle` / `providers` | P-D 新增 `contexts` / `lifecycle` |
| 上下文组装管线（context/v1） | `services/assembly.py::collect_context_fragments` + `agent._build_system_prompt` | pre-inference 征集（注册序 + 字符预算 + 崩溃隔离） |
| 会话生命周期（lifecycle/v1） | `kernel.trigger_lifecycle` + `agent.startup`（session_start） | checkpoint / resume 接线随 P-E |
| UI 面板管线（ui/v1） | `services/assembly.py::UiPanelCollector` + `streaming.py::_plugin_deck_renderable`（CLI）/ `app/serve/session.py` ticker（web） | deck 每帧征集（崩溃跳过/熔断/行数限额/节流）；web 经 `panels` 协议事件广播（变化才发） |
| 元工具（模型驱动装配） | `tools/plugin_tools.py` + `tools/write_plugin_tools.py` | 结构性工具恒先占位，子代理不继承 |

---

## 1. 模型驱动的动态装配（核心机制）

内核把插件管理本身暴露为**元工具**——永远常驻上下文、体积极小：

```
list_plugins(filter)  — 查询插件目录（只返回名称 + 一句话描述 + token 成本，不返回 schema）
load_plugin(name)     — 装配：注入该插件的工具 schema
unload_plugin(name)   — 卸载：从上下文移除，释放预算
plugin_help(name)     — 查看某插件详细用法（按需展开）
```

### 1.1 一次任务的装配流

```
任务进入
→ 模型看插件目录（轻量索引，~几百 token）
→ "这个任务要查库 + 画图" → load_plugin("dataquery"), load_plugin("dataviz")
→ 内核安全审计：权限够吗？是危险插件吗？ → 放行 / 拒绝 / 问人
→ 插件 schema 注入上下文 → 模型正常调用
→ 任务阶段切换 → unload_plugin("dataquery") 释放上下文预算
→ 全程 Tracer 记录：谁在什么时候装了什么、调了什么、花了多少
```

### 1.2 需要提前想清楚的坑

| 问题 | 缓解思路 |
|---|---|
| 装配决策可靠性 | 模型可能漏装（不知道查目录）或滥装（全装上、上下文爆炸）。按任务类型做粗粒度预推荐；内核设装配数量 / 总 token 上限 |
| 卸载的有状态性 | Memory、会话持久化这类插件有状态，unload 不是简单删 schema——要定义 `deactivate` 时的状态落盘契约。**已兑现**（P-D，2026-08-31）：unload_plugin 先回调 `on_unload` 落盘再清注册 |
| 插件间依赖 | Sub-Agent 编排插件可能依赖沙箱和 Tracer——内核能力暴露成稳定 SPI，插件面向接口编程，不互相直接调用 |
| 安全分级 | 插件分可信级（内置签名）/ 第三方 / 用户自定义，不同级别走不同审批强度 |
| Tracer 与 eval 闭环 | 轨迹统一记录后，离线 eval、bad case 回放、装配策略优化（"哪类任务该预装哪些插件"本身可以学出来）都有了统一数据源——本架构的隐性红利 |

---

## 2. 插件自描述：Manifest 与双视角

### 2.1 两个视角分离

- **内核视角：挂载点（Extension Points）**——内核预定义固定挂点，插件声明
  自己挂在哪，内核在 Loop 各阶段自动调用，**模型不感知**：

```
ingress ──▶ pre-inference ──▶ planning ──▶ tool-call ──▶ post-inference
(入口适配)   (上下文组装: Memory/RAG/Prompt) (规划策略)  (能力工具)   (输出后处理)
                 │                              │
             compaction                      orchestration
             (压缩策略)                       (子Agent编排)

lifecycle: 调度 / 持久化（挂在会话生命周期上，而非 Loop 上）
```

- **模型视角：按类型分组的插件目录**——每个插件只暴露一句话语义，schema
  按需展开：

| 类型 | 语义 | 例 |
|---|---|---|
| `capability.tool` | 能力工具类 | dataquery(查数) · dataviz(画图) · file-ops(文件) |
| `context.memory` | 上下文类 | long-term-memory(长期记忆) · code-rag(代码检索) |
| `strategy.planning` | 策略类 | todo-planner · react-planner |
| `orchestration` | 编排类 | sub-agent-pool · agent-team |
| `lifecycle` | 生命周期类 | cron-scheduler · checkpoint |

### 2.2 插件 Manifest（自描述）

```json
{
  "type": "capability.tool",          // 类型: 模型在目录里按它分组浏览
  "mount": "loop.tool-call",          // 挂载点: 只给内核用,决定何时调用,模型不感知
  "trust": "user",                    // 信任级: builtin / third-party / user
  "summary": "一句话描述,进目录索引",   // 模型的第一认知入口
  "schema": { },                      // 按需展开,不进索引
  "permissions": ["network", "fs:read"],  // 安全审计据此审批
  "cost": { "schemaTokens": 800 },    // 装配预算控制
  "isolation": "process",             // 隔离级别(user 级强制 process,不可降级)
  "timeout": "30s",
  "dependencies": ["kernel.spi.tracer"]
}
```

---

## 3. 故障隔离：三层防护

核心原则：**对主 Loop 而言，插件异常和插件正常返回"没查到"是同构的**——
都只是一次观察结果（observation）。Loop 的收敛性永远不依赖任何单个插件。

### 3.1 执行隔离（崩溃不传染）

| 信任级 | 运行方式 | 崩溃影响面 |
|---|---|---|
| builtin（签名内置） | 可 in-proc，换性能 | 内核可控，默认可信 |
| third-party | 独立进程 | 进程死掉，内核收信号，主 Loop 无感 |
| user（用户自定义） | 独立进程 + 沙箱（**强制，不可降级**） | 崩溃、死循环、内存泄漏都被关在自己的笼子里 |

### 3.2 调用防护（异常可收敛）

每次插件调用都经过内核的**调用包装器**：

| 机制 | 语义 |
|---|---|
| timeout | 超时即杀；返回结构化超时错误 |
| 熔断器 | 连续 N 次失败 → 自动 `deactivate`，从上下文摘掉 schema，防止模型反复调用坏插件 |
| 资源限额 | CPU / 内存硬顶；输出大小上限 |
| 输出校验 | 返回值过 schema 校验；非法输出视为异常 |

### 3.3 错误语义化（异常变成模型的决策输入）

插件抛异常 ≠ Loop 抛异常。结构化错误让模型自行决策：

```json
{
  "tool": "dataquery",
  "status": "plugin_error",
  "error": "Plugin crashed (exit 137, OOM). Circuit breaker: 2/3 failures.",
  "suggestion": "retry | unload | use alternative: [arkai-dataquery]"
}
```

模型拿到后自行决策：重试 / 换插件 / 降级到内置能力 / 如实告知用户——
主流量继续走，只是少了一个能力。同时 Tracer 记录、审计可查，熔断触发时
通知用户"插件 X 不稳定，已自动卸载"。

---

## 4. 插件分类与接入协议

从"插件自己声明挂哪"升级为**类别 → 接入协议 → 装配层**三级映射——与操作系统
驱动模型同构：字符设备、块设备、网络设备各有驱动接口，内核按设备类型走不同
的注册路径。**插件面向协议编程，而不是面向内核实现编程。**

### 4.1 类别 ↔ 协议 ↔ 装配层映射

```
                      内核装配器 (按 manifest.type 路由)
                                  │
 ┌──────────┬──────────┬─────────┼─────────┬──────────┬──────────┐
 ▼          ▼          ▼         ▼         ▼          ▼          ▼
Tool     Context  Planner  Orchestr. Ingress  Lifecycle EventListener
Protocol Protocol Protocol Protocol Protocol Protocol  Protocol
 │          │          │         │         │          │          │
 ▼          ▼          ▼         ▼         ▼          ▼          ▼
能力层     上下文     Loop      编排层    入口层    会话生命   事件总线
Tool      组装管线   规划槽位   SubAgent  外部输入  周期钩子   (只读订阅)
Registry  Registry  (单例)    Registry  Registry  Registry
 └──────────┴──────────┴─── Loop 各阶段消费 ──┴──────────┴─────────┘
```

### 4.2 每类协议的接口契约（SPI）

协议即契约——插件只要实现对应协议的接口，内核就知道怎么用它：

| 协议 | 核心接口 | 装配到哪 | Loop 如何消费 |
|---|---|---|---|
| ToolProtocol | `invoke(input) → output` | 能力层 Tool Registry | schema 注入上下文，模型发起调用，内核路由到插件 |
| ContextProtocol | `contribute(budget) → fragments[]` | 上下文组装管线 | pre-inference 阶段，内核按优先级 + 预算向各 provider 征集上下文片段（Memory / RAG / Prompt 都走这个） |
| PlannerProtocol | `plan(goal, state) → tasks` | Loop 规划槽位（单例） | 规划阶段调用；同时只激活一个，多装需仲裁或替换 |
| OrchestratorProtocol | `spawn(spec) → handle`、`send / receive` | 编排层 | 模型调用编排工具时，由它管理子 Agent 生命周期 |
| IngressProtocol | `start() → 事件流` | 入口层 | 不挂在 Loop 上，把外部输入（CLI / Webhook / IM）转成内核事件 |
| LifecycleProtocol | `onSessionStart / onCheckpoint / onResume` | 会话生命周期钩子 | 会话状态迁移时按序回调（调度、持久化插件走这个） |
| EventListenerProtocol | `subscribe(types) + onEvent(e)` | 事件总线 | 只读订阅，不能阻断主流程（监控、自定义埋点用） |

### 4.3 装配时的内核路由流程

```
load_plugin("dataquery")
│
├─ 1. 读 manifest.type → "capability.tool"
├─ 2. 路由到 ToolProtocolHandler
├─ 3. 校验: 插件是否实现了 ToolProtocol 接口?
│        protocol version 与内核兼容?
│        permissions 是否过安全审计?
├─ 4. 注册到能力层 Tool Registry（不是注册到"内核"这个笼统的地方）
├─ 5. 副作用: schema 注入上下文, token 预算扣减
└─ 6. Tracer 记录装配事件
```

### 4.4 这个抽象带来的性质

| 性质 | 说明 |
|---|---|
| 内核可扩展但不改核心 | 新增一个插件类别 = 定义一个新协议 + 一个新挂点 + 一个 Registry，内核五件套一行不动 |
| 协议版本化 | manifest 里声明 `protocol: "tool/v1"`，内核同时支持 v1 / v2 即可平滑升级，插件生态不因内核升级而断裂 |
| 层与层解耦 | 装配器只是"路由器"，每层自己维护 Registry 和消费时机。上下文管线不知道 Memory 插件是进程还是线程，它只认 `contribute()` 的返回 |

> **特例：单例协议 vs 多例协议。** 工具类插件天然"多装多得"；规划器、
> compaction 策略这类是"同一时刻只能有一个生效"——协议设计里要区分
> **多例协议**（注册即累加）和**单例协议**（装配即替换，或需显式仲裁）。

### 4.5 落地状态（P-D，2026-08-31）

三协议先行落地（决断 N4），其余占位：

| 协议 | 状态 | 落点 |
|---|---|---|
| `tool/v1`（capability.tool） | 已落地 | `tools` 注册表（P1 起就有）；多例 |
| `context/v1`（context.memory） | 已落地 | `contexts` 注册表；`ctx.register_context`，消费 = `collect_context_fragments`（注册序 + 字符预算 + 单插件崩溃隔离），片段并入系统提示（pre-inference） |
| `lifecycle/v1`（lifecycle） | 已落地 | `lifecycle` 注册表；`ctx.register_lifecycle`（on_session_start / on_checkpoint / on_resume / on_unload），消费 = `kernel.trigger_lifecycle`（故障隔离 + `plugin_error` 记账） |
| `ui/v1`（ui.panel，2026-09-01 增补） | 已落地 | `ui_slots` 注册表；`ctx.register_ui_slot(name, render, refresh_hz)`，`render() -> deck 行`（str/list，Rich markup）；消费 = `UiPanelCollector`（崩溃跳过 + 连续 3 次失败熔断 unregister + 单面板 8 行限额 + 节流），双客户端消费：CLI deck（输入框之下）每帧征集；web 经 `panels` 协议事件（`serve_panels`）由 ServeSession 常驻 ticker ~4Hz 广播（变化才发，行剥 rich 标签，端哑渲染纯文本）。**渲染路径强制故障隔离：渲染帧绝不能被插件拖死**。同一插件两个客户端零改动生效（协议即契约，客户端只是消费方） |
| Planner / Orchestrator / Ingress / EventListener | 占位 | `cardinality`（multi / singleton）结构已进 ProtocolSpec；单例协议"装配即替换"的仲裁待落地 |

路由规则：`manifest.type` → `protocols.PROTOCOLS` 目录；未知 / 缺失 type 默认路由
`tool/v1`（向后兼容 P-D 之前的插件；未知 type 的 warning 由 P-B 照记）。协议
一致性：声明 type 与实际注册面不符 → `manifest_warnings`（不拒载，沿用 P-B
容忍哲学）；write_plugin 生成侧强校验拒绝（注册面 AST 契约检查）。mount 由
协议表派生，模型与生成工具都不手填。

---

## 5. 现状基线（已实现）与迁移

### 5.1 四职责实现基线（2026-08-24 起陆续落地）

| 部件 | 文件 | 状态 |
|---|---|---|
| 内核本体 | `openx/kernel/__init__.py` | 注册目录驱动；base bundle 恒首挂载 |
| 注册表 | `kernel/assembly/registry.py`（PluginRegistry） | Entry 带 provenance（含 seq） |
| 加载器 | `kernel/assembly/loader.py` | 发现/解析/apply 完整；无依赖拓扑、无运行时装配 |
| 校验 | `kernel/assembly/validate.py` | tools / commands 形状校验 |
| 执行闸 | `kernel/audit/guard.py` | 七站裁决管线（K3，2026-08-28） |
| 装配策略 | `services/assembly.py` | 工具实例化仲裁 + provider 解析（K3a） |
| 协议 | `openx/core/protocol.py` | P1 事件信封（seq/ts/cause/origin/digest）+ serve 扩展 |
| 重试/形状 | `kernel/reasoning/retry.py` / `kernel/reasoning/provider.py` | 推理核心的先声：重试归内核、形状进内核（M1） |
| 记账 | `kernel.emit` / `sessions/*.jsonl` | 事件账本（K2）；**轨迹跟踪的底座** |
| 元工具 | `kernel`（list/load/unload/help）+ `tools/plugin_tools.py` | 模型驱动装配（P-A，2026-08-29）：会话级动态装载、轻量自描述、unregister |
| Manifest | `kernel/assembly/manifest.py` + `PluginInfo.manifest` | 插件自描述（P-B，2026-08-29）：schema 校验、目录暴露 type/mount/trust |
| 调用防护 | `kernel/sandbox/protect.py` + `assembly` | 故障隔离（P-C，2026-08-29）：插件工具包 timeout/输出上限/熔断/结构化错误 |
| 自产插件 | `tools/write_plugin_tools.py` + `kernel/assembly/plugin_spec.py` | 模型自产（P-F，2026-08-29）：write/test/promote 元工具 + admit 管线 + 决策记账 |
| 协议分类 | `kernel/assembly/protocols.py` + `registrations.py`（contexts/lifecycle） | 类别→协议→装配层路由（P-D，2026-08-31）：三协议目录 + 注册面扩展 |
| 上下文/生命周期消费 | `services/assembly.py::collect_context_fragments` + `agent` 接线 | P-D 消费面：片段并入系统提示、session_start / unload 钩子触发 |
| UI 面板协议 | `protocols.py`（ui/v1）+ `UiPanelCollector` + `streaming` deck 接线 | ui.panel 类插件（ui/v1，2026-09-01）：状态层面板 + 渲染故障隔离/熔断 |

### 5.2 新架构对现状的改动面

| 现状 | 新架构（五件套） | 改动性质 |
|---|---|---|
| boot 静态组合（`ensure_loaded`） | 模型驱动运行时装配（元工具） | **核心机制变化**：装配权从 boot 交给模型；boot 组合退化为出厂默认组合 |
| 装配器内嵌于 kernel `__init__.py` | ② 插件装配器显式模块：Manifest 解析、生命周期、依赖解析、目录索引 | 结构析出 |
| `guard.py` 裁决管线 | ③ 安全审计：装配请求 + 插件调用的闸门、Hook 链、审计日志、injection 防护 | 扩展：闸门从工具调用延伸到装配请求 |
| 事件账本（K2） | ④ 轨迹跟踪：推理/插件调用/装配事件/成本全量 + Trace 回放 + eval 导出 | 记账升级为轨迹 |
| 工具运行期 L1（同进程） | 执行隔离按 trust 分级（builtin in-proc / third-party 进程 / user 沙箱强制） | 与 D9 进程隔离决断对接 |
| 插件无自描述 | Manifest（type/mount/trust/summary/permissions/cost/isolation/timeout/dependencies） | **新增** |
| 插件无协议分类 | 7 类协议 SPI + 类别→协议→装配层三级映射 | **已落地**（P-D，2026-08-31 + ui/v1 增补 2026-09-01）：tool/context/lifecycle/ui 四类，其余占位（§4.5） |

### 5.3 决断点（更新）

| # | 问题 | 倾向 |
|---|---|---|
| N1 | 模型驱动装配的落地形态：元工具先以"会话内注册/卸载"实现（K6 晋升门的运行时面），还是等 boot 组合稳定后并行 | 先做会话内热插（复用五阶段校验），boot 组合退化为默认组合；元工具是薄壳包注册 API |
| N2 | 推理核心的边界：`kernel/reasoning/provider.py` + `kernel/reasoning/retry.py` 已落地，路由/fallback/限流何时入核 | 随多 provider 多实例成熟度；现状 `resolve_provider` 装配策略在消费方（K3a 已迁） |
| N3 | 轨迹跟踪与记账的关系：事件账本（K2）作为轨迹底座，成本与 eval 导出是增量——Tracer 是否独立模块 | 记账接口不动，Tracer 以消费者身份订阅事件流 + 补成本字段（协议扩展） |
| N4 | 插件协议分类是否一步到位：7 类全建，还是先 Tool/Context/Lifecycle 三类 | 先 Tool（现状工具注册表）/ Context（instructions/memory/skills 收口）/ Lifecycle（调度/持久化），其余占位。**已落地**（2026-08-31，§4.5）；Context 现为插件片段并入系统提示，instructions/memory/skills 整体收口列后续 |
| N5 | 执行隔离的推进节奏 | 延续 D9 分层定价：用户插件加载期先上进程隔离，工具运行期维持 L1 |
| N6 | 生成插件的自测是否强制（不自测的提交直接拒绝？） | **强制**：`self_test` 是沙箱放行的前提（§6.4 ③），宁缺勿滥 |
| N7 | 声明式插件是否进 P-F | 占位不做（代码插件优先），避免分心 |
| N8 | 生成插件的信任级归属：`auto` 独立一档还是并入 `user` | 独立 `auto` 档（可批量回滚、可整体禁用），不并入 user |

### 5.4 前向落地切片

1. ~~**P-A 模型驱动装配的元工具面**~~ **已完成**（2026-08-29）：
   `kernel` 的 list/load/unload/help 管理 API + `tools/plugin_tools.py` 四个元工具
   （list/help 只读、load/unload ASK）+ 会话级动态装载（复用五阶段校验，同源
   同门）+ 轻量自描述（`__openx_meta__`）。装配请求的安全审计（只读/非只读
   分级）留 P-C 前后接线。
2. ~~**P-B Manifest**~~ **已完成**（2026-08-29）：`kernel/assembly/manifest.py` schema 校验
   （形状错拒载、未知 type/mount/permission 只警告）+ `PluginInfo.manifest` 全量
   + `list_plugins` 暴露 type/mount/trust + `plugin_help` 暴露 manifest 全量与
   校验警告。
3. ~~**P-C 故障隔离（调用防护）**~~ **已完成**（2026-08-29）：`kernel/sandbox/protect.py`
   `ProtectPluginTool`（timeout/输出上限/熔断/结构化错误，全量委托 Tool 表面），
   `assembly.instantiate_tools` 只包插件工具（timeout 取 manifest 声明），熔断触发
   `kernel.unregister_tool` 自动摘除。**执行隔离（进程沙箱，D9）明确不做**——本
   切片只做"异常可收敛 + 错误语义化"。
4. ~~**P-D 协议分类**~~ **已完成**（2026-08-31）：`kernel/assembly/protocols.py`
   协议目录（tool/v1 · context/v1 · lifecycle/v1；未知/缺失 type 默认路由
   tool/v1）+ 注册面（`ctx.register_context` / `ctx.register_lifecycle`，
   registrations 目录加 contexts/lifecycle 两行）+ 消费面
   （`collect_context_fragments` 并入系统提示、`trigger_lifecycle` 故障
   隔离回调、agent.startup 接 session_start）+ unload 的 **on_unload 状态
   落盘契约**（§1.2 卸载有状态性的兑现）+ 协议一致性 warning + write_plugin
   按 type 生成三协议插件（PLUGIN_SPEC v2 常驻 + 注册面 AST 契约检查，类型
   错配即拒）。单例协议（planner/compaction）与 checkpoint/resume 接线列后续。
5. **P-E 轨迹升级**：事件账本补成本字段，Tracer 订阅 + eval 导出。
6. ~~**P-F 模型自产插件**~~ **已完成**（2026-08-29）：`kernel/assembly/plugin_spec.py`
   PluginSpec（常驻系统提示）+ `tools/write_plugin_tools.py` 三元工具（write ASK
   / test ALLOW / promote ASK）+ admit 管线（manifest 校验 → 语法/契约存在性 →
   **进程内 self_test** → 落盘 → load_plugin → 重建）+ `plugin_promoted` 决策事件。
   自测在进程内跑（D9 进程隔离为后续加固，write 的 ASK 闸是当前信任锚点）；
   promote 的 boot 持久化（进组合/overlay）列后续。

> **搁置决定（2026-08-24，延续）**：沙箱执行（K1c/K3/K4）与记账深化整体暂缓，
> 已落地 K2a/K2b 行为中性保留不回退；当前活跃方向为插件维护补全（卸载/
> unregister）与编排（ExecutionContext）。五件套架构在此基础上演进。

---

## 6. 模型自产插件（Self-Extension）：让 Agent 自己开发插件

> 前提：插件系统上线（P-A~P-E）后，agent 可经元工具自行开发插件——识别能力
> 缺口 → 读协议 → 生成 → 沙箱自测 → 会话热插 → 用户晋升 → 记账。这是架构的
> **自举引擎**：agent 补自己的能力短板；Trace-eval 闭环让"哪类任务该预装哪些
> 插件"也能学出来。
>
> 衔接：生成侧落地是架构图"候选池 → 模型配对 → 晋升门 → 激活 → 记账"
> （`openx-architecture-design.md` §9）的运行时实现；模型自产插件 id 恒带
> `auto-*` 前缀（不占用用户命名域，可一键批量回滚）。

### 6.1 三条核心原则

| 原则 | 含义 |
|---|---|
| 协议是给模型的契约 | agent 只读一份 PluginSpec 就知道怎么写插件，不逆向内核实现 |
| 沙箱是考场 | 信任锚点 = "沙箱里自测跑绿"，不是"模型说它能用" |
| 用户是审批 | 写类/常驻插件的晋升必须用户确认；只读可热插（K6 分级） |

**同源同门**：生成插件走同一五阶段校验，**无第二条加载路径**——生成的文件落进
插件目录后就是普通插件，可被 list / unload / 回滚一视同仁。

### 6.2 PluginSpec：模型可读的协议自描述

"快速生成"的前提是**只读一份东西就够**。PluginSpec 是插件协议的唯一真源，
版本化（`protocol: "plugin/v1"`），作为 prompt_fragment 常驻（或经
`plugin_help("kernel.plugin-spec")` 按需展开）：

```
PluginSpec（plugin/v1）
├── manifest 字段定义   type / trust / permissions / cost / dependencies / test
│                       （mount 由协议表派生，不手填）
├── 四协议代码契约      按 type 各取其一：tool = apply + factory(host)；
│                       context = apply + contribute()；lifecycle = apply +
│                       register_lifecycle；ui.panel = apply + register_ui_slot
│                       （render() -> deck 行；各协议最小示例见 plugin_spec.py）
├── 自测契约            self_test() 怎么写、跑在什么环境
├── 命名规则            auto-* 前缀（批量回滚的抓手）
└── 完整示例            最小工具 / 只读工具 / 有状态工具（Memory 类）
```

最小单文件形态（manifest 头 + 代码 + 自测，提交单元最小）：

```python
# ===== manifest（JSON 头，机器可读；亦可 sidecar）=====
# {"type": "capability.tool", "mount": "loop.tool-call", "trust": "auto",
#  "permissions": ["fs:read"], "cost": {"schemaTokens": 400}, "protocol": "plugin/v1"}
from openx.tools.base import Tool

class VizTool(Tool):
    name = "viz"
    description = "画调用关系图"
    async def execute(self, **kw):
        return ToolResult(output="...")

def factory(host):          # ToolHost 受限面：只读投影 + 共享状态句柄
    return [VizTool()]

def self_test():            # 沙箱内跑：绿了才放行
    assert factory(None)[0].name == "viz"
```

### 6.3 生成工具（模型侧 meta-tools）

| 工具 | 作用 |
|---|---|
| `write_plugin(manifest, code, test)` | **结构化输出**：schema 即契约，模型按 schema 填，形状上难出错（StructuredOutputTool 同款机制） |
| `test_plugin(name)` | 沙箱跑自测，返回 pass / fail + Trace |
| `promote_plugin(name)` | 用户确认后晋升 persistent（走 ② 插件装配器 + 账本决策事件） |
| `unload_plugin(name)` / `list_plugins` | 现状元工具；回滚即卸载 |

### 6.4 admit 验收管线（生成侧）

```
write_plugin 提交
  → ① 形状校验   manifest 字段齐全、permissions 在词汇表内、auto-* 前缀、代码可解析
  → ② 静态扫描   AST 粗查（声明权限 vs 实际调用、命名空间越界）
  → ③ 沙箱自测   沙箱跑 self_test → 绿了才放行        ← 信任锚点
             （2026-09-01 加固：模块代码+self_test 在 daemon 线程执行，
              join 超时（10s）即拒"死循环/挂死"，主进程不卡死）
  → ④ 会话热插   scope=session，当前会话立即可用
  → ⑤ 用户晋升   写类/常驻 → 用户确认 → persistent + 记账
  全程记账: plugin_created / validated / tested / promoted / rejected
```

### 6.5 安全边界

| 威胁 | 缓解 |
|---|---|
| 模型被 prompt injection 写出恶意插件 | 生成插件 `trust=auto` → **强制沙箱进程隔离**（同用户级，§3.1）；最坏 = 沙箱里的一个坏工具 |
| 插件拿到 agent 本体 | **ToolHost 拒绝面不变**：只拿只读投影，拿不到 loop/闸门/console |
| "模型说它安全"不可信 | 信任锚点是沙箱自测 + 用户晋升门，不是模型意图 |
| 批量回滚 | `auto-*` 命名空间一键整体回滚 |

### 6.6 闭环：自测 → Trace → eval → 更会生成

每次生成插件的自测结果、装配事件、成本全进 ④ 轨迹跟踪——Tracer-eval 闭环
（§1.2）："哪些自产插件真有用"有统一数据源；装配策略优化（哪类任务预装哪些
插件）本身可以学出来。**这个功能是让微内核架构从第一天就自我进化的引擎。**

### 6.7 取舍：代码插件 vs 声明式插件

| 形态 | 表达力 | 安全 | 定位 |
|---|---|---|---|
| 代码插件（Python 单文件） | 强——agent 本来就是编码 agent | 沙箱兜底 | **主路径** |
| 声明式插件（manifest + 配置） | 弱——只够拼装已有能力 | 天然最安全 | 快速原型入口 |

建议主路径走代码插件（agent 是编码 agent，写 Python 是强项）；声明式只做
轻量原型入口，不进 P-F 范围。

### 6.8 落地切片

**P-F 模型自产插件**（依赖 P-A 元工具面 / P-B Manifest / P-C 故障隔离沙箱 /
P-D 协议分类）：PluginSpec（§6.2）+ `write_plugin` / `test_plugin` /
`promote_plugin` 结构化输出元工具（§6.3）+ admit 生成侧管线（§6.4，复用五阶段
校验 + 沙箱自测 + 晋升门）。决断点见 §5.3 N6-N8。

---

*本文与 `openx-architecture-design.md` / `openx-kernel-design.md` 冲突时，
以本文（对齐 2026-08 架构图）为准并立即回写彼文。*
