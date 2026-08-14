"""编辑 diff 展示回归测试 —— v0.3.2。

覆盖：
- unified_diff_text：标准 unified diff 头/+- 行、无差异空串、max_lines 截断提示；
- EditFileTool.preview_diff：严格镜像 execute 语义（唯一匹配 / replace_all /
  零匹配 / 多匹配非 replace_all / old==new / 工作区外 / 目录 / 超大文件 → None）；
- WriteFileTool.preview_diff：新文件 = 空→全文、覆写 = 旧→新、同内容 → None；
- edit_file.execute 输出附带紧凑 unified diff；
- print_file_diff：rich Syntax 彩色渲染（缓冲捕获断言文本内容）+ 无变化提示；
- 执行器接线：manual 模式 edit_file 弹窗收到 diff 三元组且 details 被抑制；
  预览失败（多匹配）回退 JSON 参数；无 preview_diff 的工具 diff=None。

风格：pytest-asyncio auto、手写 Fake、禁 unittest.mock。

运行：``python -m pytest tests/test_diff_display.py -q``
"""

from __future__ import annotations

import json

import pytest
from rich.console import Console as RichConsole

from openx.config import OpenXConfig
from openx.permissions import Permission, PermissionLevel, PermissionRules
from openx.services.tool_executor import ToolExecutor
from openx.tools.base import Tool, ToolResult
from openx.tools.file_tools import EditFileTool, WriteFileTool
from openx.ui._components.misc import MiscMixin
from openx.utils.text import unified_diff_text


# ── unified_diff_text 单元 ───────────────────────────────────────

class TestUnifiedDiffText:
    def test_basic_shape(self):
        diff = unified_diff_text("f.py", "a\nb\nc", "a\nX\nc")
        assert "--- a/f.py" in diff
        assert "+++ b/f.py" in diff
        assert "-b" in diff.splitlines()
        assert "+X" in diff.splitlines()

    def test_identical_returns_empty(self):
        assert unified_diff_text("f.py", "same\n", "same\n") == ""

    def test_new_file_from_empty(self):
        diff = unified_diff_text("new.py", "", "line1\nline2")
        assert "+line1" in diff.splitlines() and "+line2" in diff.splitlines()

    def test_max_lines_truncation_notice(self):
        old = "\n".join(f"old{i}" for i in range(50))
        new = "\n".join(f"new{i}" for i in range(50))
        diff = unified_diff_text("big.py", old, new, max_lines=5)
        lines = diff.splitlines()
        assert len(lines) == 6
        assert "diff truncated" in lines[-1]


# ── EditFileTool.preview_diff ────────────────────────────────────

class TestEditPreviewDiff:
    def _tool(self, tmp_path):
        return EditFileTool(str(tmp_path))

    def test_unique_match_mirrors_execute(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("hello world\nfoo bar\n")
        result = self._tool(tmp_path).preview_diff({
            "file_path": str(f), "old_text": "world", "new_text": "universe",
        })
        assert result is not None
        path, old, new = result
        assert path == str(f)
        assert old == "hello world\nfoo bar\n"
        assert new == "hello universe\nfoo bar\n"

    def test_replace_all(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1\nx = 2\n")
        result = self._tool(tmp_path).preview_diff({
            "file_path": str(f), "old_text": "x", "new_text": "y",
            "replace_all": True,
        })
        assert result is not None and result[2] == "y = 1\ny = 2\n"

    def test_missing_file_none(self, tmp_path):
        assert self._tool(tmp_path).preview_diff({
            "file_path": str(tmp_path / "nope.py"),
            "old_text": "a", "new_text": "b",
        }) is None

    def test_zero_matches_none(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("hello\n")
        assert self._tool(tmp_path).preview_diff({
            "file_path": str(f), "old_text": "zzz", "new_text": "b",
        }) is None

    def test_ambiguous_non_replace_all_none(self, tmp_path):
        """多匹配且非 replace_all：execute 必报错 → 预览返回 None。"""
        f = tmp_path / "a.py"
        f.write_text("dup\ndup\n")
        assert self._tool(tmp_path).preview_diff({
            "file_path": str(f), "old_text": "dup", "new_text": "x",
        }) is None

    def test_noop_edit_none(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("same\n")
        assert self._tool(tmp_path).preview_diff({
            "file_path": str(f), "old_text": "same", "new_text": "same",
        }) is None

    def test_outside_workspace_none(self, tmp_path):
        outside = tmp_path.parent / "outside.py"
        outside.write_text("x\n")
        assert self._tool(tmp_path).preview_diff({
            "file_path": str(outside), "old_text": "x", "new_text": "y",
        }) is None

    def test_directory_none(self, tmp_path):
        d = tmp_path / "dir.py"
        d.mkdir()
        assert self._tool(tmp_path).preview_diff({
            "file_path": str(d), "old_text": "x", "new_text": "y",
        }) is None

    def test_huge_file_none(self, tmp_path):
        f = tmp_path / "big.py"
        f.write_text("y" * (EditFileTool._PREVIEW_MAX_CHARS + 1))
        assert self._tool(tmp_path).preview_diff({
            "file_path": str(f), "old_text": "y", "new_text": "z",
        }) is None

    def test_missing_args_none(self, tmp_path):
        assert self._tool(tmp_path).preview_diff({}) is None
        assert self._tool(tmp_path).preview_diff({"file_path": "a.py"}) is None


# ── WriteFileTool.preview_diff ───────────────────────────────────

class TestWritePreviewDiff:
    def _tool(self, tmp_path):
        return WriteFileTool(str(tmp_path))

    def test_new_file_old_empty(self, tmp_path):
        f = tmp_path / "fresh.py"
        result = self._tool(tmp_path).preview_diff({
            "file_path": str(f), "content": "print('hi')\n",
        })
        assert result == (str(f), "", "print('hi')\n")

    def test_overwrite_carries_old_content(self, tmp_path):
        f = tmp_path / "exist.py"
        f.write_text("old stuff\n")
        result = self._tool(tmp_path).preview_diff({
            "file_path": str(f), "content": "new stuff\n",
        })
        assert result == (str(f), "old stuff\n", "new stuff\n")

    def test_identical_content_none(self, tmp_path):
        f = tmp_path / "same.py"
        f.write_text("unchanged\n")
        assert self._tool(tmp_path).preview_diff({
            "file_path": str(f), "content": "unchanged\n",
        }) is None

    def test_outside_workspace_none(self, tmp_path):
        assert self._tool(tmp_path).preview_diff({
            "file_path": str(tmp_path.parent / "o.py"), "content": "x",
        }) is None

    def test_huge_content_none(self, tmp_path):
        assert self._tool(tmp_path).preview_diff({
            "file_path": str(tmp_path / "big.py"),
            "content": "z" * (WriteFileTool._PREVIEW_MAX_CHARS + 1),
        }) is None


# ── edit_file 执行结果附 diff ────────────────────────────────────

class TestEditOutputDiff:
    async def test_output_contains_compact_diff(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("hello world\n")
        result = await EditFileTool(str(tmp_path)).execute(
            str(f), "world", "universe"
        )
        assert result.success
        assert "Replaced 1 occurrence(s)" in result.output
        assert "-hello world" in result.output.splitlines()
        assert "+hello universe" in result.output.splitlines()

    async def test_unchanged_edit_no_diff_block(self, tmp_path):
        """old==new 退化替换：成功但无 diff 可附。"""
        f = tmp_path / "a.py"
        f.write_text("same\n")
        result = await EditFileTool(str(tmp_path)).execute(str(f), "same", "same")
        assert result.success
        assert "--- a/" not in result.output


# ── print_file_diff 渲染 ─────────────────────────────────────────

class TestPrintFileDiff:
    def _render(self, old: str, new: str, path: str = "a.py") -> str:
        import io
        buf = io.StringIO()
        mixin = MiscMixin()
        mixin._console = RichConsole(file=buf, width=100, force_terminal=False)
        mixin.print_file_diff(path, old, new)
        return buf.getvalue()

    def test_renders_both_sides_and_title(self):
        out = self._render("old = 1\nsame", "new = 2\nsame")
        assert "a.py" in out
        assert "old = 1" in out
        assert "new = 2" in out

    def test_respects_config_theme(self):
        import io
        buf = io.StringIO()
        mixin = MiscMixin()
        mixin.config = OpenXConfig(
            api_key="sk-x", api_base="https://x", model="m"
        )
        mixin.config.syntax_theme = "monokai"
        mixin._console = RichConsole(file=buf, width=100)
        mixin.print_file_diff("t.py", "a", "b")
        assert "t.py" in buf.getvalue()

    def test_no_changes_notice(self):
        out = self._render("same", "same")
        assert "no changes" in out


# ── 执行器接线：弹窗收到 diff ────────────────────────────────────

class FakeDiffConsole:
    """捕获 ask_permission 全参数的假控制台。"""

    def __init__(self, approve: bool = True):
        self.approve = approve
        self.calls: list[dict] = []
        self.mode = "manual"

    async def ask_permission(self, tool_name, reason, details="", args_summary="",
                       can_remember=True, diff=None):
        self.calls.append({
            "tool": tool_name, "details": details, "diff": diff,
            "can_remember": can_remember,
        })
        return (self.approve, False)


class PlainAskTool(Tool):
    """无 preview_diff 的 ASK 工具（走默认 None 分支）。"""

    name = "plain_ask"
    description = "plain"
    parameters = {"type": "object", "properties": {"x": {"type": "string"}}}

    @property
    def permission(self) -> Permission:
        return Permission(level=PermissionLevel.ASK, reason="plain ask")

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(output="ok")


def _executor(console):
    ex = ToolExecutor(console, auto_approve=False, mode="manual")
    ex._rules = PermissionRules()
    return ex


class TestExecutorDiffWiring:
    async def test_edit_file_dialog_receives_diff(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("hello world\n")
        console = FakeDiffConsole()
        ex = _executor(console)
        pc = await ex.prepare(
            "edit_file", EditFileTool(str(tmp_path)),
            json.dumps({"file_path": str(f), "old_text": "world",
                        "new_text": "universe"}),
            "t1",
        )
        assert pc.approved
        call = console.calls[0]
        assert call["diff"] == (str(f), "hello world\n", "hello universe\n")
        # diff 已含全部变更信息 → JSON details 被抑制
        assert call["details"] == ""

    async def test_write_file_dialog_diff_from_empty(self, tmp_path):
        console = FakeDiffConsole()
        ex = _executor(console)
        target = tmp_path / "new.py"
        await ex.prepare(
            "write_file", WriteFileTool(str(tmp_path)),
            json.dumps({"file_path": str(target), "content": "print(1)\n"}),
            "t2",
        )
        call = console.calls[0]
        assert call["diff"] == (str(target), "", "print(1)\n")

    async def test_ambiguous_edit_falls_back_to_json_details(self, tmp_path):
        """预览 None（多匹配）→ details 回退 JSON、diff=None，弹窗照常。"""
        f = tmp_path / "a.py"
        f.write_text("dup\ndup\n")
        console = FakeDiffConsole()
        ex = _executor(console)
        pc = await ex.prepare(
            "edit_file", EditFileTool(str(tmp_path)),
            json.dumps({"file_path": str(f), "old_text": "dup",
                        "new_text": "x"}),
            "t3",
        )
        assert pc.approved  # 弹窗照常批准（execute 阶段才会报错）
        call = console.calls[0]
        assert call["diff"] is None
        assert "dup" in call["details"] and call["details"].startswith("{")

    async def test_tool_without_preview_gets_json_details(self, tmp_path):
        console = FakeDiffConsole()
        ex = _executor(console)
        await ex.prepare(
            "plain_ask", PlainAskTool(), json.dumps({"x": "1"}), "t4",
        )
        call = console.calls[0]
        assert call["diff"] is None
        assert '"x"' in call["details"]

    async def test_denied_edit_not_executed(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("hello\n")
        console = FakeDiffConsole(approve=False)
        ex = _executor(console)
        pc = await ex.prepare(
            "edit_file", EditFileTool(str(tmp_path)),
            json.dumps({"file_path": str(f), "old_text": "hello",
                        "new_text": "bye"}),
            "t5",
        )
        assert not pc.approved
        assert f.read_text() == "hello\n"
