"""Conversation history management — trimming, compaction, and token estimation.

Extracted from ``OpenXAgent`` to keep the agent class focused on the
dialogue loop.
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
from dataclasses import dataclass, field
from typing import Any

# Sentinel 压缩后摘要消息的内容前缀；agent 自动压缩与测试据此判定压缩是否真正发生。
SUMMARY_MARKER = "[Previous conversation summary]"


@dataclass
class ConversationHistory:
    """Manages the conversation message buffer with trimming and compaction.

    ``messages`` holds the user/assistant/tool message sequence (system
    prompt is NOT stored here).  Methods ensure the sequence stays legal
    for the OpenAI API — a ``tool`` role must follow the ``assistant``
    that requested it, and the first message must be ``user``.
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    max_tokens: int = 100_000

    # ── public API ──────────────────────────────────────────────

    def estimate_tokens(self) -> int:
        """Rough token count for the current message buffer (≈ chars ÷ 4)."""
        return _estimate_tokens(self.messages)

    def fit(self) -> None:
        """Trim old messages so the buffer fits within ``max_tokens``.

        Ensures the sequence stays legal — trims whole turns from the head
        and guarantees the first message after trimming is ``user``-role.
        """
        if not self.messages:
            return

        while _estimate_tokens(self.messages) > self.max_tokens and len(self.messages) > 2:
            self.messages.pop(0)
            # skip any orphaned assistant/tool messages at the head
            while self.messages and self.messages[0].get("role") != "user":
                self.messages.pop(0)

        if self.messages and self.messages[0].get("role") != "user":
            self.messages.clear()

    def clear(self) -> None:
        """Clear all history (``/clear`` command)."""
        self.messages.clear()

    def add(self, messages: list[dict[str, Any]]) -> None:
        """Append *messages* to the buffer, then trim if over budget."""
        self.messages.extend(messages)
        self.fit()

    def validate(self) -> bool:
        """Check the buffer stays legal for the OpenAI API.

        每条 ``role=="tool"`` 消息之前（不要求紧邻）必须存在一条
        ``assistant`` 消息，其 ``tool_calls`` 里含有相同 ``tool_call_id``——
        否则 API 会拒绝该序列（孤立的 tool 结果）。
        """
        seen_ids: set[str] = set()
        for m in self.messages:
            role = m.get("role")
            if role == "assistant":
                for tc in m.get("tool_calls") or []:
                    tc_id = tc.get("id")
                    if tc_id:
                        seen_ids.add(tc_id)
            elif role == "tool":
                if m.get("tool_call_id") not in seen_ids:
                    return False
        return True

    async def compact(
        self,
        llm: Any,
        keep_last: int = 4,
    ) -> str:
        """Summarise old TURNS, keeping the most recent *keep_last* turns.

        ``keep_last`` counts **turns**, not messages: a turn starts at a
        ``role=="user"`` message and runs until the next user message.
        轮（turn）以 user 消息为边界——切割点永远落在 user 消息之前，
        因此 assistant(tool_calls) 与其 tool 结果绝不会被拆散。

        Returns the summary text.  On failure the buffer is untouched.
        """
        boundaries = [
            i for i, m in enumerate(self.messages) if m.get("role") == "user"
        ]
        if len(boundaries) <= keep_last:
            return "(history too short to compact)"

        cut = boundaries[-keep_last]
        to_summarize = self.messages[:cut]
        recent = self.messages[cut:]

        transcript = "\n\n".join(
            f"[{m.get('role')}]: {m.get('content') if isinstance(m.get('content'), str) else '(multimodal)'}"
            for m in to_summarize
            if m.get("role") in ("user", "assistant", "tool")
        )
        summary_prompt = (
            "Summarize the following conversation history concisely. "
            "Preserve key decisions, file paths, code changes, and open tasks. "
            "This summary will replace the raw history for context continuity.\n\n"
            f"{transcript}"
        )

        try:
            response = await llm.chat(
                messages=[
                    {"role": "system", "content": "You are a conversation summarizer."},
                    {"role": "user", "content": summary_prompt},
                ],
                tools=None,
                stream=False,
            )
            summary = (response.get("content") or "(summary unavailable)").strip()
        except Exception as e:
            return f"(compaction failed: {e})"

        candidate = [
            {"role": "user", "content": f"{SUMMARY_MARKER}\n{summary}"},
            *recent,
        ]
        # 防御性校验：压缩结果必须仍合法（无孤立 tool 消息），
        # 否则回滚、保持原缓冲不变。
        old = self.messages
        self.messages = candidate
        if not self.validate():
            self.messages = old
            return "(compaction produced an invalid history; kept original)"
        return summary


# ── internal helpers ─────────────────────────────────────────────


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough token count (≈ total chars ÷ 4)."""
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total += len(str(part.get("text", "")))
        for tc in m.get("tool_calls") or []:
            total += len(json.dumps(tc.get("function", {}).get("arguments", "")))
    return max(1, total // 4)


if __name__ == "__main__":
    import asyncio

    class _FakeLLM:  # offline stand-in — compact() only awaits llm.chat(...)
        async def chat(self, **_kw):
            return {"content": "user greeted; assistant replied"}

    h = ConversationHistory(max_tokens=10_000)
    h.add([
        {"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi!"},
        {"role": "user", "content": "bye"}, {"role": "assistant", "content": "bye!"},
    ])
    assert len(h.messages) == 4 and h.estimate_tokens() > 0
    assert h.validate()
    # compact 现按“轮”（user 边界）计数：2 轮历史 + keep_last=1 → 摘要旧轮
    summary = asyncio.run(h.compact(_FakeLLM(), keep_last=1))
    assert "greeted" in summary and h.messages[0]["role"] == "user"
    assert h.validate()
    h.clear()
    assert h.messages == []
    print("openx/orchestration/history.py OK ✓")
