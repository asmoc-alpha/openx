# 子系统

[English](README.md) | 中文

每个子系统一页 reference：语义、配置、限制。使用指南在 [user/](../user/index.zh.md)；分步 how-to 在 [cookbook/](../cookbook/extending.zh.md)。

| 子系统 | 页面 | 一句话 |
|---|---|---|
| Subagents | [subagents.zh.md](subagents.zh.md) | `task` 工具：内置与自定义 agent 类型、结构化输出、实时状态面板 |
| Workflows | [workflows.zh.md](workflows.zh.md) | 用 Python 脚本做确定性多 agent 编排 |
| 后台任务 | [background-tasks.zh.md](background-tasks.zh.md) | 分离式 shell 命令，可 tail 日志、可停止 |
| Hooks | [hooks.zh.md](hooks.zh.md) | 八个生命周期事件上的 Claude-Code 兼容 shell hooks |
| MCP | [mcp.zh.md](mcp.zh.md) | stdio 方式的 Model Context Protocol servers，零额外依赖 |
| 脚手架 | [scaffolds.zh.md](scaffolds.zh.md) | 声明补偿型模块为何存在、何时退场；退场/回挂全程账本留痕 |
| 离线分析 | [offline-analysis.zh.md](offline-analysis.zh.md) | 把会话账本聚类成人读报告（缺口报告 `/gaps`、装配报告 `/assembly`） |
| 经验沉淀 | [experience.zh.md](experience.zh.md) | 从会话里挖候选经验写入记忆；召回回账（`/distill`） |
| 容灾 | [recovery.zh.md](recovery.zh.md) | 回合级 checkpoint：恢复被打断的回合，且不重放已完成的工具调用 |
