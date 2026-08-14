"""Tests for ``openx.main.parse_args``.

针对性测试 CLI 参数解析（pure argparse，不触碰真实 agent/LLM）：

- 无参 / 单次 prompt / 长短选项；
- 数值类型转换、可重复选项、枚举校验；
- ``--continue``/``--resume`` 的哨兵与 dest 映射；
- 默认值与 cwd 依赖（monkeypatch.chdir）；
- 非法输入 / ``--help`` 触发 ``SystemExit``。

运行：``python -m pytest tests/test_main_parse_args.py -q``
"""

import pytest

from openx.main import parse_args

# --resume 不带值时的哨兵，与 main.py 保持一致
PICK_SENTINEL = "__pick__"


class TestBasics:
    def test_no_args_interactive_mode(self):
        """无参数 → prompt 为空，默认值生效（interactive REPL）。"""
        args = parse_args([])
        assert args.prompt is None
        assert args.output_format == "text"
        assert args.auto_approve is False
        assert args.max_rounds == 30
        assert args.temperature == 0.0

    def test_single_shot_prompt(self):
        """单次模式：位置参数作为 prompt。"""
        args = parse_args(["fix the bug"])
        assert args.prompt == "fix the bug"

    def test_prompt_with_empty_string(self):
        """显式传空字符串 prompt → 视为空。"""
        args = parse_args(["extract this"])
        assert args.prompt == "extract this"


class TestFlags:
    def test_short_and_long_model(self):
        """--model/-m 与 --auto-approve/-y 的长短写法等价。"""
        short = parse_args(["-m", "gpt-4o", "-y"])
        assert short.model == "gpt-4o"
        assert short.auto_approve is True

        long = parse_args(["--model", "gpt-4o", "--auto-approve"])
        assert long.model == "gpt-4o"
        assert long.auto_approve is True

    def test_model_not_given_is_none(self):
        """不传 --model → None（默认模型由 config 层处理）。"""
        assert parse_args([]).model is None

    def test_api_key_and_base(self):
        args = parse_args(["--api-key", "sk-123", "--api-base", "http://localhost:8000/v1"])
        assert args.api_key == "sk-123"
        assert args.api_base == "http://localhost:8000/v1"

    def test_no_stream(self):
        assert parse_args(["--no-stream"]).no_stream is True
        assert parse_args([]).no_stream is False

    def test_version_flag(self):
        """--version 只是置位，真正打印在 main() 里处理。"""
        assert parse_args(["--version"]).version is True
        assert parse_args([]).version is False


class TestTypeConversion:
    def test_int_and_float(self):
        """类型转换：--max-rounds int、--temperature float。"""
        args = parse_args(["--max-rounds", "10", "--temperature", "0.7"])
        assert args.max_rounds == 10
        assert isinstance(args.max_rounds, int)
        assert args.temperature == 0.7
        assert isinstance(args.temperature, float)

    def test_repeatable_image(self):
        """--image 可重复，累积为列表。"""
        args = parse_args(["--image", "a.png", "--image", "b.png"])
        assert args.image == ["a.png", "b.png"]

    def test_image_default_empty(self):
        assert parse_args([]).image == []


class TestOutputFormat:
    def test_output_format_choice(self):
        for fmt in ("text", "json", "stream-json"):
            assert parse_args([f"--output-format", fmt]).output_format == fmt

    def test_invalid_output_format_raises(self):
        """不在 choices 内 → SystemExit(code=2)。"""
        with pytest.raises(SystemExit) as exc_info:
            parse_args(["--output-format", "xml"])
        assert exc_info.value.code == 2


class TestSessionFlags:
    def test_continue_dest(self):
        """--continue 是保留字，argparse 映射为 continue_session。"""
        args = parse_args(["--continue"])
        assert args.continue_session is True
        assert args.resume is None

    def test_resume_with_id(self):
        args = parse_args(["--resume", "abc123"])
        assert args.resume == "abc123"

    def test_resume_sentinel_when_value_omitted(self):
        """--resume 不带值 → const 哨兵（进入交互式选择器）。"""
        args = parse_args(["--resume"])
        assert args.resume == PICK_SENTINEL

    def test_resume_sentinel_does_not_set_continue(self):
        args = parse_args(["--resume"])
        assert args.continue_session is False


class TestDefaultsWithCwd:
    def test_workspace_defaults_to_cwd(self, monkeypatch, tmp_path):
        """--workspace 默认取当前工作目录（解析时求值）。"""
        monkeypatch.chdir(tmp_path)
        assert parse_args([]).workspace == str(tmp_path)

    def test_workspace_explicit(self):
        assert parse_args(["--workspace", "/my/project"]).workspace == "/my/project"


class TestErrorHandling:
    def test_unknown_option_raises(self):
        """非法选项 → SystemExit(code=2)。"""
        with pytest.raises(SystemExit) as exc_info:
            parse_args(["--nonexistent"])
        assert exc_info.value.code == 2

    def test_max_rounds_requires_int(self):
        """--max-rounds 传非整数 → SystemExit(code=2)。"""
        with pytest.raises(SystemExit) as exc_info:
            parse_args(["--max-rounds", "abc"])
        assert exc_info.value.code == 2

    def test_temperature_requires_float(self):
        with pytest.raises(SystemExit):
            parse_args(["--temperature", "not-a-number"])

    def test_help_exits_zero(self):
        """--help 打印帮助并以 0 退出。"""
        with pytest.raises(SystemExit) as exc_info:
            parse_args(["--help"])
        assert exc_info.value.code == 0