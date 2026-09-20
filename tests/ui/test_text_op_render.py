"""文本操作展示回归测试 —— create/update/delete 分类 + 多语言高亮 diff。

覆盖（纯展示层，见 openx/ui/_diff.py + services/streaming.py）：
- ``detect_lexer``：扩展名 / 文件名 → pygments lexer 别名（未知 → None）；
- ``classify_text_op``：write_file 新文件 → create、覆写 → update、清空 →
  delete；edit_file ``new_text`` 为空 → delete，否则 update；
- ``diff_stat``：增删行数统计（头行不计）；
- ``render_diff_lines``：+/- 标记着色、语言高亮产生非默认样式 span、
  按 max_lines 截断 + "… +N more lines" 提示；
- 流式接线：``StreamingService(tool_lookup=...)`` 在 ToolStart 时经工具
  ``preview_diff`` 取变更前后内容 → 头行带 create/update/delete 标记、
  体部为多语言高亮 diff；无 ``tool_lookup`` → 回退既有结果渲染（零变化）。

风格：pytest-asyncio auto、手写 fake、禁 unittest.mock。

运行：``python -m pytest tests/ui/test_text_op_render.py -q``
"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

from rich.console import Console as RichConsole

from openx.agent import ToolResultEvent, ToolStartEvent
from openx.services.streaming import StreamingService
from openx.tools.file_tools import EditFileTool, WriteFileTool
from openx.ui._diff import (
    classify_text_op,
    detect_lexer,
    diff_stat,
    render_diff_lines,
)

# ── 单元：lexer 探测 / 分类 / 统计 ───────────────────────────────


class TestDetectLexer:
    def test_common_extensions(self):
        assert detect_lexer("src/app.py") == "python"
        assert detect_lexer("a/b.tsx") == "tsx"
        assert detect_lexer("conf.json") == "json"
        assert detect_lexer("lib.rs") == "rust"
        assert detect_lexer("styles.scss") == "scss"
        assert detect_lexer("x.yaml") == "yaml"

    def test_filename_specials(self):
        assert detect_lexer("Dockerfile") == "docker"
        assert detect_lexer("sub/Makefile") == "make"

    def test_unknown_extension_none(self):
        assert detect_lexer("weird.unknownext") is None
        assert detect_lexer("noext") is None


class TestClassifyTextOp:
    def test_write_create_update_delete(self):
        assert classify_text_op("write_file", "{}", "", "hi\n") == "create"
        assert classify_text_op("write_file", "{}", "old\n", "new\n") == "update"
        assert classify_text_op("write_file", "{}", "old\n", "") == "delete"
        assert classify_text_op("write_file", "{}", "", "") is None

    def test_edit_delete_and_update(self):
        # new_text 为空 → 删除匹配文本
        assert classify_text_op(
            "edit_file", '{"old_text": "x", "new_text": ""}', "x\ny\n", "y\n"
        ) == "delete"
        assert classify_text_op(
            "edit_file", '{"old_text": "x", "new_text": "z"}', "x\n", "z\n"
        ) == "update"

    def test_fallback_by_content_only(self):
        assert classify_text_op("weird", "", "", "a\n") == "create"
        assert classify_text_op("weird", "", "a\n", "") == "delete"
        assert classify_text_op("weird", "", "a\n", "b\n") == "update"

    def test_bad_arguments_do_not_raise(self):
        # 坏 JSON：write_file 走内容推导（不抛）
        assert classify_text_op("write_file", "{broken", "", "x\n") == "create"
        # edit_file 坏 JSON：new_text 缺失 → 视为 update（保守）
        assert classify_text_op("edit_file", "{broken", "x\n", "y\n") == "update"


class TestDiffStat:
    def test_counts(self):
        assert diff_stat("a\nb\nc\n", "a\nX\nc\n") == (1, 1)
        assert diff_stat("", "a\nb\n") == (2, 0)
        assert diff_stat("a\nb\n", "") == (0, 2)


# ── 单元：diff 渲染 ──────────────────────────────────────────────


class TestRenderDiffLines:
    def test_markers_present_and_colored(self):
        rows = render_diff_lines(
            "x = 1\nsame\n", "x = 2\nsame\n", "a.py"
        )
        plain = "\n".join(t.plain for t in rows)
        assert "- x = 1" in plain and "+ x = 2" in plain
        minus = next(t for t in rows if t.plain.startswith("-"))
        plus = next(t for t in rows if t.plain.startswith("+"))
        assert any("red" in str(s.style) for s in minus.spans)
        assert any("green" in str(s.style) for s in plus.spans)

    def test_language_highlight_applied(self):
        # python 关键字/函数名 → 除 gutter 外还有 token 级样式 span
        rows = render_diff_lines(
            "def foo():\n    pass\n", "def foo():\n    return 1\n", "m.py"
        )
        plus = next(t for t in rows if t.plain.startswith("+"))
        # spans: "+"(green) + " " + 内容 token（≥2 个不同样式）
        assert len(plus.spans) >= 3, plus.spans

    def test_truncation_notice_and_hint(self):
        old = "\n".join(f"o{i}" for i in range(50))
        new = "\n".join(f"n{i}" for i in range(50))
        rows = render_diff_lines(old, new, "big.py", max_lines=5,
                                 more_hint=" (ctrl+t to expand)")
        last = rows[-1].plain
        assert last.startswith("… +") and "more lines" in last
        assert last.endswith("(ctrl+t to expand)")

    def test_identical_returns_empty(self):
        assert render_diff_lines("same\n", "same\n", "a.py") == []


# ── 流式接线 ─────────────────────────────────────────────────────


class _Console:
    """最小 console 替身：仅需 _console（Rich）+ config.syntax_theme。"""

    def __init__(self) -> None:
        self._console = RichConsole(file=io.StringIO(), width=80)
        self.config = SimpleNamespace(syntax_theme="monokai")


def _service(tools: dict, lookup: bool = True) -> StreamingService:
    return StreamingService(
        _Console(),
        tool_lookup=(lambda name: tools.get(name)) if lookup else None,
    )


def _text(rows) -> str:
    return "\n".join(r.plain for r in rows)


class TestStreamingTextOp:
    async def test_write_new_file_renders_create_diff(self, tmp_path):
        target = tmp_path / "new.py"
        tools = {"write_file": WriteFileTool(str(tmp_path))}
        svc = _service(tools)
        args = json.dumps({"file_path": str(target),
                           "content": "print('hi')\n"})
        svc.feed(ToolStartEvent(name="write_file", arguments=args))
        svc.feed(ToolResultEvent(
            name="write_file", output="Wrote 1 lines (12 bytes)", is_error=False))

        record = svc._segments[0][1]
        assert record.op == "create"
        assert record.diff == (str(target), "", "print('hi')\n")

        rows = svc._tool_renderables(record)
        text = _text(rows)
        assert "create" in text and "write_file" in text
        assert "new.py" in text
        assert "print('hi')" in text  # diff 内容可见
        plus = [r for r in rows if r.plain.strip().startswith("+")]
        assert plus and any("green" in str(s.style) for s in plus[0].spans)

    async def test_write_overwrite_renders_update(self, tmp_path):
        target = tmp_path / "exist.py"
        target.write_text("old = 1\n")
        tools = {"write_file": WriteFileTool(str(tmp_path))}
        svc = _service(tools)
        args = json.dumps({"file_path": str(target), "content": "new = 2\n"})
        svc.feed(ToolStartEvent(name="write_file", arguments=args))
        svc.feed(ToolResultEvent(name="write_file", output="Wrote 1 lines"))

        record = svc._segments[0][1]
        assert record.op == "update"
        text = _text(svc._tool_renderables(record))
        assert "update" in text and "- old = 1" in text and "+ new = 2" in text

    async def test_edit_deleting_text_renders_delete(self, tmp_path):
        target = tmp_path / "a.py"
        target.write_text("keep\nremove me\n")
        tools = {"edit_file": EditFileTool(str(tmp_path))}
        svc = _service(tools)
        args = json.dumps({"file_path": str(target), "old_text": "remove me\n",
                           "new_text": ""})
        svc.feed(ToolStartEvent(name="edit_file", arguments=args))
        svc.feed(ToolResultEvent(name="edit_file", output="Replaced 1"))

        record = svc._segments[0][1]
        assert record.op == "delete"
        text = _text(svc._tool_renderables(record))
        assert "delete" in text and "- remove me" in text

    async def test_no_tool_lookup_falls_back_to_output(self, tmp_path):
        """无 tool_lookup：不探测，回退既有输出渲染（行为零变化）。"""
        tools = {"write_file": WriteFileTool(str(tmp_path))}
        svc = _service(tools, lookup=False)
        args = json.dumps({"file_path": str(tmp_path / "x.py"), "content": "c"})
        svc.feed(ToolStartEvent(name="write_file", arguments=args))
        svc.feed(ToolResultEvent(
            name="write_file", output="Wrote 1 lines (1 bytes) to x.py"))

        record = svc._segments[0][1]
        assert record.diff is None
        text = _text(svc._tool_renderables(record))
        assert "Wrote 1 lines" in text  # 原始输出回显
        assert "create" not in text

    async def test_running_text_op_shows_running_row(self, tmp_path):
        tools = {"write_file": WriteFileTool(str(tmp_path))}
        svc = _service(tools)
        args = json.dumps({"file_path": str(tmp_path / "r.py"), "content": "c"})
        svc.feed(ToolStartEvent(name="write_file", arguments=args))
        record = svc._segments[0][1]
        text = _text(svc._tool_renderables(record))
        assert "Running…" in text

    async def test_error_text_op_hides_unapplied_diff(self, tmp_path):
        """失败/被拒的文本操作：预览已捕获，但实际未发生 → 回退错误渲染、
        绝不把"未发生的变更"当已发生的 diff 展示。"""
        tools = {"write_file": WriteFileTool(str(tmp_path))}
        svc = _service(tools)
        args = json.dumps({"file_path": str(tmp_path / "x.py"), "content": "c\n"})
        svc.feed(ToolStartEvent(name="write_file", arguments=args))
        svc.feed(ToolResultEvent(
            name="write_file", output="Error: permission denied", is_error=True))
        record = svc._segments[0][1]
        assert record.diff is not None  # 预览确已捕获
        text = _text(svc._tool_renderables(record))
        assert "permission denied" in text  # 错误可见
        assert "create" not in text         # 未发生的变更不打标
