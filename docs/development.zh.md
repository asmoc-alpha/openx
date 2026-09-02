# 开发指南

[English](development.md) | 中文

贡献者环境与日常工作流。

## 环境准备

OpenX 需要 Python ≥ 3.10。

```bash
git clone https://github.com/asmoc-alpha/openx.git
cd openx
pip install -e ".[dev]"
```

editable 安装会把 `openx` 命令放进 PATH，并拉入 `dev` 附加依赖：pytest、pytest-asyncio、ruff。

## 运行 OpenX

```bash
openx                      # 交互式 REPL
openx "fix the failing test"   # 单次模式
```

首次运行会启动 setup wizard（API key、模型），答案保存在 `~/.openx/settings.json`。参见[配置](user/guide/configuration.zh.md)。

## 测试

```bash
pytest                                        # 全套
pytest tests/tools/test_tools_base.py         # 单文件
pytest tests/tools/test_tools_base.py -k edit # 按名称过滤
```

收集范围限定在 `tests/`（`pyproject.toml` 的 `testpaths`），async 测试无需装饰器（`asyncio_mode = "auto"`）。测试文件按与 `openx/` 包结构对应的子目录组织（`tests/orchestration/`、`tests/kernel/`、`tests/serve/`、`tests/llm/`、`tests/services/`、`tests/tools/`、`tests/ui/`、`tests/mcp/`）；顶层模块（`agent`、`main`、`image`、`instructions`）的测试放在 `tests/` 根目录。

## Lint

```bash
ruff check openx tests
```

行宽 100，目标版本 py310（`pyproject.toml` 的 `[tool.ruff]`）。

## 依赖约束：rich

流式显示依赖 rich 的私有接口（`Live._lock`、`LiveRender._shape`、`Console._lock`——见 `openx/services/streaming.py` 的 `_ResizeAwareLive`）。rich 13 的 `stop()` 刷新/提前返回语义与 done/cancel 路径的光标算术假设不符；14 与 15 行为一致（已做字节码级比对）。`rich>=14,<16` 这个 pin 就是为此而设。升级到 16 之前，须复检上述私有面。

## Release notes

发布说明位于 [`openx/CHANGELOG.md`](../openx/CHANGELOG.md)——每个版本一节 `## <version> — <title>`，最新版在文件顶部（Claude Code 风格）。`openx/changelog.py` 在导入时把它解析成启动面板的 "What's new" 与 `/release-notes`（别名 `/release`）的数据；节内的 `###` 分组标题和叙述行可以随便写，只有 `- ` bullet 会被收集。发新版的步骤：

1. 在 `openx/CHANGELOG.md` 顶部追加新版本小节。
2. 升 `pyproject.toml` 的 `version` 和 `openx/__init__.py` 的 `__version__`。

## 文档

文档位于 `docs/`，双语成对（`xxx.md` + `xxx.zh.md`）；改动时两侧同步更新。结构与写作规则见 [docs/AGENTS.md](AGENTS.md)。
