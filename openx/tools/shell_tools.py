"""Shell command execution tool for OpenX."""

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
import re
import shlex
import signal
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .base import Tool, ToolResult, truncate_output
from ..permissions import Permission, PermissionLevel, check_command_danger

if TYPE_CHECKING:
    from ..orchestration.tasks import TaskRegistry


class ShellTool(Tool):
    """Execute shell commands with safety checks."""

    name = "shell"
    description = (
        "Execute a shell command and return its output. "
        "Use for: running tests, building, installing packages, git commands, "
        "checking system state, and any other CLI tasks. "
        "Dangerous commands (rm -rf, sudo, etc.) ALWAYS require explicit user "
        "confirmation and can never be auto-approved. "
        "Commands run in a subprocess with the workspace as working directory. "
        "Environment variables and directory changes do not persist between calls. "
        "Set run_in_background=true for long-running commands (servers, watchers, "
        "builds); it returns a task id to poll with task_output / stop with task_stop."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute.",
            },
            "timeout": {
                "type": "number",
                "description": "Maximum execution time in seconds. Default: 60.",
            },
            "cwd": {
                "type": "string",
                "description": "Working directory for the command. Defaults to workspace.",
            },
            "run_in_background": {
                "type": "boolean",
                "description": (
                    "Run in background; returns a task id immediately. Poll output "
                    "with task_output, stop with task_stop. Use for long-running "
                    "commands (servers, watchers, builds). Default false."
                ),
            },
        },
        "required": ["command"],
    }

    def __init__(
        self,
        workspace: str,
        allowed_commands: Optional[list[str]] = None,
        dangerous_patterns: Optional[list[str]] = None,
        task_registry: Optional["TaskRegistry"] = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.allowed_commands = allowed_commands or []
        self.dangerous_patterns = dangerous_patterns or []
        # 后台任务注册表（Phase 7）：None → 后台模式不可用
        self.task_registry = task_registry

    @property
    def permission(self) -> Permission:
        return Permission(level=PermissionLevel.ASK, reason="Executing shell commands")

    # Leading env-assignment tokens to skip when locating the real command,
    # e.g. the ``FOO=1`` in ``FOO=1 pytest -q``.
    _ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

    def auto_allowed(self, args: dict) -> bool:
        """Pre-approve commands whose first token is in ``allowed_commands``.

        命令首词命中白名单即可跳过 ASK 询问（预批准，非硬拦截）。
        跳过前导的 ``ENV=val`` 环境变量赋值；解析失败时保守返回 False，
        退回交互式确认。
        """
        command = args.get("command", "")
        try:
            tokens = shlex.split(command)
        except ValueError:
            return False  # unbalanced quotes etc. → ask the user
        for token in tokens:
            if self._ENV_ASSIGN_RE.match(token):
                continue  # skip leading ENV=val assignments
            return token in self.allowed_commands
        return False  # empty / env-only command

    def is_high_risk(self, args: dict) -> bool:
        """命令命中 ``config.dangerous_commands`` 即高风险（always-ask）。

        执行器对高风险调用强制弹窗确认：存储 allow 规则、allowed_commands
        白名单与 auto_approve/-y 均不得跳过——即便此前批准并记住了同类
        命令，下一次仍然弹窗（危险检查在执行器中排在存储规则提前返回
        之前）。批准后即可执行（原"硬阻断"语义已改为"永远询问"）。
        后台模式走同一 prepare 闸门，绝不绕过。
        """
        is_dangerous, _ = check_command_danger(
            args.get("command", ""), self.dangerous_patterns
        )
        return is_dangerous

    async def execute(
        self,
        command: str,
        timeout: float = 60.0,
        cwd: Optional[str] = None,
        run_in_background: bool = False,
    ) -> ToolResult:
        # 危险命令不再在此硬阻断：ToolExecutor.prepare 已通过 is_high_risk
        # 将其升级为"永远弹窗确认"（前台/后台同一闸门），批准后才到这里。

        working_dir = Path(cwd).resolve() if cwd else self.workspace

        # ── Background mode (Phase 7): start and return immediately ──
        # 后台模式：交给 TaskRegistry 启动后立即返回任务 id，前台路径不受影响
        if run_in_background:
            if self.task_registry is None:
                return ToolResult(error="background tasks not available")
            try:
                handle = await self.task_registry.start(command, str(working_dir))
            except Exception as e:
                return ToolResult(error=f"Failed to start background task: {e}")
            return ToolResult(
                output=f"Background task {handle.task_id} started: {command}\n"
                f"Log: {handle.log_path}\n"
                f"Poll with task_output(task_id='{handle.task_id}'), "
                f"stop with task_stop(task_id='{handle.task_id}')."
            )

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(working_dir),
                env={**os.environ},
                preexec_fn=os.setsid,  # create new process group
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                # Kill the process group
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                return ToolResult(
                    error=f"Command timed out after {timeout}s: {command}"
                )

            output = stdout.decode("utf-8", errors="replace")
            error_output = stderr.decode("utf-8", errors="replace")

            parts = []
            if output.strip():
                parts.append(output.rstrip())
            if error_output.strip():
                parts.append(f"[stderr]\n{error_output.rstrip()}")
            if process.returncode != 0:
                parts.append(f"[exit code: {process.returncode}]")

            result_text = "\n\n".join(parts) if parts else "(no output)"

            truncated, was_truncated, notice = truncate_output(result_text)
            return ToolResult(
                output=truncated,
                error="" if process.returncode == 0 else f"Exit code: {process.returncode}",
                truncated=was_truncated,
                truncated_notice=notice,
            )

        except FileNotFoundError:
            return ToolResult(error=f"Command not found: {command.split()[0]}")
        except Exception as e:
            return ToolResult(error=f"Failed to execute command: {e}")


if __name__ == "__main__":
    # 独立调试：执行安全的 echo 命令，断言输出（绝不联网、不阻塞）
    import asyncio
    import tempfile

    async def _self_check():
        with tempfile.TemporaryDirectory() as ws:
            sh = ShellTool(ws, dangerous_patterns=[r"rm -rf /"])
            r = await sh.execute("echo hello")
            assert r.success and "hello" in r.output, r.output
            print(repr(r.output))
            # is_high_risk：命中 dangerous_patterns → True；安全命令 → False
            assert sh.is_high_risk({"command": "rm -rf /"}) is True
            assert sh.is_high_risk({"command": "echo hi"}) is False
            assert sh.is_high_risk({}) is False  # 缺 command 键不崩

    asyncio.run(_self_check())
    print("openx/tools/shell_tools.py OK ✓")
