# 技能（Skills）

[English](skills.md) | 中文

一个技能是一个**目录**，内含一个 `SKILL.md`（frontmatter + 指令正文），可附带任意
supporting files（脚本、模板、参考材料）。布局遵循开放的 **Agent Skills** 标准，
因此用 `SKILL.md` 格式编写的技能可直接装载（只读互操作），包括 Claude 的技能目录。

```
~/.openx/skills/<name>/SKILL.md            # 个人级（所有项目可用）
<workspace>/.openx/skills/<name>/SKILL.md  # 项目级（提交即可共享）
```

```markdown
---
name: docker-expert
description: Best practices for Dockerfile and compose files.
allowed-tools: read_file grep
license: MIT
---
When working with Docker files, always:
- Use multi-stage builds ...
```

frontmatter 由极简手写解析器读取（无 PyYAML）：每行一个 `key: value`。`allowed-tools`
可用逗号或空白分隔；`trigger`、`license`、`argument-hint` 可选。**目录名即命令名**——
`name:` 仅在旧扁平布局下作为回退。

## 渐进披露

系统提示只接收一份**目录**——每个技能的名称、描述，以及预批准工具 / legacy 标记。
技能的**正文绝不注入**。正文在技能真正被用到时才按需加载：

- 用户输入 `/<skill-name>`（动态命令），或
- 模型调用 `skill` 工具。

因此长参考材料在被用到之前不占上下文。正文里的 `$ARGUMENTS` 会被替换为用户
（或模型）传入的内容。

正文还可以用 `` !`cmd` `` 请求**动态上下文注入**。命令在加载期只被抽取、**绝不执行**——
它们在技能激活时经正常工具闸运行（`shell`，即 Guard / 权限 / 钩子 / 账本），
其输出替换占位符。注入失败降级为一行说明，绝不阻塞技能加载。

## 预批准工具（`allowed-tools`）

激活技能会为每个列出的工具挂上一条**会话作用域**的 allow 规则（`<tool>(*)`）。
该规则只在内存中——**绝不写入 `settings.json`**，故「装一个技能」永远不会永久放宽
权限——并在技能被停用、卸载或技能重载时摘下。`deny` 规则仍然优先，高风险工具
仍然恒弹窗确认。

## 优先级

来源按低 → 高叠加；同名技能由更高来源胜出（项目 > 个人、本家 > 互操作、
目录布局 > 旧扁平）：

| 来源 | 级别 |
|---|---|
| `~/.claude/skills/<name>/SKILL.md` | `claude`（互操作，只读） |
| `<ws>/.claude/skills/<name>/SKILL.md` | `claude-project`（互操作，只读） |
| `~/.openx/skills/<name>.md` | `global`（旧扁平） |
| `<ws>/.openx/skills/<name>.md` | `project`（旧扁平） |
| `~/.openx/skills/<name>/SKILL.md` | `global` |
| `<ws>/.openx/skills/<name>/SKILL.md` | `project` |

## 管理技能

| 命令 | 说明 |
|---|---|
| `/skill`（别名 `/skills`） | 列出已安装技能：级别、描述、预批准工具、触发词 |
| `/skill add` | 交互式创建（名称、描述、触发词、预批准工具、正文、范围） |
| `/skill install <path>` | 从技能目录、`SKILL.md` 或旧扁平 `.md` 安装（迁移为目录布局，附带文件一并复制）。加 `--project` 装到项目级 |
| `/skill show <name>` | 打印技能的元信息与正文——**不**执行注入命令 |
| `/skill remove <name>` | 卸载（项目级优先，其次个人级） |

任何已安装技能同时成为一条 `/<skill-name>` 命令，模型则可用 `skill` 工具加载它。
内置命令名绝不被劫持——名为 `help` 的技能不会覆盖 `/help`（改用 `/skill show help`）。
Web 端在「设置 → Skills」暴露同一套能力（`GET` / `POST` / `DELETE /api/skills`）。

## 限制与保证

- **名称**：`[a-z0-9]+(-[a-z0-9]+)*`，最长 64 字符。
- **描述**：截断到 1024 字符。
- 只有目录进系统提示；正文按需加载。
- 损坏的技能文件打印警告并跳过——**技能装载绝不拖垮启动**。
- 旧扁平布局（`<name>.md`）仍可读取（标记 `legacy`），重新安装时迁移为 `<name>/SKILL.md`。

## 实现

`openx/skills.py`——解析、装载、目录、安装/卸载。`openx/tools/skill_tool.py`——
`skill` 工具（模型侧入口）。`openx/agent.py`——`activate_skill` / `deactivate_skill` /
`reload_skills`（`/<name>` 与工具共用的同一实现）。

## 另见

- [命令](../user/guide/commands.zh.md)——完整的斜杠命令清单
- [架构](../architecture.zh.md)——技能在目录树中的位置
