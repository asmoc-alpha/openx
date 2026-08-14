"""User-defined hooks system (Claude Code-compatible schema).

在工具执行 / 用户提问 / 回合结束等事件点运行用户自定义的 shell 钩子，
让外部策略脚本（合规检查、审计、护栏）参与决策。

Config schema（镜像 Claude Code），配置在 ``~/.openx/settings.json``（全局）
和/或项目 ``<workspace>/.openx/settings.json``（项目级，**按事件扩展**全局——
同一事件的条目列表拼接，全局在前）::

    {
      "hooks": {
        "PreToolUse": [
          {"matcher": "shell",
           "hooks": [{"type": "command", "command": "./guard.sh", "timeout": 30}]}
        ],
        "PostToolUse": [...],
        "UserPromptSubmit": [...],
        "Stop": [...]
      }
    }

钩子语义（镜像 Claude Code）
============================
- ``matcher`` 是对工具名的 fnmatch 模式（缺省或 ``"*"`` = 所有工具）；
  仅工具事件（PreToolUse / PostToolUse）使用 matcher，UserPromptSubmit /
  Stop 忽略它。
- 事件 payload 以 JSON 写入钩子进程 stdin。
- **exit 0** → 放行；若 stdout 能解析成 ``{"decision": "block", "reason": ...}``
  则阻断，剩余钩子不再运行。
- **exit 2** → 阻断，reason 取 stderr 文本（回退 "hook exited 2"），剩余钩子
  不再运行。
- **timeout** → kill 进程，追加非阻塞警告。
- **其他非零** → 非阻塞警告（stderr 首行），不影响模型。
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
import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..config import SETTINGS_PATH

# 支持的事件类型（其余键一律忽略）
HOOK_EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop")
# 仅工具事件使用 matcher；其余事件忽略 matcher，有条目即触发
_TOOL_EVENTS = ("PreToolUse", "PostToolUse")
# 默认单钩子超时（秒）；条目里的 "timeout" 覆盖
DEFAULT_TIMEOUT = 30.0
# PostToolUse payload 中 tool_response 的截断上限（约 4000 字符）
TOOL_RESPONSE_LIMIT = 4000
# tool_input 中单个字符串值的截断上限——write_file 的巨型 content
# 绝不能原样灌满钩子 stdin 管道
TOOL_INPUT_LIMIT = 4000


# ── outcome ─────────────────────────────────────────────────────


@dataclass
class HookOutcome:
    """一次事件触发的聚合结果。

    - ``blocked`` + ``reason``：某钩子明确要求阻断（exit 2 或 stdout
      ``decision: block``）——调用方据此拦截工具/提问；
    - ``warnings``：非阻塞问题（超时、非零退出、启动失败）——只提示，
      绝不改变模型行为。
    """

    blocked: bool = False
    reason: str = ""
    warnings: list[str] = field(default_factory=list)


# ── payload builders（模块级函数，纯静态、便于测试）───────────────


def _truncate_tool_input(tool_input: dict) -> dict:
    """截断 tool_input 里的超长字符串值（如 write_file 的 content）。

    巨型参数原样 JSON 化写入钩子 stdin 会拖慢甚至撑爆管道；仅截断
    字符串值并追加 ``...[truncated]`` 标记，其他类型原样保留。
    """
    if not isinstance(tool_input, dict):
        return tool_input
    out: dict = {}
    for key, value in tool_input.items():
        if isinstance(value, str) and len(value) > TOOL_INPUT_LIMIT:
            out[key] = value[:TOOL_INPUT_LIMIT] + "...[truncated]"
        else:
            out[key] = value
    return out


def build_pretooluse_payload(
    tool_name: str,
    tool_input: dict,
    workspace: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """PreToolUse 事件 payload（tool_input 超长字符串值截断）。"""
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": _truncate_tool_input(tool_input),
        "workspace": workspace,
        "session_id": session_id,
    }


def build_posttooluse_payload(
    tool_name: str,
    tool_input: dict,
    tool_response: str,
    workspace: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """PostToolUse 事件 payload（tool_response / tool_input 均截断）。"""
    response = tool_response if isinstance(tool_response, str) else str(tool_response)
    if len(response) > TOOL_RESPONSE_LIMIT:
        response = response[:TOOL_RESPONSE_LIMIT]
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": _truncate_tool_input(tool_input),
        "tool_response": response,
        "workspace": workspace,
        "session_id": session_id,
    }


def build_userprompt_payload(
    prompt: str,
    workspace: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """UserPromptSubmit 事件 payload。"""
    return {
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
        "workspace": workspace,
        "session_id": session_id,
    }


def build_stop_payload(
    stop_reason: str,
    workspace: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Stop 事件 payload。"""
    return {
        "hook_event_name": "Stop",
        "stop_reason": stop_reason,
        "workspace": workspace,
        "session_id": session_id,
    }


# ── runner ──────────────────────────────────────────────────────


def _read_json(path: Path) -> dict:
    """读取 JSON 文件；缺失/损坏静默跳过（返回 {}），与 PermissionRules.load 一致。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, FileNotFoundError):
        return {}
    return data if isinstance(data, dict) else {}


class HookRunner:
    """加载配置并在事件点运行钩子命令。

    ``config`` 是**已按事件合并**的 hooks dict（``{event: [entry, ...]}``），
    显式传入以便测试；默认 None/{} 表示无钩子——零行为变化。
    """

    def __init__(
        self,
        config: dict | None = None,
        workspace: str = "",
        session_id: str = "",
    ) -> None:
        self.config: dict = config or {}
        self.workspace = workspace
        self.session_id = session_id

    # ── loading ─────────────────────────────────────────────

    @classmethod
    def load(cls, workspace: str = "") -> "HookRunner":
        """从全局 settings.json + 项目 ``<workspace>/.openx/settings.json`` 加载。

        项目级**扩展**全局：同一事件的条目列表拼接，全局在前（run 时按此
        顺序执行）。文件缺失/损坏静默跳过。
        """
        sources: list[Path] = [SETTINGS_PATH]
        if workspace:
            sources.append(Path(workspace) / ".openx" / "settings.json")

        merged: dict[str, list] = {}
        for src in sources:
            data = _read_json(src)
            hooks = data.get("hooks")
            if not isinstance(hooks, dict):
                continue
            for event, entries in hooks.items():
                if event not in HOOK_EVENTS or not isinstance(entries, list):
                    continue
                merged.setdefault(event, []).extend(entries)

        return cls(config=merged, workspace=str(workspace))

    # ── queries ─────────────────────────────────────────────

    def _matching_entries(self, event: str, tool_name: str = "") -> list[dict]:
        """返回该事件下匹配 tool_name 的条目列表（保持配置顺序）。"""
        entries = self.config.get(event)
        if not isinstance(entries, list):
            return []
        matched: list[dict] = []
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("hooks"):
                continue
            if event in _TOOL_EVENTS:
                # matcher 缺省或 "*" → 匹配所有工具
                matcher = entry.get("matcher") or "*"
                if not fnmatch.fnmatch(tool_name, matcher):
                    continue
            matched.append(entry)
        return matched

    def has_hooks(self, event: str, tool_name: str = "") -> bool:
        """廉价预检：无匹配条目 → False，调用方可完全跳过 payload 构建。"""
        return bool(self._matching_entries(event, tool_name))

    # ── execution ───────────────────────────────────────────

    async def run(self, event: str, payload: dict) -> HookOutcome:
        """运行事件下所有匹配钩子（顺序：全局先、项目后）。

        阻断语义（exit 2 / stdout decision:block）立即停止剩余钩子；
        超时与其他非零退出只累积警告。钩子进程启动失败同样降级为警告——
        钩子系统绝不能因自身故障打断主流程。
        """
        outcome = HookOutcome()
        tool_name = str(payload.get("tool_name") or "")
        for entry in self._matching_entries(event, tool_name):
            for hook in entry.get("hooks") or []:
                if not isinstance(hook, dict) or hook.get("type") != "command":
                    continue
                cmd = str(hook.get("command") or "").strip()
                if not cmd:
                    continue
                timeout = float(hook.get("timeout") or DEFAULT_TIMEOUT)
                if await self._run_command(cmd, payload, timeout, outcome) == "stop":
                    return outcome
        return outcome

    async def _run_command(
        self,
        cmd: str,
        payload: dict,
        timeout: float,
        outcome: HookOutcome,
    ) -> str:
        """执行单条钩子命令，返回 ``"stop"``（阻断，中止剩余）或 ``"continue"``。"""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as e:  # 启动失败（如无 shell）→ 非阻塞警告
            outcome.warnings.append(f"hook failed to start ({cmd}): {e}")
            return "continue"

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input=data), timeout=timeout
            )
        except asyncio.TimeoutError:
            # 超时 → kill 并回收进程，只记警告、不阻断。
            #
            # 两个实测陷阱（macOS/CPython 3.12）：
            # 1. wait_for 取消 communicate() 时，其内部 except 分支会"再杀一次
            #    并重新 await stdout/stderr 读到 EOF"——而钩子的孙进程（如
            #    sh -c "sleep 5" 里的 sleep）继承了管道写端，EOF 永不到来，
            #    取消流程会卡到孙进程自然退出。关闭三根管道的 transport 让
            #    那些挂起读立即结束。
            # 2. 回收限时 1s 兜底：即便孙进程仍占着管道，主流程也绝不被拖住。
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            for fd in (0, 1, 2):
                try:
                    transport = proc._transport.get_pipe_transport(fd)
                    if transport is not None:
                        transport.close()
                except Exception:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except Exception:
                pass
            outcome.warnings.append(
                f"hook timed out after {timeout:g}s: {cmd}"
            )
            return "continue"

        code = proc.returncode
        stderr_text = stderr_b.decode("utf-8", "replace").strip()

        # exit 2 → 阻断，reason 取 stderr 文本
        if code == 2:
            outcome.blocked = True
            outcome.reason = stderr_text or "hook exited 2"
            return "stop"

        # 其他非零 → 非阻塞警告（stderr 首行）
        if code != 0:
            first = stderr_text.splitlines()[0] if stderr_text else f"hook exited {code}"
            outcome.warnings.append(f"hook '{cmd}' exited {code}: {first}")
            return "continue"

        # exit 0 → 放行；stdout 若为 {"decision": "block", ...} 则阻断
        stdout_text = stdout_b.decode("utf-8", "replace").strip()
        if stdout_text:
            try:
                parsed = json.loads(stdout_text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict) and parsed.get("decision") == "block":
                outcome.blocked = True
                outcome.reason = str(parsed.get("reason") or stdout_text)
                return "stop"
        return "continue"

    # ── introspection ───────────────────────────────────────

    def describe(self) -> list[str]:
        """人类可读的钩子清单（供 /hooks 命令展示）。"""
        lines: list[str] = []
        for event in HOOK_EVENTS:
            for entry in self.config.get(event) or []:
                if not isinstance(entry, dict):
                    continue
                label = event
                matcher = entry.get("matcher")
                if matcher and event in _TOOL_EVENTS:
                    label = f"{event} [{matcher}]"
                for hook in entry.get("hooks") or []:
                    if not isinstance(hook, dict):
                        continue
                    cmd = hook.get("command", "")
                    timeout = hook.get("timeout")
                    suffix = f" (timeout {float(timeout):g}s)" if timeout else ""
                    lines.append(f"{label} → {cmd}{suffix}")
        return lines


if __name__ == "__main__":
    import tempfile

    def _script(td: Path, name: str, body: str) -> str:
        p = td / name
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(0o755)
        return str(p)

    with tempfile.TemporaryDirectory() as _td:
        td = Path(_td)
        ok = _script(td, "ok.sh", "cat > /dev/null\nexit 0\n")
        block = _script(td, "block.sh", 'echo "forbidden policy" >&2\nexit 2\n')
        js = _script(
            td, "json.sh",
            'cat > /dev/null\necho \'{"decision": "block", "reason": "json says no"}\'\n',
        )
        slow = _script(td, "slow.sh", "sleep 5\n")

        runner = HookRunner({
            "PreToolUse": [
                {"matcher": "shell", "hooks": [{"type": "command", "command": ok}]},
                {"matcher": "shell",
                 "hooks": [{"type": "command", "command": slow, "timeout": 0.2}]},
            ],
            "PostToolUse": [{"hooks": [{"type": "command", "command": ok}]}],
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": block}]}],
            "Stop": [{"hooks": [{"type": "command", "command": js}]}],
        }, workspace=_td, session_id="selftest")

        # matcher 预检：shell 命中、read_file 不命中
        assert runner.has_hooks("PreToolUse", "shell") is True
        assert runner.has_hooks("PreToolUse", "read_file") is False
        assert runner.has_hooks("UserPromptSubmit") is True
        assert runner.has_hooks("Stop") is True

        # exit 0 放行 + 超时仅警告（0.2s 超时，总耗时应远小于 sleep 5）
        import time as _time
        _t0 = _time.monotonic()
        out = asyncio.run(runner.run(
            "PreToolUse", build_pretooluse_payload("shell", {"command": "ls"}, _td, "s1")
        ))
        assert not out.blocked and len(out.warnings) == 1 and "timed out" in out.warnings[0]
        assert _time.monotonic() - _t0 < 2.0

        # exit 2 阻断，reason 取 stderr
        out = asyncio.run(runner.run(
            "UserPromptSubmit", build_userprompt_payload("rm -rf /", _td, "s1")
        ))
        assert out.blocked and "forbidden policy" in out.reason

        # stdout decision:block 阻断
        out = asyncio.run(runner.run("Stop", build_stop_payload("end_turn", _td, "s1")))
        assert out.blocked and out.reason == "json says no"

        # payload 截断：tool_response 与 tool_input 字符串值都不得超限
        _big = "y" * (TOOL_RESPONSE_LIMIT + 999)
        _post = build_posttooluse_payload(
            "write_file", {"content": _big, "n": 1}, _big,
        )
        assert len(_post["tool_response"]) == TOOL_RESPONSE_LIMIT
        assert _post["tool_input"]["content"].endswith("...[truncated]")
        assert len(_post["tool_input"]["content"]) == TOOL_INPUT_LIMIT + len("...[truncated]")
        assert _post["tool_input"]["n"] == 1  # 非字符串值原样保留
        _pre = build_pretooluse_payload("write_file", {"content": _big})
        assert _pre["tool_input"]["content"].endswith("...[truncated]")

        # describe：每个命令一行
        desc = runner.describe()
        assert len(desc) == 5 and "PreToolUse [shell]" in desc[0]

        # load：无配置的目录 → 空 runner（把 SETTINGS_PATH 临时指向不存在的
        # 路径，避免真实 ~/.openx/settings.json 里的钩子破坏自检确定性）
        _saved_settings = SETTINGS_PATH
        SETTINGS_PATH = td / "no-such-settings.json"
        try:
            empty = HookRunner.load(str(td))
        finally:
            SETTINGS_PATH = _saved_settings
        assert not empty.has_hooks("PreToolUse", "shell")
        assert asyncio.run(empty.run("Stop", {"hook_event_name": "Stop"})).warnings == []

    print("openx/core/hooks.py OK ✓")
