# 后台任务

[English](background-tasks.md) | 中文

`shell` 工具接受 `run_in_background: true`：命令被分离出去（独立进程组），输出流入 `~/.openx/tasks/` 下的日志。之后 agent 用 `task_output` tail 日志、用 `task_stop` 终止它。OpenX 退出时仍在运行的任务会被自动清理。

```json
{"command": "npm run dev", "run_in_background": true}
```

## 参见

- [模式与权限](../user/guide/modes-permissions.zh.md)——shell 调用仍走权限流程
- [Workflows](workflows.zh.md)——agent 级（而非 shell 级）的异步工作
