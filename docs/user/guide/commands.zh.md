# 命令

[English](commands.md) | 中文

两个表面：REPL 内的斜杠命令，以及 headless / CI 运行的机器可读输出。

## 交互命令

REPL 中注册的全部命令（见 `openx/cli/commands.py`）：

| 命令 | 说明 |
|---------|-------------|
| `/quit`（别名 `/exit`、`/q`） | 退出 OpenX |
| `/help` | 显示全部可用命令 |
| `/clear` | 清屏并清空对话历史 |
| `/model <name>` | 切换 LLM 模型（如 `/model gpt-4o`） |
| `/workspace <path>` | 更换工作区目录 |
| `/auto-approve` | 开关自动批准模式 |
| `/mode [mode]` | 查看或切换权限模式（manual / auto / plan） |
| `/explore` | 显示项目概览 |
| `/image <path>` | 加载并分析图片文件 |
| `/clipboard` | 粘贴并分析剪贴板截图 |
| `/init` | 创建 OPENX.md 指令文件 |
| `/instructions` | 显示已加载的 OPENX.md 指令 |
| `/memory` | 显示全部已存记忆 |
| `/remember <fact>` | 把一条事实存入持久记忆 |
| `/forget <name>` | 按名称删除一条记忆 |
| `/permissions`（别名 `/perms`） | 查看与管理已存储的权限规则 |
| `/hooks` | 显示已配置的 hooks |
| `/mcp` | 显示 MCP server 状态 |
| `/workflow [name]`（别名 `/workflows`） | 列出或运行已保存的 workflows（`.openx/workflows/`） |
| `/todos` | 显示 agent 的任务清单 |
| `/cost` | 显示累计 token 用量 |
| `/compact` | 压缩历史以释放上下文 |
| `/git` | 显示 git status |
| `/diff` | 显示 git diff |
| `/config` | 显示配置；交互式修改模型、API key、API base URL |
| `/tips` | 显示使用技巧 |
| `/release-notes`（别名 `/release`） | 浏览 release notes——选择版本查看，或 `/release <version>` |

在输入框里键入 `/` 可带补全地浏览命令：边输入边过滤（匹配名称与别名），**↑↓** 导航，**Tab** 补全，**Enter** 执行选中命令，**Esc** 关闭。

## 机器可读输出（`--output-format`）

单次模式可以输出 JSON 而非面向人的 UI——stdout **只有** JSON（一切人类可读信息走 stderr），退出码告诉 CI 运行成功（`0`）还是失败（`1`）。

| 格式 | 输出 |
|--------|--------|
| `text`（默认） | 人类可读：banner、思考指示、assistant 回复 |
| `json` | 恰好一个结果对象：`{"type": "result", "subtype": "success", "is_error": false, "duration_ms": …, "num_turns": …, "result": "final text", "session_id": "…", "usage": {"input_tokens": …, "output_tokens": …}}`（失败时 `"is_error": true` 外加 `"error"` 字段） |
| `stream-json` | NDJSON 事件流：`system/init`（模型、session id、工具列表）、`text_delta`（assistant 文本增量）、`tool_use` / `tool_result`（名称、错误标志、截断后的输出），最后是同一个 `result` 对象 |

```bash
# 面向 CI / 脚本的机器可读输出（仅单次模式）
openx "fix the failing test in tests/test_api.py" --output-format json
openx "refactor module X" --output-format stream-json

# 串联运行：把上一个会话的答案喂给下一个
SID=$(openx "analyze the auth module" --output-format json | jq -r .session_id)
```

## 参见

- [模式与权限](modes-permissions.zh.md)——`/mode` 与 `/auto-approve` 控制什么
- [会话](sessions.zh.md)——`--continue` / `--resume`
