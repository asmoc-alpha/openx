# 脚手架

[English](scaffolds.md) | 中文

**脚手架**是只为补偿模型短板而存在的模块——历史压缩、意图路由、子代理、记忆
检索、harness loop 本体。设计押注这些会随模型变强而退场；脚手架声明**它为何
存在**与**何时该走**，条件命中即退出应载集合——但**不是删除**。

## 声明脚手架

脚手架在插件 manifest（`__openx_meta__`）里声明一个 `scaffold` 块：

```python
__openx_meta__ = {
    "type": "capability.tool",
    "trust": "user",
    "summary": "压缩长历史",
    "scaffold": {
        "compensates": "模型上下文有限",              # 必答
        "exit_when": "长会话免压缩评测通过",           # 必答
        "eval_set": "evals/long-session.jsonl",       # 可选
        "fallback": "reinstall-on-regression",        # 可选
    },
}
```

`compensates` 与 `exit_when` 是**必答项**——答不出两者的模块没有资格以脚手架
身份存在：要么进内核，要么就是普通能力插件。内核只校验块的**形状**：必答项
缺失即拒载；`fallback` 值不在词汇表、块内有未知键只警告（与 `type` / `mount` /
`permissions` 同纪律）。`eval_set` 指向退场评测任务集；`fallback`
（`reinstall-on-regression`）说明模型降级时脚手架如何回挂。

## 退场与回挂

```
/scaffolds                    # 列全部脚手架及退场状态
/scaffolds retire <name>      # 退场：记 scaffold_retired + 组合跳过
/scaffolds restore <name>     # 回挂：记 scaffold_restored + 重新纳入
```

退场是**用户确认**的动作（模型绝不静默执行）。三步都留痕：

1. **评测门**——在 `eval_set` 上对比**带 vs 摘除**：摘除须不掉任务成功率，
   掉点则保持。对比结论成为决策的证据。
2. **决策**——`scaffold_retired`（或 `scaffold_restored`）事件落**全局账本**
   （`~/.openx/ledger.jsonl`），带声明、证据与归因——"这个模块为什么没了"
   只有一个权威答案。
3. **摘除**——下次组合跳过该脚手架（不导入），但代码与注册仍在。回挂只差
   一条事件。

**摘除不是删除。** 退场脚手架保留文件、仍可被发现，`/plugins` 显示为
`retired`；`/scaffolds restore <name>` 一键放回。退场集合从全局账本折叠而来，
故决策跨重启存活——新进程从账本折出同一退场集合。

## 限制

- **声明不是证据。** 内核只校验块的形状；退场由评测门的成功率对比决定。本版
  落地决策、退场登记与评测门的**策略**；真正**跑** `eval_set` 里的任务（产出
  带/摘除两侧度量）是独立切片。
- **暂无档案联动自动回挂。** 模型降级自动回挂需要模型能力档案（P6）；当前
  `/scaffolds restore` 是人工路径。
- **仅限脚手架。** 只有声明了 `scaffold` 块的插件可退场；普通能力插件无退场
  通道。

## 参见

- [扩展 OpenX](../cookbook/extending.zh.md)——在自定义插件上声明脚手架
- [架构](../architecture.zh.md)——模块布局
