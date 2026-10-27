"""终端 LaTeX → Unicode 转写（``ui/_math_text.render_math``）。

覆盖：
- 四种定界符（``$$``/``$``/``\\[``/``\\(``）的闭合 span 转写；未闭合 span
  原样（流式逐 token 安全）；
- 货币启发式：``$100 and $200`` 不是数学；
- 围栏代码块内不转写；
- 命令转写：希腊 / 算符 / 分式 / 根式（含 ``[n]``）/ 上下标（含无变体
  退路）/ 字体族 / 重音 / 函数名 / 未知命令剥名 / 不可见定界符；
- 快路径：无数学迹象的文本零改动。

运行：``python -m pytest tests/ui/test_math_text.py -q``
"""

from __future__ import annotations

from openx.ui._math_text import render_math as r


def test_delimiters_and_closed_only():
    assert r("能量 $E=mc^2$ 守恒") == "能量 E=mc² 守恒"
    assert r("$$\\alpha + \\beta$$") == "α + β"
    assert r("\\[x \\leq 10\\]") == "x ≤ 10"
    assert r("\\(a_1 + a_2\\)") == "a₁ + a₂"
    # 未闭合 span 原样（流式逐 token 到达）
    assert r("value $$x^2") == "value $$x^2"
    assert r("value $x^2") == "value $x^2"
    assert r("half \\[x^2") == "half \\[x^2"


def test_currency_heuristic():
    # 闭 $ 前有空白 → 不是数学，原样
    assert r("cost $100 and $200 total") == "cost $100 and $200 total"
    assert r("报价 $5, 请查收") == "报价 $5, 请查收"
    # 干净的两处行内数学都转
    assert r("$a$ 和 $b$") == "a 和 b"


def test_fences_untouched():
    assert r("```\n$\\alpha$\n```") == "```\n$\\alpha$\n```"
    assert r("前\n```py\n$\\frac{1}{2}$\n```\n后 $x^2$") == \
        "前\n```py\n$\\frac{1}{2}$\n```\n后 x²"


def test_inline_code_untouched():
    # 行内代码里的 LaTeX 是代码：不转写
    assert r("用 `$\\alpha$` 表示") == "用 `$\\alpha$` 表示"
    assert r("写 `$x^2$` 然后 $x^2$") == "写 `$x^2$` 然后 x²"


def test_symbols_and_ops():
    assert r("$$\\sum_{i=1}^{n} i$$") == "∑ᵢ₌₁ⁿ i"
    assert r("$\\int_0^1 x\\,dx$") == "∫₀¹ x dx"
    assert r("$\\mathbb{R}^2$") == "ℝ²"
    assert r("$\\sin^2 x + \\cos^2 x = 1$") == "sin² x + cos² x = 1"


def test_frac_and_sqrt():
    assert r("$\\frac{1}{2}$") == "1⁄2"
    assert r("$\\frac{n+1}{2}$") == "(n+1)/2"
    assert r("$\\sqrt{x}$") == "√x"
    assert r("$\\sqrt{b^2-4ac}$") == "√(b²-4ac)"
    assert r("$\\sqrt[3]{x}$") == "³√x"


def test_script_fallbacks():
    assert r("$x^{10}$") == "x¹⁰"
    assert r("$x^n + y_n$") == "xⁿ + yₙ"
    # q 无上标变体 → 整组原记法（半个怪字符好过错误排版）
    assert r("$x^q$") == "x^q"
    # b/z 无下标变体 → 整组原记法
    assert r("$x_{bz}$") == "x_{bz}"
    assert r("$x_{i+j}$") == "xᵢ₊ⱼ"
    # 命令作参数：先转写再尝试映射
    assert r("$e^{-x}$") == "e⁻ˣ"
    assert r("$\\int_0^\\infty$") == "∫₀^{∞}"


def test_accent_font_and_unknown():
    assert r("$\\vec{v}$") == "v⃗"
    assert r("$\\mathcal{L}$") == "ℒ"
    assert r("$\\foo{x}$") == "foox"  # 未知命令剥名不吞字
    assert r("$\\left( a+b \\right)$") == "( a+b )"
    assert r("$\\left. x \\right|$") == " x |"  # 不可见定界符连点吞掉


def test_environments_and_alignment():
    out = r("$$\\begin{aligned} a &= 1 \\\\ b &= 2 \\end{aligned}$$")
    assert "aligned" not in out and "\n" in out and "=" in out


def test_fast_path_no_math():
    plain = "价格是 5 元, 共 100 元; code: x^2 and {braces}"
    assert r(plain) == plain
