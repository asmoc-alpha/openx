"""Memory Tool — agent 自主决定何时记忆/召回的内置工具。

设计理念：记忆不是用户手动管理的，而是 agent 在对话中自主判断：
- 用户纠正了某个做法 → 记住正确方式
- 发现项目约定/规范 → 记住以免下次再犯
- 做出架构决策 → 记住理由
- 踩坑后找到解法 → 记住避免重复

Agent 通过调用此工具完成 remember / recall / forget 操作。
"""

from __future__ import annotations

# ── 独立调试支持 ──────────────────────────────────────────────────
if __name__ == "__main__" and not __package__:
    import sys as _sys
    from pathlib import Path as _Path
    _file = _Path(__file__).resolve()
    _root = _file.parent
    while _root != _root.parent and not (_root / "pyproject.toml").exists():
        _root = _root.parent
    _sys.path.insert(0, str(_root))
    __package__ = ".".join(_file.relative_to(_root).parts[:-1])

from typing import Any

from .base import Tool, ToolResult
from ..coding_memory import CodingMemoryStore, CATEGORIES


class MemoryTool(Tool):
    """Agent 自主记忆工具：remember / recall / forget。

    Agent 在对话中判断某信息值得持久化时调用此工具，无需用户干预。
    """

    name = "memory"
    description = (
        "Persistent coding memory. Use this tool to remember important facts, "
        "conventions, decisions, and patterns for future sessions.\n\n"
        "WHEN TO USE:\n"
        "- User corrects your approach → remember the correct way\n"
        "- You discover a project convention (naming, structure, testing) → remember it\n"
        "- An architecture/design decision is made → remember the rationale\n"
        "- You find a tricky bug solution → remember the pattern\n"
        "- User states a preference → remember it\n"
        "- You need to recall previously stored knowledge → use action='recall'\n\n"
        "WHEN NOT TO USE:\n"
        "- Information obvious from reading the code\n"
        "- Temporary/task-specific details that won't recur\n"
        "- General programming knowledge (not project-specific)"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["remember", "recall", "forget"],
                "description": (
                    "remember: store a new fact/convention/pattern. "
                    "recall: search stored memories by query or file context. "
                    "forget: remove a memory by its id."
                ),
            },
            "content": {
                "type": "string",
                "description": "The fact/convention/pattern to remember (required for 'remember').",
            },
            "category": {
                "type": "string",
                "enum": list(CATEGORIES.keys()),
                "description": (
                    "Classification: project_fact (build/structure), "
                    "code_convention (naming/style/patterns), "
                    "architecture_decision (design choices + rationale), "
                    "debug_pattern (bug solutions), "
                    "dependency (version/compat info), "
                    "workflow (CI/CD/release process)."
                ),
            },
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Search keywords for future recall (2-5 terms).",
            },
            "related_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "File paths or globs this memory relates to (e.g. ['src/**', 'Makefile']).",
            },
            "query": {
                "type": "string",
                "description": "Search query for 'recall' action.",
            },
            "context_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Current file paths being worked on (boosts path-related recall).",
            },
            "memory_id": {
                "type": "string",
                "description": "Memory ID to forget (required for 'forget').",
            },
        },
        "required": ["action"],
    }

    def __init__(self, store: CodingMemoryStore) -> None:
        self._store = store

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = kwargs.get("action", "")

        if action == "remember":
            return self._do_remember(kwargs)
        elif action == "recall":
            return self._do_recall(kwargs)
        elif action == "forget":
            return self._do_forget(kwargs)
        else:
            return ToolResult(error=f"Unknown action: {action!r}. Use remember/recall/forget.")

    def _do_remember(self, args: dict) -> ToolResult:
        content = args.get("content", "").strip()
        if not content:
            return ToolResult(error="'content' is required for remember action.")

        category = args.get("category", "project_fact")
        keywords = args.get("keywords") or []
        related_paths = args.get("related_paths") or []

        mem = self._store.remember(
            content,
            category=category,
            keywords=keywords,
            related_paths=related_paths,
            scope="project",
            source="agent",
        )
        return ToolResult(
            output=f"Remembered [{mem.category}] id={mem.id}: {mem.content[:80]}"
        )

    def _do_recall(self, args: dict) -> ToolResult:
        query = args.get("query", "")
        context_paths = args.get("context_paths") or []

        results = self._store.recall(
            query=query,
            context_paths=context_paths,
            limit=8,
        )
        if not results:
            return ToolResult(output="No relevant memories found.")

        lines = [f"Found {len(results)} relevant memories:"]
        for score, mem in results:
            path_hint = f" [{', '.join(mem.related_paths[:2])}]" if mem.related_paths else ""
            lines.append(
                f"- [{mem.category}] (id={mem.id}, score={score:.1f}){path_hint}: "
                f"{mem.content[:120]}"
            )
        return ToolResult(output="\n".join(lines))

    def _do_forget(self, args: dict) -> ToolResult:
        mem_id = args.get("memory_id", "").strip()
        if not mem_id:
            return ToolResult(error="'memory_id' is required for forget action.")
        if self._store.forget(mem_id):
            return ToolResult(output=f"Forgot memory: {mem_id}")
        return ToolResult(error=f"Memory not found: {mem_id}")


# ── 系统提示中的记忆指令 ─────────────────────────────────────────

MEMORY_INSTRUCTIONS = """
## Memory System

You have a persistent coding memory (tool: `memory`). Use it autonomously:

**Auto-remember when:**
- User corrects you ("no, we use X not Y") → remember the correct approach
- You discover project conventions by reading code → remember for next session
- A design/architecture decision is made → remember the rationale
- You solve a non-obvious bug → remember the pattern
- User expresses a preference → remember it

**Auto-recall when:**
- Starting work on a file/module → recall related conventions
- About to make a design choice → recall prior decisions
- Encountering an error → recall past debug patterns

**Rules:**
- Be concise: one memory = one atomic fact
- Always include keywords for future retrieval
- Link related_paths when the memory is file-specific
- Don't store what's obvious from reading the code
- Don't store temporary task details
"""


if __name__ == "__main__":
    import asyncio
    import tempfile
    from pathlib import Path

    async def _test():
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "proj"
            ws.mkdir()
            store = CodingMemoryStore(
                workspace=str(ws),
                global_dir=Path(td) / "global",
                projects_root=Path(td) / "projects",  # 自检不碰真实 home
            )
            tool = MemoryTool(store)

            # remember
            r = await tool.execute(
                action="remember",
                content="测试用 pytest，不用 unittest",
                category="code_convention",
                keywords=["pytest", "test"],
                related_paths=["tests/**"],
            )
            assert r.success and "Remembered" in r.output
            print(f"remember: {r.output} ✓")

            # recall
            r = await tool.execute(action="recall", query="test framework")
            assert r.success and "pytest" in r.output
            print(f"recall: found ✓")

            # recall by path
            r = await tool.execute(action="recall", context_paths=["tests/test_main.py"])
            assert r.success and "pytest" in r.output
            print(f"recall(path): found ✓")

            # forget
            mem_id = store.list_all()[0].id
            r = await tool.execute(action="forget", memory_id=mem_id)
            assert r.success
            print(f"forget: ✓")

            # schema
            schema = tool.to_openai_schema()
            assert schema["function"]["name"] == "memory"
            print(f"schema: OK ✓")

        print("\ntools/memory_tool.py OK ✓")

    asyncio.run(_test())
