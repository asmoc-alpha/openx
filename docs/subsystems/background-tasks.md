# Background tasks

English | [中文](background-tasks.zh.md)

The `shell` tool accepts `run_in_background: true`: the command is detached (its own
process group) and its output streams to a log under `~/.openx/tasks/`. The agent then
uses `task_output` to tail logs and `task_stop` to terminate. Anything still running
when OpenX exits is cleaned up automatically.

```json
{"command": "npm run dev", "run_in_background": true}
```

## See also

- [Modes & permissions](../user/guide/modes-permissions.md) — shell calls still go
  through the permission flow
- [Workflows](workflows.md) — agent-level (not shell-level) async work
