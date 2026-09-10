# 会话

[English](sessions.md) | 中文

每个对话都以 append-only JSONL 持久化在 `~/.openx/sessions/<workspace-hash>/<session-id>.jsonl`（消息事件 + 周期性元数据：token 计数、todos、首条用户消息）。图片以占位符存储，绝不存 base64。

```bash
openx --continue              # 恢复本工作区最近一次会话
openx --resume                # 交互式选择器，列出本工作区的会话
openx --resume <SESSION_ID>   # 恢复指定会话
```

恢复的历史会先清理孤儿 tool 消息，再回放给模型。

## 恢复被打断的回合

如果 OpenX 在回合进行到一半时死掉——崩溃、断电、Ctrl-C、`kill`——已完成的工具轮在每个轮次收口时都落了 checkpoint。加上 `--recover` 就能接着跑：

```bash
openx --continue --recover            # 恢复最近会话里被打断的回合
openx --resume <SESSION_ID> --recover
```

恢复**绝不重放**已经执行过的工具调用；进程死时仍在执行中的调用会以 `[status: interrupted]` 报告给模型，而不是被重试。恢复始终是显式选择——续跑会重新发起模型请求，所以应该是刻意的决定。不带 `--recover` 时，含被打断回合的会话照常加载，并会提示这件事。

如果 checkpoint 不可用（被截断、陈旧、属于别的工作区，或在另一份系统提示下写成），`--recover-mode` 决定行为：

| 模式 | 行为 |
|---|---|
| `auto`（默认） | 警告一句，然后用已保存的会话继续 |
| `strict` | 拒绝并退出 1 |
| `drop` | 静默丢弃 |

checkpoint 里有什么、什么时候写，见[容灾](../subsystems/recovery.zh.md)。

## 参见

- [命令](commands.zh.md)——通过 `session_id` 串联单次运行
- [容灾](../subsystems/recovery.zh.md)——回合级 checkpoint 与中断
