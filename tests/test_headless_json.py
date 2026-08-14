"""Headless JSON 输出模式回归测试 —— v0.3.2。

覆盖：
- _run_json：成功 → 单个 result 对象（type/subtype/is_error/duration_ms/
  num_turns/result/session_id/usage 齐全）、退出码 0；工具往返计入
  num_turns；异常 → is_error + error 字段 + 退出码 1；
- _run_stream_json：NDJSON 事件序列（system/init → tool_use →
  tool_result → text_delta → result）、init 携带 model/session_id/tools、
  失败路径 result.is_error；
- run_single_shot 分流：json/text 各自的成功与失败退出码；json 模式
  stdout 恰好一行 JSON；console 噪音重定向 stderr（raw.file 被换掉）；
- CLI：--output-format 非法值被 argparse 拒绝。

风格：pytest-asyncio auto、手写 FakeLLM、禁 unittest.mock；
stdout 捕获用 capsys（_emit 走内建 print）。

运行：``python -m pytest tests/test_headless_json.py -q``
"""

from __future__ import annotations

import json

import pytest

from openx.agent import OpenXAgent
from openx.cli.single_shot import (
    _run_json,
    _run_stream_json,
    run_single_shot,
)
from openx.config import OpenXConfig
from openx.llm import StreamDone, StreamReasoning
from openx.ui.console import Console


# ── Fakes ────────────────────────────────────────────────────────

class FakeLLM:
    """脚本化假 LLM：(content, tool_calls) 序列，chat 与 stream_chat 同源。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.call_count = 0

    def _next(self):
        content, tool_calls = self.responses[self.call_count]
        self.call_count += 1
        resp = {"role": "assistant", "content": content or None}
        if tool_calls:
            resp["tool_calls"] = tool_calls
        return resp

    async def chat(self, messages, tools=None, stream=True):
        return self._next()

    async def stream_chat(self, messages, tools=None):
        resp = self._next()
        if resp.get("content"):
            for tok in resp["content"].split():
                yield tok + " "
        yield StreamDone(
            response=resp, token_count=5, input_tokens=10,
        )


class ThinkingFakeLLM(FakeLLM):
    """FakeLLM + 正文前先吐两段 reasoning（模拟推理模型的流）。"""

    async def stream_chat(self, messages, tools=None):
        yield StreamReasoning("hmm ")
        yield StreamReasoning("let me see")
        async for ev in super().stream_chat(messages, tools):
            yield ev


class BoomLLM:
    """每次都抛异常的假 LLM（模拟 API 故障）。"""

    async def chat(self, messages, tools=None, stream=True):
        raise RuntimeError("api exploded")

    async def stream_chat(self, messages, tools=None):
        raise RuntimeError("api exploded")
        yield  # pragma: no cover —— 让本函数成为异步生成器


def _make_agent(tmp_path, llm):
    config = OpenXConfig()
    config.workspace = str(tmp_path)
    config.api_key = "sk-test"
    config.api_base = "https://example.com/v1"
    config.model = "test-model"
    agent = OpenXAgent(config)
    agent.llm = llm
    return agent


def _read_tool_call(path: str = "f.txt") -> list:
    return [{
        "id": "c1", "type": "function",
        "function": {
            "name": "read_file",
            "arguments": json.dumps({"file_path": path}),
        },
    }]


def _lines(capsys) -> list[dict]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


# ── _run_json ────────────────────────────────────────────────────

class TestRunJson:
    async def test_success_result_object(self, tmp_path, capsys):
        agent = _make_agent(tmp_path, FakeLLM([("hello world", None)]))
        code = await _run_json(agent, None, "say hi")
        assert code == 0
        events = _lines(capsys)
        assert len(events) == 1
        obj = events[0]
        assert obj["type"] == "result"
        assert obj["subtype"] == "success"
        assert obj["is_error"] is False
        assert obj["result"] == "hello world"
        assert obj["num_turns"] == 0
        assert obj["session_id"] == agent.session_id
        assert isinstance(obj["duration_ms"], int)
        assert set(obj["usage"]) == {"input_tokens", "output_tokens"}

    async def test_num_turns_counts_tool_rounds(self, tmp_path, capsys):
        (tmp_path / "f.txt").write_text("data")
        agent = _make_agent(tmp_path, FakeLLM([
            (None, _read_tool_call()),
            ("done", None),
        ]))
        code = await _run_json(agent, None, "read it")
        assert code == 0
        obj = _lines(capsys)[0]
        assert obj["num_turns"] == 1
        assert obj["result"] == "done"

    async def test_error_result(self, tmp_path, capsys):
        agent = _make_agent(tmp_path, BoomLLM())
        code = await _run_json(agent, None, "boom")
        assert code == 1
        obj = _lines(capsys)[0]
        assert obj["is_error"] is True
        assert obj["subtype"] == "error"
        assert obj["result"] is None
        assert "api exploded" in obj["error"]


# ── _run_stream_json ─────────────────────────────────────────────

class TestRunStreamJson:
    async def test_event_sequence(self, tmp_path, capsys):
        (tmp_path / "f.txt").write_text("data")
        agent = _make_agent(tmp_path, FakeLLM([
            (None, _read_tool_call()),
            ("final answer", None),
        ]))
        code = await _run_stream_json(agent, "read it")
        assert code == 0
        events = _lines(capsys)
        types = [e["type"] for e in events]
        assert types[0] == "system"
        assert events[0]["subtype"] == "init"
        assert events[0]["model"] == "test-model"
        assert events[0]["session_id"] == agent.session_id
        assert "read_file" in events[0]["tools"]
        assert "tool_use" in types
        assert "tool_result" in types
        assert "text_delta" in types
        assert types[-1] == "result"
        # 工具事件携带名字；result 携带终值与轮数
        tu = next(e for e in events if e["type"] == "tool_use")
        assert tu["name"] == "read_file"
        tr = next(e for e in events if e["type"] == "tool_result")
        assert tr["name"] == "read_file" and tr["is_error"] is False
        final = events[-1]
        assert final["result"] == "final answer"
        assert final["num_turns"] == 1

    async def test_stream_error_ends_with_error_result(self, tmp_path, capsys):
        agent = _make_agent(tmp_path, BoomLLM())
        code = await _run_stream_json(agent, "boom")
        assert code == 1
        events = _lines(capsys)
        assert events[0]["type"] == "system"
        assert events[-1]["type"] == "result"
        assert events[-1]["is_error"] is True

    async def test_thinking_delta_events(self, tmp_path, capsys):
        """reasoning 以独立 thinking_delta 事件流出：先于 text_delta、不混入。"""
        agent = _make_agent(tmp_path, ThinkingFakeLLM([("the answer", None)]))
        code = await _run_stream_json(agent, "think hard")
        assert code == 0
        events = _lines(capsys)
        types = [e["type"] for e in events]
        assert "thinking_delta" in types
        assert types.index("thinking_delta") < types.index("text_delta")
        td = [e for e in events if e["type"] == "thinking_delta"]
        assert "".join(e["text"] for e in td) == "hmm let me see"
        # reasoning 不进 result 终值（history 只存正文）
        assert events[-1]["result"] == "the answer"


# ── run_single_shot 分流与退出码 ─────────────────────────────────

class TestRunSingleShot:
    async def test_json_mode_single_line_stdout(self, tmp_path, capsys):
        agent = _make_agent(tmp_path, FakeLLM([("hi", None)]))
        console = Console(OpenXConfig())
        code = await run_single_shot(
            agent, console, "say hi", output_format="json",
        )
        assert code == 0
        out = capsys.readouterr().out
        lines = [l for l in out.splitlines() if l.strip()]
        assert len(lines) == 1  # stdout 恰好一个 JSON 对象
        assert json.loads(lines[0])["result"] == "hi"

    async def test_json_mode_failure_exit_code(self, tmp_path, capsys):
        agent = _make_agent(tmp_path, BoomLLM())
        console = Console(OpenXConfig())
        code = await run_single_shot(
            agent, console, "boom", output_format="json",
        )
        assert code == 1
        assert json.loads(
            [l for l in capsys.readouterr().out.splitlines() if l.strip()][-1]
        )["is_error"] is True

    async def test_json_mode_redirects_console_to_stderr(self, tmp_path):
        import sys
        agent = _make_agent(tmp_path, FakeLLM([("hi", None)]))
        console = Console(OpenXConfig())
        await run_single_shot(
            agent, console, "say hi", output_format="json",
        )
        assert console.raw.file is sys.stderr

    async def test_text_mode_success_and_failure_codes(self, tmp_path, capsys):
        ok_agent = _make_agent(tmp_path, FakeLLM([("hi", None)]))
        console = Console(OpenXConfig())
        assert await run_single_shot(ok_agent, console, "hi") == 0

        bad_agent = _make_agent(tmp_path, BoomLLM())
        assert await run_single_shot(bad_agent, console, "hi") == 1


# ── CLI 参数 ─────────────────────────────────────────────────────

class TestCliArgs:
    def test_output_format_choices(self):
        from openx.main import parse_args
        args = parse_args(["--output-format", "json", "do x"])
        assert args.output_format == "json"
        assert parse_args(["do x"]).output_format == "text"

    def test_invalid_format_rejected(self):
        from openx.main import parse_args
        with pytest.raises(SystemExit):
            parse_args(["--output-format", "xml", "do x"])
