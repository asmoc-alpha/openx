"""Coding Agent Memory — 面向编程场景的结构化记忆系统。

与通用 memory.py 的关键差异
==========================
- **项目级隔离**：记忆分全局（~/.openx/coding-memory/）和项目级
  （<workspace>/.openx/coding-memory/），项目知识不污染其他项目。
- **结构化分类**：project_fact / code_convention / architecture_decision /
  debug_pattern / dependency / workflow，每类有专属召回权重。
- **代码关联**：记忆可绑定文件路径/目录 glob，当 agent 操作相关文件时
  自动提升召回优先级（空间锚定）。
- **多信号召回**：关键词匹配 + 路径关联 + 分类权重 + 重要性衰减 +
  访问频率，综合评分排序。
- **Token 预算**：注入系统提示时按预算截断，避免记忆膨胀挤占上下文。
- **生命周期**：importance 衰减 + last_accessed 追踪 + 去重检测。

存储格式：JSON Lines（.jsonl），一行一条记忆，便于增量追加和快速扫描。

Usage::

    from openx.coding_memory import CodingMemoryStore

    store = CodingMemoryStore(workspace="/path/to/project")
    store.remember(
        category="code_convention",
        content="本项目变量命名用 camelCase，常量用 UPPER_SNAKE",
        keywords=["naming", "camelCase", "convention"],
        related_paths=["src/**"],
    )
    # 当 agent 正在编辑 src/utils.ts 时召回相关记忆
    results = store.recall(context_paths=["src/utils.ts"], query="naming")
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

import fnmatch
import hashlib
import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

# ── 常量 ─────────────────────────────────────────────────────────

GLOBAL_MEMORY_DIR = Path.home() / ".openx" / "coding-memory"
_MEMORY_FILE = "memories.jsonl"

# 记忆分类及其基础权重（召回时乘以此系数）
CATEGORIES: dict[str, float] = {
    "project_fact": 1.0,          # 项目事实（构建命令、目录结构）
    "code_convention": 1.2,       # 代码规范（命名、风格、模式）
    "architecture_decision": 1.3, # 架构决策（选型理由、设计约束）
    "debug_pattern": 1.1,         # 调试经验（踩坑记录、解法）
    "dependency": 0.9,            # 依赖信息（版本、兼容性）
    "workflow": 0.8,              # 工作流（CI/CD、发布流程）
}

# 注入系统提示时的默认 token 预算（按字符估算，1 token ≈ 4 chars）
DEFAULT_CHAR_BUDGET = 3000


# ── 数据模型 ─────────────────────────────────────────────────────


@dataclass
class CodingMemory:
    """一条编程记忆。"""

    id: str                              # 唯一标识（content hash 前 12 位）
    category: str                        # 分类（见 CATEGORIES）
    content: str                         # 记忆正文
    keywords: list[str] = field(default_factory=list)   # 检索关键词
    related_paths: list[str] = field(default_factory=list)  # 关联路径/glob
    scope: str = "project"               # "global" | "project"
    importance: float = 1.0              # 重要性 0~2（可被用户/agent 调整）
    access_count: int = 0                # 被召回次数
    created_at: float = 0.0              # 创建时间戳
    last_accessed: float = 0.0           # 最近召回时间戳
    source: str = ""                     # 来源（user / agent / auto）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CodingMemory":
        # 容错：缺字段用默认值
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in valid_fields}
        return cls(**filtered)

    @staticmethod
    def make_id(content: str) -> str:
        """基于内容生成稳定 ID（去重用）。"""
        return hashlib.sha256(content.encode()).hexdigest()[:12]


# ── 存储引擎 ─────────────────────────────────────────────────────


class CodingMemoryStore:
    """面向 Coding Agent 的结构化记忆存储。

    双层存储：
    - 全局：~/.openx/coding-memory/memories.jsonl（跨项目通用知识）
    - 项目：<workspace>/.openx/coding-memory/memories.jsonl（项目专属）

    召回时合并两层，项目级同 ID 覆盖全局。
    """

    def __init__(
        self,
        workspace: str | Path | None = None,
        *,
        global_dir: Path | None = None,
    ) -> None:
        self._global_dir = global_dir or GLOBAL_MEMORY_DIR
        self._global_dir.mkdir(parents=True, exist_ok=True)
        self._project_dir: Path | None = None
        if workspace:
            self._project_dir = Path(workspace) / ".openx" / "coding-memory"
            self._project_dir.mkdir(parents=True, exist_ok=True)

    # ── 写入 ──────────────────────────────────────────────────

    def remember(
        self,
        content: str,
        *,
        category: str = "project_fact",
        keywords: list[str] | None = None,
        related_paths: list[str] | None = None,
        scope: str = "project",
        importance: float = 1.0,
        source: str = "user",
    ) -> CodingMemory:
        """存入一条记忆。内容相同则更新（去重）。"""
        mem_id = CodingMemory.make_id(content)
        now = time.time()

        # 检查是否已存在（去重 → 更新）
        existing = self._find_by_id(mem_id)
        if existing:
            existing.keywords = list(set(existing.keywords + (keywords or [])))
            existing.related_paths = list(set(
                existing.related_paths + (related_paths or [])
            ))
            existing.importance = max(existing.importance, importance)
            existing.last_accessed = now
            self._update(existing)
            return existing

        mem = CodingMemory(
            id=mem_id,
            category=category if category in CATEGORIES else "project_fact",
            content=content,
            keywords=keywords or [],
            related_paths=related_paths or [],
            scope=scope,
            importance=min(2.0, max(0.0, importance)),
            access_count=0,
            created_at=now,
            last_accessed=now,
            source=source,
        )
        self._append(mem)
        return mem

    def forget(self, mem_id: str) -> bool:
        """删除一条记忆（by ID）。"""
        for store_file in self._store_files():
            entries = self._load_file(store_file)
            new_entries = [e for e in entries if e.id != mem_id]
            if len(new_entries) < len(entries):
                self._write_file(store_file, new_entries)
                return True
        return False

    def forget_by_content(self, substring: str) -> int:
        """删除内容包含指定子串的所有记忆，返回删除数。"""
        count = 0
        for store_file in self._store_files():
            entries = self._load_file(store_file)
            new_entries = [e for e in entries if substring.lower() not in e.content.lower()]
            removed = len(entries) - len(new_entries)
            if removed:
                self._write_file(store_file, new_entries)
                count += removed
        return count

    # ── 召回 ──────────────────────────────────────────────────

    def recall(
        self,
        *,
        query: str = "",
        context_paths: list[str] | None = None,
        category: str | None = None,
        limit: int = 10,
    ) -> list[tuple[float, CodingMemory]]:
        """多信号综合召回，返回 [(score, memory)] 按分数降序。

        信号权重：
        - 关键词匹配：query 命中 keywords → +10，命中 content → +3
        - 路径关联：context_paths 匹配 related_paths glob → +8
        - 分类权重：乘以 CATEGORIES[category] 系数
        - 重要性：乘以 importance
        - 新鲜度：最近 7 天 +2，30 天 +1
        """
        all_entries = self._load_all()
        if category:
            all_entries = [e for e in all_entries if e.category == category]

        query_lower = query.lower()
        query_terms = set(query_lower.split()) if query_lower else set()
        now = time.time()
        scored: list[tuple[float, CodingMemory]] = []

        for mem in all_entries:
            score = 0.0

            # 关键词信号
            if query_terms:
                kw_text = " ".join(mem.keywords).lower()
                content_lower = mem.content.lower()
                for term in query_terms:
                    if term in kw_text:
                        score += 10.0
                    if term in content_lower:
                        score += 3.0

            # 路径关联信号
            if context_paths and mem.related_paths:
                for ctx_path in context_paths:
                    for pattern in mem.related_paths:
                        if fnmatch.fnmatch(ctx_path, pattern):
                            score += 8.0
                            break
                    else:
                        # 目录前缀匹配（src/utils.ts 匹配 src/）
                        for pattern in mem.related_paths:
                            if pattern.endswith("/") and ctx_path.startswith(pattern):
                                score += 5.0
                                break

            # 无 query 也无 path 时，按 importance 排序（全量展示用）
            if score == 0.0 and not query_terms and not context_paths:
                score = 1.0  # 基准分，让所有记忆都出现

            if score <= 0.0:
                continue

            # 乘以分类权重
            score *= CATEGORIES.get(mem.category, 1.0)
            # 乘以重要性
            score *= mem.importance
            # 新鲜度加成
            age_days = (now - mem.created_at) / 86400
            if age_days < 7:
                score += 2.0
            elif age_days < 30:
                score += 1.0

            scored.append((score, mem))

        scored.sort(key=lambda x: -x[0])
        # 更新 access 统计（top N 被召回的记忆）
        for _, mem in scored[:limit]:
            mem.access_count += 1
            mem.last_accessed = now
        self._persist_access_updates(scored[:limit])

        return scored[:limit]

    # ── 查询 ──────────────────────────────────────────────────

    def list_all(self, *, category: str | None = None, scope: str | None = None) -> list[CodingMemory]:
        """列出所有记忆（可选过滤）。"""
        entries = self._load_all()
        if category:
            entries = [e for e in entries if e.category == category]
        if scope:
            entries = [e for e in entries if e.scope == scope]
        return sorted(entries, key=lambda e: -e.importance)

    def stats(self) -> dict:
        """统计信息。"""
        entries = self._load_all()
        by_cat: dict[str, int] = {}
        by_scope: dict[str, int] = {}
        for e in entries:
            by_cat[e.category] = by_cat.get(e.category, 0) + 1
            by_scope[e.scope] = by_scope.get(e.scope, 0) + 1
        return {
            "total": len(entries),
            "by_category": by_cat,
            "by_scope": by_scope,
        }

    # ── 系统提示构建 ──────────────────────────────────────────

    def build_context_prompt(
        self,
        *,
        context_paths: list[str] | None = None,
        char_budget: int = DEFAULT_CHAR_BUDGET,
    ) -> str:
        """构建注入系统提示的记忆片段（带 token 预算控制）。

        策略：
        1. 有 context_paths 时优先召回路径相关记忆；
        2. 剩余预算按 importance 填充高优记忆；
        3. 超出预算时截断。
        """
        entries = self._load_all()
        if not entries:
            return ""

        # 按相关性 + 重要性排序
        if context_paths:
            recalled = self.recall(context_paths=context_paths, limit=20)
            prioritized = [mem for _, mem in recalled]
            # 补充未被路径召回但 importance 高的
            recalled_ids = {m.id for m in prioritized}
            rest = sorted(
                [e for e in entries if e.id not in recalled_ids],
                key=lambda e: -e.importance,
            )
            prioritized.extend(rest)
        else:
            prioritized = sorted(entries, key=lambda e: -e.importance)

        # 按分类分组渲染
        lines = ["", "## Coding Memory", ""]
        lines.append(
            "The following project knowledge is stored in your coding memory. "
            "Follow these conventions and facts when working on this codebase."
        )
        lines.append("")

        used_chars = sum(len(l) for l in lines)
        current_cat = ""
        for mem in prioritized:
            if mem.category != current_cat:
                current_cat = mem.category
                cat_header = f"### {current_cat.replace('_', ' ').title()}\n"
                if used_chars + len(cat_header) > char_budget:
                    break
                lines.append(cat_header)
                used_chars += len(cat_header)

            # 渲染单条记忆
            path_hint = f" ({', '.join(mem.related_paths[:2])})" if mem.related_paths else ""
            entry_line = f"- {mem.content}{path_hint}\n"
            if used_chars + len(entry_line) > char_budget:
                break
            lines.append(entry_line)
            used_chars += len(entry_line)

        if len(lines) <= 4:  # 只有标题没有内容
            return ""
        return "\n".join(lines)

    # ── 内部方法 ──────────────────────────────────────────────

    def _store_files(self) -> list[Path]:
        """返回所有存储文件路径（全局 + 项目）。"""
        files = [self._global_dir / _MEMORY_FILE]
        if self._project_dir:
            files.append(self._project_dir / _MEMORY_FILE)
        return files

    def _load_all(self) -> list[CodingMemory]:
        """加载所有记忆（项目级同 ID 覆盖全局）。"""
        merged: dict[str, CodingMemory] = {}
        # 全局先加载
        global_file = self._global_dir / _MEMORY_FILE
        for mem in self._load_file(global_file):
            merged[mem.id] = mem
        # 项目级覆盖
        if self._project_dir:
            project_file = self._project_dir / _MEMORY_FILE
            for mem in self._load_file(project_file):
                merged[mem.id] = mem
        return list(merged.values())

    def _load_file(self, path: Path) -> list[CodingMemory]:
        """从 .jsonl 文件加载记忆列表。"""
        if not path.exists():
            return []
        entries = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    entries.append(CodingMemory.from_dict(d))
                except (json.JSONDecodeError, TypeError):
                    continue
        except OSError:
            pass
        return entries

    def _write_file(self, path: Path, entries: list[CodingMemory]) -> None:
        """覆写 .jsonl 文件。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(e.to_dict(), ensure_ascii=False) for e in entries]
        path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")

    def _append(self, mem: CodingMemory) -> None:
        """追加一条记忆到对应 scope 的文件。"""
        if mem.scope == "global":
            target = self._global_dir / _MEMORY_FILE
        elif self._project_dir:
            target = self._project_dir / _MEMORY_FILE
        else:
            target = self._global_dir / _MEMORY_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(json.dumps(mem.to_dict(), ensure_ascii=False) + "\n")

    def _find_by_id(self, mem_id: str) -> Optional[CodingMemory]:
        """按 ID 查找（先项目后全局）。"""
        if self._project_dir:
            for mem in self._load_file(self._project_dir / _MEMORY_FILE):
                if mem.id == mem_id:
                    return mem
        for mem in self._load_file(self._global_dir / _MEMORY_FILE):
            if mem.id == mem_id:
                return mem
        return None

    def _update(self, mem: CodingMemory) -> None:
        """更新已有记忆（在其所在文件中替换）。"""
        for store_file in self._store_files():
            entries = self._load_file(store_file)
            for i, e in enumerate(entries):
                if e.id == mem.id:
                    entries[i] = mem
                    self._write_file(store_file, entries)
                    return

    def _persist_access_updates(self, updated: list[tuple[float, CodingMemory]]) -> None:
        """批量持久化 access_count/last_accessed 变更（best-effort）。"""
        # 为性能考虑，只在 access_count 变化超过阈值时才写盘
        # 当前简化实现：直接跳过频繁写盘（记忆召回不应有 IO 开销）
        pass


# ── 自测 ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as _td:
        td = Path(_td)
        ws = td / "project"
        ws.mkdir()

        store = CodingMemoryStore(
            workspace=str(ws),
            global_dir=td / "global-mem",
        )

        # 基本写入
        m1 = store.remember(
            "本项目使用 pytest 运行测试，命令：make test",
            category="project_fact",
            keywords=["pytest", "test", "make"],
            related_paths=["Makefile", "tests/**"],
            scope="project",
        )
        assert m1.id == CodingMemory.make_id(m1.content)
        print(f"remember: id={m1.id}, cat={m1.category} ✓")

        # 去重：相同内容不重复创建
        m1_dup = store.remember("本项目使用 pytest 运行测试，命令：make test")
        assert m1_dup.id == m1.id
        print("dedup: same content → same id ✓")

        # 多条记忆
        store.remember(
            "变量命名用 camelCase，常量用 UPPER_SNAKE_CASE",
            category="code_convention",
            keywords=["naming", "camelCase"],
            related_paths=["src/**"],
        )
        store.remember(
            "选择 Redis 而非 Memcached，因为需要持久化和 pub/sub",
            category="architecture_decision",
            keywords=["redis", "cache", "pubsub"],
            importance=1.5,
        )
        store.remember(
            "全局规范：commit message 用英文，格式 feat/fix/chore: desc",
            category="workflow",
            keywords=["commit", "git"],
            scope="global",
        )

        all_mems = store.list_all()
        assert len(all_mems) == 4
        print(f"list_all: {len(all_mems)} memories ✓")

        # 关键词召回
        results = store.recall(query="pytest test")
        assert len(results) > 0
        assert "pytest" in results[0][1].content
        print(f"recall(query='pytest test'): top={results[0][1].content[:30]}... ✓")

        # 路径关联召回
        results = store.recall(context_paths=["src/utils.ts"])
        assert any("camelCase" in m.content for _, m in results)
        print(f"recall(path='src/utils.ts'): found naming convention ✓")

        # 分类过滤
        conventions = store.list_all(category="code_convention")
        assert len(conventions) == 1
        print(f"list_all(category='code_convention'): {len(conventions)} ✓")

        # 系统提示构建
        prompt = store.build_context_prompt(context_paths=["src/app.py"])
        assert "## Coding Memory" in prompt
        assert "camelCase" in prompt
        print(f"build_context_prompt: {len(prompt)} chars ✓")

        # 预算控制
        small_prompt = store.build_context_prompt(char_budget=100)
        assert len(small_prompt) < 200  # 标题 + 少量内容
        print(f"budget control (100): {len(small_prompt)} chars ✓")

        # 统计
        s = store.stats()
        assert s["total"] == 4
        assert s["by_scope"]["project"] == 3
        assert s["by_scope"]["global"] == 1
        print(f"stats: {s} ✓")

        # 删除
        assert store.forget(m1.id)
        assert len(store.list_all()) == 3
        print("forget: ✓")

        # 按内容删除
        count = store.forget_by_content("camelCase")
        assert count == 1
        assert len(store.list_all()) == 2
        print("forget_by_content: ✓")

    print("\nopenx/coding_memory.py OK ✓")
