# MCP (Model Context Protocol)

English | [中文](mcp.zh.md)

Connect external MCP servers over stdio — no extra Python dependencies (OpenX speaks
newline-delimited JSON-RPC directly). Configure under `mcpServers` in
`~/.openx/settings.json` (global) or `<workspace>/.openx/settings.json` (project-level;
same-name entries override global ones):

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

## Semantics

Remote tools are exposed as `mcp__<server>__<tool>` and always require interactive
approval (ASK permission). Servers connect at startup; failures degrade to a warning,
never a crash. Check status with `/mcp`.

Implementation: `openx/mcp/` — stdio NDJSON transport (spawn + line framing),
zero-dependency JSON-RPC client, `MCPTool` wrapper, and server lifecycle/config
loading.

## See also

- [Configuration](../user/guide/configuration.md) — settings precedence
