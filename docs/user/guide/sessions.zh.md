# 会话

[English](sessions.md) | 中文

每个对话都以 append-only JSONL 持久化在 `~/.openx/sessions/<workspace-hash>/<session-id>.jsonl`（消息事件 + 周期性元数据：token 计数、todos、首条用户消息）。图片以占位符存储，绝不存 base64。

```bash
openx --continue              # 恢复本工作区最近一次会话
openx --resume                # 交互式选择器，列出本工作区的会话
openx --resume <SESSION_ID>   # 恢复指定会话
```

恢复的历史会先清理孤儿 tool 消息，再回放给模型。

## 参见

- [命令](commands.zh.md)——通过 `session_id` 串联单次运行
