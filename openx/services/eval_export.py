"""P-E 轨迹升级 · **eval 导出**——账本 → 评测任务集 + 对照数据。

设计（`docs/design/openx-self-evolution-design.md` §6.1）：退场评测门（E4）
的输入是"任务集 + 对照数据"，两者都来自既有账本，**不需要新采集机制**。
本模块是这批数据的**离线消费方**：

- 输入：会话文件（``~/.openx/sessions/<hash>/<sid>.jsonl``）——消息行 +
  账本行（含 P-E 的 ``turn_usage`` 成本字段）。单遍顺序扫描：行序即时序。
- 输出：JSONL，**一条会话一行**（"任务" = 一次会话），每行：

      {"schema": "openx-eval/v1", "session_id", "workspace", "model", "group",
       "turns": [{"index", "user", "assistant", "tools", "tool_calls",
                  "input_tokens", "output_tokens", "cached_tokens",
                  "plugin_tokens", "duration_ms"}],
       "totals": {...}}

配对规则：``message`` 行 role=user 开启一个新回合；assistant 行累计输出文本
与调用过的工具名；``turn_usage`` 账本行按出现次序配对**当前**回合，填入成本
字段。**不落工具输出**（只留工具名与调用计数），避免记录被大输出撑爆。

本模块只**读**会话（机制住内核、消费住 services）；默认导出路径在 home
（``~/.openx``），**绝不**在项目里自动建 ``.openx``。
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
from typing import Any, Optional

from ..orchestration.sessions import SessionStore

#: 导出记录 schema 版本（消费方据此判断字段形状）。
SCHEMA = "openx-eval/v1"

#: 默认导出落点；测试 monkeypatch 本模块属性以隔离真实用户目录。
DEFAULT_EXPORT_PATH = Path.home() / ".openx" / "eval-export.jsonl"

_USAGE_KEYS = ("input_tokens", "output_tokens", "cached_tokens", "plugin_tokens",
               "duration_ms")


def _text_of(content: Any) -> str:
    """消息 ``content``（str 或多模态 parts 列表）-> 纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(p.get("text") or "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return "" if content is None else str(content)


def _tool_names(message: dict[str, Any]) -> list[str]:
    """一条 assistant 消息里 ``tool_calls`` 的工具名（保序、可重复）。"""
    names: list[str] = []
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function")
        name = fn.get("name") if isinstance(fn, dict) else None
        if name:
            names.append(str(name))
    return names


def export_session(path: Path) -> Optional[dict[str, Any]]:
    """一个会话文件 -> 一条 eval 记录；无任何带用户输入的回合时返回 None。

    单遍扫描 ``SessionStore.iter_events``：消息行与账本行同文件、行序即时序。
    ``turn_usage`` 账本行在其回合的消息**之后**出现（回合收尾才 emit），故按
    出现次序落到"当前打开的回合"上。
    """
    path = Path(path)
    meta = SessionStore._load_meta_only(path)
    events = SessionStore.iter_events(path)

    turns: list[dict[str, Any]] = []
    cur: Optional[dict[str, Any]] = None
    pending_usage: list[dict[str, Any]] = []

    def _close_turn() -> None:
        """把当前回合收尾入列（无用户文本的半截回合丢弃）。"""
        nonlocal cur
        if cur is not None and cur["user"]:
            turns.append(cur)
        cur = None

    for event in events:
        etype = event.get("type")
        if etype == "message":
            message = event.get("message")
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role == "user":
                _close_turn()
                cur = {
                    "index": len(turns) + 1,
                    "user": _text_of(message.get("content")),
                    "assistant": "",
                    "tools": [],
                    "tool_calls": 0,
                }
                for key in _USAGE_KEYS:
                    cur[key] = 0
            elif role == "assistant" and cur is not None:
                text = _text_of(message.get("content"))
                if text:
                    cur["assistant"] = (cur["assistant"] + "\n" + text).strip()
                names = _tool_names(message)
                cur["tool_calls"] += len(names)
                cur["tools"].extend(names)
        elif etype == "turn_usage" and cur is not None:
            pending_usage.append(event)

    _close_turn()

    # 用量按顺序回填到各回合（turn_usage 事件与回合一一对应）
    for turn, usage in zip(turns, pending_usage):
        for key in _USAGE_KEYS:
            turn[key] = int(usage.get(key, 0) or 0)

    # 工具名去重（保序）；回合无工具时为空列表
    for turn in turns:
        turn["tools"] = list(dict.fromkeys(turn["tools"]))

    if not turns:
        return None

    totals = {key: sum(int(t.get(key, 0) or 0) for t in turns) for key in _USAGE_KEYS}
    totals["turns"] = len(turns)
    return {
        "schema": SCHEMA,
        "session_id": meta.session_id if meta is not None else path.stem,
        "workspace": meta.workspace if meta is not None else "",
        "model": meta.model if meta is not None else "",
        "group": meta.group if meta is not None else "",
        "turns": turns,
        "totals": totals,
    }


def export_workspace(
    workspace: str, out_path: Optional[Path] = None
) -> tuple[Path, int]:
    """导出某工作区全部会话 -> JSONL。返回 ``(输出路径, 记录数)``。

    记录数为**实际写出的会话条数**（无回合的空会话被跳过，不写空行）。
    """
    target = Path(out_path) if out_path is not None else Path(DEFAULT_EXPORT_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for meta in SessionStore.list_for_workspace(workspace):
        if meta.path is None:
            continue
        record = export_session(meta.path)
        if record is not None:
            records.append(record)
    with target.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return target, len(records)


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as _td:
        path = Path(_td) / "s1.jsonl"
        lines = [
            {"type": "meta", "version": 1, "session_id": "s1",
             "workspace": "/tmp/ws", "model": "m-1", "group": "default",
             "created_at": "2026-01-01T00:00:00+00:00",
             "updated_at": "2026-01-01T00:01:00+00:00"},
            {"type": "message", "ts": 1.0,
             "message": {"role": "user", "content": "fix the bug"}},
            {"type": "message", "ts": 2.0,
             "message": {"role": "assistant", "content": "",
                         "tool_calls": [{"function": {"name": "read_file"}},
                                        {"function": {"name": "grep"}}]}},
            {"type": "message", "ts": 3.0,
             "message": {"role": "tool", "tool_call_id": "c1", "content": "..."}},
            {"type": "message", "ts": 4.0,
             "message": {"role": "assistant", "content": "done"}},
            # 账本行：seq + payload（iter_events 的判别键）
            {"seq": 1, "ts": 4.5, "origin": "kernel",
             "payload": {"type": "turn_usage", "session_id": "s1", "turn_index": 1,
                         "input_tokens": 1200, "output_tokens": 300,
                         "cached_tokens": 0, "plugin_tokens": 400,
                         "duration_ms": 3400}},
            {"type": "message", "ts": 5.0,
             "message": {"role": "user", "content": "and now add a test"}},
            {"type": "message", "ts": 6.0,
             "message": {"role": "assistant", "content": "added"}},
            {"seq": 2, "ts": 6.5, "origin": "kernel",
             "payload": {"type": "turn_usage", "session_id": "s1", "turn_index": 2,
                         "input_tokens": 50, "output_tokens": 10,
                         "cached_tokens": 0, "plugin_tokens": 0,
                         "duration_ms": 900}},
        ]
        path.write_text(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n",
            encoding="utf-8",
        )

        rec = export_session(path)
        assert rec is not None and rec["schema"] == SCHEMA
        assert rec["session_id"] == "s1" and rec["model"] == "m-1"
        assert len(rec["turns"]) == 2
        t1, t2 = rec["turns"]
        assert t1["user"] == "fix the bug" and t1["assistant"] == "done"
        assert t1["tools"] == ["read_file", "grep"] and t1["tool_calls"] == 2
        assert (t1["input_tokens"], t1["output_tokens"]) == (1200, 300)
        assert t1["duration_ms"] == 3400 and t1["plugin_tokens"] == 400
        assert t2["user"] == "and now add a test" and t2["tools"] == []
        assert t2["input_tokens"] == 50
        assert rec["totals"]["turns"] == 2
        assert rec["totals"]["input_tokens"] == 1250
        assert rec["totals"]["output_tokens"] == 310
        # 可 JSON 序列化（要写 JSONL）
        json.loads(json.dumps(rec))

        # 空会话（只有 meta）：无可导出回合 -> None
        empty = Path(_td) / "empty.jsonl"
        empty.write_text(json.dumps(lines[0]) + "\n", encoding="utf-8")
        assert export_session(empty) is None

    print("openx/services/eval_export.py OK ✓")
