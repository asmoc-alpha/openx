# 容灾（checkpoint）

[English](recovery.md) | 中文

一次要跑很多工具的回合可能很久。进程中途死掉——崩溃、断电、Ctrl-C、`kill`——此前做过的全部工作会直接丢失：OpenX 只在**回合边界**持久化会话，进行到一半的回合被整个丢弃。

容灾补上了这段窗口。**每个工具轮之后**，OpenX 把本回合在途的消息写成 checkpoint。重启后用 `--recover` 恢复，已完成的轮次原样回来——**且已执行过的工具调用不会被重放**。

```bash
openx --continue --recover          # 恢复最近会话的回合
openx --resume <session-id> --recover
```

## checkpoint 里有什么

checkpoint 是会话账本旁边的旁挂文件：

```
~/.openx/sessions/<workspace-hash>/<session-id>.jsonl       ← 会话账本
~/.openx/sessions/<workspace-hash>/<session-id>.ckpt.json   ← checkpoint
```

旁挂文件承载**快照体**——当前回合的消息（与 provider 当时看到的逐字节一致）、工具轮数、todos 与 token 累计。账本承载**事实**：每次落盘一条 `checkpoint` 事件，记下 phase、reason 与快照摘要。两者互相引用，所以任意一侧被改动或截断都能被发现。

账本也因此保持精简：一份 checkpoint 的体量可能包含几 MB 的工具输出，而 web 回放会把每条账本 payload 原样推给前端——把快照塞进事件流会让每次回放都被撑爆。

## 为什么已完成的调用绝不重放

checkpoint 只在某一轮的**全部**工具结果都追加进消息列表**之后**才提交。那一刻每个 `tool_call` 都已有一条配对的 `tool` 结果，于是消息日志本身就是幂等单元——循环里没有任何"跳过表"。恢复时带着这段前缀重入循环，模型不可能再发起一个已应答的调用。

每轮落两次盘：

| phase | 写入时机 | 含义 |
|---|---|---|
| `inflight` | 工具执行之前 | 这些调用正在跑，结果未知 |
| `committed` | 结果追加之后 | 该轮已完整收口，安全 |

如果进程死在工具**执行途中**，在途调用的结果是真的未知——文件可能已经写了、请求可能已经发出。恢复不做猜测、也不重试：它为每个这样的调用合成一条 `[status: interrupted]` 结果，让模型自己看到发生了什么并决定下一步。这样"不重放"是绝对的承诺，而不是近似。

## 什么时候落 checkpoint

| 触发 | 结果 |
|---|---|
| 每个工具轮 | 提交一次 checkpoint |
| 回合正常结束 | 删除旁挂文件——"没有陈旧 checkpoint"是默认状态 |
| Ctrl-C / `SIGTERM` / Esc / web 打断 | 进程退出前把在途回合落盘 |
| 工具轮数触顶 | 记一条 `resource_gate_tripped` 事件 + 一份可续跑的 checkpoint |

因为每轮之后 checkpoint 已经在盘上，`kill -9` 与断电都能恢复到最近一个已完成的轮次，完全不依赖信号处理。信号只额外补上当前这一轮的部分进展，以及"为什么停下"的审计记录。

## 用不了的 checkpoint

恢复绝不挡住启动。若 checkpoint 缺失、陈旧、被截断、属于别的会话或工作区，或者是在另一份系统提示下写的（期间有插件装卸），它会被弃用，会话照常恢复、只是丢掉被打断的那一轮。`--recover-mode` 决定这件事的动静：

| 模式 | 行为 |
|---|---|
| `auto`（默认） | 警告一句，然后用已保存的会话继续 |
| `strict` | 拒绝并退出 1——给"必须能续跑"的脚本用 |
| `drop` | 静默丢弃 |

若 checkpoint 描述的那一轮其实已经写进会话文件（崩在持久化与删旁挂文件之间），它也会被无声丢弃——那正是这种崩溃时序的正常结果。

## 写一个观察 checkpoint 的插件

插件可以注册 `on_checkpoint` / `on_resume` 生命周期钩子：

```python
ctx.register_lifecycle(
    "my-state",
    on_checkpoint=lambda payload: flush_my_state(),
    on_resume=lambda payload: reload_my_state(),
)
```

payload 是一个 dict：checkpoint 侧含 `reason`、`phase`、`ledger_seq`、`tool_rounds`、`session_id`、`workspace`；resume 侧另含 `repaired_calls`、`todos`、`counters`。零参钩子照常工作——只有当钩子收得下 payload 时才会传。

这些钩子是**观察者 + 自我落盘点，不是修改器**：不得写 checkpoint（那是内核的活）、不得改 `todos`，且必须快速返回——这条路径在回合的关键路径上。在 `SIGINT` 路径上，异步钩子可能来不及跑完进程就退出了；需要持久化的东西请在此时由插件自己写到自己的位置。

## 限制

- checkpoint 上限 4 MiB。超限的快照**拒绝落盘，绝不裁剪**——被裁剪的快照重建出的 prompt 会和模型当时看到的不是同一份。此时恢复锚在上一轮。
- 写盘失败一律静默处理，绝不打断回合；持久化是优化，不在关键路径上。
- 恢复是显式选择：回合绝不会被自动续跑，因为续跑会重新发起模型请求。
- 子代理不做 checkpoint——它们的中间过程本就不该进会话文件。
- checkpoint 不是会话账本的替代品：账本是 append-only 的审计真源，旁挂文件是它的可续跑索引。

## 参见

- [会话](../user/guide/sessions.zh.md)——会话持久化与 `--continue` / `--resume`
- [模式与权限](../user/guide/modes-permissions.zh.md)——容灾不改变工具能做什么
- [Hooks](hooks.zh.md)——面向用户的 shell 钩子链
- [后台任务](background-tasks.zh.md)——跨回合存活的分离工作
