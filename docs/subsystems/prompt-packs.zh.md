# 提示包（Prompt packs）

[English](prompt-packs.md) | 中文

提示包是**随产品发行的、按需加载的系统提示片段**。系统提示里只常驻一份**目录**
（一条 pack 一行说明），正文只在激活后才进提示。与 [技能](skills.zh.md) 是同一条
哲学，区别在归属：技能是*用户/项目*的指令包（可安装、可含附带文件），提示包是
*产品内置*的专业化契约（怎么写插件……），随版本一起走、不需要安装。

这是渐进披露的第二条通道：模型不该为了"偶尔才用到一次的专业契约"而在开机时就把
所有契约背下来。

## 内置 pack

| Pack | 覆盖范围 |
|---|---|
| `plugin-authoring` | 编写 OpenX 插件/组件：单文件清单、四种 `apply(ctx)` 协议、`self_test`。正文 = `openx/kernel/assembly/plugin_spec.py::PLUGIN_SPEC`（唯一真源——`write_plugin` 的准入闸校验的正是这份文本） |

新增一个 = 在 `builtin_packs()`（`openx/prompt_packs.py`）里加一条：名称、一行说明、
正文、触发关键词。

## 三条激活通道

三条通道全部汇入**同一个实现** `agent.activate_prompt_pack(name, reason=…)`，
"激活意味着什么"因此只有一处定义：

| 通道 | 触发方式 | `reason` |
|---|---|---|
| 模型主动 | `load_prompt` 工具（模型读了目录那行，判断自己需要正文） | `tool` |
| 用户显式 | `/prompt <name>` | `command` |
| 关键词 | 用户消息命中该 pack 的 `triggers` | `keyword` |
| 兜底 | 调了 `write_plugin`，契约必须在上下文里 | `write_plugin` |
| 摘除 | `deactivate_prompt_pack` | `deactivate` |

激活原子地做三件事：把名字加进会话的激活集合、记一条 `prompt_pack` 账本事件
（pack、reason、正文字符数）、重建缓存的系统提示。随后 `_sync_system_message` 在
下一次请求前把它同步进消息序列——所以一次 `load_prompt` 影响的是**下一轮**（round），
而不是下一个回合（turn）。

激活是**幂等**的：同一个 pack 再调一次 `load_prompt` 只返回正文，不重复记账、不重复
重建提示。摘除只移除正文，目录仍在，模型随时可以再加载。

## 渲染契约

由 `builtin-context` 插件产出的片段（`contribute_prompt_packs`，priority 60——排在
指令 10 / 记忆说明 40 / skills 50 之后）：

```markdown
## Prompt Packs (loaded on demand)

The catalog below is always in context; a pack's FULL text is not — ...

- `plugin-authoring` [loaded]: how to write an OpenX plugin or component: ...

<!-- BEGIN PROMPT PACK: plugin-authoring -->
...正文...
<!-- END PROMPT PACK: plugin-authoring -->
```

- 目录恒在，已激活的带 `[loaded]` 标记；
- 片段**不带前导空行/分隔符**——分隔由消费方统一施加（与 `format_instructions`
  同一契约）；
- `render_prompt_packs` 是纯函数：不读盘、不抛异常；未知或空 registry 一律返回空串
  （对提示组装而言"没有 pack"与"贡献为空"同构）。

## 触发关键词

`triggers` 在用户消息上做**大小写不敏感的子串匹配**。用子串而非分词，是因为中文没有
空格可切——「帮我写个插件」必须能命中 `插件`，这也是
`prompt_packs.py::PromptPackRegistry.match` 先 lower 输入、并要求 triggers 一律小写的
原因（有测试钉住这条）。宁窄勿宽：误命中要白付一整份正文的 token，漏命中只多花一次
`load_prompt`。

## 入口

CLI 里是 `/prompt [list|<name>|off [name]]`。Web 端把同一对能力暴露成 HTTP——
`GET /api/prompts`（目录 + `active` 标记，**不下发正文**：UI 没有理由替模型付它
可能永远用不到的 token）与 `POST /api/prompts`（`{"name", "active"}`），走的
正是同一个 `activate_prompt_pack`，所以账本事件与命令通道一字不差。

## 作用域与限制

- **会话态**：激活集合在内存里，绝不落盘——重启即回到"只背目录"，这正是按需加载的
  意义。
- **仅顶层**：子代理拿到的是空 registry（能力继承 = 父集的子集），既不继承也不为契约
  付 token。因此 `task` / `workflow` 派生的子代理没有 `load_prompt` 工具。
- 正文不单独占用 context 插件的字符预算；它与其它内置片段一样走 `builtin-context`
  这一条。
- 账本事件是**会话事件**（与 `memory_recall` 同一条归属纪律），不是跨会话决策。

## 实现

`openx/prompt_packs.py` —— 数据模型、registry、匹配、目录/正文渲染。
`openx/tools/prompt_tool.py` —— `load_prompt` 工具。`openx/agent.py` ——
`activate_prompt_pack` / `deactivate_prompt_pack` / `auto_activate_prompt_packs`
（所有通道共享的唯一实现）。`openx/app/cli/commands.py` —— `/prompt`。
`openx/builtin/context.py` —— context-v1 片段。`openx/kernel/protocol.py` ——
`prompt_pack` 账本事件。

## 另见

- [技能](skills.zh.md) —— 同一想法的用户/项目侧对应物
- [插件](plugins.zh.md) —— `plugin-authoring` 教的东西，以及它对应校验的那道闸
- [命令](../user/guide/commands.zh.md) —— 完整斜杠命令表里的 `/prompt`
