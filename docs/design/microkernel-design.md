# 微内核设计定稿 · 编排 / 沙箱执行 / 插件维护 / 记账

> 状态：**责任模型已定稿**（2026-08-24）：内核维护四职责。其余决断点
> （D9-D11 等）仍开放，逐项定稿后回写 `openx-kernel-design.md` 的对应
> 机制章节。
>
> 上位文档：`openx-architecture-design.md`（v4.1）、`openx-kernel-design.md`
> （详设，机制章节在新责任模型下全部有效，对应关系见 §0.2）。

---

## 0. 责任模型（已定稿）

### 0.1 四职责定义

| 职责 | 定义 | 不做什么 |
|---|---|---|
| ① 编排 | 把执行所需的插件装配成**可执行单元**（组合输入 -> 应载清单 -> 注册表 -> 绑定） | 不实现 loop 本身（loop 是被装配进单元的零件） |
| ② 沙箱执行 | agent 的执行与编排所创建的一切，运行在内核构造的**安全环境**里：边界（fs/网络/资源）+ 闸门（裁决） | 不做 UI、不做策略内容（策略是插件贡献） |
| ③ 插件维护 | 一切外部插件**注册进内核才可被编排**：单一门（发现/校验/provenance/清单/晋升） | 不评判插件好坏（形状校验 + 用户裁决） |
| ④ 记账 | 一切执行与决策的**唯一事件出口**：append-only、可回放、可归因、决策留痕 | 不做展示（协议层是账本的外化）、不做存储工具（核外） |

### 0.2 与 v1 详设（装配/把关/记账）的映射

| v1 | 定稿 | 备注 |
|---|---|---|
| 装配 | ① 编排 + ③ 插件维护 | 编排是装配的延伸：不止装载注册，还要**绑定成可执行单元**（loop、scope、闸门、记录的组装）；插件维护是装配的入口面（单一门） |
| 把关 | ② 沙箱执行（闸门部分） | 裁决管线成为执行环境的**动态边界**，语义不变：工具调用必经闸、只紧不松、资源闸 |
| 记账 | ④ 记账 | **定稿（2026-08-24）：维持 v1 地位，显式为第四职责**。信封 schema 与 emit 出口是内核不变量；文件写入经 attach 注入的 sink（实现选择，不改变职责归属） |
| （无） | ② 沙箱（边界部分） | **新增**：执行环境的静态边界。对现状最大的补强方向（见 §4） |

一句话：v1 说"内核做什么"（装配、把关、记账），定稿说"内核提供什么"
（单元、环境、入口、证据）。机制同构，增量有二：**沙箱边界显式化**与
**记账职责显式化**。

---

## 1. 现状基线（2026-08-24）

| 部件 | 文件 | 状态 |
|---|---|---|
| 内核本体 | `openx/kernel/__init__.py` | 注册目录驱动；base bundle 恒首挂载 |
| 注册表 | `kernel/registry.py`（PluginRegistry） | Entry 带 provenance（含 seq）；无 unregister |
| 加载器 | `kernel/loader.py` | 发现/解析/apply 完整；无依赖拓扑、无作用域 |
| ctx | `kernel/context.py` | 给予面三个注册 API |
| 校验 | `kernel/validate.py` | tools / commands 形状校验 |
| 清单 | `kernel/inventory.py` | 只读投影 |
| base bundle | `openx/builtin/`（tools/providers） | 工厂注册，内置=致命、禁用表无效 |
| 协议 | `openx/core/protocol.py` | P1 下行构造器 + Event 信封（seq/ts/cause/origin/digest） |
| 权限桥 | `app/cli/single_shot.py` | NDJSON 双向，fail-closed |
| 权限裁决 | `permissions.py` + `services/tool_executor.py` | 在 executor 串行准备段，未入内核 |
| 资源上限 | `agent.py` loop | 轮次上限在 loop 里 |
| fs 边界 | 各 Tool 构造参数（`ws`、`allow_outside`） | **散落在工具层**，非内核供给 |

### 1.1 已知问题清单

| # | 问题 | 位置 |
|---|---|---|
| B1 | ~~贡献点注册表双处手写~~ **已修**：`kernel/registrations.py` 目录驱动 | `kernel/__init__.py` |
| B2 | ~~`_load_key` 加载完成前赋值~~ **已修**：全部处理完才提交键 | `kernel/__init__.py` `_reload` |
| B3 | ~~工厂产出重名工具静默覆盖~~ **已修**：实例化时先产出者赢 + 记警告 | `instantiate_tools` |
| B4 | ~~"内置优先"靠消费方回报~~ **已修**：注册序即优先级（结构性），`merge_tools`/工具侧 `note_conflict` 已删（命令侧保留：内置命令尚非插件） | `instantiate_tools` |
| B5 | ~~Entry 无 inserted_at_seq~~ **已修**：`Entry.seq` 回填 registered 事件序号（scope 字段仍缺，随作用域机制补） | `kernel/registry.py` |
| B6 | ~~注册/拒载无事件~~ **已修**：registered/rejected/plugin_loaded/plugin_failed/composition_resolved 上账本 | `kernel.emit` |
| B7 | ~~协议事件无信封~~ **已修**：Event + envelope 投影；转录事件的信封化（cause 链）随 K3 接线 | `core/protocol.py` |
| B8 | 消费方 API 仍以定制方法为主（`registry(kind)` 为统一通道雏形）；`service()` 收敛推迟到 K3 Guard 动工时一并做 | `kernel/__init__.py` |
| B9 | **插件 `apply(ctx)` 在主进程内执行任意 Python**--ctx 拒绝面靠不暴露引用，是约定不是隔离 | `kernel/loader.py:77-92` |
| B10 | fs/网络/命令边界由各工具自持（构造参数），内核不供给、不可审计 | `tools/*.py` 各处 |
| B11 | 子代理的权限收缩（`CHILD_EXCLUDED_TOOLS`）靠工具表静态排除，非 scope 派生 | `core/subagent.py` |

---

## 2. 插件维护（③ 单一门）

外部插件进系统的**唯一通道**：发现 -> 校验 -> 注册（带 provenance）->
清单 -> （K6 起）晋升门。未注册 = 不可编排 = 不可执行，不存在旁路。

### 2.1 插件注册目录（修 B1，已实施）

```python
# kernel/registrations.py（已实施）
@dataclass(frozen=True)
class PluginRegistration:
    """一类插件注册项的元数据。"""
    kind: str                # "tools" / "commands" / ...
    validator: Callable[[str, object], list[str]]
    conflict: str = "first-wins"
    hotplug: str = "session"

REGISTRATIONS: tuple[PluginRegistration, ...] = (
    PluginRegistration("tools", _validate_factory),
    PluginRegistration("commands", validate_command),
)
```

内核持有 `self.registries = {r.kind: PluginRegistry(r.kind, r.validator)
for r in REGISTRATIONS}`；重载的重置变成一行重建 dict。**新增一类注册项
= REGISTRATIONS 加一行**，内核主体不动（B1 消除）。`conflict`/`hotplug`
字段现在只声明、K3/K6 才消费，但结构一次定形。

### 2.2 tool_factories 并入 tools（D1 已定，已实施）

值统一为工厂 `factory(agent) -> list[Tool]`（裸实例由 ctx 包一层适配，
形状即时校验）。收益：内置恒首挂载 + 注册序即优先级 = **内置优先成为
结构性保证**（B3、B4 同消，`merge_tools` 与工具侧 `note_conflict` 已删）；
消费方只剩一个取用形态 `instantiate_tools(agent, ...)`。

### 2.3 Entry 补 provenance（B5 已部分实施）

`Entry.seq`（inserted_at_seq）已回填 registered 事件序号；`source`/
`scope` 字段随作用域机制（session/persistent）补齐：

```python
@dataclass
class Entry:
    name: str
    value: object
    plugin: str          # provenance：来源插件 id（source 在 PluginInfo 上）
    warnings: list[str]
    seq: int | None      # registered 事件的账本序号（已实施）
    # scope: str         # "session" | "persistent"（待作用域机制）
```

### 2.4 加载时序修复（B2 已实施）

`_load_key` 在**全部插件处理完成后**才赋值；中途异常（含内置致命）保持
旧键，下次 `ensure_loaded` 完整重试（回归测试 `test_half_loaded_state_retries`）。

---

## 3. 编排（① 装配成可执行单元）

### 3.1 可执行单元（ExecutionContext）

v1 的装配止于"注册表填充"；编排要再进一步--**绑定**：

```python
@dataclass
class ExecutionContext:
    """一次 agent 执行所需的全部绑定。"""
    tools: dict[str, Tool]        # 注册表实例化（工厂以 agent 为参）
    scope: ExecutionScope         # 沙箱边界（见 §4），内核供给
    ledger_tap: Callable          # 记录出口（见 §5），内核供给
    unit_id: str                  # 单元标识（顶层会话 / subagent / workflow run）
    parent: str | None            # 派生关系：子单元 scope 必为父的子集
```

- `kernel.assemble(spec) -> ExecutionContext`：组合输入（P1：workspace +
  禁用表）-> 应载清单 -> 注册表 -> 以 agent 为参绑定。
- **派生即沙箱语义**：subagent / workflow / team 队友 = `kernel.spawn(
  child_spec, parent=unit)`，子单元的 ExecutionScope 由内核做**子集运算**
  派生（修 B11：`CHILD_EXCLUDED_TOOLS` 静态排除表退位为 scope 的一条
  派生规则）。权限继承（v1 §2.5）由此免费获得。
- loop 是单元内的零件，不是内核--"换 loop 不换产品"不变。

### 3.2 消费方 API 收敛（修 B8）

| 目标 API | 现状 | 动作 |
|---|---|---|
| `assemble(spec)` | `ensure_loaded` + `instantiate_tools` + `merge_tools` | 三步并为一步，agent 只拿 ExecutionContext |
| `service(kind)` | `lookup_command` / `command_menu_entries` / ... | 注册表统一取用通道；定制方法先并存后删 |
| `spawn(child, parent)` | subagent/workflow 自行构造 | K3+ 再接线，P1 只定签名 |

---

## 4. 沙箱执行（② 安全环境）

### 4.1 沙箱的三个面

执行环境的边界由三部分构成，全部**由内核供给、工具只消费**（修 B10）：

```
ExecutionContext
├── 静态边界  ExecutionScope：fs 根（workspace）· 只读豁免 · 网络开关 ·
│             命令白/黑名单 · 危险命令表      <- 现散落在 Tool 构造参数
├── 动态闸门  裁决管线（v1 §2.2 七站固定序）：工具调用必经，只紧不松
│             <- 现在 executor 串行准备段，K3 析出
└── 资源底线  资源闸：轮次 / 预算 / 停止语义   <- 现在 agent loop 里，K4 析出
```

P1 动作：`ExecutionScope` 对象化，工具构造参数（`ws`、`allow_outside`、
`allowed_commands`、`dangerous_commands`）改由 scope 供给。行为≡现状，
但边界从"每个工具自觉"变为"内核发放"。

### 4.2 进程隔离：真正的决断点（D9）

**现状最大的洞是 B9**：插件 `apply(ctx)` 在主进程跑任意 Python--ctx
拒绝面只是不暴露引用，`import os; os.system(...)` 拦不住。沙箱若要当真，
必须回答插件代码怎么跑：

| 路线 | 形态 | 代价 |
|---|---|---|
| L1 语言级（现状延伸） | 同进程；能力面收窄 + 资源闸 + 全量记账 | B9 依旧是洞；信任模型 = "装了插件就信任其代码"（pip 同款） |
| L2 进程隔离 | 插件/工具跑子进程（macOS sandbox-exec / Linux seccomp+ns），IPC 回内核 | **与现有工具设计正面冲突**：TodoWriteTool 共享 `agent.todos`、AskUserTool 共享 console、ShellTool 共享 tasks 注册表--共享引用跨不了进程。builtin 工具须改写为 broker 模式（工具=RPC，状态内核持有） |
| L3 容器/microVM | 每单元一沙 | 部署重；远期 |

**诚实结论**：L2 是真沙箱，但它推翻"工具=同进程 Python 对象"的地基，
builtin 十九个工具全部重写为状态服务。这不是一个切片，是一次地基更换。
**建议分层定价**：对用户插件 apply（加载期，代码来源不可信）优先上 L2--
加载期只需要 ctx 的注册 API，面窄，IPC 代价小；对工具执行（运行期，
builtin+已注册插件）P1 维持 L1，broker 化作为远期路线图。加载期与运行期
分开定价，是本节的核心提案。

### 4.3 沙箱与把关的关系

裁决（弹窗、危险命令、存储规则）是**交互语义**，沙箱（fs 根、网络）是
**结构语义**。两者都挂在 ExecutionContext 上：闸门在每次工具调用前问
"这个调用允许吗"，边界在每次执行时限定"能在哪落子"。v1 的"只紧不松"
半格同时覆盖两者（scope 子集派生 = 半格单调映射的静态面）。

---

## 5. 记账（④ 第四职责，已定稿）

2026-08-24 定稿：记账为内核第四职责，与编排 / 沙箱执行 / 插件维护并列。
**记账是让"自主"可以被委托的证据系统**：沙箱回答"在哪跑、跑没跑过闸"，
记账回答"发生过什么、为什么、谁批准的"。环境有边界、有闸门、有记录，
三面齐全才叫安全环境。

**职责在核内的含义**（什么是内核不变量）：

- **事件信封 schema**（seq/ts/session/type/payload/cause/origin/digest）--
  v1 不变量 #3 原样保留；
- **`emit()` 唯一事件出口**：一切执行经内核，一切事件经 emit，没有旁路
  写协议的口子；
- **记账纪律**：append-only（内核无 update/delete API）、记账先于动作
  （宁可记了没执行，不可执行了没记）、决策留痕覆盖自身。

**实现选择（不改变职责归属）**：文件写入经 attach 注入的 sink
（SessionStore 挂接）--内核定义格式与纪律，不亲自做 IO；账本文件的损坏
恢复、轮转、审计工具皆在核外。分界线一句话：**格式与出口在核内，存储
与工具在核外**。

**P1 已实施**（K2a/K2b，2026-08-24）：`Event` 信封 + `project` 下行投影
+ `digest_of` 哈希链在 `core/protocol.py`；`kernel.attach_ledger(sink,
session, start_seq)` + `kernel.emit()` + 组合族事件
（composition_resolved / plugin_loaded / plugin_failed / registered /
rejected）在 `kernel/__init__.py`；agent `__init__` 挂接
`SessionStore.append_event`，seq 经 `ledger_start_seq()` 续起（恢复会话
不重号）。转录事件（text/tool_use/…）的信封化与 cause 链随 K3 接线。

---

## 6. 决断点（讨论清单）

| # | 问题 | 倾向 |
|---|---|---|
| ~~D1~~ | ~~tool_factories 并入 tools~~ | **已定并实施**：tools 单一注册项，工厂形态统一 |
| ~~D3~~ | ~~K1/K2 交错落地~~ | **已定并实施**：K1a/K1b/K2a/K2b 一次落地，Entry.seq 一步到位 |
| ~~D6~~ | ~~空组合的 composition_resolved 是否每载必记~~ | **已定并实施**：幂等跳过不记（键变化才记） |
| ~~D8~~ | ~~记账地位~~ | **已定稿（2026-08-24）：第四职责**，见 §5 |
| D9 | 进程隔离分层：插件加载期先上 L2、工具运行期维持 L1？ | 是；broker 化列远期路线图 |
| D10 | ExecutionScope 的 P1 范围：只收编现有参数（ws/allow_outside/allowed/dangerous）还是连网络开关也一并声明 | 只收编现有，网络开关占位 |
| D11 | 子单元 scope 派生（B11）何时接线：P1 只定 ExecutionContext.parent 字段，K3 spawn 时实现 | P1 只定字段 |

---

## 7. 落地切片修订

1. ~~**K1a 目录表驱动**~~ **已完成**（2026-08-24）：`kernel/registrations.py`
   目录驱动注册表生成 + `_load_key` 时序修复（B1、B2）。行为≡现状。
2. ~~**K1b 工厂归一**~~ **已完成**：tools 单一注册项，工厂形态统一，
   结构性内置优先，删 `merge_tools`（B3、B4）。行为≡现状。
3. **K1c ExecutionScope**：边界对象化，工具参数改内核发放（B10）。
   行为≡现状。
4. ~~**K2a 信封**~~ **已完成**：Event + project 投影 + digest 哈希链，
   下行逐字段等价（B7）。
5. ~~**K2b 突变记账**~~ **已完成**：attach_ledger + 组合族事件（B6）；
   Entry.seq 回填（B5 部分）。
6. **K3 闸门入核**：裁决管线析出 `kernel/guard.py`，七站固定序 +
   permission_decision 事件；ExecutionContext 动态闸门面就位。
7. **K4 资源闸**：轮次/预算/停止从 loop 析出，scope 资源底线就位。
8. **K5 全局账本 / K6 晋升门 / 插件加载期进程隔离（D9）**：后议。

> **搁置决定（2026-08-24）**：沙箱执行（K1c/K3/K4）与记账的后续推进
> （转录事件信封化、K5 全局账本）**整体暂缓**，待责任模型想清楚后再启。
> 已落地的 K2a/K2b 记账切片行为中性，保留不回退；四职责的责任定义不变，
> 搁置的是实施节奏而非架构结论。当前活跃方向：插件维护补全
> （卸载/unregister）与编排（ExecutionContext）。

---

*本文与 `openx-kernel-design.md` 冲突时，以讨论结论为准并立即回写彼文。*
