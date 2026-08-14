"""Phase 3 并行工具执行回归测试。

覆盖：串行准备 / 并行执行（asyncio.gather）的真实重叠、结果按原 tool_call
顺序回喂、权限弹窗在任何 execute 之前全部完成、未知工具与合法工具混批、
execute() 兼容包装、on_prompt_start/end 钩子，以及 StreamingService 的
InputCapture 暂停/恢复（Bug 10）。

运行：``python -m pytest tests/test_parallel.py -q``
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from openx.agent import ToolResultEvent, ToolStartEvent
from openx.config import OpenXConfig
from openx.llm import StreamDone
from openx.permissions import PermissionRules
from openx.services.tool_executor import PreparedCall, ToolExecutor
from openx.tools.base import Tool, ToolResult
from openx.tools.file_tools import WriteFileTool


def _render_stream_chunk(chunk) -> str:
    """stream_run 事件 → REPL 展示串（与 StreamingService.feed 同渲染）。"""
    if isinstance(chunk, ToolStartEvent):
        return f"\n\n[dim]● {chunk.name}[/dim]\n"
    if isinstance(chunk, ToolResultEvent):
        if chunk.is_error:
            return f"  Error: {chunk.output}\n"
        return f"{chunk.output}\n" if chunk.output else ""
    return chunk


# ── Fakes（沿用 test_new_features / test_bugfixes 的手写风格）────────

class FakeLLM:
    """可脚本化的假 LLM：一轮可返回多个 tool_calls，最后一轮给文本。"""

    def __init__(self, responses):
        # responses: list of (content, tool_calls)
        self.responses = list(responses)
        self.call_count = 0

    async def stream_chat(self, messages, tools=None):
        content, tool_calls = self.responses[self.call_count]
        self.call_count += 1
        if content:
            for tok in content.split():
                yield tok + " "
        resp = {"role": "assistant", "content": content or None}
        if tool_calls:
            resp["tool_calls"] = tool_calls
        yield StreamDone(response=resp, token_count=5, input_tokens=10)

    async def chat(self, messages, tools=None, stream=True):
        content, tool_calls = self.responses[self.call_count]
        self.call_count += 1
        resp = {"role": "assistant", "content": content}
        if tool_calls:
            resp["tool_calls"] = tool_calls
        return resp


class FakeConsole:
    """Duck-typed console：记录权限询问事件，返回预设批准结果。"""

    def __init__(self, approve: bool = True):
        self.approve = approve
        self.asked: list[str] = []

    async def ask_permission(self, tool_name, reason, details="", args_summary="",
                       can_remember=True, diff=None):
        self.asked.append(tool_name)
        return (self.approve, False)


def _tc(tc_id: str, name: str, args: dict) -> dict:
    """构造一个 OpenAI 风格的 tool_call 条目。"""
    return {
        "id": tc_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _make_agent(tmp_path, responses):
    """构造挂载 FakeLLM 的 OpenXAgent（绕过真实 API 与 settings.json）。"""
    from openx.agent import OpenXAgent
    config = OpenXConfig()
    config.workspace = str(tmp_path)
    config.api_key = "sk-test"
    config.api_base = "https://example.com/v1"
    config.model = "test-model"
    agent = OpenXAgent(config)
    agent.llm = FakeLLM(responses)
    agent.tool_executor._rules = PermissionRules()  # 忽略真实 settings.json
    return agent


def _tool_messages(agent) -> list[dict]:
    """历史里的 tool 角色消息（按追加顺序）。"""
    return [m for m in agent.history.messages if m["role"] == "tool"]


# ── 1. 真实重叠：三个 read_file 并发执行 ──────────────────────────

class TestParallelOverlap:
    """gather 让多个工具执行真正重叠，而不是一个接一个。"""

    @pytest.mark.asyncio
    async def test_three_reads_overlap_in_time(self, tmp_path, monkeypatch):
        for name in ("a.txt", "b.txt", "c.txt"):
            (tmp_path / name).write_text(f"content of {name}\n")

        tcs = [
            _tc(f"c{i}", "read_file", {"file_path": f})
            for i, f in enumerate(("a.txt", "b.txt", "c.txt"))
        ]
        agent = _make_agent(tmp_path, [(None, tcs), ("all read", None)])

        # 在实例上包一层计时壳：记录 (start, end)，再委托真实读取
        tool = agent.tools["read_file"]
        real_execute = tool.execute
        spans: list[tuple[float, float]] = []

        async def timed_execute(**kw):
            start = time.monotonic()
            await asyncio.sleep(0.05)
            result = await real_execute(**kw)
            spans.append((start, time.monotonic()))
            return result

        monkeypatch.setattr(tool, "execute", timed_execute)

        out = await agent.run("read the three files")
        assert out == "all read"

        # 3 个调用全部执行且时间窗真实重叠：最晚开始 < 最早结束
        assert len(spans) == 3
        starts = [s for s, _ in spans]
        ends = [e for _, e in spans]
        assert max(starts) < min(ends), f"no overlap: spans={spans}"

        # 3 个结果全部回喂历史，顺序与 tool_calls 一致
        msgs = _tool_messages(agent)
        assert [m["tool_call_id"] for m in msgs] == ["c0", "c1", "c2"]
        assert all("content of" in m["content"] for m in msgs)


# ── 2. 顺序保证：完成顺序乱了，回喂顺序也不能乱 ──────────────────

class TestResultOrderPreserved:
    """先完成的调用不得插队：tool 消息按原 tool_call 顺序追加。"""

    @pytest.mark.asyncio
    async def test_stream_run_appends_in_call_order(self, tmp_path, monkeypatch):
        for i in range(3):
            (tmp_path / f"f{i}.txt").write_text(f"data{i}")

        # 故意用非字母序 id；并让第 1 个调用耗时最长（最后完成）
        tcs = [
            _tc("z9", "read_file", {"file_path": "f0.txt"}),
            _tc("a1", "read_file", {"file_path": "f1.txt"}),
            _tc("m5", "read_file", {"file_path": "f2.txt"}),
        ]
        agent = _make_agent(tmp_path, [(None, tcs), ("done", None)])

        tool = agent.tools["read_file"]
        real_execute = tool.execute
        delays = {"f0.txt": 0.04, "f1.txt": 0.0, "f2.txt": 0.02}

        async def staggered_execute(**kw):
            await asyncio.sleep(delays[kw["file_path"]])
            return await real_execute(**kw)

        monkeypatch.setattr(tool, "execute", staggered_execute)

        chunks = [c async for c in agent.stream_run("read them")]
        # 工具事件 → REPL 展示串后合并（与 StreamingService.feed 同渲染）
        joined = "".join(_render_stream_chunk(c) for c in chunks)
        assert "● read_file" in joined  # 每个调用的回显行仍在（[dim] 标签契约）

        msgs = _tool_messages(agent)
        assert [m["tool_call_id"] for m in msgs] == ["z9", "a1", "m5"]
        # read_file 输出带行号前缀（"     1\tdata0"）——内容顺序不乱即可
        assert [m["content"].split("\t")[-1] for m in msgs] == [
            "data0", "data1", "data2"
        ]


# ── 3. 串行准备：所有权限弹窗先于任何 execute ─────────────────────

class TestSerialPromptPhase:
    """prepare 阶段串行跑完全部弹窗，才轮到 gather 执行。"""

    @pytest.mark.asyncio
    async def test_two_write_prompts_before_any_execute(self, tmp_path, monkeypatch):
        events: list[str] = []

        class RecordingConsole:
            async def ask_permission(self, tool_name, reason, details="", args_summary="",
                               can_remember=True, diff=None):
                events.append(f"prompt:{tool_name}")
                return (True, False)

        tcs = [
            _tc("w1", "write_file", {"file_path": "x1.txt", "content": "one"}),
            _tc("w2", "write_file", {"file_path": "x2.txt", "content": "two"}),
        ]
        agent = _make_agent(tmp_path, [(None, tcs), ("wrote both", None)])
        agent.tool_executor.console = RecordingConsole()

        tool = agent.tools["write_file"]
        real_execute = tool.execute

        async def recording_execute(**kw):
            events.append(f"executing:{kw['file_path']}")
            await asyncio.sleep(0.01)
            return await real_execute(**kw)

        monkeypatch.setattr(tool, "execute", recording_execute)

        out = await agent.run("write two files")
        assert out == "wrote both"

        prompts = [e for e in events if e.startswith("prompt:")]
        execs = [e for e in events if e.startswith("executing:")]
        assert len(prompts) == 2                     # 恰好 2 次询问
        assert len(execs) == 2                       # 两个写入都执行
        # 最后一个弹窗早于第一次执行：弹窗与执行之间绝无交错
        last_prompt = max(events.index(p) for p in prompts)
        first_exec = min(events.index(e) for e in execs)
        assert last_prompt < first_exec, f"interleaved: {events}"

        assert (tmp_path / "x1.txt").read_text() == "one"
        assert (tmp_path / "x2.txt").read_text() == "two"


# ── 4. 混批：未知工具 + 合法工具同一轮 ────────────────────────────

class TestMixedBatch:
    """未知工具在 prepare 落 pre_result；同批合法调用照常执行。"""

    @pytest.mark.asyncio
    async def test_unknown_and_valid_in_one_turn(self, tmp_path):
        (tmp_path / "ok.txt").write_text("fine")
        tcs = [
            _tc("u1", "no_such_tool", {}),
            _tc("r1", "read_file", {"file_path": "ok.txt"}),
        ]
        agent = _make_agent(tmp_path, [(None, tcs), ("final text", None)])

        out = await agent.run("do both")
        assert out == "final text"

        by_id = {m["tool_call_id"]: m["content"] for m in _tool_messages(agent)}
        assert "Unknown tool: no_such_tool" in by_id["u1"]  # 错误结果回喂
        assert "fine" in by_id["r1"]                        # 合法调用成功
        assert agent.history.validate()                     # 序列依然合法


# ── 5. 兼容包装：execute() 仍返回 (ToolResult, approved) ─────────

class _EchoTool(Tool):
    """自检用回声工具。"""

    name = "echo"

    async def execute(self, **kw):
        return ToolResult(output=f"echo:{kw.get('text', '')}")


class TestExecuteBackCompat:
    """旧的单次 execute() 路径行为不变。"""

    @staticmethod
    def _executor():
        executor = ToolExecutor(FakeConsole(), auto_approve=True)
        executor._rules = PermissionRules()
        return executor

    @pytest.mark.asyncio
    async def test_execute_returns_tool_result_tuple(self):
        result, approved = await self._executor().execute(
            "echo", _EchoTool(), '{"text": "hi"}', "tc-1"
        )
        assert isinstance(result, ToolResult)
        assert approved and result.success
        assert result.output == "echo:hi"

    @pytest.mark.asyncio
    async def test_execute_still_rejects_bad_json(self):
        result, approved = await self._executor().execute(
            "echo", _EchoTool(), "{invalid"
        )
        assert not approved
        assert result.error.startswith("Invalid arguments")

    def test_prepared_call_defaults(self):
        pc = PreparedCall(tc_id="id", tool_name="echo", tool=None)
        assert pc.args is None
        assert pc.pre_result is None
        assert pc.approved is True


# ── 6. 弹窗钩子：on_prompt_start / on_prompt_end ─────────────────

class TestPromptCallbacks:
    """每次交互式询问前后成对触发；异常被吞，绝不外泄。"""

    @staticmethod
    def _asking_executor(console):
        executor = ToolExecutor(console, auto_approve=False)
        executor._rules = PermissionRules()
        return executor

    @pytest.mark.asyncio
    async def test_callbacks_wrap_each_prompt(self, tmp_path):
        events: list[str] = []
        console = FakeConsole(approve=True)
        original_ask = console.ask_permission

        async def recording_ask(*a, **kw):
            events.append("prompt")
            return await original_ask(*a, **kw)

        console.ask_permission = recording_ask
        executor = self._asking_executor(console)
        executor.on_prompt_start = lambda: events.append("start")
        executor.on_prompt_end = lambda: events.append("end")

        tool = WriteFileTool(str(tmp_path))
        for name in ("a.txt", "b.txt"):
            args = json.dumps({"file_path": name, "content": "x"})
            result, approved = await executor.execute("write_file", tool, args)
            assert approved and result.success

        # start/end 计数 == 弹窗数，且严格包裹每次弹窗
        assert events == ["start", "prompt", "end", "start", "prompt", "end"]

    @pytest.mark.asyncio
    async def test_end_fires_even_when_prompt_raises(self, tmp_path):
        events: list[str] = []

        class RaisingConsole:
            async def ask_permission(self, *a, **kw):
                events.append("prompt")
                raise RuntimeError("stdin lost")

        executor = self._asking_executor(RaisingConsole())
        executor.on_prompt_start = lambda: events.append("start")
        executor.on_prompt_end = lambda: events.append("end")

        args = json.dumps({"file_path": "z.txt", "content": "1"})
        result, approved = await executor.execute(
            "write_file", WriteFileTool(str(tmp_path)), args
        )
        # 弹窗异常被 prepare 兜底成错误结果——绝不外抛
        assert not approved
        assert "stdin lost" in result.error
        assert events == ["start", "prompt", "end"]  # end 依然触发

    @pytest.mark.asyncio
    async def test_callback_exceptions_swallowed(self, tmp_path):
        def bad_callback():
            raise ValueError("callback boom")

        executor = self._asking_executor(FakeConsole(approve=True))
        executor.on_prompt_start = bad_callback
        executor.on_prompt_end = bad_callback

        args = json.dumps({"file_path": "q.txt", "content": "1"})
        result, approved = await executor.execute(
            "write_file", WriteFileTool(str(tmp_path)), args
        )
        assert approved and result.success  # 钩子异常不影响权限流程


# ── 7. Bug 10：StreamingService 暂停/恢复 InputCapture ────────────

class TestCapturePauseResume:
    """pause_capture 移交排队行并恢复终端；resume 重启捕获；done 后为空操作。"""

    @staticmethod
    def _service():
        from types import SimpleNamespace
        from rich.text import Text
        from openx.services.streaming import StreamingService

        console = SimpleNamespace(
            _console=None,
            _input_queue=[],
            _frame_on_screen=False,
            _input_capture=None,
            _frame_renderable=lambda i, o: Text(""),  # 桩，避免真实终端 I/O
        )
        return StreamingService(console, input_tokens=0), console

    def test_pause_drains_queue_and_resume_restarts(self):
        from openx.ui.input_capture import InputCapture

        svc, console = self._service()
        cap = InputCapture()          # 不调用 start()：stdin 非 TTY 时本就是空操作
        cap._queue.append("typed during stream")
        svc._capture = cap
        console._input_capture = cap

        svc.pause_capture()
        assert svc._capture is None
        assert console._input_capture is None
        # 已排队整行移交控制台发送队列，不丢失
        assert console._input_queue == ["typed during stream"]

        svc.resume_capture()
        assert svc._capture is not None
        assert console._input_capture is svc._capture

    def test_pause_resume_noops_when_capture_absent(self):
        svc, console = self._service()
        svc.pause_capture()   # 未捕获：不抛异常
        assert console._input_capture is None
        svc.resume_capture()
        assert svc._capture is not None
        # 再次 resume：已有捕获时不重建
        same = svc._capture
        svc.resume_capture()
        assert svc._capture is same

    def test_no_resume_after_done(self):
        svc, console = self._service()
        svc._done = True
        svc.resume_capture()
        assert svc._capture is None  # 流已结束，绝不重启捕获
