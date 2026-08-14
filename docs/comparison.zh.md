# 与 Claude Code 的对比

[English](comparison.md) | 中文

OpenX 用 Python 重新实现了 Claude Code 的体验，可对接任意 OpenAI 兼容 API。当前的功能对齐情况：

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
