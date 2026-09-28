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

## Kernel registration (session-scoped)

At startup, each connected server's remote tools are **registered with the kernel
through the promotion gate** (K6) as a **session-scoped contribution** — one entry per
server (`plugin_id = mcp:<server>`, a single factory producing all of its `mcp__*`
tools), so `provenance`, shape validation and ledger accounting all flow through the
same gate as any other plugin. Because `mcpServers` is written by the user (installing
is the authorization) and every remote call is still independently gated as ASK, MCP
registration uses the read-only (`observe`) class and needs no extra prompt. Shutting
down the session removes the contribution (`rollback = unload`); a composition reload
replays it, so the tools never silently disappear mid-session. See
`docs/openx-kernel-design.md` §2.4.

Implementation: `openx/mcp/` — stdio NDJSON transport (spawn + line framing),
zero-dependency JSON-RPC client, `MCPTool` wrapper, and server lifecycle/config
loading.

## See also

- [Configuration](../user/guide/configuration.md) — settings precedence
