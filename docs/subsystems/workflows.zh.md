# Workflows

[English](workflows.md) | 中文

Workflow 是**确定性**编排多个子 agent 的 Python 脚本——扇出搜索、并行评审、分阶段流水线——用普通 Python 控制流代替模型即兴发挥（Claude Code Workflow 工具的 Python 原生适配）。一个 workflow 定义可选的 `meta` 字典和一个 async `main` 入口，入口接收五个 hook：

| Hook | 签名 | 行为 |
|------|-----------|----------|
| `agent` | `await agent(prompt, label=None, phase=None, subagent_type="general-purpose", schema=None)` | 运行一个子 agent，返回其最终文本——给了 `schema`（JSON Schema）时，返回子 agent 通过 `structured_output` 交付的**校验过的 Python 对象**（失败或 schema 未满足时为 `None`） |
| `parallel` | `await parallel([lambda: agent(...), ...])` | 屏障：thunk 并发执行，结果按原顺序返回，失败的 thunk → `None` |
| `pipeline` | `await pipeline(items, stage1, stage2, ...)` | 每个 item 独立流过所有 stage（stage 之间**没有屏障**）；stage 以 `stage(prev_result, original_item, index)` 被调用，失败的 item → `None` |
| `phase` | `phase(title)` | 记录一个阶段标记（统计 + 暗色进度行） |
| `log` | `log(message)` | 输出一行暗色进度信息 |

保存的 workflow 位于 `<workspace>/.openx/workflows/<name>.py`：

```python
# .openx/workflows/review.py
meta = {
    "name": "review",
    "description": "Review changed files and verify findings",
    "phases": [{"title": "Review"}, {"title": "Verify"}],
}

async def main(agent, parallel, pipeline, phase, log, args):
    phase("Review")
    findings = await parallel([
        lambda: agent("Review module X for bugs", label="review:x"),
        lambda: agent("Review module Y for bugs", label="review:y"),
    ])
    phase("Verify")
    verified = await pipeline(
        [f for f in findings if f],
        lambda f, orig, i: agent(f"Verify this finding: {orig[:200]}"),
    )
    log(f"done: {len([v for v in verified if v])} verified")
    return {"findings": findings, "verified": verified}
```

用 `/workflow review` 运行（裸 `/workflow` 列出已保存的），或让 agent 通过 `workflow` 工具内联运行：`script` = 内联源码，`name` = 已保存的 workflow，可选 `args`（任意 JSON）原样传给 `main`。工具把 `main` 的返回值以 JSON 返回，外加统计页脚（agent 运行/失败数、tokens、耗时）。

## 语义与限制

- **并发**上限为 `max(2, min(16, cpu_count − 2))` 个并发 agent（Claude Code 的公式），并有单次运行 500 个 agent 的兜底，失控脚本会被中止。
- **信任**：workflow 脚本在无沙箱、完整本地权限下运行——与 shell 同级信任。`workflow` 工具执行前总是请求许可。
- 同一次运行的所有并发子 agent 共享一把 prompt 锁，交互式权限询问不会在终端上重叠。
- **v1 限制**：暂无断点续跑缓存、暂无 budget 对象。

## 参见

- [Subagents](subagents.zh.md)——每次 `agent(...)` 调用 spawn 的是什么
- [后台任务](background-tasks.zh.md)——shell 侧的异步工作对应物
