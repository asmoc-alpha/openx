# 与 Claude Code 的对比

[English](comparison.md) | 中文

OpenX 与 Claude Code 在功能面上有大量重合，但 OpenX 追求的是另一种架构：以最小的可审计内核 + 可替换插件构成的**可验证、可演进** agent runtime，而非某一产品的复刻。当前功能面对齐情况：

| 功能 | Claude Code | OpenX |
|---------|-------------|-------|
| Agentic loop | ✅ | ✅ |
| 文件读/写/编辑 | ✅ | ✅ |
| Shell 命令 | ✅ | ✅ |
| 代码搜索（grep/glob） | ✅ | ✅ |
| Git 集成 | ✅ | ✅ |
| 权限系统 | ✅ | ✅ |
| 流式输出 | ✅ | ✅ |
| 带退避的重试（429/5xx、Retry-After、流中断） | ✅ | ✅ |
| Rich 终端 UI | ✅ | ✅ |
| 并行工具调用 | ✅ | ✅ |
| Manual 模式（默认每次写入都确认） | ✅ | ✅ |
| Plan 模式 | ✅ | ✅ |
| Subagents（Task） | ✅ | ✅（内置 + `.openx/agents/*.md`） |
| Workflows（确定性编排） | ✅ | ✅（Python 脚本，`.openx/workflows/`） |
| Hooks | ✅ | ✅（相同的事件 schema） |
| MCP | ✅ | ✅（stdio servers） |
| 会话恢复 | ✅ | ✅ |
| Headless JSON 输出（json / stream-json、退出码） | ✅ | ✅ |
| 后台任务 | ✅ | ✅ |
| 自动压缩（compaction） | ✅ | ✅ |
| Notebook 编辑 | ✅ | ❌ |
| Anthropic 原生 API 格式 | ✅ | ❌（仅 OpenAI 兼容端点） |
| 多 provider | ❌（仅 Anthropic） | ✅（OpenAI 兼容） |
| 开源 | ❌ | ✅（MIT） |
| 语言 | TypeScript | Python |

## 参见

- [Subagents](subsystems/subagents.zh.md) · [Workflows](subsystems/workflows.zh.md) ·
  [Hooks](subsystems/hooks.zh.md) · [MCP](subsystems/mcp.zh.md)
- [模式与权限](user/guide/modes-permissions.zh.md)
