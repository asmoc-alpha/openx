"""Prompt packs —— 按需加载的提示片段（渐进披露的第二条通道）。

系统提示不该一开机就背下所有专业化契约：**常驻的只有目录**（一条 pack 一行
说明），正文在真正要用时才进上下文。这与 skills（``skills.py``）是同一条
哲学，区别在归属——skills 是**用户/项目**的指令包（``SKILL.md``，可由用户
安装、可含附带文件），prompt packs 是**产品内置**的专业化片段（编写插件/
组件这类场景化契约），随版本一起走、不需要安装。

三条激活通道（都由消费方实现，本模块只提供数据与渲染）：

- **关键词自动**：用户消息命中 ``triggers``（小写子串）即激活；
- **模型主动**：``load_prompt`` 工具（``tools/prompt_tool.py``）；
- **用户显式**：``/prompt <name>`` 斜杠命令。

渲染契约（由 ``builtin-context`` 插件当 context/v1 片段产出）：

- 目录恒在（含 ``[loaded]`` 标记），正文只在激活后出现，用
  ``<!-- BEGIN PROMPT PACK: <name> -->`` / ``<!-- END ... -->`` 包裹；
- 片段**不含前导空行/分隔符**——分隔由消费方统一施加（同
  ``instructions.format_instructions``）。

失败语义：``render_prompt_packs`` 是纯函数，不读盘、不抛异常；未知/空
registry 一律返回空串（对提示组装而言"没有 pack"与"贡献为空"同构）。
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

from dataclasses import dataclass, field
from typing import Collection, Iterable, Optional

# 目录小节标题（消费方不感知文案，改这里即可）
CATALOG_HEADER = "## Prompt Packs (loaded on demand)"

_CATALOG_PREAMBLE = """\
The catalog below is always in context; a pack's FULL text is not — it enters
the prompt only once it is active. Activate one when your work matches it:

- call the `load_prompt` tool with the pack name (preferred), or
- the user types `/prompt <name>`, or
- a request mentioning one of the pack's trigger words activates it by itself.

Load a pack BEFORE doing the work it covers — the contract in it is what makes
the result valid; guessing from the one-line summary is not enough.
"""


# ── 数据模型 ────────────────────────────────────────────────────


@dataclass(frozen=True)
class PromptPack:
    """一个按需加载的提示片段。

    ``summary`` 是**恒常驻**的那一行——它决定模型是否知道该加载这个 pack，
    所以要写清"什么工作在它覆盖范围内"。``triggers`` 是自动激活的关键词
    （小写、子串匹配；宁窄勿宽，误命中全是白付的 token）。
    """

    name: str
    summary: str
    body: str
    triggers: tuple[str, ...] = ()

    @property
    def chars(self) -> int:
        return len(self.body)


@dataclass
class PromptPackRegistry:
    """按注册序排列的 pack 集合（同名后者覆盖，覆盖即原地保序）。

    空 registry 是合法状态（渲染零贡献）——"装了但一个 pack 都没有"与
    "没有这个提示源"同构，消费方不必分支。装载入口是
    :func:`load_prompt_packs`。
    """

    packs: dict[str, PromptPack] = field(default_factory=dict)

    # ── 查询 ────────────────────────────────────────────────────
    def get(self, name: str) -> Optional[PromptPack]:
        return self.packs.get(str(name or "").strip())

    def names(self) -> list[str]:
        return list(self.packs)

    def match(self, text: str) -> list[str]:
        """关键词命中：返回命中 ``triggers`` 的 pack 名（注册序，可多选）。

        子串匹配而非分词——中文没有空格可切，「帮我写个插件」要能命中
        ``插件``；代价是 triggers 必须写得够具体（见各 pack 的注释）。
        """
        lowered = str(text or "").lower()
        if not lowered:
            return []
        return [
            pack.name
            for pack in self.packs.values()
            if any(t and t in lowered for t in pack.triggers)
        ]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name.strip() in self.packs

    def __iter__(self):
        """迭代 pack 名（注册序）——``sorted(registry)`` 即可用名清单。"""
        return iter(self.packs)

    def __len__(self) -> int:
        return len(self.packs)


# ── 内置 pack（正文按需加载）────────────────────────────────────


def _plugin_spec() -> str:
    """插件编写契约正文（``kernel/assembly/plugin_spec.py`` 是唯一真源）。

    **惰性 import 是硬要求**：``openx.kernel`` 的包初始化会拉起
    ``openx.builtin``，而 ``builtin/context.py`` 又 import 本模块——模块级
    import kernel 会成环（本模块是那个循环里的下游）。正文反正只在**装载
    pack**时才需要，装载发生在 agent 构造之后，届时内核早已就位。
    """
    from .kernel.assembly.plugin_spec import PLUGIN_SPEC

    return PLUGIN_SPEC


def builtin_packs() -> tuple[PromptPack, ...]:
    """内置 pack 表（加 pack 就在这里加一条——正文与关键词一起写在这）。

    triggers 意图覆盖"要动手写/改插件"的说法；刻意不收裸「组件」——
    React/Vue 组件讨论会误命中，白付 ~700 token。
    """
    return (
        PromptPack(
            name="plugin-authoring",
            summary=(
                "how to write an OpenX plugin or component: single-file manifest, "
                "the four apply(ctx) protocols (tool / context / lifecycle / "
                "ui.panel), self_test — load before calling write_plugin"
            ),
            body=_plugin_spec(),
            triggers=(
                "插件",
                "plugin",
                "write_plugin",
                "load_plugin",
                "unload_plugin",
                "self_test",
                "apply(ctx)",
                "register_tool_factory",
                "register_context",
                "register_ui_slot",
                "capability.tool",
                "context.memory",
                "ui.panel",
                "开发组件",
                "组件开发",
            ),
        ),
    )


def load_prompt_packs() -> PromptPackRegistry:
    """装载内置 pack 集合（纯内存；不读盘、不可能失败）。"""
    return PromptPackRegistry({pack.name: pack for pack in builtin_packs()})


# ── 渲染（context/v1 片段）──────────────────────────────────────


def render_prompt_packs(
    registry: Optional[PromptPackRegistry],
    active: Optional[Collection[str]] = None,
) -> str:
    """把目录 + 已激活正文渲染成系统提示**片段**（无 pack → 空串）。

    ``registry is None``（子代理 / 无 host 调用）→ 空串：没有目录就没有
    引导，正文更不该出现。
    """
    if registry is None or not len(registry):
        return ""
    loaded = {str(n).strip() for n in (active or ()) if str(n).strip()}

    parts: list[str] = [CATALOG_HEADER + "\n\n", _CATALOG_PREAMBLE, "\n"]
    for pack in registry.packs.values():
        mark = " [loaded]" if pack.name in loaded else ""
        parts.append(f"- `{pack.name}`{mark}: {pack.summary}\n")

    bodies = [
        pack for name, pack in registry.packs.items() if name in loaded
    ]
    for pack in bodies:
        parts.append(f"\n<!-- BEGIN PROMPT PACK: {pack.name} -->\n")
        parts.append(pack.body.strip() + "\n")
        parts.append(f"<!-- END PROMPT PACK: {pack.name} -->\n")

    return "".join(parts).strip()


def active_names(packs: Iterable[str], registry: PromptPackRegistry) -> list[str]:
    """筛掉未知名、去重、按注册序（``/prompt`` 展示与事件记账共用）。"""
    wanted = {str(n).strip() for n in packs}
    return [name for name in registry.packs if name in wanted]


if __name__ == "__main__":
    reg = load_prompt_packs()
    assert "plugin-authoring" in reg, reg.names()
    assert sorted(reg) == ["plugin-authoring"], sorted(reg)

    # 目录常驻、正文不在
    catalog = render_prompt_packs(reg, ())
    assert CATALOG_HEADER in catalog
    assert "`plugin-authoring`:" in catalog and "[loaded]" not in catalog
    assert "__openx_meta__" not in catalog, "未激活时正文不得进提示"

    # 激活后正文 + 标记 + BEGIN/END 边界
    body = render_prompt_packs(reg, {"plugin-authoring"})
    assert "[loaded]" in body and "__openx_meta__" in body
    assert "<!-- BEGIN PROMPT PACK: plugin-authoring -->" in body
    assert body.index("<!-- END PROMPT PACK: plugin-authoring -->") > body.index(
        "<!-- BEGIN PROMPT PACK: plugin-authoring -->"
    )
    assert body.startswith(CATALOG_HEADER), "片段不得带前导空行"

    # 关键词匹配：正反例
    assert reg.match("帮我写个插件，注册一个工具") == ["plugin-authoring"]
    assert reg.match("help me develop a plugin for this repo") == ["plugin-authoring"]
    assert reg.match("这个 React 组件怎么拆分") == [], "裸「组件」不得误命中"
    assert reg.match("用户想要开发组件再加载 prompt") == ["plugin-authoring"]
    assert reg.match("") == [] and reg.match("重构登录页面") == []

    # 空/缺 registry：零贡献，不炸
    assert render_prompt_packs(None) == ""
    assert render_prompt_packs(PromptPackRegistry(packs={})) == ""
    assert active_names(["ghost", "plugin-authoring", "plugin-authoring"], reg) == [
        "plugin-authoring"
    ]

    print(
        f"prompt packs: {reg.names()} "
        f"catalog={len(catalog)} chars, "
        f"body={reg.get('plugin-authoring').chars} chars ✓"
    )
