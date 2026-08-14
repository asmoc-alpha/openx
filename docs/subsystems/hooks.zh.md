# Hooks

[English](hooks.md) | 中文

Shell hooks 在四个事件上运行——`PreToolUse`、`PostToolUse`、`UserPromptSubmit`、`Stop`——配置在 `~/.openx/settings.json`（全局）和/或 `<workspace>/.openx/settings.json`（项目级；按事件扩展全局列表）。schema 与 Claude Code 一致：

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

## 语义

- `matcher` 是对工具名的 fnmatch 模式（省略或 `"*"` 表示所有工具）；只有 tool 事件使用 matcher。
- 事件 payload 以 JSON 写入 hook 的 stdin。
- **exit 0** → 放行；如果 stdout 可解析为 `{"decision": "block", "reason": "…"}`，则拦截该调用。
- **exit 2** → 拦截；理由取自 stderr。
- 超时会杀掉 hook（仅警告）；其他非零退出只警告、不拦截。

用 `/hooks` 查看已配置的 hooks。hook 失败绝不会卡死 REPL。

## 参见

- [配置](../user/guide/configuration.zh.md)——settings 文件的位置
- [模式与权限](../user/guide/modes-permissions.zh.md)——hooks 与权限分级互补
