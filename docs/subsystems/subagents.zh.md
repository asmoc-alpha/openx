# Subagents

[English](subagents.md) | 中文

`task` 工具把一块自包含的工作委托给拥有独立上下文窗口的子 agent。

## Agent 类型

内置类型：

- `general-purpose`——除 `task` 与 `ask_user` 外的全部工具
- `explore`——只读工具（`read_file`、`grep`、`glob`、`list_directory`……）

自定义 subagent 放在 `<workspace>/.openx/agents/*.md`——极简的 `key: value` frontmatter 加上正文，正文会成为额外的 system prompt：

```markdown
---
name: reviewer
description: Reviews code for quality issues.
tools: read_file, grep, glob
model: gpt-4o-mini
---

You are a strict code reviewer. Focus on correctness, readability, and tests.
Report findings as a numbered list with file:line references.
```

`tools` 是可选的逗号分隔白名单（省略表示全部工具）；`model` 是可选覆盖（省略则继承父级）。子 agent 共享父级的 console、权限规则、hooks 和后台任务注册表——且不能再生自己的 subagent（不允许嵌套）。

## 结构化输出

给 `task` 工具传一个 `schema`（JSON Schema 对象），子 agent 就必须恰好调用一次 `structured_output` 工具来交付结果，且 `data` 符合该 schema——纯文本的最终答案会被丢弃。校验失败会作为 tool error 报回给子 agent，让它在同一次运行内纠正并重试。成功时 `task` 工具返回校验过的对象（JSON 字符串，而非自由文本）；一个从未调用 `structured_output` 就结束的子 agent 会被报告为失败。在 workflow 中，`agent(prompt, schema=...)` 直接把校验过的 **Python 对象**交给脚本——无需解析。

## 实时状态面板

agent 流式输出期间，输入框**下方**渲染一个状态面板，实时更新（5 Hz）：

- **Plan 面板**——agent 的 todos 清单：`✓` 已完成（绿色）、进行中显示 spinner 加任务的 `activeForm`、`○` 待办；长列表折叠为六行加 `+N more`。
- **Agents 行**——每个运行中的子 agent（`task` 工具或 workflow）一行：spinner、描述标签、工具计数、耗时；结束时 `✓`/`✗`。超过四个 agent 后折叠。

流式输出期间按 **Ctrl-O** 可把主响应区循环切入某个子 agent 的详情视图（其捕获的工具活动与文本）再切回来。turn 结束面板消失；短终端预算会在面板挤占回复之前先裁剪它。

## 参见

- [Workflows](workflows.zh.md)——确定性地编排多个子 agent
- [模式与权限](../user/guide/modes-permissions.zh.md)——子 agent 快照父级的模式
