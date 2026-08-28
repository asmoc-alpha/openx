# OpenX 内核详设 v2.1 · 编排 / 沙箱执行 / 插件维护 / 记账

> v2.1（2026-08-27）修订，均来自对 K1/K2 落地代码的审视：
> ① **实例化期给予面 ToolHost**（§1.4）——注册期拒绝面延伸到工具
> 实例化，插件任何阶段拿不到 agent 本体；② **内核 API 收敛**回四件 +
> 注册表只读视图，消费方装配策略迁出内核（§0，新切片 K3a）；
> ③ **hooks→Verdict 映射**定稿（§2.2），K3 落地依据；④ **boot 信任门**
> （§1.4）：项目级插件发现 ∩ workspace trust；⑤ 切片序补 **K3a / K7 /
> K8**（§4）——机制切片之外补能力迁移主线；⑥ 子会话**能力继承**写入
> §2.5；⑦ overlay 与 `plugins.disabled` 的迁移语义（§1.3）。
>
> 上位文档：`openx-architecture-design.md`（v4.1 总架构）。责任模型
> 2026-08-24 定稿：内核四职责（定稿讨论见 `design/microkernel-design.md`）。
> 与本文机制章节的对应：**编排** = §1 装配 + 绑定为可执行单元；
> **插件维护** = §1.1-§1.2 插件注册目录与注册表（单一门）；
> **沙箱执行** = §2 把关 + 执行环境静态边界（fs/网络/资源）；
> **记账** = §3。
>
> 统一骨架：四职责不是四个并列模块，而是一个闭环--
> **编排装配并绑定（每次突变记账）；沙箱执行裁决与隔离（每次裁决记账）；
> 插件维护把守唯一入口（注册即记账的最小事件）；记账审计前三者**。
> 把关是记账的最高消费者。

---

## 0. 内核对象总览

```
PluginKernel
├── Registrations 插件注册目录（不变量 #6：内核枚举，插件/模型不能发明位置）
├── Registries    每类注册项一张；Entry 带 provenance；突变日志上账本
├── Composition   组合输入（档案 × overlay）-> 应载清单 -> 决议记账
├── Guard         裁决管线 + 资源闸 + 晋升门   （不变量 #2 #5）
└── Ledger        会话账本 + 全局账本          （不变量 #3）

boot(composition) -> 注册表填充 -> loop 绑定
每轮:  intent -> loop -> tool_call --必经--> guard.gate() -> 执行
       一切事件 ----------必经---------> ledger.emit() -> 协议外化
```

内核 API 四件（其余皆拒绝面）：

```python
class Kernel:
    # ① 装配
    def boot(self, composition) -> None
    def service(self, name) -> object          # 注册表即 IPC：唯一取用通道
    # ② 把关
    def gate(self, tool_call) -> Verdict       # 执行闸：每个工具调用必经
    def admit(self, contribution) -> Verdict   # 晋升门：动态插入必经
    # ③ 记账
    def emit(self, event) -> None              # 唯一事件出口；append-only
```

**取用通道收敛**：四件之外，内核只暴露 `registry(kind)` **只读视图**。
消费方装配策略——工具实例化与冲突仲裁、provider 解析与回退、命令
菜单合并——住在消费方，不住内核；否则每加一类注册项内核就要长一个
新方法，与"目录加一行不改内核主体"自相矛盾。现状内核上的
`build_provider / instantiate_tools / lookup_command /
command_menu_entries / note_command_conflict` 是 K1 过渡形态，K3a
迁出（§4 切片序）。`agent.py` 里 provider 不可用时警告并回退
openai-compat 的策略同属装配策略，一并迁出。

---

## 1. 装配：注册与编排

### 1.1 插件注册目录（Registrations）

插件能往内核注册什么，由内核枚举（标准六）。目录表驱动--每类注册项
一份元数据，注册表按元数据自动生成，新增一类注册项 = 目录加一行 +
一个校验器，不改内核主体。

| 注册项类型 | 热插档 | 冲突规则 | 消费方 |
|---|---|---|---|
| `tools` / `tool_factories` | 会话内 | 全局名，先见者赢 | loop（经执行闸） |
| `commands` | 会话内 | 全局名 + 别名同规则 | 端层命令分发 |
| `prompt_fragments` | 会话内 | 按槽位 append（system 开头/结尾/工具节） | Context 组装 |
| `events` | 会话内 | **强制命名空间** `x-<vendor>.*` | 协议层 |
| `ui_slots` | 会话内 | slot id 唯一；几何白名单（只加面板/行） | 端层渲染 |
| `hooks` | 会话内 | 多挂全跑；**只紧不松过滤** | Guard |
| `providers` | 会话边界 | 单实现槽（一槽一值） | 零件层 |
| `memory_backends` | 会话边界 | 单实现槽 | 零件层 |
| `coordination` | 会话边界 | 单实现槽 | 零件层 |
| `loop` / `executor` / `ledger 格式` / `权限策略骨架` | 仅 boot | 单实现槽 | 内核 |

**命名空间三律**：

1. 核心注册名全局唯一--先见者赢，加载序（拓扑序，内置恒首）即优先级；
2. 自定义事件强制 `x-<vendor>.<name>`，违例拒载--协议扩展不得伪装核心事件；
3. 模型自产插件 id 强制前缀（如 `auto-*`），永不占用用户命名域--同源同门
   的落点：进同一候选池，但可被一眼识别、一键整体回滚。

### 1.2 注册表语义（Registries）

```
Entry = { name, value, plugin, provenance, warnings }
provenance = { plugin_id, source, scope, inserted_at_seq }
```

- **仲裁**：插件 vs 插件 = 先见者赢（后注册被拒、记 problem）；插件 vs
  内置 = 内置赢（内置恒首挂载，结构性保证，不靠运行时让步）。
- **突变即事件**：`registered / rejected / unregistered` 一律 append 到
  账本（§3.2 组合族）。由此**回滚 = 卸载**不只是可执行，且可审计：
  "这个工具什么时候来的、谁装的、何时撤的"答案在账本里。
- **撤销纪律**：`unregister` 仅两种来源合法--贡献注册者自身（provenance
  校验）或用户显式操作。模型配对产生的卸载建议必须过晋升门，同插入。
- **作用域是注册的属性**：`session`（动态插入，不进下次 boot 组合）/
  `persistent`（写回组合，下次 boot 生效）。灰度 = 先 session 后晋升。

### 1.3 组合输入（Composition）

```
model_profile（按模型版本的能力面）
  × 用户 overlay（~/.openx/openx.yml，补丁原语：add/remove/replace/enable/disable）
  × 项目 overlay（.openx/openx.yml，同原语）
  = 应载清单（computed bundle）——loader 只装载清单内插件
```

- **决议记账**：每次 boot 把计算结果固化为 `composition_resolved` 事件
  （含 profile 摘要、overlay 操作、最终清单）。这是"演进即重组"可审计
  的落点--任何一次会话的组合都能事后复现。
- 补丁语义按 cordis.patch.yml 式：overlay 只作用于 f(档案) 的计算结果，
  不直接互相覆盖；同键冲突用户级赢项目级。
- **不加载 ≡ 现状**（标准四）：overlay 为空且档案未声明任何脚手架
  requires 时，应载清单 = 内置插件 + 目录/entry-points 全集，行为与
  今天逐字节等价。
- P1 落地形态：档案与 overlay 尚未引入，应载清单退化为"全集"--但
  决议记账从第一天就有（空组合也记），账本格式不因功能分期而改。
- **迁移语义**：settings.json 顶层 `plugins.disabled`（P1 开关）在
  overlay 落地时升格为用户级 overlay 的 `disable` 原语语法糖--迁移期
  双读（两处并集生效），写只走 overlay；不出现两个并存的写真相源。

### 1.4 加载编排：五阶段

```
发现 -> 解析 -> apply(ctx) -> 校验 -> 激活
```

- **发现**：应载清单 ∩（用户目录 + 项目目录 + entry-points + base bundle）。
  同 id 先见者赢，用户级先于项目级。**boot 信任门**：发现 ∩ trust--
  项目级目录（`.openx/plugins`）仅当 workspace 已信任才进应载清单；
  未信任 = 整目录跳过并记 `plugin_skipped`（组合族事件，含目录与
  trust 状态），不是静默忽略。用户级目录与 entry-points 默认可信
  （用户自己安装即是授权动作）。trust 判定是 CLI 层职责，内核只消费
  判定结果--它作为组合输入的一项进 boot，决议记账自然留痕。
- **解析**：importlib；失败跳过不炸。
- **apply(ctx)**：ctx 给予面 = 注册 API + logger + 只读 workspace/配置；
  拒绝面 = 不暴露 loop、权限闸门、裸 console、他插件状态（靠不暴露引用，
  不靠自律）。
- **实例化期给予面（ToolHost）**：拒绝面必须延伸到工具实例化期，否则
  注册期成立、实例化期破功。工具工厂签名为
  ``factory(host) -> list[Tool]``，host 是 agent 的只读数据投影
  （`kernel/host.py`）：只读 workspace/配置字段 + 共享状态句柄
  （todos/tasks/coding_memory）。**插件在任何阶段都拿不到 agent
  本体**--loop、权限闸门、llm、hooks、完整 config 皆不可达。面上
  字段按"首个真实消费方出现才加入"最小化（受限 console、emit 等
  窄方法待有消费方再补）。**结构性工具**（task/workflow/
  exit_plan_mode/choose_mode/ask_user/structured_output）属内核驻留
  编排核心，由消费方直接装配、恒先占位（插件同名被拒记警告），不
  经 host 也不经插件注册--StructuredOutputTool 既有先例。K1 的
  `factory(agent)` 形态由 K3a 迁移。
- **校验**：逐注册跑形状校验器（形状/命名空间/越界）；违例拒载记入
  inventory。
- **激活**：依赖拓扑定序。插件声明 `provides` / `requires`（对注册项或
  插件 id），环 = 错误；未声明依赖按发现序。**激活的精确语义**：注册表
  条目对消费方的可见性翻转--校验通过前条目存在于表但对 `service()` 不可
  见，消费方永不吃到半成品插件。

**失败语义三级**：

| 来源 | apply 失败 | 禁用表 |
|---|---|---|
| 内置（base bundle） | 致命--产品带病不该运行 | 无效 |
| 用户/第三方 | 隔离--该插件 failed，进程不死 | 生效 |
| 动态插入 | 拒绝 + 记账（晋升门未过者不进表） | -- |

### 1.5 运行时编排：注册表即 IPC

装配管静态组合，运行时管**事件怎么流**。两条铁律：

1. **零引用**：插件彼此不认识，loop/executor 也不认识任何具体插件，
   双方只面对注册表（`kernel.service(name)` 是唯一取用通道）。loop 不
   import 任何零件--零件从注册表来，这是"换 loop 不换产品"的运行时前提。
2. **内核是唯一事件中继**：一切下行事件由内核 `emit()`（自动加盖
   provenance 与 cause），端与插件只订阅。插件产自定义事件同样经
   `ctx.emit()` 走内核--没有旁路写协议的口子。

### 1.6 动态插入路径

```
候选（模型配对/用户指定）
  -> admit() 晋升门（§2.4）
  -> 同一五阶段校验（复用 loader，无第二条加载路径）
  -> 注册（scope=session）
  -> 下轮生效：loop 下一轮 / 端下一帧
```

与 boot 装配的**唯一**差异是入口多一道晋升门、注册带 session 作用域。
加载逻辑零分叉--"同源同门"由代码结构保证，不靠约定。

---

## 2. 把关（Guard）

### 2.1 裁决半格：把"只紧不松"代数化

```
Verdict:  DENY  ⊳  ASK  ⊳  ALLOW_ONCE  ⊳  ALLOW_SESSION  ⊳  ALLOW

折叠规则：管线各站各出一个 Verdict，最终裁决 = 上确界（最严者）。
```

三条推论，即"只紧不松"的完整覆盖：

- **管线内**：任何站只能升严不能降宽。hook 贡献返回 ALLOW 不能推翻工具
  自声明的 ASK；返回 DENY 恒有效。贡献被过滤的不是"意见"，是"降宽意见"。
- **跨时间**：已存的宽裁决（ALLOW_SESSION）不能覆盖强制的严检查
  （is_high_risk → 至少 ASK）。落点是**固定序**：强制检查在存储裁决
  之前（§2.2 第 4/6 站），存储永不放宽危险。
- **跨层级**：团队权限继承是半格上的单调映射，子会话裁决 ≥ 父会话
  同名裁决的严格度（§2.5）。

### 2.2 裁决管线（固定序，序即不变量）

```
tool_call
  ① 硬拒绝        deny 规则 / 黑名单 / 模式拦截（plan 模式写操作）
  ② 自声明        permission level -> 默认 Verdict
  ③ 高危强制      is_high_risk -> 抬到至少 ASK（不可被后续任何站降宽）
  ④ 策略贡献      hooks 逐个跑，只紧不松过滤后折叠
  ⑤ force_prompt  manual 模式下 ASK 级 / 高危 -> 抬到 ASK
  ⑥ 存储裁决      已存规则 -> 可至 ALLOW_SESSION；③⑤已抬严者不受影响
  ⑦ 用户裁决      弹窗 / 远程批准（fail-closed）
  -> Verdict + permission_decision 事件上账本
```

每一站的输入、输出、依据全部进 `permission_decision` 事件的 payload
（§3.2 控制族）--裁决可审计不是口号，是管线的数据形状。

**hooks → Verdict 映射（K3 落地依据，行为 ≡ 现状）**：

| hook 产出（现状语义） | 折叠入管线的 Verdict |
|---|---|
| exit 0，stdout 无 `decision:block` | 无意见--不参与折叠 |
| exit 0，stdout `{"decision":"block","reason":...}` | DENY（reason 入 payload） |
| exit 2 | DENY（reason 取 stderr） |
| 其余非零 / 超时 / 启动失败 | 无意见 + warning 附入 `permission_decision` payload 展示 |

现状"hook 故障只警告不阻断"是有意保留的行为（每步行为 ≡ 现状）；
未来要收紧应做成可配项（`hook_failure_mode=ask`），不在这个映射里
改默认。注册时声明产出方向（决断点 #4）落地后，声明了产出方向的
hook 在此表基础上再受静态校验约束。

**存储 allow 与工具级 DENY 的先后**（K3 落地时钉死的现状语义）：
②自声明的 DENY 在管线内**挂起**，⑥存储 allow 可越过它放行--用户
显式落盘的授权规则优先于工具自声明（现行 executor 的顺序如此，
行为 ≡ 现状）。这与半格"DENY 恒有效"的纯度有张力，纯化（DENY
终局化）留给评审决断，改动即行为变更，须单独切片。

### 2.3 资源闸：可执行性来自"内核持有执行闸"

资源闸管的是**信任的底线**而非模型能力：停止语义、轮次上限、预算底线
（token/时间/费用）。

强制机制是物理的，不靠 loop 自觉：**执行闸在内核手里**--loop 实现无论
是 harness loop 还是 model-native，工具调用必须经 `kernel.gate()`，绕过
路径不存在（零件从 `service()` 来，`service()` 发出的工具句柄全部带闸）。
每轮循环必过资源闸检查，触顶 = `resource_gate_tripped` 事件 + 停止语义
（graceful interrupt，排队意图不丢）。

> 这一条是"内核在 loop 之上"的实质理由：把关若要独立于 loop 演进，
> 执行路径的咽喉必须握在不被替换者手里。

### 2.4 晋升门（admit）

| 贡献分级 | 判据 | 流程 |
|---|---|---|
| 只读（observe） | ui_slot / prompt 片段 / 只读工具 / x-* 事件 | 校验过即热插（纯加法，回滚=卸载） |
| 变更（act） | 写工具 / hooks / 单槽实现 | 用户确认（弹窗/远程）+ scope=session 灰度 -> 晋升（写全局账本 + 下次 boot 进组合） |

- 模型配对只产**候选 + 建议位置**；位置合法域 = 插件注册目录（§1.1），
  目录外的建议一律拒--prompt injection 最坏结果 = 推荐错插件。
- 晋升与回滚都走 `admit()` 的逆操作 + 全局账本决策事件：谁批准、依据
  什么、何时、一键回滚。

### 2.5 团队安全

- **权限继承**：子会话（task/workflow/serve 管理的队友）权限集 = 父的
  子集，裁决在半格上单调映射；父被收紧，子同轮收紧。
- **能力继承**：与权限继承同向单调--子会话能力集 = 父的子集。默认
  不继承用户插件工具与结构性工具（task/workflow/exit_plan_mode/
  ask_user 恒排除）；子代理规格的工具白名单可显式纳入 `mcp__*` 与
  插件工具。K1 的 `instantiate_tools(include_plugins=False)` 参数是
  此策略的临时编码，K3a 迁出内核时落为消费方的显式规则。
- **全队弹窗队列**：裁决串行化于父会话的 Guard--子会话请求汇入同一
  队列，永不并行弹窗（复用既有 prompt_lock 传播语义，升格为内核保证）。

### 2.6 远程批准：fail-closed 三律

`permission_request` 经协议下行，`permission_response` 按 request_id
配对唤醒。三条不可协商：

1. **EOF / 断流 = 拒绝**；
2. **超时 = 拒绝**；
3. **未匹配 request_id = 拒绝**（不猜、不默认、不重放旧批准）。

通道失联时唯一合法方向是收紧。headless 与 serve 共用此桥，差异只在
下行载体（stdin NDJSON vs WebSocket）。

---

## 3. 记账（Ledger）

### 3.1 信封格式：协议 = 账本的外化

```
Event = {
  seq, ts, session,           # 簿记：序号/时间/会话
  type, payload,              # 语义：类型/内容
  cause,                      # 因果前驱 seq（tool_result.cause = tool_use.seq）
  origin,                     # 归因：user | model | plugin:<id> | kernel
}
```

- **单一真源**：核心 schema 定义在 `openx/core/protocol.py`，协议下行
  事件 = 信封去掉簿记字段的投影。账本不另造格式，协议不另造语义。
- **cause 链即归因链**：任何结果可沿 cause 回溯到发起意图--"这行代码是
  哪次批准的哪个工具改的"是一条链上溯，不是一次考古。
- **origin 与 provenance 分工**：事件级 origin 说"谁干的"；注册级
  provenance 说"能力哪来的"。归因 = 两链之交。

### 3.2 事件族与双账本

| 族 | 事件例 | 落账 |
|---|---|---|
| 转录 | text / thinking / tool_use / tool_result | 会话账本 |
| 控制 | permission_request / permission_decision / resource_gate_tripped / interrupt | 会话账本 |
| 组合 | composition_resolved / plugin_loaded / plugin_failed / plugin_skipped / registered / rejected / unregistered | 会话账本（引用全局条目） |
| 决策 | plugin_promoted / plugin_rolled_back / scaffold_retired / scaffold_restored / ratchet_tightened | **全局账本** |

**双账本**：

- **会话账本** `~/.openx/sessions/*.jsonl`（现有会话存储升格）：回放与
  单会话审计的依据。
- **全局账本** `~/.openx/ledger.jsonl`：跨会话决策留痕。晋升、退场、
  回滚、棘轮收紧是跨会话事实，塞进任一会话都是错误归属--"这个插件哪来
  的、为什么压缩模块没了"的答案只该有一个权威所在地。
- 关系：会话账本以 (ledger, seq) 引用全局条目，不复制内容。回放单会话
  时全局条目按需展开。

### 3.3 回放语义

- 回放 = 按序重发存储事件，复盘与实时共用同一 schema（协议=账本外化的
  直接推论）。
- **渲染纯函数律**：端对事件流的渲染必须是纯函数--端不得持有会话状态
  语义。这是端层"薄客户端"不变量的可验证表述。
- **动作不重放**：回放只覆盖事件面；工具执行、外部副作用永不重演。
  回放是观察，不是执行--这是"回放=重发"的安全边界。

### 3.4 防篡改：轻量哈希链

每条事件计算 `digest = h(prev_digest || canonical(event))` 附入信封。
校验工具扫描账本即可发现任何中段篡改/删除。

强度取舍：目标是**事后审计可发现**，不是密码学对抗--摘要链即可，不引
签名、不引外部信任锚。digest 断链 = 审计告警事件（本身也记账），不阻断
读取。

### 3.5 记账的可执行性

- **append-only 是物理的**：内核只有 `emit()`，没有 update/delete API；
  账本文件只以追加模式打开。
- **记账先于动作**：`permission_decision` 先落账，工具后执行；资源闸
  触顶先记账，后停止。宁可记了没执行，不可执行了没记。
- **决策留痕覆盖自身**：本设计的每次演进（晋升门放行、脚手架退场、
  棘轮收紧）都产生决策事件--账本升级自己的历史也在账本里。

---

## 4. 与现有代码的对齐与落地切片

| 现有 | 本设计归属 | 差距 |
|---|---|---|
| `kernel/registrations.py` + `registry.py`（PluginRegistry/Entry/seq） | §1.1-§1.2 | 已落地（K1）；补作用域、卸载 |
| `kernel/loader.py` 五阶段 | §1.4 | 补依赖拓扑、作用域、动态插入复用 |
| `kernel/validate.py` 形状校验 | §1.1 | 已落地（随 K1 目录化） |
| `builtin/tools.py` base bundle | §1.3 计算组合的内置项 | 已对齐 |
| ~~`permissions.py` + executor prepare 闸门~~ | §2.2 升格入 `kernel/guard.py` | 已落地（K3）：七站管线 + 半格折叠 + `permission_decision` 记账；prompter/rules/mode 闭包注入，UI 不进内核 |
| `core/protocol.py`（Event 信封 + digest 链） | §3.1 单一真源 | 已落地（K2）；转录事件 cause 链随 K3 |
| `kernel.emit`/`attach_ledger` + `sessions/*.jsonl` 信封行 | §3.2 会话账本 | 已落地（K2）；双账本与决策事件族随 K5 |
| `app/cli/commands.py` 内置命令 dict | §1.1 commands | 半插件化：插件命令已走注册表，27 个内置命令仍硬编码、消费方双源合并；升格 builtin-commands 插件随 K8 |
| `mcp/`（`mcp__*` 工具直并入 agent.tools） | §1.1 tools + §1.6 | 绕过注册表：无校验/仲裁/provenance/记账；K8 收口，兼作 K6 admit() 的 pilot |
| `instructions.py` / `skills.py` / 记忆提示常量 | §1.1 prompt_fragments | 硬连线 prompt 源；K7 收口 |
| `core/hooks.py` | §2.2 第④站 | 独立机制（exit 2 阻断），未入半格；K3 按 §2.2 映射表折叠 |
| `memory.py` / `coding_memory.py` | §1.1 memory_backends | 硬连线；P2+ |
| `core/sessions/history/subagent/workflow/tasks` | §1.1 coordination | 硬连线；P2+ |
| `agent.py` 直 import 具体工具（`git_status`、`MEMORY_INSTRUCTIONS`、`StructuredOutputTool`） | §1.5 零引用 | 破洞：loop 认识具体插件；随 K7/K8 修 |
| `tools/base.py`（Tool/ToolResult 形状） | §1.1 校验器 | 形状应上移内核（`kernel/provider.py` 先例）；随 K8 |
| ~~内核上消费方助手（`instantiate_tools`/`build_provider`/`lookup_command` 等）~~ | §0 取用通道收敛 | 已落地（K3a）：迁往 `services/assembly.py` 与 `commands.py` |
| ~~工具工厂签名 `factory(agent)`~~ | §1.4 ToolHost | 已落地（K3a）：`factory(host)`，`kernel/host.py` |

**切片序（每步行为≡现状）**：

1. ~~**K1 目录表驱动**~~ **已完成**：插件注册目录对象化
   （`kernel/registrations.py`），新增一类注册项不改内核主体。
2. ~~**K2 信封 + 突变记账**~~ **已完成**：protocol.py Event 信封
   （seq/ts/cause/origin/digest）+ 下行投影；注册表突变、组合决议
   上账本（attach_ledger 挂接会话存储，seq 续起）。
3. ~~**K3a ToolHost 收窄 + 内核 API 收敛**~~ **已完成**（2026-08-27）：
   `kernel/host.py` ToolHost 数据投影，工厂签名 `factory(agent)` →
   `factory(host)`；装配策略迁出内核（`services/assembly.py` 工具
   实例化仲裁 + provider 解析，`commands.py` 命令分发/菜单），内核
   API 回归 ensure_loaded / registry(kind) / emit / inventory + ctx
   回调；结构性工具消费方直接装配、恒先占位（reserved 仲裁）。
4. ~~**K3 Guard 升格**~~ **已完成**（2026-08-28）：裁决管线析出入
   `kernel/guard.py`——Verdict 半格（IntEnum，min 即抬严）、七站
   固定序、hooks 按 §2.2 映射表折入第④站、每次裁决记
   `permission_decision`（含逐站 trace）。executor 只留 prompter
   （UI）与并行扇出。落地形态：Guard 由 executor 持有、每会话状态
   闭包注入；`kernel.gate()` 单例形态待会话上下文对象化（K1c）。
   两处现状语义保留并注释：存储 allow 可越过工具级 DENY；hook 故障
   只警告不阻断（收紧做成 `hook_failure_mode` 可配项，留评审）。
5. **K4 资源闸析出**：轮次/停止/预算从 loop 不变量析出，为 loop 槽化
   （P2）清场。
6. **K5 全局账本**：决策事件族 + 双账本引用。
7. **K6 晋升门 + 动态插入**：admit() 会话内热插路径（只读先行）。
   **以 MCP 为 pilot**：connect 即 session 作用域动态插入，复用同一
   五阶段校验，不另造测试场景。
8. **K7 prompt_fragments**：instructions（OPENX.md）/ skills / 记忆
   提示统一收口为 prompt 片段注册项；agent 不再 import 具体插件的
   提示常量（`MEMORY_INSTRUCTIONS` 等零引用破洞随修）。
9. **K8 能力注册表化**：内置命令升格 builtin-commands 插件
   （commands.py 只剩分发）；MCP 工具走 tools 注册表
   （provenance=`mcp:<server>`）；Tool/ToolResult 形状上移内核。

---

## 5. 留给评审的四个决断点

1. **半格粒度**：条件 ALLOW 拆 ALLOW_ONCE / ALLOW_SESSION 两档，还是
   合并一档带过期语义？（现实现是两档：单次/记住；建议保持）
2. **全局账本的信任边界**：用户可否手编全局账本（如手动记一条"我批准
   过"）？建议不可--手编断链，审计告警；用户意图走 overlay/晋升门。
3. **哈希链强度**：单链摘要 vs 每会话一条链 + 全局链锚定？（建议前者，
   后者等 P6 脚手架退场需要跨会话证据链时再升）
4. **hooks 的只紧不松过滤在运行时判定**（折叠时丢弃降宽意见）还是在
   注册时判定（声明产出方向，静态可审计）？（建议注册时声明 + 运行时
   校验双保险）
5. **boot 信任门的默认档位**：entry-points（pip 安装）默认可信与否？
   建议可信--`pip install` 本身是用户的显式授权动作，与 clone 即得的
   项目级目录性质不同；项目级目录恒过 workspace trust（§1.4）。

---

*本文与 `openx-architecture-design.md`（v4.1）配套；机制变更须先改本文
再改代码。*
