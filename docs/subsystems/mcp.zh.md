# MCP（Model Context Protocol）

[English](mcp.md) | 中文

通过 stdio 连接外部 MCP servers——零额外 Python 依赖（OpenX 直接说换行分隔的 JSON-RPC）。在 `~/.openx/settings.json`（全局）或 `<workspace>/.openx/settings.json`（项目级；同名条目覆盖全局）的 `mcpServers` 下配置：

```json
{
  "mcpServers": {
    "everything": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-everything"]
    }
  }
}
```

## 语义

远端工具以 `mcp__<server>__<tool>` 暴露，且总是需要交互式批准（ASK 权限）。servers 在启动时连接；失败降级为警告，绝不崩溃。用 `/mcp` 查看状态。

实现位于 `openx/mcp/`——stdio NDJSON 传输（spawn + 行分帧）、零依赖 JSON-RPC 客户端、`MCPTool` 包装、server 生命周期与配置加载。

## 参见

- [配置](../user/guide/configuration.zh.md)——settings 优先级
