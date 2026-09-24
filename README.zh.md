<div align="center">

<img src="assets/logo.svg" alt="OpenX mascot" width="160">

# OpenX

</div>

[![tests](https://github.com/asmoc-alpha/openx/actions/workflows/test.yml/badge.svg)](https://github.com/asmoc-alpha/openx/actions/workflows/test.yml)

[English](README.md) | 中文

**可验证、可演进的 agent runtime——用 LLM 与你的代码库对话。**

OpenX 是一个用 Python 构建的开源终端 agent runtime，目标是一个**可验证、可演进**的运行时：信任基座（微内核：编排 · 沙箱执行 · 插件维护 · 记账）保持最小且可审计，工具、命令、上下文与 provider 协议都落在可替换的插件里，agent 能在运行中扩展自身。用自然语言交给它任务，它会读取、编写、修改、搜索你的代码——并带有权限控制、会话、subagents、workflows 和 MCP 支持。

## 特性

- 🧠 Agentic loop，工具并行执行，自动带退避重试
- 🛡️ 三种权限模式：`manual` / `auto` / `plan`——复杂任务会自己切进计划模式，先把计划交给你审批再动手
- 🤖 Subagents——内置类型 + 自定义 `.openx/agents/*.md`，可选结构化输出
- 🔁 Workflows——用 Python 脚本做确定性多 agent 编排
- 🧩 插件——工具、命令、上下文与 provider 可在运行中装卸；agent 能自己编写、测试并提升插件。装卸、编写与提升都会先征求你的批准
- 🪝 Hooks——Claude-Code 兼容的 shell hooks，按会话阶段插入（SessionStart/UserPromptSubmit/工具前后/子代理收尾/PreCompact/Stop/SessionEnd）
- 🔌 MCP——stdio servers，零额外依赖
- 💾 会话——持久化与恢复（`--continue` / `--resume`）；回合级 checkpoint 会在每个工具轮落盘，`--recover` 可接回被崩溃、`kill` 或 Ctrl-C 打断的那一轮（需显式开启，绝不自动恢复）
- 🌐 Web 端——`openx serve` 提供浏览器对话、远程计划/权限审批与会话回放（需 `OPENX_EXTRAS=web`）
- 🌗 后台任务 · 🗜️ 自动压缩（compaction） · 🧠 持久记忆
- 📤 面向 CI 的 headless JSON 输出（`--output-format json` / `stream-json`）
- 🖼️ 图片分析 · 🎨 带 markdown 与语法高亮的 rich 终端 UI——连续只读工具调用折成一行摘要（`Ctrl+T` 展开）
- 🔐 三级权限系统 · 📁 工作区边界
- ⚙️ 兼容任意 OpenAI 兼容 API；Anthropic 兼容端点经可选 `anthropic` extra 启用

## 安装

```bash
curl -fsSL https://raw.githubusercontent.com/asmoc-alpha/openx/main/install.sh | bash
```

安装脚本按 pipx → `uv` → `~/.openx/venv` 虚拟环境的顺序选择，并在结束时自检。设 `OPENX_EXTRAS=web` 可一并装上 Web 端（`curl … | OPENX_EXTRAS=web bash`）；设 `OPENX_REF=<git-ref>` 可改装其它版本（默认钉在发布 tag `v0.1.2`）。

从 clone 安装亦可：

```bash
git clone https://github.com/asmoc-alpha/openx.git
cd openx
pip install -e .
```

也可以直接从 git 源钉版本安装——安装脚本内部走的就是这条：

```bash
pip install "openx @ git+https://github.com/asmoc-alpha/openx.git@v0.1.2"
```

> **不要用 `pip install openx`。** PyPI 上的 `openx` 是另一个无关项目，OpenX 只通过上面的 git 源分发。

需要 Python ≥ 3.10。首次运行时，`openx` 会启动交互式 setup wizard，写入一个 `default` 模型组到 `~/.openx/settings.json`。模型/凭据配置**只**在该文件的 `modelGroups` 块里——每个组共享一套 key/端点，并可定义逐角色模型（`main`/`exec`/`mini`/`modal`）。key 可用 `env:VAR` 引用环境变量：

```bash
export OPENAI_API_KEY=sk-your-key-here   # 组里写 "apiKey": "env:OPENAI_API_KEY"
```

schema 见 [配置](docs/user/guide/configuration.zh.md)。

## 快速开始

```bash
openx                                          # 交互式 REPL
openx "add type hints to all functions in src/"   # 单次模式
openx --workspace /path/to/project "explain this codebase"
openx --continue                               # 恢复最近一次会话
openx --continue --recover                     # 接回被崩溃/Ctrl-C 打断的那一轮
openx serve                                    # Web 端（需 OPENX_EXTRAS=web）
openx "fix the failing test" --output-format json   # headless / CI
```

`openx --version` 打印版本。模型选择是斜杠命令（`/model`），不是启动参数——`--model` 已在 0.1.1 移除。

全部参数与斜杠命令见 [docs/user/guide/commands.zh.md](docs/user/guide/commands.zh.md)。

## 文档

| 页面 | 内容 |
|---|---|
| [用户指南](docs/user/index.zh.md) | 命令、模式与权限、配置、会话 |
| [子系统参考](docs/subsystems/README.zh.md) | subagents、workflows、后台任务、hooks、MCP、脚手架、离线分析、容灾 |
| [Web 端（openx serve）](docs/openx-serve.md) | 浏览器对话、远程审批、会话回放 |
| [架构](docs/architecture.zh.md) | 模块树与运行时循环 |
| [开发指南](docs/development.zh.md) | 贡献者环境、测试、lint |
| [Cookbook](docs/cookbook/extending.zh.md) | 用自定义 tool 扩展 OpenX |
| [对比](docs/comparison.zh.md) | OpenX 与 Claude Code 的对比 |
| [Changelog](openx/CHANGELOG.md) | 发布历史（`/release-notes` 的数据源） |

## 开发

从[开发指南](docs/development.zh.md)和[架构文档](docs/architecture.zh.md)开始。

Agent 请遵循 [docs/AGENTS.md](docs/AGENTS.md)。

## 贡献

环境搭建与 PR 流程见 [CONTRIBUTING.zh.md](CONTRIBUTING.zh.md)。欢迎 PR！可以帮忙的方向：

- 更多工具（lint、测试框架、包管理器）
- Notebook 编辑支持
- 更多 provider 协议族（内置 openai-compat 与 anthropic-compat）
- HTTP/SSE MCP 传输（目前仅 stdio）
- 插件进程隔离——插件代码目前仍在进程内运行

## 许可证

MIT——见 [LICENSE](LICENSE)。
