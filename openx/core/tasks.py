"""Background task registry — long-running shell processes with log capture.

后台任务（Phase 7）：``shell(run_in_background=true)`` 把命令交给
:class:`TaskRegistry` 启动后立即返回，stdout/stderr 落盘到
``~/.openx/tasks/<id>.log``；``task_output`` 工具轮询日志尾部，
``task_stop`` 工具终止进程组。CLI 退出时 ``cleanup()`` 收尾所有
仍在运行的任务。

Design notes 设计要点
=====================
- **立即返回**：``start()`` 只 fork 子进程与 watcher 协程，不等待完成；
- **进程组隔离**：``preexec_fn=os.setsid`` 让每个任务自成进程组，
  ``stop()`` 用 ``killpg`` 连任务派生的子孙进程一起终止；
- **watcher**：asyncio 任务等待进程退出，回填 ``exit_code`` / ``end_time``
  并关闭日志 fd；引用保存在 ``_watchers`` 集合里防止被 GC 提前回收;
- **跨 loop 兜底**：CLI 退出清理发生在新建的 event loop 上（旧 loop 已关，
  watcher 协程无法再运行），``stop()`` 因此在 OS 层（``killpg(pgid, 0)``）
  探测进程组存亡并回填退出码——best-effort，绝不抛异常。
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

import asyncio
import os
import signal
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# 后台任务日志根目录；测试 monkeypatch 此模块级常量以隔离真实用户数据。
# Mirrors the SESSIONS_DIR pattern in sessions.py.
TASKS_DIR = Path.home() / ".openx" / "tasks"


def _now_iso() -> str:
    """UTC ISO-8601 时间戳（带时区偏移，fromisoformat 可直接解析）。"""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TaskHandle:
    """一个后台任务的全部状态。

    ``exit_code`` 为 ``None`` 表示仍在运行；进程退出时由 watcher 协程
    回填退出码与结束时间。
    """

    task_id: str                                  # "b1", "b2", ...（单调递增）
    command: str
    cwd: Optional[str]
    log_path: Path
    started_at: str
    process: asyncio.subprocess.Process
    exit_code: Optional[int] = None               # None = 仍在运行；watcher 回填
    end_time: Optional[str] = None

    @property
    def running(self) -> bool:
        return self.exit_code is None


class TaskRegistry:
    """后台任务注册表：启动 / 查询 / 读日志 / 终止 / 退出清理。

    ``tasks_dir`` 缺省取模块级 ``TASKS_DIR``（测试 monkeypatch 它即可隔离）；
    目录**惰性创建**——只有真正 ``start()`` 任务时才 mkdir，构造注册表
    （例如 agent 构造）绝不触碰磁盘。
    """

    def __init__(self, tasks_dir: Optional[Path] = None) -> None:
        self.tasks_dir = Path(tasks_dir) if tasks_dir is not None else TASKS_DIR
        self._tasks: dict[str, TaskHandle] = {}
        # watcher 协程引用：asyncio 只持弱引用，必须自己留住防止 GC
        self._watchers: set[asyncio.Task] = set()

    # ── 启动与观察 ──────────────────────────────────────────────

    async def start(self, command: str, cwd: Optional[str] = None) -> TaskHandle:
        """启动后台任务并**立即返回**句柄（不等待命令完成）。

        stdout/stderr 合并写入 ``<tasks_dir>/<id>.log``，stdin 为 DEVNULL，
        子进程自成进程组（``os.setsid``）以便整组终止。
        """
        self.tasks_dir.mkdir(parents=True, exist_ok=True)  # 惰性 mkdir -p
        task_id = f"b{len(self._tasks) + 1}"  # 单调递增（条目只增不删）
        log_path = self.tasks_dir / f"{task_id}.log"
        log_file = open(log_path, "wb")
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=cwd,
                preexec_fn=os.setsid,  # 自成进程组，killpg 可连子孙进程一起终止
            )
        except Exception:
            log_file.close()
            raise

        handle = TaskHandle(
            task_id=task_id,
            command=command,
            cwd=cwd,
            log_path=log_path,
            started_at=_now_iso(),
            process=process,
        )
        self._tasks[task_id] = handle
        watcher = asyncio.create_task(self._watch(handle, log_file))
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)
        return handle

    async def _watch(self, handle: TaskHandle, log_file) -> None:
        """等待进程退出 → 回填退出码与结束时间 → 关闭日志 fd。

        loop 关闭导致本协程被取消时（CLI 退出路径），``finally`` 仍会
        关掉日志 fd；退出码改由 ``stop()`` 的 OS 层兜底回填。
        """
        try:
            handle.exit_code = await handle.process.wait()
            handle.end_time = _now_iso()
        finally:
            try:
                log_file.close()
            except Exception:
                pass

    # ── 查询 ────────────────────────────────────────────────────

    def get(self, task_id: str) -> Optional[TaskHandle]:
        return self._tasks.get(task_id)

    def all(self) -> list[TaskHandle]:
        return list(self._tasks.values())

    def tail_log(self, task_id: str, tail_lines: int = 200) -> Optional[str]:
        """返回任务日志的最后 *tail_lines* 行；未知 id → ``None``。

        日志以 ``errors="replace"`` 解码，损坏字节绝不抛异常。
        """
        handle = self._tasks.get(task_id)
        if handle is None:
            return None
        try:
            data = handle.log_path.read_bytes()
        except OSError:
            return ""
        lines = data.decode("utf-8", errors="replace").splitlines()
        if tail_lines > 0:
            lines = lines[-tail_lines:]
        return "\n".join(lines)

    # ── 终止 ────────────────────────────────────────────────────

    async def stop(self, task_id: str) -> str:
        """终止任务：SIGTERM → 等待 ~3s → SIGKILL；返回人类可读结果行。

        信号发给整个进程组（``killpg``），连任务派生的子孙进程一起收掉。
        绝不抛异常：未知 id / 已退出 / 进程组已消失都返回说明性字符串。
        存亡探测优先看 watcher 回填的 ``exit_code``，并辅以 OS 层
        ``killpg(pgid, 0)`` 兜底（跨 loop 清理时 watcher 已无法运行）。
        """
        handle = self._tasks.get(task_id)
        if handle is None:
            return f"unknown task: {task_id}"
        if not handle.running:
            return f"{task_id} already exited {handle.exit_code}"

        pgid = handle.process.pid  # setsid → 进程组 id == pid

        def _group_alive() -> bool:
            try:
                os.killpg(pgid, 0)  # 0 号信号：仅探测进程组是否存在
                return True
            except (ProcessLookupError, PermissionError):
                return False

        def _signal_group(sig: int) -> None:
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError):
                pass

        escalated = False
        _signal_group(signal.SIGTERM)
        for _ in range(30):  # ≤3s
            if not handle.running or not _group_alive():
                break
            await asyncio.sleep(0.1)

        if handle.running and _group_alive():
            escalated = True
            _signal_group(signal.SIGKILL)
            for _ in range(30):  # ≤3s
                if not handle.running or not _group_alive():
                    break
                await asyncio.sleep(0.1)

        if handle.running:
            if _group_alive():
                return f"{task_id} still running after SIGKILL"
            # watcher 未能运行（如 CLI 退出清理在新 loop 上）→ OS 层回填
            handle.exit_code = -signal.SIGKILL if escalated else -signal.SIGTERM
            handle.end_time = _now_iso()
        return f"Stopped {task_id} (exit {handle.exit_code})"

    async def cleanup(self) -> None:
        """停止所有仍在运行的任务（CLI 退出调用）；best-effort，绝不抛异常。"""
        for handle in list(self._tasks.values()):
            if handle.running:
                try:
                    await self.stop(handle.task_id)
                except Exception:
                    pass


if __name__ == "__main__":
    # 独立调试：临时目录里启动/观察/终止任务（绝不触碰真实 ~/.openx）
    import tempfile

    async def _self_check():
        with tempfile.TemporaryDirectory() as _td:
            reg = TaskRegistry(Path(_td))

            # 启动立即返回 + watcher 回填 + 日志落盘
            h1 = await reg.start("sleep 0.1; echo hi")
            assert h1.task_id == "b1" and h1.running
            await asyncio.sleep(0.4)
            assert not h1.running and h1.exit_code == 0 and h1.end_time
            assert "hi" in (reg.tail_log("b1") or "")
            assert reg.tail_log("b99") is None

            # stop：SIGTERM 终止 sleep 30，退出码为负
            h2 = await reg.start("sleep 30")
            assert h2.task_id == "b2"
            msg = await reg.stop("b2")
            assert "Stopped b2" in msg and not h2.running
            assert h2.exit_code is not None and h2.exit_code < 0

            # cleanup：收尾仍在运行的任务
            await reg.start("sleep 30")
            await reg.cleanup()
            assert all(not t.running for t in reg.all())

    asyncio.run(_self_check())
    print("openx/core/tasks.py OK ✓")
