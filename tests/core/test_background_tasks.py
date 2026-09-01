"""Phase 7 后台 shell 任务回归测试。

覆盖：TaskRegistry 生命周期（start 立即返回 / watcher 回填退出码）/
tail_log / stop（进程组终止 + 已退出 + 未知 id）/ cleanup / id 单调递增 /
ShellTool run_in_background 集成 / task_output & task_stop 工具 /
前台路径回归 / 危险命令后台永远弹窗（拒绝不启动、批准可执行）/ 无 registry 报错 / agent 接线
（18 个工具 + 共享 registry）。

TASKS_DIR 与 hooks SETTINGS_PATH 均 monkeypatch 到 tmp_path，
绝不触碰真实 ~/.openx。

运行：``python -m pytest tests/test_background_tasks.py -q``
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from openx.core.tasks import TaskRegistry
from openx.tools.shell_tools import ShellTool
from openx.tools.task_tools import TaskOutputTool, TaskStopTool


# ── fixtures ────────────────────────────────────────────────────


@pytest.fixture
def tasks_tmp(tmp_path, monkeypatch):
    """隔离任务目录与全局 settings.json（agent 构造会读后者）。"""
    monkeypatch.setattr("openx.core.tasks.TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(
        "openx.core.hooks.SETTINGS_PATH", tmp_path / "no-such-settings.json"
    )
    return tmp_path / "tasks"


@pytest.fixture
async def registry(tasks_tmp):
    """读取已 patch 的 TASKS_DIR；teardown 兜底清理，失败也不留 sleep 进程。"""
    reg = TaskRegistry()
    yield reg
    await reg.cleanup()


# ── 1. 生命周期 ─────────────────────────────────────────────────


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_returns_immediately_and_watcher_fills_exit(self, registry):
        t0 = time.monotonic()
        handle = await registry.start("sleep 0.2; echo bg-done")
        assert time.monotonic() - t0 < 0.5  # 立即返回，不等待命令完成
        assert handle.task_id == "b1"
        assert handle.running and handle.exit_code is None

        await asyncio.sleep(0.6)
        assert not handle.running
        assert handle.exit_code == 0
        assert handle.end_time is not None
        assert "bg-done" in handle.log_path.read_text()


# ── 2. 日志尾部 ─────────────────────────────────────────────────


class TestTailLog:
    @pytest.mark.asyncio
    async def test_tail_contains_output(self, registry):
        await registry.start("sleep 0.1; echo bg-done")
        await asyncio.sleep(0.5)
        tail = registry.tail_log("b1", 50)
        assert tail is not None and "bg-done" in tail

    def test_unknown_id_returns_none(self, registry):
        assert registry.tail_log("b999") is None


# ── 3. 终止 ─────────────────────────────────────────────────────


class TestStop:
    @pytest.mark.asyncio
    async def test_stop_kills_process_group(self, registry):
        handle = await registry.start("sleep 30")
        pgid = handle.process.pid  # setsid → pgid == pid

        t0 = time.monotonic()
        msg = await registry.stop("b1")
        assert time.monotonic() - t0 < 3.0
        assert "Stopped b1" in msg
        assert not handle.running
        assert handle.exit_code is not None and handle.exit_code < 0
        # 进程组已整体消失（连同 sleep 子孙进程）
        with pytest.raises(ProcessLookupError):
            os.killpg(pgid, 0)

    @pytest.mark.asyncio
    async def test_stop_already_exited(self, registry):
        await registry.start("true")
        await asyncio.sleep(0.3)
        msg = await registry.stop("b1")
        assert "already exited 0" in msg

    @pytest.mark.asyncio
    async def test_stop_unknown_task(self, registry):
        assert "unknown task" in await registry.stop("b42")


# ── 4. 退出清理 ─────────────────────────────────────────────────


class TestCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_stops_all_running(self, registry):
        h1 = await registry.start("sleep 30")
        h2 = await registry.start("sleep 30")
        await registry.cleanup()
        assert not h1.running and not h2.running


# ── 5. id 单调递增 ──────────────────────────────────────────────


class TestIds:
    @pytest.mark.asyncio
    async def test_ids_increment_across_starts(self, registry):
        h1 = await registry.start("sleep 30")
        h2 = await registry.start("sleep 30")
        assert (h1.task_id, h2.task_id) == ("b1", "b2")
        await registry.cleanup()
        h3 = await registry.start("true")
        assert h3.task_id == "b3"  # stop 后仍递增（条目只增不删）
        await asyncio.sleep(0.3)


# ── 6. ShellTool 后台集成 + task 工具 ───────────────────────────


class TestShellIntegration:
    @pytest.mark.asyncio
    async def test_background_start_poll_stop_flow(self, tmp_path, registry):
        shell = ShellTool(str(tmp_path), task_registry=registry)

        t0 = time.monotonic()
        r = await shell.execute(command="sleep 5", run_in_background=True)
        assert time.monotonic() - t0 < 1.0  # 立即返回
        assert r.success
        assert "Background task b1 started" in r.output
        assert "task_output(task_id='b1')" in r.output
        assert "task_stop(task_id='b1')" in r.output

        out_tool, stop_tool = TaskOutputTool(registry), TaskStopTool(registry)

        # 运行中：状态行 + 空日志占位
        r2 = await out_tool.execute(task_id="b1")
        assert r2.success
        assert "[status: running]" in r2.output
        assert "(no output yet)" in r2.output

        # 停止后：状态行变 exited
        r3 = await stop_tool.execute(task_id="b1")
        assert r3.success and "Stopped b1" in r3.output
        r4 = await out_tool.execute(task_id="b1")
        assert "[status: exited" in r4.output

    @pytest.mark.asyncio
    async def test_task_output_unknown_id_lists_known(self, registry):
        await registry.start("sleep 30")
        out_tool = TaskOutputTool(registry)
        r = await out_tool.execute(task_id="b99")
        assert not r.success
        assert "b1" in r.error  # 错误里列出已知任务 id

    @pytest.mark.asyncio
    async def test_task_stop_unknown_id_errors(self, registry):
        stop_tool = TaskStopTool(registry)
        r = await stop_tool.execute(task_id="b99")
        assert not r.success and "unknown task" in r.error


# ── 7. 前台路径回归 ─────────────────────────────────────────────


class TestForegroundRegression:
    @pytest.mark.asyncio
    async def test_foreground_echo_unchanged(self, tmp_path, registry):
        shell = ShellTool(str(tmp_path), task_registry=registry)
        r = await shell.execute(command="echo hi")
        assert r.success
        assert "hi" in r.output
        assert registry.all() == []  # 前台执行不进任务注册表

    @pytest.mark.asyncio
    async def test_foreground_nonzero_exit_format(self, tmp_path):
        shell = ShellTool(str(tmp_path))  # 无 registry 也不影响前台
        r = await shell.execute(command="exit 3")
        assert not r.success
        assert "[exit code: 3]" in r.output


# ── 8. 后台模式不绕过安全检查 ───────────────────────────────────


class TestSafety:
    """危险命令永远弹窗（2026-07 高危升级）。

    原 ShellTool.execute 内的硬阻断已上移到 ToolExecutor.prepare 闸门
    （``is_high_risk`` → force_prompt）：auto_approve=True 也必弹窗，
    拒绝 → 任务绝不启动；批准 → 可执行（用户明确授权）。后台模式
    走同一 prepare 闸门，绝不绕过。
    """

    @pytest.mark.asyncio
    async def test_dangerous_background_denied_before_start(self, tmp_path, registry):
        """弹窗拒绝 → 任务永不启动（即便 auto_approve=True）。"""
        from openx.permissions import PermissionRules
        from openx.services.tool_executor import ToolExecutor

        class DenyConsole:
            async def ask_permission(self, tool_name, reason, details="",
                               args_summary="", can_remember=True, diff=None):
                return (False, False)

        shell = ShellTool(
            str(tmp_path),
            dangerous_patterns=[r"rm -rf /"],
            task_registry=registry,
        )
        executor = ToolExecutor(
            DenyConsole(), auto_approve=True, mode="auto",
            rules=PermissionRules(),
        )
        pc = await executor.prepare(
            "shell", shell,
            '{"command": "rm -rf /", "run_in_background": true}', "t1")
        result = await executor.execute_prepared(pc)
        assert not result.success
        assert "Permission denied by user" in result.error
        assert registry.all() == []  # 拦截发生在启动之前

    @pytest.mark.asyncio
    async def test_dangerous_background_approved_starts(self, tmp_path, registry):
        """弹窗批准 → 高危命令也可执行（用户明确授权后）。"""
        from openx.permissions import PermissionRules
        from openx.services.tool_executor import ToolExecutor

        class ApproveConsole:
            def __init__(self):
                self.asked: list[str] = []

            async def ask_permission(self, tool_name, reason, details="",
                               args_summary="", can_remember=True, diff=None):
                self.asked.append(tool_name)
                return (True, False)

        console = ApproveConsole()
        shell = ShellTool(
            str(tmp_path),
            dangerous_patterns=["sleep 5"],  # 把无害命令标为高危以测弹窗路径
            task_registry=registry,
        )
        executor = ToolExecutor(
            console, auto_approve=True, mode="auto",
            rules=PermissionRules(),
        )
        pc = await executor.prepare(
            "shell", shell,
            '{"command": "sleep 5", "run_in_background": true}', "t2")
        assert console.asked == ["shell"], "高危命令即便 -y 也必须弹窗"
        result = await executor.execute_prepared(pc)
        assert result.success, result.error
        assert len(registry.all()) == 1  # 批准后任务正常启动


# ── 9. 无 registry 的后台请求 ───────────────────────────────────


class TestNoRegistry:
    @pytest.mark.asyncio
    async def test_background_without_registry_errors_cleanly(self, tmp_path):
        shell = ShellTool(str(tmp_path))  # 未注入 registry
        r = await shell.execute(command="sleep 5", run_in_background=True)
        assert not r.success
        assert "background tasks not available" in r.error


# ── 10. agent 接线 ──────────────────────────────────────────────


class TestAgentWiring:
    def test_agent_wires_22_tools_with_shared_registry(self, tmp_path, tasks_tmp):
        from openx.agent import OpenXAgent
        from openx.config import OpenXConfig

        config = OpenXConfig()
        config.workspace = str(tmp_path)
        config.api_key = "sk-test"
        config.api_base = "https://example.com/v1"
        config.model = "test-model"
        agent = OpenXAgent(config)

        assert len(agent.tools) == 29  # +memory +4 元工具（P-A）+3 自产工具（P-F）
        assert "task_output" in agent.tools
        assert "task_stop" in agent.tools
        assert "task" in agent.tools
        assert "choose_mode" in agent.tools
        assert "memory" in agent.tools
        # 模型驱动装配/自产元工具：结构性常驻，列表里可见
        for meta in ("list_plugins", "plugin_help", "load_plugin", "unload_plugin",
                     "write_plugin", "test_plugin", "promote_plugin"):
            assert meta in agent.tools
        assert isinstance(agent.tasks, TaskRegistry)
        assert agent.tools["shell"].task_registry is agent.tasks
