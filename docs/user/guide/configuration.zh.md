# 配置

[English](configuration.md) | 中文

模型/凭据配置**唯一**来自 `~/.openx/settings.json` 的 `modelGroups` 块。旧的扁平形态
（settings `env` 段的 LLM 三键、`~/.openx/config.json`、`.openx.json`、以及直读
`OPENAI_API_KEY`/`OPENAI_API_BASE`/`OPENX_MODEL`）**都不再**作为模型配置。

首次运行时 `openx` 发现没有配置任何模型组，会启动交互式 setup wizard，写入一个
`default` 模型组并设 `activeGroup`。

## 模型组（`~/.openx/settings.json`）

`modelGroups` 把「组名 → 组定义」映射起来；`activeGroup` 指名当前使用的组。每个组
可共享一套 `kind`/`apiKey`/`apiBase`，并可定义至多四个角色模型：

| 角色键 | 短名 | 用途 |
|---|---|---|
| `openx-main-model` | `main` | agent 自身主推理模型（必填） |
| `openx-exec-model` | `exec` | 子代理/任务委派 |
| `openx-mini-model` | `mini` | 轻量任务（历史压缩） |
| `openx-modal-model` | `modal` | 带图回合 |

```json
{
  "activeGroup": "default",
  "modelGroups": {
    "default": {
      "kind": "openai-compat",
      "apiKey": "env:OPENAI_API_KEY",
      "apiBase": "https://api.openai.com/v1",
      "openx-main-model": "gpt-4o",
      "openx-exec-model": { "model": "gpt-4o-mini" },
      "openx-mini-model": { "model": "gpt-4o-mini" },
      "openx-modal-model": "gpt-4o"
    },
    "local": {
      "kind": "openai-compat",
      "apiBase": "http://localhost:11434/v1",
      "apiKey": "env:OPENAI_API_KEY",
      "openx-main-model": "llama3.1"
    }
  }
}
```

角色值既可以是「模型名」字符串简写，也可以是对象：

```json
"openx-exec-model": {
  "model": "claude-sonnet-5",
  "kind": "anthropic-compat",
  "apiKey": "env:ANTHROPIC_API_KEY",
  "temperature": 0.2,
  "max_tokens": 4096,
  "max_retries": 3,
  "retry_base_delay": 1.0
}
```

角色对象可覆盖 `kind`/`apiKey`/`apiBase`（甚至单个角色换 provider/端点）与请求参数。
缺席的角色运行时回落该组 `main` 绑定（模型与凭据一并继承）。

`anthropic-compat` 讲 Anthropic-format 协议——不少厂商都提供这类兼容端点（如 DeepSeek 的
`https://api.deepseek.com/anthropic`）。设 `apiBase` 即指向任意兼容端点；留空默认走
Anthropic 官方。旧 kind `anthropic` 仍作别名接受。

### 凭据经 `env:VAR`

任意 `apiKey`/`apiBase` 值可写作 `env:变量名`，运行时从进程环境取值——这是唯一允许的
外部凭据通道。组可以完全不写凭据，改用 `env:` 引用环境变量里的 key。

### 组名

字母、数字、`.`、`_`、`-`（不含 `:`——冒号前缀用来选角色）。运行时用
`/model <组>` 切组，`/model <组>:<角色>` 设置某角色模型。

## 环境变量

只有非 provider 旋钮会从环境直读：

```bash
export OPENX_AUTO_APPROVE=true   # 跳过权限询问
export OPENX_WEB_SEARCH=ddg      # 或 'bing' / 'auto'
```

provider 的模型/key/base 必须在模型组里配置（可经 `env:VAR`），绝不从 `OPENAI_*`
自动读取。

## 项目配置（`<workspace>/.openx/settings.json`）

项目级文件可设 `allowed_commands` 等非模型旋钮（预批准匹配的 shell 命令）。它不能定义
模型/凭据——那些属于全局 `~/.openx/settings.json` 的模型组——但可以经 `activeGroup`
**指定本工作区默认用哪个全局组**：

```json
{
  "allowed_commands": ["npm", "npx", "docker", "make"],
  "activeGroup": "work"
}
```

组的定义仍在全局，这里只做**按工作区的选择**。激活优先级：会话内 `/model` 切换
（当前组）> 项目默认组 > 全局 `activeGroup`。带默认组的项目每次启动都落在该默认组；
会话内用 `/model` 切走只影响本次（仍写全局 `activeGroup`），下次启动回到项目默认。
默认组指向不存在的组名时回落全局激活组。

## 已移除的 CLI 参数

启动参数 `--model`/`--api-key`/`--api-base`（以及 `--max-rounds`/`--temperature`）
已移除——模型/凭据/端点只在模型组里配，请求调参归组/角色或项目文件。运行期用
`/model`、`/config`；项目也可用 `activeGroup` 选组。完全没有配置任何组时，openx
会启动 setup 向导。

## 重试

瞬态 API 错误——HTTP 429/408/409/5xx、连接失败、超时、以及尚未产生任何屏幕输出就
断开的流——会自动重试，最多 `max_retries` 次（默认 4；0 表示禁用）。延迟采用指数退避
`base·2^attempt` 加抖动，基数为 `retry_base_delay`（默认 1.0 秒），上限 60 秒；
`Retry-After` 响应头优先于公式。重试也不可能成功的错误（400/401/403/404）立即抛出。
流式响应一旦已经产生可见文本，断连会显式报错而不是静默重启。`max_retries`/
`retry_base_delay` 可在组级或角色级声明。

## 参见

- [开发指南](../../development.zh.md)——首次运行的 setup wizard
- [Hooks](../../subsystems/hooks.zh.md)——`hooks` 配置块
- [MCP](../../subsystems/mcp.zh.md)——`mcpServers` 配置块
