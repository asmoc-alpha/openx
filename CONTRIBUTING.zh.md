# 为 OpenX 做贡献

感谢考虑参与贡献！issue、文档改进、bug 修复都欢迎。（[English](CONTRIBUTING.md)）

## 开发环境

需要 Python ≥ 3.10。

```bash
git clone https://github.com/asmoc-alpha/openx.git   # 或你的 fork
cd openx
pip install -e ".[dev]"

python -m pytest tests -q     # 跑测试
```

## 测试

- `pytest-asyncio` 为 `auto` 模式——直接写 async 测试，无需装饰器。
- 用手写 fake（`tests/test_bugfixes.py` 里的 `FakeLLM`、`FakeConsole`），不用 `unittest.mock`。
- 终端 UI 行为用 [pyte](https://pypi.org/project/pyte/) 屏幕模拟和真实 pty 端到端
  harness 测试——参考 `tests/services/test_terminal_interaction.py` 与
  `tests/services/test_esc_interrupt.py` 的既有模式。
- settings/sessions/tasks 路径通过模块常量 monkeypatch；测试绝不触碰真实用户状态。

任何用户可见的行为变更都应带回归测试。

## 代码约定

- 贴合周边代码：注释密度、命名、错误处理习惯。
- UI 文案使用 `openx/ui/_style.py` 的几何标记家族与颜色常量（无 emoji、单一强调色）。
- 文档双语——改文档页时同步更新对应的 `.zh.md`。
- 发版簿记（维护者）：版本号在**两处**维护（`pyproject.toml` 与
  `openx/__init__.__version__`），发布说明写入 `openx/CHANGELOG.md`
  （启动面板与 `/release-notes` 的数据源）。

## Pull Request

1. 非小改动请先开/认领 issue，避免工作撞车。
2. PR 保持聚焦——一个 PR 一个变更。
3. 确保 `python -m pytest tests -q` 全绿；CI 在 3.10 与 3.12 上运行。
4. 填写 PR 模板；UI 变更附终端截图。

## 许可

贡献即表示同意以项目 MIT 许可证发布你的工作。
