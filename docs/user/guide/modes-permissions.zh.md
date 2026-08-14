# 模式与权限

[English](modes-permissions.md) | 中文

## 权限模式

OpenX 以 **manual** 模式启动，通过 `/mode [manual|auto|plan]` 切换（不带参数则打印当前模式）。

| | manual（默认） | auto | plan |
|---|---|---|---|
| 只读工具（read_file、grep、glob、list_directory、git_*、web_*） | 直接放行 | 直接放行 | 直接放行 |
| 写入工具（write_file、edit_file、shell、workflow、MCP） | **每次调用都确认**——已存规则、shell 白名单和 `-y` 一律忽略 | 正常流程：除非有已存规则 / 白名单 / `-y` 放行，否则询问 | 对模型隐藏，**且**在执行器层硬门控 |
| 危险 shell 命令（`config.dangerous_commands`：rm -rf、sudo、mkfs…） | 总是确认 | **总是确认**——规则、白名单或 `-y` 都不能跳过；批准后执行 | 拦截（plan 门） |

**choose_mode 流程。** 当你交给 agent 一个需要改文件或跑命令的任务时，它在 manual 模式下的第一个动作是 `choose_mode` 询问，选项为 **Auto** / **Plan** / **留在 manual**。这个选择只问一次并一直生效，直到你自己切换模式；切回 `/mode manual` 会重新启用它。纯问答类请求在 manual 模式下直接回答，不触发任何询问。

**Plan 模式。** 一切有修改行为的工具既从模型的工具 schema 中移除，*又*在执行器层门控，因此 agent 只能做只读探索。探索完成后它调用 `exit_plan_mode` 提交计划；你交互式地批准或拒绝。批准后切到 auto 模式并开启自动批准，agent 执行计划；拒绝则留在 plan 模式。

**注意。** 单次 / headless 运行（`openx "..."`）强制使用 auto 模式，权限对话框不会阻塞非 TTY 的 stdin。Subagent 在 spawn 时快照父级的模式（manual 父级的子 agent 写文件仍要确认）。模式不跨会话持久化——每次启动都是全新的 manual 同意。

## 权限分级

OpenX 是三级权限系统：

- **Allow**——始终执行（读文件、列目录、grep、git status）
- **Ask**——弹窗确认（写文件、shell 命令、MCP 工具）
- **Deny**——始终拦截（`rm -rf`、`sudo`、fork bomb 等）

批准询问时可以选择"不再询问"——规则会被存储，用 `/permissions` 管理（`/permissions rm <pattern>`、`/permissions clear`）。用 `--auto-approve` 或 `/auto-approve` 完全跳过询问。

## 参见

- [命令](commands.zh.md)——`/mode`、`/auto-approve`、`/permissions`
- [Hooks](../../subsystems/hooks.zh.md)——PreToolUse hook 也可以拦截调用
