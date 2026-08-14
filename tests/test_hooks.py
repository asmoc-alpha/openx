"""Phase 5 hooks 系统回归测试。

覆盖：exit 0 放行 / exit 2 阻断 / stdout decision:block / 超时非阻塞警告 /
matcher 作用域 / 配置缺失与全局-项目合并 / PostToolUse payload 投递 /
UserPromptSubmit payload 与 matcher 忽略 / agent 级 Stop 钩子与 session_id
接线 / set_plan_mode 重复启用不覆盖保存值。

钩子脚本是真实写入 tmp_path 并 chmod +x 的 shell 脚本；settings.json 路径
经 monkeypatch 隔离，绝不触碰真实 ~/.openx。

运行：``python -m pytest tests/test_hooks.py -q``
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from openx.config import OpenXConfig
from openx.core.hooks import (
    TOOL_RESPONSE_LIMIT,
    HookOutcome,
    HookRunner,
    build_posttooluse_payload,
    build_pretooluse_payload,
    build_stop_payload,
    build_userprompt_payload,
)
from openx.permissions import PermissionRules
from openx.services.tool_executor import ToolExecutor
from openx.tools.base import Tool, ToolResult


# ── helpers / fakes ─────────────────────────────────────────────


def _script(path: Path, body: str) -> str:
    """写入一个可执行 shell 脚本，返回其路径字符串。"""
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return str(path)


class _EchoTool(Tool):
    """回显 text 参数的简单工具。"""

    name = "echo"

    async def execute(self, **kw):
        return ToolResult(output=f"echo:{kw.get('text', '')}")


class _CountTool(Tool):
    """记录执行次数的工具——断言"工具从未执行"用。"""

    name = "counter"

    def __init__(self):
        self.ran = 0

    async def execute(self, **kw):
        self.ran += 1
        return ToolResult(output="ran")


class FakeConsole:
    """Duck-typed console：记录权限询问与钩子警告。"""

    def __init__(self, approve: bool = True):
        self.approve = approve
        self.asked: list[str] = []
        self.warnings: list[str] = []

    async def ask_permission(self, tool_name, reason, details="", args_summary="",
                       can_remember=True, diff=None):
        self.asked.append(tool_name)
        return (self.approve, False)

    def print_warning(self, message: str) -> None:
        self.warnings.append(message)


def _executor(hooks_cfg, tmp_path, console=None, session="t0"):
    """构造挂载指定 hooks 配置的 auto-approve executor（忽略真实 settings）。"""
    console = console or FakeConsole()
    ex = ToolExecutor(
        console,
        auto_approve=True,
        hook_runner=HookRunner(
            hooks_cfg, workspace=str(tmp_path), session_id=session
        ),
    )
    ex._rules = PermissionRules()  # 忽略真实 settings.json，保证确定性
    return ex


def _make_agent(tmp_path, responses):
    """构造挂载 FakeLLM 的 OpenXAgent（绕过真实 API 与 settings.json）。"""
    from openx.agent import OpenXAgent
    from .test_bugfixes import FakeLLM

    config = OpenXConfig()
    config.workspace = str(tmp_path)
    config.api_key = "sk-test"
    config.api_base = "https://example.com/v1"
    config.model = "test-model"
    agent = OpenXAgent(config)
    agent.llm = FakeLLM(responses)
    agent.tool_executor._rules = PermissionRules()  # 忽略真实 settings.json
    return agent


# ── 1/2/3. 退出码语义 ───────────────────────────────────────────


class TestExitCodes:
    """exit 0 放行、exit 2 阻断、stdout decision:block 阻断。"""

    @pytest.mark.asyncio
    async def test_exit0_passes_and_tool_runs(self, tmp_path):
        ok = _script(tmp_path / "ok.sh", "cat > /dev/null\nexit 0\n")
        ex = _executor(
            {"PreToolUse": [{"hooks": [{"type": "command", "command": ok}]}]},
            tmp_path,
        )
        tool = _CountTool()
        result, approved = await ex.execute("counter", tool, "{}")
        assert approved and result.success and result.output == "ran"
        assert tool.ran == 1  # 钩子放行 → 工具正常执行

    @pytest.mark.asyncio
    async def test_exit2_blocks_at_runner_level(self, tmp_path):
        block = _script(
            tmp_path / "block.sh", 'echo "forbidden policy" >&2\nexit 2\n'
        )
        runner = HookRunner({"PreToolUse": [{"hooks": [{"type": "command", "command": block}]}]})
        outcome = await runner.run(
            "PreToolUse", build_pretooluse_payload("shell", {"command": "rm -rf /"})
        )
        assert outcome.blocked is True
        assert "forbidden policy" in outcome.reason

    @pytest.mark.asyncio
    async def test_exit2_blocks_tool_execution(self, tmp_path):
        block = _script(
            tmp_path / "block.sh", 'echo "forbidden policy" >&2\nexit 2\n'
        )
        ex = _executor(
            {"PreToolUse": [{"hooks": [{"type": "command", "command": block}]}]},
            tmp_path,
        )
        tool = _CountTool()
        result, approved = await ex.execute("counter", tool, "{}")
        assert not approved
        assert tool.ran == 0  # 工具从未执行
        assert "Blocked by PreToolUse hook" in result.error
        assert "forbidden policy" in result.error

    @pytest.mark.asyncio
    async def test_blocked_call_skips_posttooluse(self, tmp_path):
        block = _script(tmp_path / "block.sh", "exit 2\n")
        dump = tmp_path / "post.json"
        ex = _executor(
            {
                "PreToolUse": [{"hooks": [{"type": "command", "command": block}]}],
                "PostToolUse": [
                    {"hooks": [{"type": "command", "command": f'cat > "{dump}"'}]}
                ],
            },
            tmp_path,
        )
        await ex.execute("counter", _CountTool(), "{}")
        assert not dump.exists()  # 被阻断的调用不触发 PostToolUse

    @pytest.mark.asyncio
    async def test_stdout_decision_block_on_exit0(self, tmp_path):
        js = _script(
            tmp_path / "json.sh",
            "cat > /dev/null\n"
            "echo '{\"decision\": \"block\", \"reason\": \"json says no\"}'\n"
            "exit 0\n",
        )
        runner = HookRunner({"PreToolUse": [{"hooks": [{"type": "command", "command": js}]}]})
        outcome = await runner.run(
            "PreToolUse", build_pretooluse_payload("shell", {})
        )
        assert outcome.blocked is True
        assert outcome.reason == "json says no"


# ── 4. 超时 ─────────────────────────────────────────────────────


class TestTimeout:
    """超时钩子：kill + 非阻塞警告，且及时返回。"""

    @pytest.mark.asyncio
    async def test_timeout_warns_not_blocks_and_returns_promptly(self, tmp_path):
        slow = _script(tmp_path / "slow.sh", "sleep 5\n")
        runner = HookRunner(
            {"PreToolUse": [{"hooks": [{"type": "command", "command": slow, "timeout": 0.2}]}]}
        )
        t0 = time.monotonic()
        outcome = await runner.run(
            "PreToolUse", build_pretooluse_payload("shell", {"command": "ls"})
        )
        elapsed = time.monotonic() - t0
        assert outcome.blocked is False
        assert outcome.warnings
        assert any("timed out" in w for w in outcome.warnings)
        assert elapsed < 2.0  # 远小于 sleep 5，也远小于默认 30s 超时


# ── 5. matcher 作用域 ───────────────────────────────────────────


class TestMatcherScoping:
    """matcher 是工具名的 fnmatch 模式；不匹配的工具绝不触发钩子。"""

    def test_precheck_scoping(self, tmp_path):
        runner = HookRunner(
            {"PreToolUse": [{"matcher": "shell", "hooks": [{"type": "command", "command": "true"}]}]}
        )
        assert runner.has_hooks("PreToolUse", "read_file") is False
        assert runner.has_hooks("PreToolUse", "shell") is True

    @pytest.mark.asyncio
    async def test_unmatched_tool_spawns_nothing(self, tmp_path):
        marker = tmp_path / "ran.marker"
        touch = _script(tmp_path / "touch.sh", f'touch "{marker}"\nexit 0\n')
        runner = HookRunner(
            {"PreToolUse": [{"matcher": "shell", "hooks": [{"type": "command", "command": touch}]}]}
        )
        outcome = await runner.run(
            "PreToolUse", build_pretooluse_payload("read_file", {"file_path": "x"})
        )
        assert not outcome.blocked and outcome.warnings == []
        assert not marker.exists()  # 钩子进程从未启动

    def test_wildcard_and_absent_match_all(self, tmp_path):
        runner = HookRunner(
            {"PreToolUse": [
                {"matcher": "*", "hooks": [{"type": "command", "command": "true"}]},
                {"hooks": [{"type": "command", "command": "true"}]},  # 缺省 matcher
            ]}
        )
        assert runner.has_hooks("PreToolUse", "any_tool_name") is True

    def test_glob_pattern(self, tmp_path):
        runner = HookRunner(
            {"PreToolUse": [{"matcher": "git_*", "hooks": [{"type": "command", "command": "true"}]}]}
        )
        assert runner.has_hooks("PreToolUse", "git_status") is True
        assert runner.has_hooks("PreToolUse", "shell") is False


# ── 6/7. 配置加载与合并 ─────────────────────────────────────────


class TestConfigLoading:
    """load()：缺失文件静默跳过；项目设置按事件扩展全局（全局在前）。"""

    def test_load_no_settings_anywhere(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "openx.core.hooks.SETTINGS_PATH", tmp_path / "no-such-settings.json"
        )
        runner = HookRunner.load(str(tmp_path))  # 无 .openx/settings.json
        for event in ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"):
            assert runner.has_hooks(event, "shell") is False

    @pytest.mark.asyncio
    async def test_run_without_hooks_returns_empty_outcome(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "openx.core.hooks.SETTINGS_PATH", tmp_path / "no-such-settings.json"
        )
        runner = HookRunner.load(str(tmp_path))
        outcome = await runner.run("Stop", {"hook_event_name": "Stop"})
        assert outcome == HookOutcome()

    def test_global_and_project_merge_global_first(self, tmp_path, monkeypatch):
        order_file = tmp_path / "order.txt"
        # 全局 settings（monkeypatch 隔离）
        global_settings = tmp_path / "global-settings.json"
        global_settings.write_text(json.dumps({
            "hooks": {"PreToolUse": [{
                "matcher": "shell",
                "hooks": [{"type": "command",
                           "command": f'echo global >> "{order_file}"'}],
            }]},
        }))
        monkeypatch.setattr("openx.core.hooks.SETTINGS_PATH", global_settings)
        # 项目 settings
        (tmp_path / ".openx").mkdir()
        (tmp_path / ".openx" / "settings.json").write_text(json.dumps({
            "hooks": {"PreToolUse": [{
                "matcher": "*",
                "hooks": [{"type": "command",
                           "command": f'echo project >> "{order_file}"'}],
            }]},
        }))

        runner = HookRunner.load(str(tmp_path))
        entries = runner.config["PreToolUse"]
        assert len(entries) == 2  # 两个事件条目都可见

    @pytest.mark.asyncio
    async def test_merge_execution_order_global_first(self, tmp_path, monkeypatch):
        order_file = tmp_path / "order.txt"
        global_settings = tmp_path / "global-settings.json"
        global_settings.write_text(json.dumps({
            "hooks": {"PreToolUse": [{
                "hooks": [{"type": "command",
                           "command": f'echo global >> "{order_file}"'}],
            }]},
        }))
        monkeypatch.setattr("openx.core.hooks.SETTINGS_PATH", global_settings)
        (tmp_path / ".openx").mkdir()
        (tmp_path / ".openx" / "settings.json").write_text(json.dumps({
            "hooks": {"PreToolUse": [{
                "hooks": [{"type": "command",
                           "command": f'echo project >> "{order_file}"'}],
            }]},
        }))

        runner = HookRunner.load(str(tmp_path))
        await runner.run("PreToolUse", build_pretooluse_payload("shell", {}))
        assert order_file.read_text().split() == ["global", "project"]

    def test_project_only_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "openx.core.hooks.SETTINGS_PATH", tmp_path / "no-such-settings.json"
        )
        (tmp_path / ".openx").mkdir()
        (tmp_path / ".openx" / "settings.json").write_text(json.dumps({
            "hooks": {"UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "true"}]}
            ]},
        }))
        runner = HookRunner.load(str(tmp_path))
        assert runner.has_hooks("UserPromptSubmit") is True
        assert runner.has_hooks("PreToolUse", "shell") is False

    def test_corrupt_settings_skipped_silently(self, tmp_path, monkeypatch):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json at all")
        monkeypatch.setattr("openx.core.hooks.SETTINGS_PATH", bad)
        runner = HookRunner.load(str(tmp_path))
        assert runner.has_hooks("Stop") is False


# ── 8. PostToolUse 集成 ─────────────────────────────────────────


class TestPostToolUseIntegration:
    """工具成功后触发 PostToolUse：payload 含 tool_name + tool_response。"""

    @pytest.mark.asyncio
    async def test_payload_dumped_to_file(self, tmp_path):
        dump = tmp_path / "post.json"
        ex = _executor(
            {"PostToolUse": [
                {"hooks": [{"type": "command", "command": f'cat > "{dump}"'}]}
            ]},
            tmp_path,
            session="sess-8",
        )
        result, approved = await ex.execute("echo", _EchoTool(), '{"text": "hello world"}')
        assert approved and result.success

        data = json.loads(dump.read_text())
        assert data["hook_event_name"] == "PostToolUse"
        assert data["tool_name"] == "echo"
        assert data["tool_input"] == {"text": "hello world"}
        assert "hello world" in data["tool_response"]
        assert data["workspace"] == str(tmp_path)
        assert data["session_id"] == "sess-8"

    @pytest.mark.asyncio
    async def test_failing_post_hook_is_nonblocking(self, tmp_path):
        # exit 3（非 0 非 2）→ 非阻塞警告；工具结果不受影响
        console = FakeConsole()
        ex = _executor(
            {"PostToolUse": [
                {"hooks": [{"type": "command", "command": "echo oops >&2; exit 3"}]}
            ]},
            tmp_path,
            console=console,
        )
        result, approved = await ex.execute("echo", _EchoTool(), '{"text": "x"}')
        assert approved and result.success and result.output == "echo:x"
        assert console.warnings  # 警告经 console.print_warning 打印
        assert any("exited 3" in w for w in console.warnings)


# ── executor 与存储规则的交互 ───────────────────────────────────


class TestHookVsStoredRules:
    """钩子策略优先于缓存的用户决定：即便存储 allow 也能驳回。"""

    @pytest.mark.asyncio
    async def test_hook_can_veto_stored_allow(self, tmp_path):
        block = _script(tmp_path / "block.sh", "echo policy >&2; exit 2\n")
        ex = _executor(
            {"PreToolUse": [{"hooks": [{"type": "command", "command": block}]}]},
            tmp_path,
        )
        ex._rules.allow.append("counter")  # 存储规则已放行
        tool = _CountTool()
        result, approved = await ex.execute("counter", tool, "{}")
        assert not approved and tool.ran == 0
        assert "Blocked by PreToolUse hook" in result.error


# ── 9. UserPromptSubmit ─────────────────────────────────────────


class TestUserPromptSubmit:
    """payload 携带 prompt；matcher 对非工具事件无意义。"""

    @pytest.mark.asyncio
    async def test_payload_contains_prompt_and_blocks(self, tmp_path):
        dump = tmp_path / "prompt.json"
        block = _script(
            tmp_path / "block.sh",
            f'cat > "{dump}"\necho "prompt not allowed" >&2\nexit 2\n',
        )
        runner = HookRunner(
            {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": block}]}]},
            workspace=str(tmp_path),
            session_id="s9",
        )
        outcome = await runner.run(
            "UserPromptSubmit",
            build_userprompt_payload("do something bad", str(tmp_path), "s9"),
        )
        assert outcome.blocked and "prompt not allowed" in outcome.reason
        data = json.loads(dump.read_text())
        assert data["hook_event_name"] == "UserPromptSubmit"
        assert data["prompt"] == "do something bad"
        assert data["session_id"] == "s9"

    def test_matcher_ignored_for_prompt_events(self, tmp_path):
        # 条目带着 matcher "shell"——对 UserPromptSubmit 依然生效
        runner = HookRunner(
            {"UserPromptSubmit": [
                {"matcher": "shell", "hooks": [{"type": "command", "command": "true"}]}
            ]}
        )
        assert runner.has_hooks("UserPromptSubmit") is True


# ── payload 构造器 ──────────────────────────────────────────────


class TestPayloadBuilders:
    """payload 字段齐全；tool_response 截断到约 4000 字符。"""

    def test_pretooluse_fields(self):
        p = build_pretooluse_payload("shell", {"command": "ls"}, "/ws", "s1")
        assert p == {
            "hook_event_name": "PreToolUse",
            "tool_name": "shell",
            "tool_input": {"command": "ls"},
            "workspace": "/ws",
            "session_id": "s1",
        }

    def test_posttooluse_truncation(self):
        big = "x" * (TOOL_RESPONSE_LIMIT + 500)
        p = build_posttooluse_payload("read_file", {}, big)
        assert len(p["tool_response"]) == TOOL_RESPONSE_LIMIT

    def test_stop_payload(self):
        p = build_stop_payload("end_turn", "/ws", "s1")
        assert p["stop_reason"] == "end_turn" and p["hook_event_name"] == "Stop"


# ── describe() 与 /hooks ────────────────────────────────────────


class TestDescribe:
    """describe() 为每个事件/matcher/命令产出一行人类可读描述。"""

    def test_describe_lines(self):
        runner = HookRunner({
            "PreToolUse": [
                {"matcher": "shell",
                 "hooks": [{"type": "command", "command": "./guard.sh", "timeout": 30}]},
            ],
            "Stop": [{"hooks": [{"type": "command", "command": "./stop.sh"}]}],
        })
        lines = runner.describe()
        assert len(lines) == 2
        assert "PreToolUse [shell]" in lines[0] and "./guard.sh" in lines[0]
        assert "timeout 30s" in lines[0]
        assert lines[1].startswith("Stop") and "./stop.sh" in lines[1]

    def test_describe_empty(self):
        assert HookRunner().describe() == []


# ── agent 级集成：接线、Stop 钩子、plan-mode 守卫 ────────────────


class TestAgentIntegration:
    """session_id / hooks 接线；Stop 钩子在回合收尾触发。"""

    def test_session_id_and_hooks_wired(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "openx.core.hooks.SETTINGS_PATH", tmp_path / "no-such-settings.json"
        )
        agent = _make_agent(tmp_path, [])
        assert len(agent.session_id) == 12
        int(agent.session_id, 16)  # 12 位十六进制
        assert agent.tool_executor.hooks is agent.hooks
        assert agent.hooks.session_id == agent.session_id
        assert agent.hooks.workspace == str(agent.workspace)

    @pytest.mark.asyncio
    async def test_stop_hook_fires_on_end_turn(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "openx.core.hooks.SETTINGS_PATH", tmp_path / "no-such-settings.json"
        )
        dump = tmp_path / "stop.json"
        (tmp_path / ".openx").mkdir()
        (tmp_path / ".openx" / "settings.json").write_text(json.dumps({
            "hooks": {"Stop": [
                {"hooks": [{"type": "command", "command": f'cat > "{dump}"'}]}
            ]},
        }))
        agent = _make_agent(tmp_path, [("done here", None)])
        out = await agent.run("hi")
        assert out == "done here"
        data = json.loads(dump.read_text())
        assert data["hook_event_name"] == "Stop"
        assert data["stop_reason"] == "end_turn"
        assert data["session_id"] == agent.session_id

    @pytest.mark.asyncio
    async def test_stream_run_also_fires_stop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "openx.core.hooks.SETTINGS_PATH", tmp_path / "no-such-settings.json"
        )
        dump = tmp_path / "stop.json"
        (tmp_path / ".openx").mkdir()
        (tmp_path / ".openx" / "settings.json").write_text(json.dumps({
            "hooks": {"Stop": [
                {"hooks": [{"type": "command", "command": f'cat > "{dump}"'}]}
            ]},
        }))
        agent = _make_agent(tmp_path, [("streamed answer", None)])
        chunks = [c async for c in agent.stream_run("hi")]
        assert "".join(chunks).startswith("streamed")
        data = json.loads(dump.read_text())
        assert data["stop_reason"] == "end_turn"


class TestPlanModeSaveGuard:
    """set_plan_mode(True) 重复调用不得覆盖已保存的 auto_approve。"""

    def test_double_enable_preserves_saved_value(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "openx.core.hooks.SETTINGS_PATH", tmp_path / "no-such-settings.json"
        )
        config = OpenXConfig()
        config.workspace = str(tmp_path)
        config.auto_approve = True
        from openx.agent import OpenXAgent
        agent = OpenXAgent(config)
        assert agent.tool_executor.auto_approve is True

        agent.set_plan_mode(True)
        assert agent._pre_plan_auto_approve is True  # 保存了进入前的值
        agent.tool_executor.auto_approve = False  # 闸门已关

        agent.set_plan_mode(True)  # 重复进入：不得覆盖保存值
        assert agent._pre_plan_auto_approve is True

        agent.set_plan_mode(False)  # 退出：原样还原并清空
        assert agent.tool_executor.auto_approve is True
        assert agent._pre_plan_auto_approve is None
