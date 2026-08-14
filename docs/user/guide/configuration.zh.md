# 配置

[English](configuration.md) | 中文

配置按从低到高的优先级合并：`settings.json` env → `~/.openx/config.json` → 项目 settings → 旧式 `.openx.json` → 环境变量 → CLI 参数。

## 环境变量

```bash
OPENAI_API_KEY      # 必填：你的 API key
OPENAI_API_BASE     # API base URL（任意 OpenAI 兼容端点）
OPENX_MODEL         # 模型名（默认 gpt-4o）
OPENX_AUTO_APPROVE  # 设为 'true' 跳过询问
```

## Settings（`~/.openx/settings.json`）

由首次运行的 setup wizard 写入；同时也是 `hooks`、`mcpServers` 和受信任工作区目录列表的存放处：

```json
{
  "env": {
    "OPENX_API_KEY": "sk-...",
    "OPENX_BASE_URL": "https://api.openai.com/v1",
    "OPENX_DEFAULT_MODEL": "gpt-4o"
  }
}
```

## Config 文件（`~/.openx/config.json`）

```json
{
  "api_key": "sk-...",
  "api_base": "https://api.openai.com/v1",
  "model": "gpt-4o",
  "temperature": 0.0,
  "auto_approve": false,
  "max_retries": 4,
  "retry_base_delay": 1.0
}
```

### 重试

瞬态 API 错误——HTTP 429/408/409/5xx、连接失败、超时、以及尚未产生任何屏幕输出就断开的流——会自动重试，最多 `max_retries` 次（0 表示禁用）。延迟采用指数退避 `base·2^attempt` 加抖动，基数为 `retry_base_delay`（秒），上限 60 秒；`Retry-After` 响应头优先于公式。重试也不可能成功的错误（400/401/403/404）立即抛出。流式响应一旦已经产生可见文本，断连会显式报错而不是静默重启（那会重复输出）。每次重试都会打印一行警告，长时间的停顿从不会莫名其妙。

## 项目配置（`<workspace>/.openx/settings.json`）

```json
{
  "model": "gpt-4o",
  "allowed_commands": ["npm", "npx", "docker", "make"]
}
```

项目级 `allowed_commands` 预批准匹配的 shell 命令（不再询问）。旧式 `.openx.json` 项目文件仍会读取，但已弃用。

## 参见

- [开发指南](../../development.zh.md)——首次运行的 setup wizard
- [Hooks](../../subsystems/hooks.zh.md)——`hooks` 配置块
- [MCP](../../subsystems/mcp.zh.md)——`mcpServers` 配置块
