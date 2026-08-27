# 架构

[English](architecture.md) | 中文

OpenX 是一个单一的 Python 包（`openx/`），配套根目录下的 `tests/` 测试套件。本页是模块树、运行时循环与分层职责的有序地图——修改 `openx/` 之前请先阅读。

## 模块树

```
openx/
├── openx/
│   ├── main.py            # CLI 入口：参数、setup wizard、信任检查、会话、分发
│   ├── agent.py           # 核心 agent loop（流式 + 非流式、plan mode、subagents）
│   ├── config.py          # 配置：settings.json + config 文件 + 环境变量合并
│   ├── permissions.py     # 权限分级 + 已存储的 allow/deny 规则
│   ├── memory.py          # 持久记忆（~/.openx/memory/）
│   ├── instructions.py    # OPENX.md 加载（全局 / 项目 / 子目录）
│   ├── image.py           # 图片与剪贴板辅助（多模态）
│   ├── cli/
│   │   ├── commands.py    # 斜杠命令注册表（27 条命令）
│   │   ├── interactive.py # REPL 循环 + 流式显示
│   │   ├── single_shot.py # 单次模式
│   │   └── setup_wizard.py# 首次运行配置向导
│   ├── kernel/            # 微内核：插件注册目录/注册表/加载器/清单/记账
│   │   └── registrations.py 等
│   ├── builtin/           # base bundle 内置插件包（tools/providers，"一切能力皆插件"）
│   │   ├── tools.py       #   内置工具工厂
│   │   └── providers.py   #   内置 provider 实现（openai-compat / anthropic）
│   ├── core/
│   │   ├── protocol.py    # 会话协议 schema + 事件信封（账本 = 协议外化）
│   │   ├── history.py     # 对话历史 + 基于轮次的压缩（compaction）
│   │   ├── hooks.py       # 用户 hooks（Claude Code 风格的 schema）
│   │   ├── sessions.py    # 会话持久化（JSONL，--continue / --resume）
│   │   ├── tasks.py       # 后台任务注册表
│   │   ├── subagent.py    # Subagent 定义（内置 + .openx/agents/*.md）
│   │   └── workflow.py    # Workflow 引擎（确定性多 agent 编排）
│   ├── llm/
│   │   └── client.py      # 异步 LLM 客户端（OpenAI 兼容、流式）
│   ├── mcp/
│   │   ├── transport.py   # stdio NDJSON 传输（spawn + 行分帧）
│   │   ├── client.py      # 零依赖 JSON-RPC 客户端
│   │   ├── tools.py       # MCPTool 包装（mcp__<server>__<tool>）
│   │   └── manager.py     # server 生命周期 + 配置加载
│   ├── tools/
│   │   ├── base.py        # Tool 基类 + 结果类型
│   │   ├── file_tools.py  # read_file、write_file、edit_file、glob、list_directory
│   │   ├── shell_tools.py # shell（支持 run_in_background）
│   │   ├── search_tools.py# grep
│   │   ├── git_tools.py   # git_status、git_diff、git_log、git_branch
│   │   ├── todo_tools.py  # todo_write
│   │   ├── web_tools.py   # web_fetch、web_search
│   │   ├── ask_user_tool.py # ask_user
│   │   ├── plan_tools.py  # exit_plan_mode
│   │   ├── mode_tools.py  # choose_mode（manual → auto/plan 选择）
│   │   ├── task_tools.py  # task_output、task_stop
│   │   ├── subagent_tool.py # task（委托给 subagent）
│   │   └── workflow_tool.py # workflow（运行编排脚本）
│   ├── services/
│   │   ├── tool_executor.py # 权限 + hook 门控，串行准备 → 并行执行
│   │   ├── streaming.py   # 流式显示服务
│   │   └── exploration.py # 项目概览探测
│   ├── ui/                # Rich TUI：console、内嵌输入框、对话框、输入捕获
│   └── utils/             # 路径、文本、错误辅助
├── tests/                 # 子目录与 openx/ 布局对应（core/ llm/ services/ tools/ ui/ mcp/）
├── docs/
├── pyproject.toml
└── README.md
```

## 一轮对话如何运转

1. **用户发送消息**——自然语言请求。
2. **Agent 探索**——读文件、搜代码、列目录。
3. **Agent 规划**——用 LLM 推理决定下一步。
4. **Agent 执行**——调用工具；相互独立的调用在串行准备（解析、校验、权限询问）之后，经 `asyncio.gather` 并行执行。
5. **循环**——把结果喂回 LLM，直到任务完成。
6. **回复**——带执行摘要的最终文本回复。

权限检查与 hook 触发发生在 `services/tool_executor.py` 的串行准备阶段；只有执行环节才展开并行。

## 分层

| 层 | 模块 | 职责 |
|---|---|---|
| 产品表面 | `cli/`、`ui/` | REPL、单次与 headless 入口；终端渲染 |
| 内核 | `kernel/`（微内核）、`agent.py`、`services/tool_executor.py`、`services/streaming.py` | turn 循环、工具分发（串行准备 → 并行执行）、流式显示 |
| 模型层 | `llm/` | OpenAI 兼容异步客户端、流式、带退避的重试 |
| 能力层 | `tools/`、`mcp/` | 面向模型的工具（fs、shell、搜索、git、web、todo、plan、task、workflow）与外部 MCP 工具 |
| 上下文与记忆 | `instructions.py`、`memory.py`、`core/history.py` | OPENX.md 指令、持久记忆、历史 + 压缩 |
| 编排层 | `core/subagent.py`、`core/workflow.py`、`core/tasks.py`、`core/hooks.py` | subagents、确定性 workflows、后台任务、生命周期 hooks |
| 状态层 | `core/sessions.py`、`config.py` | 会话持久化/恢复、分层配置 |
| 协作层 | `permissions.py` | 权限分级、已存储规则、危险命令门控 |

## 参见

- [开发指南](development.zh.md)——贡献者环境与日常工作流
- [用户指南](user/index.zh.md)——命令、模式与权限、配置、会话
- [子系统参考](subsystems/README.zh.md)——subagents、workflows、hooks、MCP、后台任务
