# 组合输入

[English](composition.md) | 中文

内核不装载「能找到的所有插件」，而是装载**计算出的应载清单（bundle）**——
当前工作区的*组合输入*解析结果。这让每次会话的插件集可复现、可审计。

```
model_profile（按模型版本的能力面）
  × 用户 overlay    （~/.openx/openx.json）    补丁原语
  × 项目 overlay    （<ws>/.openx/openx.json） 同原语
  = 应载清单（计算）—— loader 只装载清单内插件
```

```bash
/composition        # 人读：应载清单 + 跳过项 + 原因
/composition json   # 结构化摘要（kernel.composition_summary）
```

## 三份输入

**模型档案**——`~/.openx/profiles/<name>.json` 下的具名 JSON 档，由
`settings.json` 的 `plugins.profile` 选中：

```json
{ "name": "gpt-5", "retire": ["histcompact"], "require": [] }
```

`retire` 标记该模型不再需要的脚手架（跳过硬装载，代码与注册仍在）；`require`
把它们重新纳入。档案是*派生*输入，改档即重算应载清单——消融线的「降级自动
回挂」由此天然获得。档案**不覆盖**用户经账本*裁决*的退场（那须
`/scaffolds restore`）。

**用户 / 项目 overlay**——作用于档案结果的补丁原语：

```json
{ "plugins": { "enable": ["auto-greet"], "disable": ["noisy"] } }
```

- 同键冲突：**用户级赢项目级**。
- `settings.json` 的 `plugins.disabled` 是用户级 `disable` 的旧形态；双读
  （并集生效），但写只走 overlay——没有两个并存的写真相源。
- `add` / `remove` / `replace` 解析并记录，暂为保留原语。

## 默认

- **空 overlay ≡ 现状。** 无 overlay、无档案时，应载清单 = 内置插件 + 目录里
  发现的全部插件。
- **`auto-*` 默认排除。** 模型自产插件（`auto-*`）不进 boot 组合，除非被
  enable——这正是「先 session 后 persistent」的兑现：`promote_plugin` 把插件
  写进用户 overlay 的 `enable`，此后它才跨重启存活。见下文「晋升与回滚」。

## 晋升与回滚（E7）

对一个 `auto-*` 插件执行 `promote_plugin`：

1. 在全局账本记 `plugin_promoted` 决策（跨会话事实）；
2. 置 `trust=user` 与 `scope=persistent`；
3. **把插件写进用户 overlay 的 `enable`**——故下次 boot 它进应载清单。

回滚即卸载：对 persistent 插件调用 `unload_plugin` 会从 overlay 的 `enable`
摘除它并记 `plugin_rolled_back`；下次 boot 回到出厂默认（不在清单内）。

## 账本

每次**实际重组**都追加一条 `composition_resolved` 事件，携带档案名、应用的
overlay 操作、最终应载清单与跳过原因——任何一次会话的组合都能事后复现。

## 限制

- **JSON 而非 YAML。** overlay 用 JSON 以保持 OpenX 零额外依赖；设计草图原写
  `.yml`。
- **`add` / `remove` / `replace` 保留。** 目前只消费 `enable` / `disable`
  （已覆盖插件级组卷）。
- **档案是声明式的。** OpenX 不探测模型能力面；档案由你撰写。

## 参见

- [脚手架](scaffolds.zh.md)——退场声明与评测门（E1/E4）
- [自演进设计](../design/openx-self-evolution-design.md)——§1.1 环⑤
