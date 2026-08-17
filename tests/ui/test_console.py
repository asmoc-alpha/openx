"""Tests for the Console UI components."""

import io
import pytest
from pathlib import Path

from rich.console import Console as RichConsole

from openx.config import OpenXConfig
from openx.llm import StreamDone
from openx.ui.console import Console, _shorten_path, _trunc
from openx.instructions import ProjectInfo


class FakeLLM:
    """可脚本化假 LLM（REPL 级测试用，绝不发真实请求）。

    Minimal scriptable fake LLM for REPL-level tests — never hits the API.
    """

    def __init__(self, responses):
        self.responses = list(responses)  # list of (content, tool_calls)
        self.call_count = 0

    async def stream_chat(self, messages, tools=None):
        content, tool_calls = self.responses[self.call_count]
        self.call_count += 1
        if content:
            yield content
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


@pytest.fixture
def config():
    c = OpenXConfig()
    c.workspace = "/tmp/test-project"
    c.model = "gpt-4o"
    return c


@pytest.fixture
def console(config):
    """Console with output captured to a string buffer."""
    c = Console(config)
    c._console = RichConsole(file=io.StringIO(), force_terminal=True, highlight=False)
    c._terminal_width = 100
    return c


def _capture(console: Console) -> str:
    """Get the captured output from a console's string buffer."""
    return console._console.file.getvalue()  # type: ignore[union-attr]


class TestHeader:
    """Header bar tests."""

    def test_header_shows_workspace(self, console):
        console.print_header(instructions_loaded=False)
        output = _capture(console)
        assert "tmp/test-project" in output

    def test_header_shows_model(self, console):
        console.print_header(instructions_loaded=False)
        output = _capture(console)
        assert "gpt-4o" in output

    def test_header_shows_openx_md_loaded(self, console):
        console.print_header(instructions_loaded=True)
        output = _capture(console)
        assert "OPENX" in output
        assert "✓" in output

    def test_header_shows_openx_md_not_loaded(self, console):
        console.print_header(instructions_loaded=False)
        output = _capture(console)
        # v0.5.0：未加载时不显示 OPENX.md 标记，只显示品牌/模型/路径
        assert "openx" in output
        assert "gpt-4o" in output
        assert "OPENX.md" not in output


class TestProjectOverview:
    """Project overview panel tests."""

    def test_overview_shows_type(self, console):
        info = ProjectInfo(project_type="Python", project_type_file="pyproject.toml")
        console.print_project_overview(info)
        output = _capture(console)
        assert "Python" in output
        assert "pyproject.toml" in output

    def test_overview_shows_unknown_type(self, console):
        info = ProjectInfo(project_type="Unknown")
        console.print_project_overview(info)
        output = _capture(console)
        assert "Unknown" in output

    def test_overview_shows_file_counts(self, console):
        info = ProjectInfo(
            project_type="Python",
            file_counts={".py": 15, ".md": 3, ".toml": 2},
            total_files=20,
        )
        console.print_project_overview(info)
        output = _capture(console)
        assert "20 total" in output
        assert "15" in output
        assert ".py" in output

    def test_overview_shows_empty_project(self, console):
        info = ProjectInfo()
        console.print_project_overview(info)
        output = _capture(console)
        assert "(empty)" in output

    def test_overview_shows_git_clean(self, console):
        info = ProjectInfo(
            git_branch="main",
            git_status_summary="clean",
        )
        console.print_project_overview(info)
        output = _capture(console)
        assert "main" in output
        assert "clean" in output

    def test_overview_shows_git_modified(self, console):
        info = ProjectInfo(
            git_branch="main",
            git_status_summary="2 modified",
        )
        console.print_project_overview(info)
        output = _capture(console)
        assert "2 modified" in output

    def test_overview_shows_config_files(self, console):
        info = ProjectInfo(
            config_files=["pyproject.toml", ".gitignore", "README.md"],
        )
        console.print_project_overview(info)
        output = _capture(console)
        assert "pyproject.toml" in output
        assert ".gitignore" in output

    def test_overview_shows_openx_md_loaded(self, console):
        info = ProjectInfo(openx_md_loaded=True, openx_md_sections=5)
        console.print_project_overview(info)
        output = _capture(console)
        assert "loaded" in output
        assert "5 sections" in output

    def test_overview_shows_openx_md_not_found(self, console):
        info = ProjectInfo(openx_md_loaded=False)
        console.print_project_overview(info)
        output = _capture(console)
        assert "/init" in output


class TestPathShortening:
    """Tests for _shorten_path helper."""

    def test_short_path_passes_through(self):
        assert _shorten_path(Path("/tmp/proj"), max_len=40) == "/tmp/proj"

    def test_long_path_with_tilde(self):
        home = Path.home()
        # Create a path that's long enough to exceed max_len even after tilde
        long_path = home / ("x" * 50)
        result = _shorten_path(long_path, max_len=40)
        assert len(result) <= 40

    def test_long_path_trimmed(self):
        result = _shorten_path(Path("/" + "x" * 60), max_len=30)
        assert len(result) <= 30
        assert result.startswith("...")


class TestTrunc:
    """Tests for _trunc helper."""

    def test_short_text_passes_through(self):
        assert _trunc("hello", 10) == "hello"

    def test_long_text_truncated(self):
        assert _trunc("hello world", 8) == "hello..."
        assert len(_trunc("hello world", 8)) <= 8


class TestHelp:
    """Help display tests."""

    def test_help_contains_commands(self, console):
        console.print_help()
        output = _capture(console)
        assert "/help" in output
        assert "/quit" in output
        assert "/init" in output
        assert "/instructions" in output
        assert "/explore" in output
        assert "/config" in output


class TestStartup:
    """Startup display tests."""

    def test_show_startup(self, console):
        info = ProjectInfo(
            project_type="Python",
            project_type_file="pyproject.toml",
            file_counts={".py": 5},
            total_files=5,
        )
        console.show_startup(info, instructions_loaded=True)
        output = _capture(console)
        # v0.5.0：像素吉祥物 + 品牌/模型/路径并排面板
        assert "gpt-4o" in output
        assert "openx" in output
        # 吉祥物在场（线条形圆角机器人）
        assert "╭──┴──╮" in output and "╰──┬──╯" in output
        # Status line still shows after the panel
        assert "/help" in output

    def test_show_startup_single_shot(self, console):
        console.show_startup_single_shot("fix the bug")
        output = _capture(console)
        assert "fix the bug" in output
        assert "gpt-4o" in output


class TestEOFHandling:
    """EOF 必须干净退出 REPL，绝不空转死循环（`openx </dev/null>` 回归）。

    EOF must break the REPL cleanly — never busy-loop.  Non-TTY EOF yields
    ``None`` from the input reader; an empty Enter still yields ``""`` so the
    REPL re-prompts exactly as before.
    """

    def test_read_line_returns_none_on_eof(self, console, monkeypatch):
        # 非 TTY 且 stdin 已耗尽：readline() == "" → None（区别于空行的 ""）
        # Exhausted non-TTY stdin: readline() == "" must map to None, not "".
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        assert console._read_line_interactive() is None

    def test_read_line_returns_empty_on_blank_line(self, console, monkeypatch):
        # 空回车仍是 ""，REPL 依此重新提示（既有行为不变）
        # An empty Enter still yields "" so the REPL re-prompts as before.
        monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
        assert console._read_line_interactive() == ""

    @pytest.mark.asyncio
    async def test_run_interactive_breaks_on_eof(self, tmp_path, monkeypatch):
        # print_user_prompt 返回 None（EOF）：REPL 恰好提示一次就退出，
        # 而不是反复拿到空输入空转。Prompt exactly once on EOF — no loop.
        from openx.agent import OpenXAgent
        from openx.app.cli.interactive import run_interactive
        from openx.mcp.manager import MCPManager

        config = OpenXConfig()
        config.workspace = str(tmp_path)
        config.api_key = "sk-test"
        config.api_base = "https://example.com/v1"
        config.model = "test-model"
        agent = OpenXAgent(config)
        agent.llm = FakeLLM([("never called", None)])
        agent.mcp = MCPManager({})  # 隔离真实 settings.json 的 mcpServers / hermetic

        console = Console(config)
        console._console = RichConsole(
            file=io.StringIO(), force_terminal=True, highlight=False
        )
        console._terminal_width = 100

        calls = {"n": 0}

        def fake_prompt(*args, **kwargs):
            calls["n"] += 1
            return None  # EOF：非 TTY stdin 已耗尽 / non-TTY stdin exhausted

        monkeypatch.setattr(console, "print_user_prompt", fake_prompt)

        await run_interactive(agent, console)

        assert calls["n"] == 1  # 只提示一次就退出 / prompted once, then exited
        assert agent.llm.call_count == 0  # 未发任何请求 / no LLM call made


class TestSentMessageBanner:
    """用户已发送消息横幅：OpenClaw TUI 深色石板块定稿（2026-08-12）。

    史：浅色块 7 候选被否决回退无背景 → 用户指定参考 OpenClaw 加深色
    块。配色 = OpenClaw src/tui/theme/theme.ts 深色主题 userBg/userText。
    """

    def _truecolor(self, console) -> Console:
        console._console = RichConsole(
            file=io.StringIO(), force_terminal=True, highlight=False,
            color_system="truecolor",
        )
        return console

    def test_banner_uses_openclaw_slate_background(self, console):
        console = self._truecolor(console)
        console.print_sent_message("hello world")
        out = _capture(console)
        assert "hello world" in out
        # OpenClaw userBg #2B2F36 → 48;2;43;47;54（行级 style，连
        # padding 铺满整行——rich 14 Table.style 只喂 border 是死路）
        assert "48;2;43;47;54" in out, "横幅背景不是 OpenClaw 石板灰"
        # OpenClaw userText #F3EEE0 → 38;2;243;238;224
        assert "38;2;243;238;224" in out, "横幅正文不是暖白"
        # 左竖条 ▎ 保留（无背景版遗留，用户指定）：bold cyan = 1;36——
        # 与行背景合并成单条 SGR（1;36;48;2;…），只钉前缀
        assert "1;36;" in out and "▎" in out, "竖条缺失或不是 bold cyan"

    def test_banner_survives_markup_like_text(self, console):
        # 用户输入可含方括号——字面展示，不得抛 MarkupError / 误解析样式
        console = self._truecolor(console)
        console.print_sent_message("[x] done [bold]")
        assert "[x] done [bold]" in _capture(console)

    def test_banner_multiline(self, console):
        # 多行输入（Shift+Enter / 多行粘贴）逐行展示
        console = self._truecolor(console)
        console.print_sent_message("line one\nline two")
        out = _capture(console)
        assert "line one" in out and "line two" in out
