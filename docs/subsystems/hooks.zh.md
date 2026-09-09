# Hooks

[English](hooks.md) | 中文

Shell hooks 把一次会话**分阶段**，在各个阶段运行用户自定义命令。配置在
`~/.openx/settings.json`（全局）和/或 `<workspace>/.openx/settings.json`
（项目级；按事件扩展全局列表）。schema 与 Claude Code 一致：

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "shell",
        "hooks": [
          {"type": "command", "command": "./guard.sh", "timeout": 30}
        ]
      }
    ]
  }
}
```

## 会话阶段

一次会话按下表分阶段，每个阶段对应一个可插入 hook 的事件：

| 事件 | 阶段 | payload 关键字段 |
| --- | --- | --- |
| `SessionStart` | 会话开始（进程启动 `startup` / 清上下文重开 `clear`） | `source` |
| `UserPromptSubmit` | 用户提问送达模型之前 | `prompt` |
| `PreToolUse` | 每次工具调用之前 | `tool_name`, `tool_input` |
| `PostToolUse` | 每次工具调用之后 | `tool_name`, `tool_input`, `tool_response` |
| `SubagentStop` | 一个子代理收尾（task 委派） | `subagent_type`, `description` |
| `PreCompact` | 历史压缩之前（手动 `/compact` 或自动） | `trigger` (`manual`/`auto`) |
| `Stop` | 回合收尾（正常结束 / 达到最大轮次） | `stop_reason` |
| `SessionEnd` | 会话结束（CLI 退出 / serve 停服） | `reason` |

payload 一律以 JSON 写入 hook 进程的 stdin，并附 `workspace` 与
`session_id`。

### 阻断能力分级

**只有 `UserPromptSubmit` 与 `PreToolUse` 真阻断**——前者拦下本轮提问
（CLI 与 web 同语义），后者拦下工具调用。其余阶段事件是**通知型**：hook
的 exit 2 / `decision: block` 一律降级为警告，绝不拦截生命周期本身——会话
不能被自己的审计脚本锁死。

## 语义

- `matcher` 是对工具名的 fnmatch 模式（省略或 `"*"` 表示所有工具）；只有
  tool 事件（`PreToolUse` / `PostToolUse`）使用 matcher，其余阶段事件忽略它。
- **exit 0** → 放行；如果 stdout 可解析为 `{"decision": "block", "reason": "…"}`，
  则按上文分级阻断或告警。
- **exit 2** → 阻断，理由取自 stderr（同样按分级消费）。
- 超时会杀掉 hook（仅警告）；其他非零退出只警告、不拦截。
- 同一事件的条目按「全局在前、项目在后」的顺序执行；某个 hook 阻断后，
  剩余 hook 不再运行。

用 `/hooks` 查看已配置的 hooks。hook 失败绝不会卡死 REPL 或 web 会话。

## 参见

- [配置](../user/guide/configuration.zh.md)——settings 文件的位置
- [模式与权限](../user/guide/modes-permissions.zh.md)——hooks 与权限分级互补
