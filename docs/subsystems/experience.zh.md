# 经验沉淀

[English](experience.md) | 中文

会话账本不只是审计轨迹——它是**经验**的原材料。沉淀闭环把它变成持久记忆，
让一次学到的教训存活到下一次会话：

```
会话账本（事件流）
  → 提炼：挖出候选经验
  → 写入：落进 coding memory（人确认）
  → 召回：并入下一次会话的系统提示
  → 回账：记录哪些记忆被召回（memory_recall 事件）
```

```bash
/distill            # 最近会话的候选经验（人读）
/distill json       # 结构化报告（schema openx-distill-report/v1）
/distill save       # 把候选落进 coding memory（source=distill）
/distill recall      # 召回回账：哪条记忆被召回过、几次
```

## 提炼什么

`/distill` 扫最近的会话，挖三类**候选**经验（保守启发式——宁可漏报不可杜撰）：

| 类别 | 信号 | 变成 |
|---|---|---|
| `workflow` | 反复成功的 shell 命令 | *"常用命令：`pytest -q`"* |
| `debug_pattern` | 某工具先报错、后成功 | *"`grep` 曾因 … 失败——调整参数重试"* |
| `project_fact` | 反复编辑的文件 | *"高频编辑文件：…"* |

只跑一次的命令被忽略；候选去重并按类序、频次排序。候选是**建议不是事实**——
未经你确认，什么都不落库。

## 写入（人确认）

`/distill save` 把候选写进 coding memory，打上 `source="distill"`。这个来源标记
让"提炼得来"的记忆与模型自主写入（`source="agent"`）、用户写入者区分开来，日后
可审阅、可按来源批量清理。去重由记忆库自己完成（同内容原地更新）。

## 召回回账

agent 每次组系统提示时，实际并入的每条 coding memory 都记一条 `memory_recall`
会话账本事件（含 id、分类、字符数）。`/distill recall` 把这些事件汇总成频次榜
——回答"记忆质量"问题*"被频繁召回的记忆真的有用吗？"*的数据源。事件只如实记录
召回事实，不做判断。

## 限制

- **是证据不是动作。** 提炼只提议，处置交给人（`/distill save`）。对会话账本
  只读。
- **启发式且保守。** 类别从工具轨迹里挖；只出现一次的命令/文件不成候选。预期
  是漏报，不是噪音。
- **是召回记账，不是召回打分。** `memory_recall` 说明注入了什么；一条记忆是否
  *有用*是后续分析。
- **写入进 home。** `/distill save` 写 `~/.openx/coding-memory/`，绝不进项目
  `.openx`。

## 参见

- [离线分析](offline-analysis.zh.md)——姊妹报告（缺口、装配）
- [自演进设计](../design/openx-self-evolution-design.md)——§4.4
