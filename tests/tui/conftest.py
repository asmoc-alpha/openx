"""tests/tui 的共享夹具。

**输入历史必须隔离**：``openx/tui/history.py`` 默认落盘到 ``~/.openx/history``
（与行内 REPL 共用的真实用户文件）。TUI 测试会大量按 Enter 提交（``"hi"``、
``"/help"``、``"gpt-5"``…），不隔离就会把测试噪声写进用户自己的历史——
既污染数据，也让"历史"相关断言依赖跑过多少次测试。这里把模块级常量指到
``tmp_path``；被测代码在**构造时**才读这个常量（``PromptHistory.__init__``），
所以每个用例拿到的都是干净的空历史。
"""

from __future__ import annotations

import pytest

from openx.tui import history as history_mod


@pytest.fixture(autouse=True)
def _isolated_history(tmp_path, monkeypatch):
    """把历史文件指到临时目录（自动作用于 tests/tui 下的全部用例）。"""
    monkeypatch.setattr(history_mod, "HISTORY_PATH", tmp_path / "history")
