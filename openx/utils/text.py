"""Text and output utilities."""

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


def truncate_output(
    text: str,
    max_lines: int = 2000,
    max_chars: int = 50_000,
) -> tuple[str, bool, str]:
    """Truncate tool output and produce a notice if needed.

    Returns ``(truncated_text, was_truncated, notice)``.
    """
    lines = text.splitlines()
    truncated = False

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True

    result = "\n".join(lines)
    if len(result) > max_chars:
        # Keep whole lines up to max_chars
        kept = ""
        for line in lines:
            if len(kept) + len(line) + 1 > max_chars:
                truncated = True
                break
            kept += line + "\n"
        result = kept.rstrip("\n")

    notice = ""
    if truncated:
        original_lines = text.count("\n") + 1
        notice = (
            f"\n\n[Output truncated. Showing ~{result.count(chr(10)) + 1} "
            f"of {original_lines} lines / {len(result)} of {len(text)} chars]"
        )

    return result, truncated, notice


def estimate_tokens(text: str) -> int:
    """Rough token count estimate (≈ chars ÷ 4)."""
    return max(1, len(text) // 4)


def unified_diff_text(
    path: str,
    old: str,
    new: str,
    context: int = 3,
    max_lines: int = 0,
) -> str:
    """生成 unified diff 文本（``difflib``，无外部依赖）。

    用于 edit_file/write_file 的变更展示——权限弹窗预览、工具结果摘要、
    ``print_file_diff`` 渲染共用此单一实现。

    - ``path`` 仅作 ``---/+++`` 头展示（``a/<path>`` / ``b/<path>``）；
    - ``context`` 为每个 hunk 的上下文行数（difflib ``n`` 参数）；
    - ``max_lines > 0`` 时按行截断并附 "(diff truncated…)" 提示；
    - 无差异返回空串。
    """
    import difflib  # 局部导入：模块顶层保持零依赖面

    lines = list(difflib.unified_diff(
        old.splitlines(),
        new.splitlines(),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        lineterm="",
        n=context,
    ))
    if max_lines and len(lines) > max_lines:
        dropped = len(lines) - max_lines
        lines = lines[:max_lines] + [f"... (diff truncated, {dropped} more lines)"]
    return "\n".join(lines)


if __name__ == "__main__":
    # 构造超长文本（50 行），用小 max_lines 触发截断
    long_text = "\n".join(f"line {i}" for i in range(50))
    out, was_truncated, notice = truncate_output(long_text, max_lines=5)
    print(f"truncated={was_truncated}, kept lines={out.count(chr(10)) + 1}")
    print(f"notice={notice.strip()}")
    assert was_truncated and out.count("\n") + 1 == 5 and notice

    short_out, short_trunc, short_notice = truncate_output("hello", max_lines=5)
    assert not short_trunc and short_notice == ""

    # estimate_tokens：≈ chars ÷ 4，且至少为 1
    print(f"estimate_tokens('x' * 100) = {estimate_tokens('x' * 100)}")
    print(f"estimate_tokens('') = {estimate_tokens('')}")
    assert estimate_tokens("x" * 100) == 25 and estimate_tokens("") == 1
    print("openx/utils/text.py OK ✓")
