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

## 内核注册（会话作用域）

启动时，每个已连接 server 的远端工具经**晋升门**（K6）注册为内核的**会话作用域贡献**——一个 server 一条（`plugin_id = mcp:<server>`，一个工厂产出它全部的 `mcp__*` 工具），于是 `provenance`、形状校验与记账与其它插件走同一道门。因 `mcpServers` 由用户写入（安装即授权）、且每次远端调用仍被独立地以 ASK 把守，MCP 注册走只读档（`observe`），无需额外弹窗。会话结束即撤销该贡献（**回滚 = 卸载**）；组合重组会**重放**它，故工具绝不在会话中途无声消失。详见 `docs/openx-kernel-design.md` §2.4。

实现位于 `openx/mcp/`——stdio NDJSON 传输（spawn + 行分帧）、零依赖 JSON-RPC 客户端、`MCPTool` 包装、server 生命周期与配置加载。

## 参见

- [配置](../user/guide/configuration.zh.md)——settings 优先级
