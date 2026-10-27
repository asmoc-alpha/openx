"""LaTeX 数学 → Unicode 近似转写（终端面）。

终端没有 TeX 排版引擎（Rich 生态无此物，matplotlib 渲图依赖过重），模型输出
里的 ``$$...$$`` / ``$...$`` / ``\\[...\\]`` / ``\\(...\\)`` 只能**字面显示**——
反斜杠命令、花括号、上下标符号堆成一团。本模块把**已闭合**的数学 span 转写成
Unicode 近似（希腊字母、算符、上下标、分式、根式、重音、黑板/花体）：

- **只转闭合 span**：模型输出逐 token 到达，``$$`` 开而未合时保持原样，闭合
  帧自然成型（与 Web 端 ``renderMarkdown`` 的未闭合围栏同一条纪律）；
- **围栏内不动**：代码块里的 LaTeX 是代码，转写反而毁人；
- **货币启发式**：行内 ``$`` 要求开侧紧邻非空白、闭侧紧邻非空白——
  ``$100 and $200`` 不匹配（闭 ``$`` 前有空格），``$x^2$`` 匹配；
- **无损退路**：字符没有上/下标变体时整组保留原记法（``x^q`` 退化为原样
  而非半个怪字符）；未知命令剥反斜杠留名，可读但不误导。

这是**近似**：矩阵/对齐等结构环境只做最简处理（剥 begin/end、``&``→空格、
``\\\\``→换行）。Web 端有真排版（内嵌 KaTeX），本模块只是终端的体面退路。
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

import re

__all__ = ["render_math"]


# ── 符号表 ────────────────────────────────────────────────────────

#: 反斜杠命令 → Unicode 字符（希腊字母 / 算符 / 关系 / 箭头 / 杂项）。
SYMBOLS: dict[str, str] = {
    # 希腊（小写 + 变体）
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ϵ",
    "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ",
    "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "omicron": "ο", "pi": "π", "varpi": "ϖ", "rho": "ρ", "varrho": "ϱ",
    "sigma": "σ", "varsigma": "ς", "tau": "τ", "upsilon": "υ", "phi": "ϕ",
    "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    # 希腊（大写）
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ",
    "Omega": "Ω",
    # 二元算符
    "pm": "±", "mp": "∓", "times": "×", "div": "÷", "cdot": "⋅", "ast": "∗",
    "star": "⋆", "circ": "∘", "bullet": "•", "oplus": "⊕", "ominus": "⊖",
    "otimes": "⊗", "odot": "⊙", "dagger": "†", "ddagger": "‡",
    # 关系
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠",
    "approx": "≈", "equiv": "≡", "sim": "∼", "simeq": "≃", "cong": "≅",
    "propto": "∝", "ll": "≪", "gg": "≫", "doteq": "≐",
    # 集合
    "in": "∈", "notin": "∉", "ni": "∋", "subset": "⊂", "subseteq": "⊆",
    "supset": "⊃", "supseteq": "⊇", "cup": "∪", "cap": "∩", "emptyset": "∅",
    "varnothing": "∅", "setminus": "∖",
    # 逻辑
    "forall": "∀", "exists": "∃", "nexists": "∄", "neg": "¬", "lnot": "¬",
    "land": "∧", "wedge": "∧", "lor": "∨", "vee": "∨", "models": "⊨",
    # 箭头
    "to": "→", "rightarrow": "→", "leftarrow": "←", "leftrightarrow": "↔",
    "Rightarrow": "⇒", "Leftarrow": "⇐", "Leftrightarrow": "⇔",
    "mapsto": "↦", "uparrow": "↑", "downarrow": "↓", "hookrightarrow": "↪",
    # 大算符 / 微积分
    "sum": "∑", "prod": "∏", "coprod": "∐", "int": "∫", "iint": "∬",
    "iiint": "∭", "oint": "∮", "bigcup": "⋃", "bigcap": "⋂",
    "bigoplus": "⨁", "bigotimes": "⨂",
    # 杂项
    "infty": "∞", "partial": "∂", "nabla": "∇", "hbar": "ℏ", "ell": "ℓ",
    "Re": "ℜ", "Im": "ℑ", "aleph": "ℵ", "angle": "∠", "perp": "⊥",
    "parallel": "∥", "degree": "°", "prime": "′", "wp": "℘",
    "ldots": "…", "cdots": "⋯", "vdots": "⋮", "ddots": "⋱", "dots": "…",
    "lceil": "⌈", "rceil": "⌉", "lfloor": "⌊", "rfloor": "⌋",
    "langle": "⟨", "rangle": "⟩",
}

#: 单字符重音命令 → 组合变音符号（加在首个字符后）。
ACCENTS: dict[str, str] = {
    "vec": "⃗",
    "hat": "̂",
    "bar": "̄",
    "tilde": "̃",
    "dot": "̇",
    "ddot": "̈",
    "overline": "̅",
}

#: 字体族命令 → 数学字母表（Unicode 数学字母数字块的洞已用 Letterlike
#: Symbols 区字符填平）。\mathcal 只有 26 大写；\mathrm 直立 = 原样。
_FONTS: dict[str, str] = {
    "mathcal": "𝒜ℬ𝒞𝒟ℰℱ𝒢ℋℐ𝒥𝒦ℒℳ𝒩𝒪𝒫𝒬ℛ𝒮𝒯𝒰𝒱𝒲𝒳𝒴𝒵",
    "mathbf": "𝐀𝐁𝐂𝐃𝐄𝐅𝐆𝐇𝐈𝐉𝐊𝐋𝐌𝐍𝐎𝐏𝐐𝐑𝐒𝐓𝐔𝐕𝐖𝐗𝐘𝐙"
              "𝐚𝐛𝐜𝐝𝐞𝐟𝐠𝐡𝐢𝐣𝐤𝐥𝐦𝐧𝐨𝐩𝐪𝐫𝐬𝐭𝐮𝐯𝐰𝐱𝐲𝐳",
    "mathbb": "𝔸𝔹ℂ𝔻𝔼𝔽𝔾ℍ𝕀𝕁𝕂𝕃𝕄ℕ𝕆ℙℚℝ𝕊𝕋𝕌𝕍𝕎𝕏𝕐ℤ"
              "𝕒𝕓𝕔𝕕𝕖𝕗𝕘𝕙𝕚𝕛𝕜𝕝𝕞𝕟𝕠𝕡𝕢𝕣𝕤𝕥𝕦𝕧𝕨𝕩𝕪𝕫",
    "mathfrak": "𝔄𝔅ℭ𝔇𝔈𝔉𝔊ℌℑ𝔍𝔎𝔏𝔐𝔑𝔒𝔓𝔔ℜ𝔖𝔗𝔘𝔙𝔚𝔛𝔜ℨ"
                "𝔞𝔟𝔠𝔡𝔢𝔣𝔤𝔥𝔦𝔧𝔨𝔩𝔪𝔫𝔬𝔭𝔮𝔯𝔰𝔱𝔲𝔳𝔴𝔵𝔶𝔷",
    "mathrm": "",
}

#: 上标 / 下标变体（Unicode 没有的字母如 q、b 缺席 → 整组退回原记法）。
_SUPERSCRIPT = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶",
    "7": "⁷", "8": "⁸", "9": "⁹", "+": "⁺", "-": "⁻", "=": "⁼", "(": "⁽",
    ")": "⁾", "n": "ⁿ", "i": "ⁱ",
    "a": "ᵃ", "b": "ᵇ", "c": "ᶜ", "d": "ᵈ", "e": "ᵉ", "f": "ᶠ", "g": "ᵍ",
    "h": "ʰ", "j": "ʲ", "k": "ᵏ", "l": "ˡ", "m": "ᵐ", "o": "ᵒ", "p": "ᵖ",
    "r": "ʳ", "s": "ˢ", "t": "ᵗ", "u": "ᵘ", "v": "ᵛ", "w": "ʷ", "x": "ˣ",
    "y": "ʸ", "z": "ᶻ",
}
_SUBSCRIPT = {
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆",
    "7": "₇", "8": "₈", "9": "₉", "+": "₊", "-": "₋", "=": "₌", "(": "₍",
    ")": "₎",
    "a": "ₐ", "e": "ₑ", "h": "ₕ", "i": "ᵢ", "j": "ⱼ", "k": "ₖ", "l": "ₗ",
    "m": "ₘ", "n": "ₙ", "o": "ₒ", "p": "ₚ", "r": "ᵣ", "s": "ₛ", "t": "ₜ",
    "u": "ᵤ", "v": "ᵥ", "x": "ₓ",
}

#: 函数名命令：剥反斜杠即可读（\sin x → sin x）。
_OP_NAMES = (
    "sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh "
    "ln log exp lim sup inf max min det dim ker deg gcd arg Pr "
    "mod bmod pmod"
).split()

# 行内 span：开 $ 紧邻非空白、闭 $ 紧邻非空白（货币启发式），不含换行。
_INLINE_RE = re.compile(r"\$(?=[^\s$])([^$\n]+?)(?<=[^\s])\$")
_DISPLAY_DOLLAR_RE = re.compile(r"\$\$([\s\S]+?)\$\$")
_DISPLAY_BRACKET_RE = re.compile(r"\\\[((?:[^\]\\]|\\.)+?)\\\]")
_INLINE_PAREN_RE = re.compile(r"\\\(((?:[^)\\]|\\.)+?)\\\)")
# 闭合围栏（流式中未闭合围栏按普通文本走，与 Web 端同纪律）。
_FENCE_RE = re.compile(r"```[^\n]*\n[\s\S]*?```")
# 行内代码（`` `code` ``）：数学不在代码里排版。
_CODE_SPAN_RE = re.compile(r"`[^`\n]+`")


def render_math(text: str) -> str:
    """把文本中的已闭合 LaTeX 数学 span 转成 Unicode 近似；其余原样。

    围栏代码块与行内代码先抽离再恢复（内部不转写——数学不在代码里排版）；
    四种定界符各归各的正则，行内 ``$...$`` 带货币启发式。逐 token 流式
    友好：未闭合的 span 不匹配正则，自然保持原样，闭合帧整体成型。
    """
    if "$" not in text and "\\(" not in text and "\\[" not in text:
        return text  # 快路径：无数学迹象，零开销
    protected: list[str] = []

    def _keep(m: re.Match) -> str:
        protected.append(m.group(0))
        return f"\x00F{len(protected) - 1}\x00"

    src = _FENCE_RE.sub(_keep, text)
    src = _CODE_SPAN_RE.sub(_keep, src)
    src = _DISPLAY_DOLLAR_RE.sub(lambda m: _convert(m.group(1)), src)
    src = _DISPLAY_BRACKET_RE.sub(lambda m: _convert(m.group(1)), src)
    src = _INLINE_PAREN_RE.sub(lambda m: _convert(m.group(1)), src)
    src = _INLINE_RE.sub(lambda m: _convert(m.group(1)), src)
    return re.sub(r"\x00F(\d+)\x00", lambda m: protected[int(m.group(1))], src)


# ── span 内部转写 ────────────────────────────────────────────────

_CMD_RE = re.compile(r"\\([a-zA-Z]+|.)")
_ARG_RE = re.compile(r"\s*(\{[^{}]*\}|\\[a-zA-Z]+|[^\s{}])")  # {组} / 命令 / 单 token


def _arg(src: str, at: int) -> tuple[str, int]:
    """读 at 起的一个参数：{组}（剥括号）、命令或单个 token。返回 (文本, 下一位置)。"""
    m = _ARG_RE.match(src, at)
    if not m:
        return ("", at)
    tok = m.group(1)
    return (tok[1:-1] if tok.startswith("{") else tok, m.end())


def _convert(src: str) -> str:
    """一个数学 span → Unicode 近似。命令流处理 + 花括号剥组。"""
    out: list[str] = []
    pos = 0
    while pos < len(src):
        ch = src[pos]
        if ch == "\\":
            m = _CMD_RE.match(src, pos)
            if not m:  # 尾部孤立反斜杠
                out.append(ch)
                pos += 1
                continue
            text, pos = _command(m.group(1), src, m.end())
            out.append(text)
            continue
        if ch in "{}":  # 裸花括号 = 纯分组，剥壳留内容
            pos += 1
            continue
        if ch in "^_":
            raw, pos = _arg(src, pos + 1)
            out.append(_script(_convert(raw), ch))
            continue
        if ch == "&":  # 对齐列 → 空格
            out.append(" ")
            pos += 1
            continue
        out.append(ch)
        pos += 1
    return "".join(out)


def _command(name: str, src: str, pos: int) -> tuple[str, int]:
    """单个命令的转写；返回 (文本, 消费后的下一位置)——带参命令把参数
    一并吃掉，主循环才不会把参数体再输出一遍。"""
    if name in SYMBOLS:
        return SYMBOLS[name], pos
    if name in ACCENTS:
        text, p = _arg(src, pos)
        accented = (text[:1] + ACCENTS[name] + text[1:]) if text else text
        return accented, p
    if name in ("frac", "dfrac", "tfrac"):
        num, p = _arg(src, pos)
        den, p = _arg(src, p)
        return _frac(_convert(num), _convert(den)), p
    if name == "sqrt":
        root = ""
        if pos < len(src) and src[pos] == "[":  # 可选参 [n]：ⁿ√
            close = src.find("]", pos)
            if close != -1:
                root, pos = src[pos + 1:close], close + 1
        text, p = _arg(src, pos)
        body = _convert(text)
        sup = _script(_convert(root), "^") if root else ""
        return f"{sup}√{body if len(body) <= 1 else f'({body})'}", p
    if name in ("text", "operatorname"):
        text, p = _arg(src, pos)
        return text, p
    if name in _FONTS:
        text, p = _arg(src, pos)
        return _font_text(name, _convert(text)), p
    if name in ("left", "right", "big", "Big", "bigg", "Bigg"):
        if pos < len(src) and src[pos] == ".":  # 不可见定界符连字符一起吞
            return "", pos + 1
        return "", pos  # 定界符字符留给主循环原样输出
    if name in ("begin", "end"):
        _, p = _arg(src, pos)
        return "", p
    if name in _OP_NAMES:
        return name, pos
    if name in ("limits", "nolimits", "displaystyle", "textstyle",
                "scriptstyle", "scriptscriptstyle"):
        return "", pos
    if name in (",", ";", ":", " "):
        return " ", pos
    if name == "!":
        return "", pos
    if name == "quad":
        return "  ", pos
    if name == "qquad":
        return "    ", pos
    if name == "\\":  # LaTeX 行间换行（源里两个反斜杠被 _CMD_RE 并作一处）
        return "\n", pos
    if len(name) == 1 and name in ".,;:!?":
        return name, pos
    # 未知命令：剥反斜杠留名——可读的降级好过静默吞字
    return name, pos


def _frac(num: str, den: str) -> str:
    """分式：两侧都是单 token → 斜杠分式（1⁄2）；否则括号包裹。"""
    if len(num) <= 1 and len(den) <= 1:
        return f"{num}⁄{den}"

    def _side(t: str) -> str:
        return t if len(t) <= 1 and t.isalnum() else f"({t})"

    return f"{_side(num)}/{_side(den)}"


def _script(text: str, mark: str) -> str:
    """上/下标：逐字符映射；任一字符无变体 → 整组保留原记法。"""
    table = _SUPERSCRIPT if mark == "^" else _SUBSCRIPT
    if text and all(c in table for c in text):
        return "".join(table[c] for c in text)
    # 退路：原记法（^q / _{n+1}）——半个怪字符好过错误排版
    return f"{mark}{text}" if len(text) <= 1 and text.isalnum() else f"{mark}{{{text}}}"


def _font_text(font: str, text: str) -> str:
    """字体族：逐字符映射到数学字母表，无映射（数字/符号）原样。"""
    table = _FONTS[font]
    if not table:
        return text
    out = []
    for c in text:
        idx = ord(c) - ord("A") if "A" <= c <= "Z" else (
            ord(c) - ord("a") + 26 if "a" <= c <= "z" else -1)
        out.append(table[idx] if 0 <= idx < len(table) else c)
    return "".join(out)


if __name__ == "__main__":
    _r = render_math
    # 行内 / 展示 / 各种定界符
    assert _r("能量 $E=mc^2$ 守恒") == "能量 E=mc² 守恒"
    assert _r("$$\\alpha + \\beta$$") == "α + β"
    assert _r("\\[x \\leq 10\\]") == "x ≤ 10"
    assert _r("\\(a_1 + a_2\\)") == "a₁ + a₂"
    # 积分带上下标（命令作参数）与间距命令
    assert _r("$$\\int_0^\\infty e^{-x}\\,dx$$") == "∫₀^{∞} e⁻ˣ dx"
    # 货币启发式：闭 $ 前有空白 → 不是数学
    assert _r("cost $100 and $200 total") == "cost $100 and $200 total"
    # 未闭合 span 不动（流式）
    assert _r("value $$x^2") == "value $$x^2"
    assert _r("value $x^2") == "value $x^2"
    # 围栏内不动
    assert _r("```\n$\\alpha$\n```") == "```\n$\\alpha$\n```"
    # 分式 / 根式 / 上下标（含退路）
    assert _r("$\\frac{1}{2}$") == "1⁄2"
    assert _r("$\\frac{n+1}{2}$") == "(n+1)/2"
    assert _r("$\\sqrt{x}$") == "√x"
    assert _r("$\\sqrt{b^2-4ac}$") == "√(b²-4ac)"
    assert _r("$\\sqrt[3]{x}$") == "³√x"
    assert _r("$x^{10}$") == "x¹⁰"
    assert _r("$x^q$") == "x^q"  # q 无上标变体 → 原记法
    assert _r("$x^n + y_n$") == "xⁿ + yₙ"
    # 字体族 / 重音 / 函数名 / 未知命令
    assert _r("$\\mathbb{R}$") == "ℝ"
    assert _r("$\\vec{v}$") == "v⃗"
    assert _r("$\\sin^2 x$") == "sin² x"
    assert _r("$\\foo{x}$") == "foox"  # 未知命令剥名，参数照转
    # 环境 / 对齐：剥 begin/end、& → 空格、\\ → 换行
    _env = _r("$$\\begin{aligned} a &= 1 \\\\ b &= 2 \\end{aligned}$$")
    assert "aligned" not in _env and "\n" in _env and "=" in _env
    # \left(\right) 剥命令留定界符；\left. \right. 连点一起吞
    assert _r("$\\left( a+b \\right)$") == "( a+b )"
    assert _r("$\\left. x \\right|$") == " x |"
    # 快路径：无数学迹象零改动（转义美元不构成 span）
    _plain = "价格是 5 元, 共 100 元"
    assert _r(_plain) == _plain
    print("openx/ui/_math_text.py OK ✓")
