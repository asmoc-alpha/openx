<div align="center">

<img src="assets/logo.svg" alt="OpenX mascot" width="160">

# OpenX

</div>

[![tests](https://github.com/asmoc-alpha/openx/actions/workflows/test.yml/badge.svg)](https://github.com/asmoc-alpha/openx/actions/workflows/test.yml)

[English](README.md) | 中文

**Agentic coding CLI——用 LLM 与你的代码库对话。**

OpenX 是一个用 Python 构建的开源终端 coding agent。用自然语言交给它任务，它会读取、编写、修改、搜索你的代码——并带有权限控制、会话、subagents、workflows 和 MCP 支持。

## 特性

- 🧠 Agentic loop，工具并行执行，自动带退避重试
- 🛡️ 三种权限模式：`manual` / `auto` / `plan`
- 🤖 Subagents——内置类型 + 自定义 `.openx/agents/*.md`，可选结构化输出
- 🔁 Workflows——用 Python 脚本做确定性多 agent 编排
- 🪝 Hooks——Claude-Code 兼容的 shell hooks，覆盖工具调用、prompt 与 stop
- 🔌 MCP——stdio servers，零额外依赖
- 💾 会话——持久化与恢复（`--continue` / `--resume`）
- 🌗 后台任务 · 🗜️ 自动压缩（compaction） · 🧠 持久记忆
- 📤 面向 CI 的 headless JSON 输出（`--output-format json` / `stream-json`）
- 🖼️ 图片分析 · 🎨 带 markdown 与语法高亮的 rich 终端 UI
- 🔐 三级权限系统 · 📁 工作区边界
- ⚙️ 兼容任意 OpenAI 兼容 API（多 provider）

## 安装

```bash
git clone https://github.com/asmoc-alpha/openx.git
cd openx
pip install -e .

# 或从 PyPI（即将上线）
pip install openx
```

需要 Python ≥ 3.10。首次运行时，`openx` 会启动交互式 setup wizard，并把答案保存到 `~/.openx/settings.json`。也可以通过环境变量配置：

```bash
export OPENAI_API_KEY=sk-your-key-here

# 可选：使用其他 OpenAI 兼容 provider
export OPENAI_API_BASE=https://api.openai.com/v1
export OPENX_MODEL=gpt-4o
```

## 快速开始

```bash
openx                                          # 交互式 REPL
openx "add type hints to all functions in src/"   # 单次模式
openx --model gpt-4o --workspace /path/to/project "explain this codebase"
openx --continue                               # 恢复最近一次会话
openx "fix the failing test" --output-format json   # headless / CI
```

全部参数与斜杠命令见 [docs/user/guide/commands.zh.md](docs/user/guide/commands.zh.md)。

## 文档

| 页面 | 内容 |
|---|---|
| [用户指南](docs/user/index.zh.md) | 命令、模式与权限、配置、会话 |
| [子系统参考](docs/subsystems/README.zh.md) | subagents、workflows、后台任务、hooks、MCP |
| [架构](docs/architecture.zh.md) | 模块树与运行时循环 |
| [开发指南](docs/development.zh.md) | 贡献者环境、测试、lint |
| [Cookbook](docs/cookbook/extending.zh.md) | 用自定义 tool 扩展 OpenX |
| [对比](docs/comparison.zh.md) | 与 Claude Code 的功能对齐 |
| [Changelog](openx/CHANGELOG.md) | 发布历史（`/release-notes` 的数据源） |

## 开发

从[开发指南](docs/development.zh.md)和[架构文档](docs/architecture.zh.md)开始。

Agent 请遵循 [docs/AGENTS.md](docs/AGENTS.md)。

## 贡献

环境搭建与 PR 流程见 [CONTRIBUTING.zh.md](CONTRIBUTING.zh.md)。欢迎 PR！可以帮忙的方向：

- 更多工具（lint、测试框架、包管理器）
- Notebook 编辑支持
- Anthropic 原生 API 格式（目前仅 OpenAI 兼容）
- HTTP/SSE MCP 传输（目前仅 stdio）
- 插件系统

## 许可证

MIT——见 [LICENSE](LICENSE)。
