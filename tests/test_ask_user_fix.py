"""ask_user 弹窗竞态与入参健壮性修复的回归测试。

用户报告：模型调用 ask_user 时屏幕疯狂打印、问题迟迟弹不出来。

两条已确认的根因：

A. **弹窗 vs 流式竞态。** ask_user / exit_plan_mode 的弹窗发生在工具
   ``execute()`` 内部——executor 的 ``on_prompt_start/end`` 钩子只包裹
   ``prepare()`` 里的权限弹窗，够不到它们。于是 Rich ``Live`` 以 10Hz 与
   弹窗的 ANSI 光标控制互踩（疯狂打印），``InputCapture`` 线程与弹窗的
   raw-mode 直读互抢按键（问题无法作答）。
   修复：StreamingService 引用计数式 ``pause()``/``resume()``（同时停 Live
   刷新与捕获）+ Console 级 ``on_dialog_start/end`` 钩子（DialogsMixin 在
   交互选择器前后 try/finally 触发），``interactive.py`` 把钩子接到
   ``display.pause/resume``。

B. **工具调用错误循环。** 模型给出的 ``options`` 形状多变（字符串数组 /
   JSON 编码字符串 / 缺 label），旧代码 ``options[:4]`` + 直传 console：
   字符串被切成前 4 个**字符**、字符串项没有 ``.get`` → ``execute()`` 抛
   AttributeError → ToolExecutor 转成错误结果 → 模型重试 ask_user → 每次
   重试刷一行 ``● ask_user``，屏幕被刷屏而问题永远弹不出来。
   修复：防御性规范化（dict / str / JSON 字符串皆收），不可用时返回清晰
   错误结果引导模型纠正，绝不抛异常出 ``execute()``。

运行：``python -m pytest tests/test_ask_user_fix.py -q``
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from rich.console import Console as RichConsole
from rich.text import Text

from openx.config import OpenXConfig
from openx.services.streaming import StreamingService
from openx.tools.ask_user_tool import AskUserTool, _coerce_bool, _normalize_options
from openx.ui.console import Console


# ── 工具替身（house style：手写 fake，不用 unittest.mock）─────────


def _service():
    """构造不触碰真实终端的 StreamingService（Live 渲染进 StringIO）。"""
    buf = io.StringIO()
    console = SimpleNamespace(
        _console=RichConsole(
            file=buf, width=80, height=24, force_terminal=True
        ),
        _input_queue=[],
        _frame_on_screen=False,
        _input_capture=None,
        _frame_renderable=lambda i, o: Text("frame"),
    )
    return StreamingService(console, input_tokens=0), console, buf


class DeterministicLive:
    """Live 测试替身工厂：关自动刷新线程与 stdout 劫持。

    自动刷新线程会与测试的 StringIO console 并发写（StringIO 非线程安全，
    并发写会段错误）；stdout 劫持会吞掉 pytest 输出。测试里统一换成
    auto_refresh=False 的真实 Live 子类，手动驱动刷新。
    """

    @staticmethod
    def patch(monkeypatch):
        import openx.services.streaming as streaming_mod
        from rich.live import Live

        class _Live(Live):
            def __init__(self, *args, **kwargs):
                kwargs.update(
                    auto_refresh=False,
                    redirect_stdout=False,
                    redirect_stderr=False,
                )
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(streaming_mod, "Live", _Live)


class RecordingConsole:
    """记录 ask_user_question 收到的规范化参数；可配置返回值或异常。"""

    def __init__(self, answer=("A",), raises=None):
        self.calls: list[dict] = []
        self._answer = answer
        self._raises = raises

    def ask_user_question(self, question, options, multi_select=False):
        self.calls.append(
            {"question": question, "options": options, "multi_select": multi_select}
        )
        if self._raises is not None:
            raise self._raises
        return list(self._answer)


# ── A1. 引用计数式 pause / resume ─────────────────────────────────


class TestRefCountedPause:
    """pause×N 需要 resume×N 才恢复——钩子叠加触发必须安全。"""

    @pytest.fixture(autouse=True)
    def _deterministic_live(self, monkeypatch):
        DeterministicLive.patch(monkeypatch)  # 无后台刷新线程，避免竞态

    def test_nested_pause_needs_matching_resumes(self):
        svc, console, _ = _service()
        svc.start()
        assert svc._live.is_started and svc._capture is not None

        svc.pause()  # 例如 console 弹窗钩子
        assert not svc._live.is_started  # Live 刷新停掉
        assert svc._capture is None      # 捕获停掉（排队行已移交）
        assert console._input_capture is None

        svc.pause()   # executor 权限钩子叠加第二次
        svc.resume()  # 内层结束——还差一次
        assert not svc._live.is_started, "pause×2 后 resume×1 必须仍在暂停"
        assert svc._capture is None

        svc.resume()  # 外层结束
        assert svc._live.is_started, "resume×2 后必须完全恢复"
        assert svc._capture is not None
        assert console._input_capture is svc._capture

        svc.done()
        assert not svc._live.is_started

    def test_resume_without_pause_is_noop(self):
        svc, console, _ = _service()
        svc.start()
        live = svc._live
        svc.resume()  # 无配对的 pause：计数保持 0，不得误停/误启
        assert svc._pause_count == 0
        assert svc._live.is_started and svc._live is live

    def test_no_resume_after_done(self):
        svc, _, _ = _service()
        svc.start()
        svc.pause()
        svc._done = True  # 模拟流在暂停期间被收尾
        svc.resume()
        assert not svc._live.is_started  # 流已结束，绝不重启 Live
        assert svc._capture is None

    def test_pause_drains_queued_lines(self):
        """旧 pause_capture 契约保留：暂停把已排队整行移交控制台队列。"""
        from openx.ui.input_capture import InputCapture

        svc, console, _ = _service()
        cap = InputCapture()  # 不 start()：stdin 非 TTY 时本就是空操作
        cap._queue.append("typed during stream")
        svc._capture = cap
        console._input_capture = cap

        svc.pause()
        assert console._input_queue == ["typed during stream"]

    def test_back_compat_aliases_delegate(self):
        """pause_capture/resume_capture 现为 pause/resume 的别名。"""
        svc, _, _ = _service()
        svc.start()
        svc.pause_capture()
        assert svc._pause_count == 1 and not svc._live.is_started
        svc.resume_capture()
        assert svc._pause_count == 0 and svc._live.is_started
        svc.done()


# ── A2. Console 级弹窗钩子 ────────────────────────────────────────


class TestDialogHooks:
    """ask_user_question / confirm_plan / ask_permission 在交互选择器
    前后成对触发 on_dialog_start/end（try/finally，钩子异常被吞）。"""

    @staticmethod
    def _console():
        c = Console(config=OpenXConfig())
        c._console = RichConsole(file=io.StringIO(), width=100)
        events: list[str] = []

        def fake_select(options, default_index=0, prompt="Choose:", **_kw):
            events.append("select")  # 记录选择器触发时机，绝不读 stdin
            return options[default_index][1]

        c._interactive_select = fake_select
        return c, events

    def test_ask_user_question_fires_hooks_around_selector(self):
        c, events = self._console()
        c.on_dialog_start = lambda: events.append("start")
        c.on_dialog_end = lambda: events.append("end")
        result = c.ask_user_question("Which?", [{"label": "A"}, {"label": "B"}])
        assert result == "A"
        # start 必须在读取输入之前、end 必须在其后
        assert events == ["start", "select", "end"]

    def test_confirm_plan_fires_hooks_around_selector(self):
        c, events = self._console()
        c.on_dialog_start = lambda: events.append("start")
        c.on_dialog_end = lambda: events.append("end")
        assert c.confirm_plan() is True
        assert events == ["start", "select", "end"]

    async def test_ask_permission_fires_hooks_around_selector(self):
        c, events = self._console()
        c.on_dialog_start = lambda: events.append("start")
        c.on_dialog_end = lambda: events.append("end")
        # 无 _streaming_service → 传统全屏弹窗路径（钩子照旧）
        assert await c.ask_permission("shell", "run tests") == (True, False)
        assert events == ["start", "select", "end"]

    def test_ask_trust_directory_fires_hooks(self):
        c, events = self._console()
        c.on_dialog_start = lambda: events.append("start")
        c.on_dialog_end = lambda: events.append("end")
        assert c.ask_trust_directory("/tmp/ws") is True
        assert events == ["start", "select", "end"]

    def test_end_fires_even_if_selector_raises(self):
        c, events = self._console()

        def boom(options, default_index=0, prompt="Choose:", **_kw):
            events.append("select")
            raise RuntimeError("termios lost")

        c._interactive_select = boom
        c.on_dialog_start = lambda: events.append("start")
        c.on_dialog_end = lambda: events.append("end")
        with pytest.raises(RuntimeError):
            c.confirm_plan()
        assert events == ["start", "select", "end"]  # try/finally 保证成对

    def test_hook_errors_are_swallowed(self):
        c, _ = self._console()
        c.on_dialog_start = lambda: 1 / 0
        c.on_dialog_end = lambda: 1 / 0
        # 钩子自身抛异常绝不能拖垮弹窗本身
        assert c.confirm_plan() is True

    def test_no_hooks_is_zero_behavior_change(self):
        c, events = self._console()
        assert c.on_dialog_start is None and c.on_dialog_end is None
        assert c.confirm_plan() is True
        assert events == ["select"]

    def test_interactive_select_shows_cursor(self):
        """弹窗读输入前兜底发 ?25h（光标可见），幂等无害。"""
        c = Console(config=OpenXConfig())
        c._console = RichConsole(file=io.StringIO(), width=100)
        # 非 TTY 走数字菜单分支——桩掉，绝不读真实 stdin
        c._numbered_select = lambda options, default_index=0, prompt="Choose:": (
            options[default_index][1]
        )
        import openx.ui._components.dialogs as dialogs_mod

        captured = io.StringIO()
        real_stdout = dialogs_mod._sys.stdout
        dialogs_mod._sys.stdout = captured  # 临时换出模块级 stdout
        try:
            c._interactive_select([("Yes", True), ("No", False)])
        finally:
            dialogs_mod._sys.stdout = real_stdout
        assert "\033[?25h" in captured.getvalue()


# ── A3. interactive.py 的钩子接线 ─────────────────────────────────


class TestInteractiveWiring:
    """_stream_response 期间 console 弹窗钩子指向 display.pause/resume，
    流结束后清空，绝不泄漏到后续非流式路径。"""

    @pytest.mark.asyncio
    async def test_stream_response_wires_and_clears_console_hooks(
        self, tmp_path, monkeypatch
    ):
        from openx.cli.interactive import _stream_response

        DeterministicLive.patch(monkeypatch)  # 流式全程无后台线程竞态

        console = Console(config=OpenXConfig(workspace=str(tmp_path)))
        console._console = RichConsole(
            file=io.StringIO(), width=80, height=24, force_terminal=True
        )
        seen: dict = {}

        class FakeExecutor:
            on_prompt_start = None
            on_prompt_end = None

        class FakeAgent:
            config = SimpleNamespace(stream=True)
            total_input_tokens = 0
            total_output_tokens = 0
            tool_executor = FakeExecutor()
            todos: list = []    # 状态层 providers（v0.4.0）
            fleet = None

            async def stream_run(self, content):
                # 在流式进行中最接近真实时机地采样钩子状态
                seen["dialog_start"] = console.on_dialog_start
                seen["dialog_end"] = console.on_dialog_end
                seen["prompt_start"] = self.tool_executor.on_prompt_start
                yield "hello"

        await _stream_response(FakeAgent(), console, "hi")

        # 流式期间：弹窗钩子接到 StreamingService.pause/resume
        assert getattr(seen["dialog_start"], "__func__", None) is StreamingService.pause
        assert getattr(seen["dialog_end"], "__func__", None) is StreamingService.resume
        assert seen["prompt_start"] is not None
        # 流结束：四类钩子全部清空
        assert console.on_dialog_start is None
        assert console.on_dialog_end is None
        assert FakeAgent.tool_executor.on_prompt_start is None
        assert FakeAgent.tool_executor.on_prompt_end is None


# ── B1. options 规范化 ────────────────────────────────────────────


class TestNormalizeOptions:
    """_normalize_options：dict / str / JSON 字符串皆收，无 label 丢弃。"""

    def test_dicts_pass_through(self):
        out = _normalize_options([{"label": "A", "description": "first"}, {"label": "B"}])
        assert out == [{"label": "A", "description": "first"}, {"label": "B"}]

    def test_strings_become_label_dicts(self):
        assert _normalize_options(["Yes", "No"]) == [{"label": "Yes"}, {"label": "No"}]

    def test_json_string_is_parsed(self):
        out = _normalize_options('[{"label": "A"}, {"label": "B"}]')
        assert out == [{"label": "A"}, {"label": "B"}]

    def test_json_string_of_plain_strings(self):
        assert _normalize_options('["A", "B"]') == [{"label": "A"}, {"label": "B"}]

    def test_unparseable_string_is_none(self):
        assert _normalize_options("not json at all") is None

    def test_non_list_is_none(self):
        assert _normalize_options(42) is None
        assert _normalize_options({"label": "A"}) is None
        assert _normalize_options(None) is None

    def test_entries_without_label_are_dropped(self):
        out = _normalize_options([{"description": "no label"}, "B", {"label": "  "}, "C"])
        assert out == [{"label": "B"}, {"label": "C"}]

    def test_mixed_scalar_items_stringified(self):
        assert _normalize_options([1, 2]) == [{"label": "1"}, {"label": "2"}]

    def test_non_string_label_coerced(self):
        assert _normalize_options([{"label": 7}, {"label": "B"}])[0] == {"label": "7"}


class TestCoerceBool:
    def test_real_bools(self):
        assert _coerce_bool(True) is True and _coerce_bool(False) is False

    def test_truthy_strings(self):
        for v in ("true", "True", " TRUE ", "1", "yes"):
            assert _coerce_bool(v) is True

    def test_falsy_strings(self):
        # "false" 在 Python 里是真值——必须按字面解析成 False
        for v in ("false", "0", "no", ""):
            assert _coerce_bool(v) is False


# ── B2. AskUserTool.execute 的畸形入参健壮性 ──────────────────────


class TestAskUserExecuteRobustness:
    """任何畸形入参都落成清晰错误结果——绝不抛异常出 execute()。"""

    @pytest.mark.asyncio
    async def test_options_as_dicts_reach_dialog_as_dicts(self):
        console = RecordingConsole(answer=("Keep both",))
        tool = AskUserTool(console)
        result = await tool.execute(
            question="Merge strategy?",
            options=[
                {"label": "Keep both", "description": "no conflicts"},
                {"label": "Squash"},
            ],
        )
        assert result.success and "Keep both" in result.output
        call = console.calls[0]
        assert call["question"] == "Merge strategy?"
        assert call["options"][0] == {"label": "Keep both", "description": "no conflicts"}
        assert call["multi_select"] is False

    @pytest.mark.asyncio
    async def test_options_as_plain_strings_are_normalized(self):
        console = RecordingConsole(answer=("A",))
        tool = AskUserTool(console)
        result = await tool.execute(question="?", options=["A", "B"])
        assert result.success and "A" in result.output
        assert console.calls[0]["options"] == [{"label": "A"}, {"label": "B"}]

    @pytest.mark.asyncio
    async def test_options_as_json_string_are_parsed(self):
        console = RecordingConsole(answer=("A",))
        tool = AskUserTool(console)
        result = await tool.execute(
            question="?", options='[{"label": "A"}, {"label": "B"}]'
        )
        assert result.success
        assert console.calls[0]["options"] == [{"label": "A"}, {"label": "B"}]

    @pytest.mark.asyncio
    async def test_too_few_options_is_clean_error_without_dialog(self):
        console = RecordingConsole()
        tool = AskUserTool(console)
        for bad in ([{"label": "only"}], [], ["one"], "[{\"label\": \"x\"}]"):
            result = await tool.execute(question="?", options=bad)
            assert not result.success
            assert "at least 2 options" in result.error  # 引导模型补足选项
        assert console.calls == []  # 退化弹窗绝不打开（旧代码会无限阻塞）

    @pytest.mark.asyncio
    async def test_options_clamped_to_four(self):
        console = RecordingConsole(answer=("A",))
        tool = AskUserTool(console)
        result = await tool.execute(
            question="?", options=["A", "B", "C", "D", "E", "F"]
        )
        assert result.success
        assert len(console.calls[0]["options"]) == 4

    @pytest.mark.asyncio
    async def test_missing_question_is_clean_error(self):
        console = RecordingConsole()
        tool = AskUserTool(console)
        result = await tool.execute(options=["A", "B"])  # 无 question
        assert not result.success and "question" in result.error
        assert console.calls == []

    @pytest.mark.asyncio
    async def test_non_string_question_coerced(self):
        console = RecordingConsole(answer=("A",))
        tool = AskUserTool(console)
        result = await tool.execute(question=123, options=["A", "B"])
        assert result.success
        assert console.calls[0]["question"] == "123"

    @pytest.mark.asyncio
    async def test_multi_select_string_coerced(self):
        console = RecordingConsole(answer=("A", "B"))
        tool = AskUserTool(console)
        await tool.execute(question="?", options=["A", "B"], multi_select="true")
        assert console.calls[0]["multi_select"] is True
        await tool.execute(question="?", options=["A", "B"], multi_select="false")
        assert console.calls[1]["multi_select"] is False  # "false" 不得为真

    @pytest.mark.asyncio
    async def test_extra_kwargs_are_absorbed(self):
        console = RecordingConsole(answer=("A",))
        tool = AskUserTool(console)
        # 模型偶发多塞字段——吸收而非 TypeError（TypeError → 错误 → 重试循环）
        result = await tool.execute(
            question="?", options=["A", "B"], timeout=30, style="fancy"
        )
        assert result.success

    @pytest.mark.asyncio
    async def test_console_exception_becomes_error_result(self):
        console = RecordingConsole(raises=RuntimeError("stdin lost"))
        tool = AskUserTool(console)
        result = await tool.execute(question="?", options=["A", "B"])
        assert not result.success
        assert "stdin lost" in result.error
        assert "instead of retrying" in result.error  # 明确劝阻重试循环

    @pytest.mark.asyncio
    async def test_unusable_options_string_is_clean_error(self):
        console = RecordingConsole()
        tool = AskUserTool(console)
        # 旧代码：'[{"l'（字符串前 4 字符）直传 console → AttributeError
        result = await tool.execute(
            question="?", options='[{"label": "A"}, {"label": "B"}'  # 坏 JSON
        )
        assert not result.success
        assert "options" in result.error
        assert console.calls == []
