"""Session persistence — append-only JSONL logs under ``~/.openx/sessions``.

会话持久化（Phase 6）：每个会话一个 JSONL 文件，按工作区分目录存放。

文件格式（append-only，绝不重写）::

    {"type": "meta", "version": 1, "session_id": ..., "workspace": ...,
     "model": ..., "created_at": ..., "updated_at": ...}        # 第 1 行
    {"type": "message", "ts": ..., "message": {...}}            # 每条消息一行
    {"type": "meta_update", "total_input_tokens": ..., ...}     # 元数据增量

设计要点
========
- **append-only**：token 用量 / todos / updated_at 等易变字段以
  ``meta_update`` 行前向合并（load 时顺序回放），避免重写整文件。
- **隐私与安全**：多模态消息里的 ``image_url`` part（base64 data URL）
  写盘前一律替换为占位文本——base64 图片绝不上磁盘。
- **健壮性**：损坏行跳过并告警；孤立 tool 消息（无匹配 assistant
  tool_call）按 ``ConversationHistory.validate()`` 语义丢弃并告警——
  加载永不抛异常，恢复的序列必须对 OpenAI API 合法。
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

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 会话文件根目录；测试 monkeypatch 此模块级常量以隔离真实用户数据。
# Mirrors the SETTINGS_PATH pattern in config.py.
SESSIONS_DIR = Path.home() / ".openx" / "sessions"

# 写盘时图片 part 的占位文本 —— base64 data URL 绝不落盘
IMAGE_PLACEHOLDER_TEXT = "[image omitted from session log]"

# meta_update 允许前向合并的字段白名单（其余键一律忽略）
_META_UPDATE_FIELDS = (
    "total_input_tokens",
    "total_output_tokens",
    "todos",
    "first_user_message",
    "updated_at",
)


def _now_iso() -> str:
    """UTC ISO-8601 时间戳（带时区偏移，fromisoformat 可直接解析）。"""
    return datetime.now(timezone.utc).isoformat()


# ── meta ────────────────────────────────────────────────────────


@dataclass
class SessionMeta:
    """一个会话的元信息（meta 行 + meta_update 前向合并后的视图）。"""

    session_id: str
    workspace: str
    model: str
    created_at: str
    updated_at: str
    first_user_message: str = ""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    todos: list = field(default_factory=list)
    path: Path | None = None  # create/load 之后回填


# ── sanitize helpers ────────────────────────────────────────────


def _sanitize_message(message: dict[str, Any]) -> dict[str, Any]:
    """写盘前清洗单条消息：替换 image_url part，绝不写 base64。

    返回副本——调用方（agent 历史）持有的原消息绝不被改动。
    """
    content = message.get("content")
    if not isinstance(content, list):
        return message
    parts: list[Any] = []
    changed = False
    for part in content:
        if isinstance(part, dict) and part.get("type") == "image_url":
            parts.append({"type": "text", "text": IMAGE_PLACEHOLDER_TEXT})
            changed = True
        else:
            parts.append(part)
    if not changed:
        return message
    sanitized = dict(message)
    sanitized["content"] = parts
    return sanitized


def _drop_orphan_tool_messages(
    messages: list[dict[str, Any]], label: str = ""
) -> list[dict[str, Any]]:
    """丢弃孤立 tool 消息（role=tool 且无先行 assistant tool_call 匹配其 id）。

    语义与 ``ConversationHistory.validate()`` 一致：tool 结果之前（不要求
    紧邻）必须存在携带相同 ``tool_call_id`` 的 assistant 消息，否则恢复的
    序列会被 OpenAI API 拒绝。仅告警、绝不抛异常。
    """
    seen_ids: set[str] = set()
    kept: list[dict[str, Any]] = []
    dropped = 0
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                tc_id = tc.get("id")
                if tc_id:
                    seen_ids.add(tc_id)
            kept.append(m)
        elif role == "tool":
            if m.get("tool_call_id") in seen_ids:
                kept.append(m)
            else:
                dropped += 1
        else:
            kept.append(m)
    if dropped:
        print(
            f"warning: dropped {dropped} orphan tool message(s) "
            f"while loading session {label or '(unknown)'}"
        )
    return kept


def _meta_from_line(line: dict[str, Any], path: Path | None = None) -> SessionMeta:
    """把 meta JSON 行构造成 SessionMeta（容忍字段缺失）。"""
    meta = SessionMeta(
        session_id=str(line.get("session_id") or ""),
        workspace=str(line.get("workspace") or ""),
        model=str(line.get("model") or ""),
        created_at=str(line.get("created_at") or ""),
        updated_at=str(line.get("updated_at") or line.get("created_at") or ""),
        first_user_message=str(line.get("first_user_message") or ""),
        total_input_tokens=int(line.get("total_input_tokens") or 0),
        total_output_tokens=int(line.get("total_output_tokens") or 0),
        todos=list(line.get("todos") or []),
    )
    meta.path = path
    return meta


# ── store ───────────────────────────────────────────────────────


class SessionStore:
    """单个会话文件的读写句柄（create / open 工厂构造）。

    写入一律追加（append-only）：消息经 ``append_messages``，易变元数据
    经 ``update_meta`` 以 meta_update 行前向合并——永不重写旧行。
    """

    def __init__(self, meta: SessionMeta, path: Path) -> None:
        self.meta = meta
        self.path = Path(path)
        self.meta.path = self.path

    # ── factories ───────────────────────────────────────────

    @staticmethod
    def workspace_hash(workspace: str) -> str:
        """工作区绝对路径的 16 位十六进制指纹（目录名，确定性）。"""
        return hashlib.sha1(
            str(Path(workspace).resolve()).encode()
        ).hexdigest()[:16]

    @classmethod
    def create(
        cls,
        workspace: str,
        model: str,
        session_id: str | None = None,
    ) -> "SessionStore":
        """新建会话：mkdir -p 工作区目录并写入 meta 首行。

        ``first_user_message`` 留空，待首条用户消息到达后经
        ``update_meta`` 回填。
        """
        session_id = session_id or uuid.uuid4().hex[:12]
        directory = SESSIONS_DIR / cls.workspace_hash(workspace)
        directory.mkdir(parents=True, exist_ok=True)
        now = _now_iso()
        meta = SessionMeta(
            session_id=session_id,
            workspace=str(Path(workspace).resolve()),
            model=model,
            created_at=now,
            updated_at=now,
        )
        path = directory / f"{session_id}.jsonl"
        line = {
            "type": "meta",
            "version": 1,
            "session_id": meta.session_id,
            "workspace": meta.workspace,
            "model": meta.model,
            "created_at": meta.created_at,
            "updated_at": meta.updated_at,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        return cls(meta, path)

    @classmethod
    def open(cls, meta: SessionMeta) -> "SessionStore":
        """打开既有会话文件续写（--continue / --resume 复用）。"""
        if meta.path is None:
            raise ValueError("SessionMeta.path is required to open a session")
        return cls(meta, Path(meta.path))

    # ── reading ─────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path) -> tuple[SessionMeta, list[dict[str, Any]]]:
        """完整加载：meta（含 meta_update 前向合并）+ 清洗后的消息序列。

        损坏行跳过并告警；孤立 tool 消息被丢弃并告警——永不抛异常。
        meta 缺失（文件被截断等）时按文件名兜底合成，保证可恢复。
        """
        path = Path(path)
        meta: SessionMeta | None = None
        messages: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            print(f"warning: cannot read session file {path}: {e}")
            return _meta_from_line({"session_id": path.stem}, path), []

        for lineno, raw in enumerate(lines, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                line = json.loads(raw)
            except json.JSONDecodeError:
                print(
                    f"warning: skipping corrupt session line "
                    f"{path.name}:{lineno}"
                )
                continue
            if not isinstance(line, dict):
                continue
            kind = line.get("type")
            if kind == "meta":
                meta = _meta_from_line(line, path)
            elif kind == "meta_update":
                if meta is not None:
                    cls._apply_meta_update(meta, line)
            elif kind == "message":
                msg = line.get("message")
                if isinstance(msg, dict):
                    messages.append(msg)

        if meta is None:
            print(f"warning: session file {path.name} has no meta line")
            meta = _meta_from_line({"session_id": path.stem}, path)
        meta.path = path
        return meta, _drop_orphan_tool_messages(messages, label=meta.session_id)

    @classmethod
    def _load_meta_only(cls, path: Path) -> SessionMeta | None:
        """廉价加载：只解析 meta / meta_update 行，跳过 message 行。

        先按受控序列化前缀快速跳过巨型 message 行（不 json.loads），
        其余行才解析——列表页无需为每条消息付解析代价。
        """
        path = Path(path)
        meta: SessionMeta | None = None
        try:
            with path.open("r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    # 快速路径：我们写出的 message 行必以此前缀开头
                    if raw.startswith('{"type": "message"') or raw.startswith(
                        '{"type":"message"'
                    ):
                        continue
                    try:
                        line = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(line, dict):
                        continue
                    kind = line.get("type")
                    if kind == "meta":
                        meta = _meta_from_line(line, path)
                    elif kind == "meta_update" and meta is not None:
                        cls._apply_meta_update(meta, line)
        except OSError:
            return None
        if meta is not None:
            meta.path = path
        return meta

    @classmethod
    def iter_events(cls, path: Path) -> list[dict[str, Any]]:
        """按文件序产出统一回放事件列表（消息行 + 账本行投影）。

        append-only 即时序：行序即时间序。判别：**账本行**带 ``seq`` /
        ``payload`` 键（``Event.to_line()`` 信封），投影为
        ``{**payload, "seq", "ts", "cause", "origin"}`` 供前端排序/展示；
        **消息行**原样 ``{"type":"message","ts":...,"message":{...}}``。
        meta / meta_update 行不参与回放（元信息由列表端点承载）。损坏行
        跳过并告警，永不抛异常。

        供 serve 复盘端点使用：``SessionStore.load`` 只回 message 行、静默
        跳过账本行（审计/回放语义不同），复盘须两者都读——本方法补足。
        """
        path = Path(path)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        events: list[dict[str, Any]] = []
        for lineno, raw in enumerate(lines, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                line = json.loads(raw)
            except json.JSONDecodeError:
                print(f"warning: skipping corrupt session line {path.name}:{lineno}")
                continue
            if not isinstance(line, dict):
                continue
            if "seq" in line and "payload" in line:
                payload = dict(line.get("payload") or {})
                for key in ("seq", "ts", "cause", "origin"):
                    if line.get(key) is not None:
                        payload.setdefault(key, line.get(key))
                events.append(payload)
            elif line.get("type") == "message":
                events.append(line)
        return events

    @staticmethod
    def _apply_meta_update(meta: SessionMeta, line: dict[str, Any]) -> None:
        """把一行 meta_update 前向合并进 meta（仅白名单字段）。"""
        for key in _META_UPDATE_FIELDS:
            if key not in line:
                continue
            value = line[key]
            if key in ("total_input_tokens", "total_output_tokens"):
                try:
                    setattr(meta, key, int(value or 0))
                except (TypeError, ValueError):
                    continue
            elif key == "todos":
                meta.todos = list(value or [])
            elif key == "first_user_message":
                meta.first_user_message = str(value or "")
            elif key == "updated_at":
                meta.updated_at = str(value or meta.updated_at)

    # ── writing (append-only) ───────────────────────────────

    def append_messages(self, messages: list[dict[str, Any]]) -> None:
        """追加消息行；image_url part 替换为占位文本（base64 绝不落盘）。"""
        now = _now_iso()
        chunks: list[str] = []
        for msg in messages:
            record = {
                "type": "message",
                "ts": now,
                "message": _sanitize_message(msg),
            }
            chunks.append(json.dumps(record, ensure_ascii=False, default=str))
        with self.path.open("a", encoding="utf-8") as f:
            f.write("\n".join(chunks) + "\n")

    def update_meta(self, **fields: Any) -> None:
        """追加 meta_update 行（append-only，不重写）并同步内存 meta。

        ``updated_at`` 一律取当前时间——调用方显式传入也会被覆盖。
        """
        payload: dict[str, Any] = {
            k: v for k, v in fields.items() if k in _META_UPDATE_FIELDS
        }
        payload["type"] = "meta_update"
        payload["updated_at"] = _now_iso()
        self._apply_meta_update(self.meta, payload)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    def append_event(self, event: Any) -> None:
        """追加内核事件信封行（会话账本，K2b）。

        信封行携带 seq/digest 字段，与 message/meta 行共存；load() 对
        message/meta 之外的行静默跳过--账本行不参与会话恢复，只服务
        审计与回放。
        """
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_line(), ensure_ascii=False, default=str) + "\n")

    def ledger_start_seq(self) -> int:
        """既有信封条目数（恢复会话时 kernel.attach_ledger 的续接起点）。"""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        count = 0
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                line = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(line, dict) and "seq" in line and "digest" in line:
                count += 1
        return count

    # ── listing ─────────────────────────────────────────────

    @classmethod
    def list_for_workspace(cls, workspace: str) -> list[SessionMeta]:
        """列出该工作区全部会话（按 updated_at 倒序，最新在前）。"""
        directory = SESSIONS_DIR / cls.workspace_hash(workspace)
        if not directory.is_dir():
            return []
        metas: list[SessionMeta] = []
        for path in sorted(directory.glob("*.jsonl")):
            meta = cls._load_meta_only(path)
            if meta is not None:
                metas.append(meta)
        metas.sort(key=lambda m: m.updated_at, reverse=True)
        return metas


# ── convenience resolvers ───────────────────────────────────────


def resolve_latest(workspace: str) -> SessionMeta | None:
    """--continue：返回该工作区最新会话的 meta，无则 None。"""
    metas = SessionStore.list_for_workspace(workspace)
    return metas[0] if metas else None


def resolve_by_id(workspace: str, session_id: str) -> SessionMeta | None:
    """--resume <id>：精确匹配该工作区下的会话 id，未命中返回 None。"""
    path = (
        SESSIONS_DIR
        / SessionStore.workspace_hash(workspace)
        / f"{session_id}.jsonl"
    )
    if not path.is_file():
        return None
    return SessionStore._load_meta_only(path)


if __name__ == "__main__":
    import tempfile

    # 自检全程使用临时目录，绝不触碰真实 ~/.openx/sessions
    with tempfile.TemporaryDirectory() as _td:
        _saved = SESSIONS_DIR
        SESSIONS_DIR = Path(_td) / "sessions"
        try:
            ws = str(Path(_td) / "ws")
            # create → append → update_meta → load 往返
            store = SessionStore.create(ws, "selftest-model", session_id="selftest01")
            assert store.path.is_file()
            store.append_messages([
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi!"},
            ])
            store.update_meta(total_input_tokens=10, total_output_tokens=3)
            meta, messages = SessionStore.load(store.path)
            assert meta.session_id == "selftest01" and meta.total_input_tokens == 10
            assert [m["role"] for m in messages] == ["user", "assistant"]

            # 图片 part 绝不落盘
            b64 = "BASE64" * 40
            store.append_messages([
                {"role": "user", "content": [
                    {"type": "text", "text": "look"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ]},
            ])
            raw = store.path.read_bytes()
            assert b"[image omitted" in raw and b64.encode() not in raw

            # workspace_hash 确定性 + 列表倒序 + resolver
            h1 = SessionStore.workspace_hash(ws)
            assert h1 == SessionStore.workspace_hash(ws) and len(h1) == 16
            other = SessionStore.create(str(Path(_td) / "other"), "m")
            metas = SessionStore.list_for_workspace(ws)
            assert [m.session_id for m in metas] == ["selftest01"]  # 其他工作区被排除
            assert resolve_latest(ws).session_id == "selftest01"
            assert resolve_by_id(ws, "selftest01").session_id == "selftest01"
            assert resolve_latest(str(Path(_td) / "empty")) is None
            assert resolve_by_id(ws, "no-such-id") is None
            del other

            # iter_events 回放：message 行 + 账本行按文件序统一产出。
            # 模拟一次 permission_decision 账本事件落盘（真实路径由内核
            # emit → append_event 写入），断言两种行都出现且序正确。
            from openx.kernel.protocol import Event
            store.append_event(Event(
                seq=1, ts=1.0, session="selftest01", type="permission_decision",
                payload={"type": "permission_decision", "tool": "shell",
                         "approved": True, "verdict": "ALLOW"},
                origin="kernel", digest="d1",
            ))
            evs = SessionStore.iter_events(store.path)
            kinds = [e.get("type") for e in evs]
            assert "message" in kinds and "permission_decision" in kinds, kinds
            # 账本行投影合并了 seq/ts，可供前端排序
            perm = next(e for e in evs if e["type"] == "permission_decision")
            assert perm["seq"] == 1 and perm["tool"] == "shell"
            # 顺序 = 文件序：三条 message 在前、账本事件在后
            assert kinds.index("permission_decision") > kinds.index("message")
        finally:
            SESSIONS_DIR = _saved

    print("openx/orchestration/sessions.py OK ✓")
