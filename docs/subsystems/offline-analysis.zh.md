# 离线分析（报告）

[English](offline-analysis.md) | 中文

自演进闭环靠**证据**运转——而原始证据早已被记录：每个会话的账本里都有转录、
权限裁决与成本字段。离线分析读这些账本，聚类成**人读报告**。系统只出证据，人
读完再决定（是否沉淀规则、写插件、调整装配）——这里的一切都不自行行动。

```bash
/gaps            # 最近会话的缺口报告（人读）
/gaps context    # 同上，但输出成可回喂的上下文片段
/gaps json       # 结构化报告（schema openx-gap-report/v1）

/assembly              # 装配报告：装了什么 vs 实际用了什么
/assembly suggestions  # 调优建议（人终审）
/assembly json         # 结构化报告（schema openx-assembly-report/v1）
```

## 缺口报告（`/gaps`）

`/gaps` 扫当前工作区最近的会话，聚四类**反复出现、可操作**的信号：

| 类别 | 信号 | 识别来源 |
|---|---|---|
| `tool_failure` | 某工具反复报错 | 工具结果消息（行首 `Error:`）按 `tool_call_id` 回填工具名 |
| `permission_friction` | 同工具反复 ASK 且被批准 | `permission_decision`（verdict ASK，approved） |
| `denied_calls` | 同工具反复被拒 | `permission_decision`（未批准） |
| `detour` | 同一 (工具, 参数) 在单会话内反复调用 | assistant `tool_calls` |

偶发、一次性的信号被各类阈值滤掉；报告按严重度排序（先计数、后类序）。每条
都带一句处置建议——例如"频繁被批准——考虑沉淀一条权限规则"。

## 装配报告（`/assembly`）

`/assembly` 把**当前组合**（`/plugins`）与**近期用量**（会话账本里的工具调用）
对齐，回答"装进来了——但有人用吗？"：

- **用量**——每个 active 插件的工具调用次数，按频率排序（"目录按使用频率排序"
  的数据源）；
- **装了却未用**——工具零调用的 active 非内置插件（考虑不装；`auto-*` 走批量
  回滚）；
- **最常用工具**——跨会话调用最多的工具。

`/assembly suggestions` 把调优建议打成文本——例如"回滚 auto 插件 'x'"——交人
处置。只对**贡献工具**的插件判"未使用"：`context.memory` / `lifecycle` /
`ui.panel` 类插件不贡献工具，标记为不可测，而不是误报"没用"。

## 把报告回喂

报告也是 prompt 侧产物：`as_context_fragment` 把它压成一串短条目
（`/gaps context` 打印它）。粘进你的 `OPENX.md`，或用 `context.memory` 插件贡献
它，下次会话的系统提示就带着"最近观察到的缺口"。无缺口时片段为空——没话说的
时候不注入。

## 限制

- **是证据不是动作。** 报告只提议，处置交给人（或将来的在线一步，且仍需过门）。
  `/gaps` 与 `/assembly` 都绝不写规则、插件或配置——`/assembly suggestions` 只
  打文本给你处置。
- **只读、离线。** 只读当前工作区的会话账本（外加只读的插件清单），别的不碰；
  绝不建项目 `.openx`。
- **错误检测是启发式。** 工具失败靠工具结果文本**行首**的 `Error:` 判定
  （账本不单存错误标志）；特殊输出可能误判。是提示，不是裁决。
- **用量 = 工具调用。** 装配报告统计 assistant `tool_calls`；不贡献工具的插件
  标记为不可测，而非计为未使用。
- **建议当前是文本。** `/assembly suggestions` 只出文本；把建议写回组合的
  overlay 原语属后续切片。

## 参见

- [脚手架](scaffolds.zh.md)——退场评测门消费评测数据
- [自演进设计](../design/openx-self-evolution-design.md)——完整闭环
