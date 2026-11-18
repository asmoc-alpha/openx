"""Language-aware before/after diff rendering for the streaming transcript.

文本操作（create / update / delete）的「变更前后对比」渲染归属地
==============================================================
主转录里的文本操作块需要三件事，且**全在展示层**完成（绝不改工具 schema
或结果文本，模型可见接口零变化）：

1. **操作分类**（``classify_text_op``）：由变更本身推导 create / update /
   delete 三类，供头行打标；
2. **变更前后对比**（``render_diff_lines``）：unified diff 逐行渲染成 Rich
   ``Text``，``+``/``-`` 标记保留并按语义着色（绿/红）；
3. **多语言代码高亮**：每行**内容**按文件扩展名对应的语言 lexer 着色
   （``detect_lexer`` + pygments → ANSI → ``Text.from_ansi``）——同一份 diff
   里 Python / JS / JSON / Rust… 各按各的语言上色。

高亮只用 pygments 公有 API（``highlight`` + ``Terminal256Formatter`` +
``Text.from_ansi``），不触碰 rich 私有面；任何环节失败一律 fail-open
（退回无高亮的纯色标记），坏行绝不能让会话崩。
"""

from __future__ import annotations

# ── 独立调试支持：允许直接运行本文件（python openx/.../xxx.py）──────
if __name__ == "__main__" and not __package__:
    import sys as _sys
    from pathlib import Path as _Path
    _file = _Path(__file__).resolve()
    _root = _file.parent
    while _root != _root.parent and not (_root / "pyproject.toml").exists():
        _root = _root.parent
    _sys.path.insert(0, str(_root))
    __package__ = ".".join(_file.relative_to(_root).parts[:-1])

import json
from pathlib import Path

from rich.text import Text

from ._style import GUTTER

# ── 扩展名 → pygments lexer 别名（多语言高亮表）────────────────────
# 覆盖常见语言；未命中 → None（无语言高亮，仅 +/- 语义色）。
EXT_LEXERS: dict[str, str] = {
    # 脚本 / 通用
    ".py": "python", ".pyi": "python", ".pyw": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "jsx", ".ts": "typescript", ".tsx": "tsx",
    ".rb": "ruby", ".php": "php", ".pl": "perl", ".pm": "perl",
    ".lua": "lua", ".r": "r", ".jl": "julia",
    # 系统 / 编译型
    ".go": "go", ".rs": "rust", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".cs": "csharp", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".swift": "swift", ".scala": "scala", ".dart": "dart", ".m": "objective-c",
    # 标记 / 数据 / 配置
    ".json": "json", ".jsonc": "json", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".ini": "ini", ".cfg": "ini", ".conf": "ini",
    ".xml": "xml", ".html": "html", ".htm": "html", ".svg": "xml",
    ".md": "markdown", ".markdown": "markdown", ".rst": "rst",
    ".css": "css", ".scss": "scss", ".sass": "sass", ".less": "less",
    # shell / 构建 / 其他
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".fish": "fish",
    ".ps1": "powershell", ".psm1": "powershell",
    ".sql": "sql", ".tex": "tex", ".vim": "vim", ".dockerfile": "docker",
    ".gradle": "groovy", ".groovy": "groovy", ".el": "common-lisp",
    ".ex": "elixir", ".exs": "elixir", ".erl": "erlang", ".hs": "haskell",
    ".clj": "clojure", ".fs": "fsharp", ".vb": "vbnet",
}

# 无扩展名 / 特殊文件名 → lexer（小写匹配）
_FILENAME_LEXERS: dict[str, str] = {
    "dockerfile": "docker",
    "makefile": "make",
    "gnumakefile": "make",
    "cmakelists.txt": "cmake",
    ".gitignore": "gitignore",
    ".dockerignore": "gitignore",
    ".env": "bash",
    "rakefile": "ruby",
    "gemfile": "ruby",
    "justfile": "make",
}

# 操作标记的语义配色（green 增 / red 删；update 用黄色区分）
_OP_STYLES: dict[str, str] = {
    "create": "green",
    "update": "yellow",
    "delete": "red",
}

#: 结果/预览里**自带 unified diff** 的文本操作工具。
#:
#: 三个界面共用这一份集合：行内 ``_tool_renderables`` 的 ``is_edit``、全屏
#: ``ToolBlock`` 的正文着色、serve 的 ``output_payload`` 判定。谁再添一个写
#: 文件的工具，只需要在这里加名字——三处一起认出它。
TEXT_OP_TOOLS = frozenset({"edit_file", "write_file"})


def detect_lexer(path: str) -> str | None:
    """文件路径 → pygments lexer 别名（多语言高亮）；未知 → None。"""
    try:
        p = Path(str(path))
    except Exception:
        return None
    name = p.name.lower()
    if name in _FILENAME_LEXERS:
        return _FILENAME_LEXERS[name]
    return EXT_LEXERS.get(p.suffix.lower())


def op_style(op: str) -> str:
    """操作名 → 语义色（未知回落 dim）。"""
    return _OP_STYLES.get(op, "dim")


def classify_text_op(
    name: str, arguments: str, old: str, new: str
) -> str | None:
    """文本操作 → ``create`` / ``update`` / ``delete``（无变更 → None）。

    纯展示层推导（无 delete 工具）：分类只依据工具名 + 参数 + 变更前后内容：

    - ``write_file``：old 空 → create；old 非空且 new 空 → delete；否则 update；
    - ``edit_file``：``new_text`` 为空串（删掉匹配文本）→ delete，否则 update；
    - 其他/兜底：按内容推导（仅增 → create、仅减 → delete、其余 → update）。

    任一环节异常 → None（调用方回退既有渲染）。
    """
    try:
        args = json.loads(arguments) if arguments else {}
    except (ValueError, TypeError):
        args = {}
    if not isinstance(args, dict):
        args = {}

    if name == "edit_file":
        # 仅在参数明确给出且 new_text 为空串时判为 delete（删掉匹配文本）；
        # 参数缺失/坏 JSON（无法判定）保守回落 update——绝不无根据地声称删除。
        if "new_text" in args:
            new_text = args.get("new_text", "")
            if isinstance(new_text, str) and new_text.strip() == "":
                return "delete"
        return "update"

    if name == "write_file":
        if not old and new:
            return "create"
        if old and not new:
            return "delete"
        if not old and not new:
            return None
        return "update"

    # 兜底：仅按内容推导（未知工具名但带变更预览）
    if not old and new:
        return "create"
    if old and not new:
        return "delete"
    if not old and not new:
        return None
    return "update"


def diff_stat(old: str, new: str) -> tuple[int, int]:
    """变更行数统计 ``(added, removed)``（``---``/``+++`` 头不计）。"""
    import difflib

    added = removed = 0
    for ln in difflib.unified_diff(
        old.splitlines(), new.splitlines(), lineterm="", n=0
    ):
        if ln.startswith(("+++", "---")):
            continue
        if ln.startswith("+"):
            added += 1
        elif ln.startswith("-"):
            removed += 1
    return added, removed


def _highlight_ansi(code: str, lexer_name: str, theme: str) -> str:
    """单行源码 → ANSI 串（语言高亮）；任何失败 → 原样返回（fail-open）。"""
    try:
        from pygments import highlight
        from pygments.formatters import Terminal256Formatter
        from pygments.lexers import get_lexer_by_name

        lexer = get_lexer_by_name(lexer_name)
        out = highlight(
            code, lexer, Terminal256Formatter(style=theme, nowrap=True)
        )
        return out.rstrip("\n")
    except Exception:
        return code


def _content_text(content: str, lexer_name: str | None, theme: str) -> Text:
    """行内容 → Rich ``Text``（有 lexer 则语言高亮，否则纯文本）。"""
    if not content:
        return Text()
    ansi = _highlight_ansi(content, lexer_name, theme) if lexer_name else content
    try:
        return Text.from_ansi(ansi)
    except Exception:
        return Text(content)


def diff_line_style(line: str) -> str | None:
    """diff 文本的一行 → 语义色（``None`` = 按普通正文渲染）。

    ``+``/``-`` 的开头判定必须让位给头行：``--- a/x`` 也是以 ``-`` 开头的，
    先判 ``@@``/``---``/``+++`` 才不会被染成"删除行"。截断提示
    （``... (diff truncated, N more lines)``）不是 diff 行，返回 ``None``。

    **三个界面共用这一份判定**：行内 ``_result_line_markup``、全屏
    ``ToolBlock`` 的正文着色、以及 serve 下发给浏览器的 ``diff`` 载荷
    （:func:`diff_payload`）都走这里，绿色到底代表"加了"还是"工具输出"不该
    由界面决定。
    """
    if line.startswith(("@@", "---", "+++")):
        return "dim"
    if line.startswith("-"):
        return "red"
    if line.startswith("+"):
        return "green"
    return None


#: 语义色 → 线上 kind（CSS 类名 ``.d-<kind>`` 一一对应）。
#: 前端不认识颜色名，只认识"这行是加的/删的/块头/普通文本"。
_KIND_BY_STYLE: dict[str, str] = {
    "green": "add",
    "red": "del",
    "dim": "hunk",
}


def diff_line_kind(line: str) -> str:
    """diff 文本的一行 → ``add`` / ``del`` / ``hunk`` / ``text``。

    :func:`diff_line_style` 的线上对偶：终端要的是样式名，浏览器要的是
    kind（再映射成 CSS 类）。两者共用一个判定，改一处两边都跟着变。
    """
    return _KIND_BY_STYLE.get(diff_line_style(line) or "", "text")


def colorized_output_lines(
    output: str,
    *,
    is_edit: bool = False,
    limit: int = 0,
    more_hint: str = "",
) -> list[Text]:
    """工具输出 → 逐行 ``Text``（``is_edit`` 才按 diff 语义着色）。

    ``is_edit`` 为假时逐行原样（工具输出不是 diff，不该把 ``-`` 开头的
    ``ls -la`` 输出染红）。``limit > 0`` 时截断并附一行 ``… +N lines``。
    """
    lines = (output or "").splitlines()
    if limit and len(lines) > limit:
        rest = len(lines) - limit
        rows = [Text(ln, style=diff_line_style(ln) if is_edit else None)
                for ln in lines[:limit]]
        rows.append(Text(f"… +{rest} lines{more_hint}", style="dim"))
        return rows
    return [Text(ln, style=diff_line_style(ln) if is_edit else None)
            for ln in lines]


def text_op_lines(
    diff: tuple[str, str, str],
    *,
    theme: str = "monokai",
    max_lines: int = 12,
    more_hint: str = "",
    indent: str = "     ",
) -> list[Text]:
    """文本操作的变更块：``⎿ +A −D`` 统计行 + 语言高亮 diff 行。

    ``indent`` 由调用方给（行内的结果槽线缩进 5 空格；全屏工具块正文自己
    决定）。无内容变化 → 一行 ``(no content change)``。
    """
    path, old, new = diff
    added, removed = diff_stat(old, new)
    rows: list[Text] = [Text.assemble(
        (indent[:2], "dim"), (f"{GUTTER}  ", "dim"),
        (f"+{added}", "green"), (" ", ""), (f"−{removed}", "red"),
    )]
    diff_lines = render_diff_lines(
        old, new, path, theme=theme, max_lines=max_lines, more_hint=more_hint
    )
    if not diff_lines:
        rows.append(Text(f"{indent}(no content change)", style="dim"))
        return rows
    for ln in diff_lines:
        row = Text(indent, style="dim")
        row.append_text(ln)
        rows.append(row)
    return rows


def diff_payload(
    diff: tuple[str, str, str],
    *,
    max_lines: int = 12,
    context: int = 3,
    more_hint: str = "",
) -> dict:
    """``(path, old, new)`` → **可 JSON 序列化**的 diff 载荷（serve 用）。

    载荷形状::

        {"path": "openx/x.py", "added": 1, "removed": 1,
         "rows": [{"kind": "hunk"|"add"|"del"|"ctx"|"more", "text": "..."}, …]}

    这是 :func:`text_op_lines` 的线上对偶：终端要 ``Text`` + 样式，浏览器要
    kind + 明文（再挂 CSS 类）。**分类只在这里做一次**——前端不认识 diff
    语法，它只认识 ``add`` / ``del`` / ``hunk`` 三个词。

    行截断与终端一致（``max_lines``，尾附 ``more`` 行）；无差异 → ``rows`` 为空。
    """
    import difflib

    path, old, new = diff
    added, removed = diff_stat(old, new)
    raw = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile="", tofile="", lineterm="", n=context,
    ))
    body = [
        ln for ln in raw
        if not (ln.startswith(("--- ", "+++ ")) or ln in ("---", "+++"))
    ]
    rows: list[dict] = []
    dropped = 0
    if max_lines and len(body) > max_lines:
        dropped = len(body) - max_lines
        body = body[:max_lines]
    for ln in body:
        if ln.startswith("@@"):
            rows.append({"kind": "hunk", "text": ln})
            continue
        marker, content = ln[:1], ln[1:]
        # 与 diff_line_kind 同一判定（上下文行以单个空格开头 → 无样式 →
        # "text"；这里显式叫 ctx，前端据此用等宽但不上色的类）。
        rows.append({
            "kind": {"+": "add", "-": "del"}.get(marker, "ctx"),
            "text": f"{marker} {content}",
        })
    if dropped:
        rows.append({"kind": "more",
                     "text": f"… +{dropped} more lines{more_hint}"})
    return {
        "path": str(path),
        "added": added,
        "removed": removed,
        "rows": rows,
    }


def output_payload(output: str, *, is_diff: bool = False, limit: int = 40) -> dict:
    """工具原始输出 → ``{"rows": [{"kind", "text"}, …]}``（serve 的退路）。

    **故意不带** ``path`` / ``added`` / ``removed``：这条路上我们手里只有一段
    文本，不知道文件路径也不该替它算统计——"不知道就不出现该键"，与
    ``protocol.tool_result`` 的 ``duration_ms`` 同一条纪律。

    ``is_diff`` 为假时全部是 ``text``（``ls -la`` 的输出不是 diff，不该被染
    红）；为真时按 :func:`diff_line_kind` 分类。``limit > 0`` 截断并附 ``more``。
    """
    lines = (output or "").splitlines()
    dropped = 0
    if limit and len(lines) > limit:
        dropped = len(lines) - limit
        lines = lines[:limit]
    rows = [{"kind": diff_line_kind(ln) if is_diff else "text", "text": ln}
            for ln in lines]
    if dropped:
        rows.append({"kind": "more", "text": f"… +{dropped} lines"})
    return {"rows": rows}


def render_diff_lines(
    old: str,
    new: str,
    path: str,
    theme: str = "monokai",
    max_lines: int = 12,
    context: int = 3,
    more_hint: str = "",
) -> list[Text]:
    """变更前后对比 → Rich ``Text`` 行列表（无缩进，缩进由调用方加）。

    - ``@@`` 块头 → 青色；``+`` 行 → 绿标 + 语言高亮；``-`` 行 → 红标 +
      语言高亮；上下文行 → dim 标 + 语言高亮；
    - ``---``/``+++`` 头行略去（路径已在操作块头行展示）；
    - ``max_lines > 0`` 时按行截断，尾附 ``… +N more lines{more_hint}``
      （``more_hint`` 供调用方并入如 " (ctrl+t to expand)" 的热键提示）；
      无差异返回空列表。
    """
    import difflib

    lexer_name = detect_lexer(path)
    raw = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile="", tofile="", lineterm="", n=context,
    ))
    # 略去 ---/+++ 头（去掉后缀空格对齐的路径噪声）
    body = [
        ln for ln in raw
        if not (ln.startswith(("--- ", "+++ ")) or ln in ("---", "+++"))
    ]
    truncated = 0
    if max_lines and len(body) > max_lines:
        truncated = len(body) - max_lines
        body = body[:max_lines]

    lines: list[Text] = []
    for ln in body:
        if ln.startswith("@@"):
            lines.append(Text(ln, style="cyan"))
            continue
        marker = ln[:1]
        content = ln[1:]
        t = Text()
        if marker == "+":
            t.append("+", style="green")
            t.append(" ")
            t.append_text(_content_text(content, lexer_name, theme))
        elif marker == "-":
            t.append("-", style="red")
            t.append(" ")
            t.append_text(_content_text(content, lexer_name, theme))
        else:  # 上下文行（difflib 以单个空格开头）
            t.append(" ", style="dim")
            t.append(" ")
            t.append_text(_content_text(content, lexer_name, theme))
        lines.append(t)

    if truncated:
        lines.append(
            Text(f"… +{truncated} more lines{more_hint}", style="dim")
        )
    return lines


if __name__ == "__main__":
    # 独立调试：lexer 探测 / 分类 / 统计 / diff 渲染（纯内存，无副作用）
    assert detect_lexer("src/app.py") == "python"
    assert detect_lexer("a/b.tsx") == "tsx"
    assert detect_lexer("conf.json") == "json"
    assert detect_lexer("lib.rs") == "rust"
    assert detect_lexer("Dockerfile") == "docker"
    assert detect_lexer("weird.unknownext") is None

    # classify：create / update / delete 三类
    assert classify_text_op("write_file", "{}", "", "hello\n") == "create"
    assert classify_text_op("write_file", "{}", "old\n", "new\n") == "update"
    assert classify_text_op("write_file", "{}", "old\n", "") == "delete"
    assert classify_text_op(
        "edit_file", '{"old_text": "x", "new_text": ""}', "x\ny\n", "y\n"
    ) == "delete"
    assert classify_text_op(
        "edit_file", '{"old_text": "x", "new_text": "z"}', "x\n", "z\n"
    ) == "update"
    assert classify_text_op("write_file", "{}", "", "") is None

    a, r = diff_stat("a\nb\nc\n", "a\nX\nc\n")
    assert (a, r) == (1, 1), (a, r)

    # 渲染：包含 +/- 标记行；python 关键字带非默认样式
    rows = render_diff_lines(
        "def f():\n    return 1\n", "def f():\n    return 2\n", "m.py"
    )
    plain = "\n".join(t.plain for t in rows)
    assert "-" in plain and "+" in plain
    assert any(
        any("bold" in str(s.style) or "color" in str(s.style) or "#" in str(s.style)
            for s in t.spans)
        for t in rows if t.plain.strip().startswith("+")
    ), "期望语言高亮产生非默认样式 span"

    # 截断提示
    big_old = "\n".join(f"o{i}" for i in range(50))
    big_new = "\n".join(f"n{i}" for i in range(50))
    capped = render_diff_lines(big_old, big_new, "big.py", max_lines=5)
    assert capped[-1].plain.startswith("… +")

    # 行级语义色：头行优先于 -/+，截断提示不着色
    assert diff_line_style("+new") == "green"
    assert diff_line_style("-old") == "red"
    assert diff_line_style("@@ -1,3 +1,3 @@") == "dim"
    assert diff_line_style("--- a/x.py") == "dim"   # 也以 - 开头，须先判头行
    assert diff_line_style("+++ b/x.py") == "dim"
    assert diff_line_style(" context") is None
    assert diff_line_style("... (diff truncated, 2 more lines)") is None

    # 输出着色：非文本操作不着色（ls -la 不该被染红），limit 附尾行
    plain_rows = colorized_output_lines("-rw-r--r-- 1 a b c", is_edit=False)
    assert plain_rows[0].style is None, plain_rows[0].style
    edit_rows = colorized_output_lines("--- a/x\n+x", is_edit=True)
    assert [r.style for r in edit_rows] == ["dim", "green"]
    capped_out = colorized_output_lines("a\nb\nc", is_edit=False, limit=2)
    assert capped_out[-1].plain == "… +1 lines", capped_out[-1].plain

    # 文本操作块：统计行 + 缩进 diff
    op_rows = text_op_lines(("m.py", "def f():\n    return 1\n",
                             "def f():\n    return 2\n"))
    assert op_rows[0].plain.strip().startswith(GUTTER), op_rows[0].plain
    assert "+1" in op_rows[0].plain and "−1" in op_rows[0].plain
    assert any(r.plain.strip() == "+     return 2" for r in op_rows), \
        [r.plain for r in op_rows]
    assert text_op_lines(("m.py", "same\n", "same\n"))[-1].plain.strip() == \
        "(no content change)"

    # 线上 kind：与 diff_line_style 同一判定
    assert diff_line_kind("+x") == "add"
    assert diff_line_kind("-x") == "del"
    assert diff_line_kind("--- a/x") == "hunk"   # 头行优先，不是 del
    assert diff_line_kind(" ctx") == "text"

    # serve 载荷：可 JSON 往返，add/del/hunk/ctx 齐全，带统计
    payload = diff_payload(("m.py", "a = 1\n", "a = 2\n"))
    assert payload["path"] == "m.py"
    assert (payload["added"], payload["removed"]) == (1, 1)
    kinds = [r["kind"] for r in payload["rows"]]
    assert "add" in kinds and "del" in kinds and "hunk" in kinds, kinds
    assert any(r["kind"] == "add" and r["text"] == "+ a = 2"
               for r in payload["rows"]), payload["rows"]
    json.dumps(payload)  # 必须可序列化（下行就是 JSON）

    # 输出载荷：非 diff 全是 text；不知道的键不出现；limit 附 more
    plain = output_payload("-rw-r--r-- 1 a b c")
    assert [r["kind"] for r in plain["rows"]] == ["text"]
    assert "path" not in plain and "added" not in plain
    typed = output_payload("--- a/x\n+x", is_diff=True)
    assert [r["kind"] for r in typed["rows"]] == ["hunk", "add"]
    capped = output_payload("a\nb\nc", limit=2)
    assert capped["rows"][-1]["kind"] == "more", capped["rows"]

    from rich.console import Console
    Console().print("\n".join(t.plain for t in rows))
    print("openx/ui/_diff.py OK ✓")
