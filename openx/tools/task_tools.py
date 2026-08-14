"""Background task tools — ``task_output`` / ``task_stop`` (Phase 7).

后台任务工具：与 :class:`~openx.core.tasks.TaskRegistry` 共享同一实例。

- ``task_output``：读取后台任务日志尾部 + 状态行（running/exited）。
  只读 OpenX 自己的日志 → ALLOW，无需用户确认。
- ``task_stop``：终止 OpenX 自己启动的进程组（SIGTERM → SIGKILL）。
  只会杀 OpenX 派生的任务进程组（对应 Claude Code 的 KillShell 免询问
  语义）→ ALLOW。
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

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .base import Tool, ToolResult
from ..permissions import Permission, PermissionLevel

if TYPE_CHECKING:
    from ..core.tasks import TaskRegistry


def _elapsed_since(iso_ts: str) -> str:
    """Human-readable elapsed time: ``42s`` / ``3m05s`` / ``1h02m``.

    解析失败返回 ``"?"``——状态行只是辅助信息，绝不抛异常。
    """
    try:
        started = datetime.fromisoformat(iso_ts)
        secs = int((datetime.now(timezone.utc) - started).total_seconds())
    except (ValueError, TypeError):
        return "?"
    secs = max(secs, 0)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


class TaskOutputTool(Tool):
    """Poll the latest output of a background task (read-only)."""

    name = "task_output"
    description = (
        "Read the latest output of a background shell task started with "
        "shell(run_in_background=true). Returns a status line (running or "
        "exited with code) plus the last lines of the task's log. Read-only."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": (
                    "The background task id returned by "
                    "shell(run_in_background=true), e.g. 'b1'."
                ),
            },
            "tail_lines": {
                "type": "integer",
                "description": (
                    "Number of trailing log lines to return (1-2000). "
                    "Default: 200."
                ),
            },
        },
        "required": ["task_id"],
    }

    def __init__(self, task_registry: "TaskRegistry") -> None:
        self.task_registry = task_registry

    @property
    def permission(self) -> Permission:
        return Permission(
            level=PermissionLevel.ALLOW,
            reason="Reading OpenX's own background task logs",
        )

    async def execute(self, task_id: str, tail_lines: int = 200) -> ToolResult:
        handle = self.task_registry.get(task_id)
        if handle is None:
            known = [h.task_id for h in self.task_registry.all()]
            listing = ", ".join(known) if known else "(none)"
            return ToolResult(
                error=f"Unknown task id: {task_id}. Known tasks: {listing}"
            )
        # clamp 到 1..2000（LLM 可能传字符串/浮点/越界值）
        try:
            n = int(tail_lines)
        except (TypeError, ValueError):
            n = 200
        n = max(1, min(n, 2000))

        if handle.running:
            status = f"[status: running] ({_elapsed_since(handle.started_at)} elapsed)"
        else:
            status = f"[status: exited {handle.exit_code}]"
        tail = self.task_registry.tail_log(task_id, n) or ""
        body = tail.strip() or "(no output yet)"
        return ToolResult(output=f"{status}\n{body}")


class TaskStopTool(Tool):
    """Stop a background task's process group (OpenX-spawned only)."""

    name = "task_stop"
    description = (
        "Stop a background shell task started with shell(run_in_background=true). "
        "Sends SIGTERM to the task's process group, escalating to SIGKILL if it "
        "is still alive after ~3 seconds."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": (
                    "The background task id returned by "
                    "shell(run_in_background=true), e.g. 'b1'."
                ),
            },
        },
        "required": ["task_id"],
    }

    def __init__(self, task_registry: "TaskRegistry") -> None:
        self.task_registry = task_registry

    @property
    def permission(self) -> Permission:
        # 只终止 OpenX 自己派生的任务进程组——免询问（mirror Claude Code KillShell）
        return Permission(
            level=PermissionLevel.ALLOW,
            reason="Only kills OpenX-spawned background task process groups",
        )

    async def execute(self, task_id: str) -> ToolResult:
        message = await self.task_registry.stop(task_id)
        if message.startswith("unknown task"):
            return ToolResult(error=message)
        return ToolResult(output=message)


if __name__ == "__main__":
    # 独立调试：临时目录里验证两个工具的读取/终止路径（绝不触碰真实用户数据）
    import asyncio
    import tempfile
    from pathlib import Path

    async def _self_check():
        from ..core.tasks import TaskRegistry

        with tempfile.TemporaryDirectory() as _td:
            reg = TaskRegistry(Path(_td))
            out_tool, stop_tool = TaskOutputTool(reg), TaskStopTool(reg)

            handle = await reg.start("sleep 0.1; echo hi")
            await asyncio.sleep(0.4)
            r = await out_tool.execute(task_id=handle.task_id, tail_lines=10)
            assert r.success and "[status: exited 0]" in r.output and "hi" in r.output
            r = await stop_tool.execute(task_id=handle.task_id)
            assert r.success and "already exited 0" in r.output

            r = await out_tool.execute(task_id="b99")
            assert not r.success and "b1" in r.error
            r = await stop_tool.execute(task_id="b99")
            assert not r.success and "unknown task" in r.error

    asyncio.run(_self_check())
    print("openx/tools/task_tools.py OK ✓")
