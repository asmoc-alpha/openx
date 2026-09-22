"""Tests for the code-search backend (openx/tools/fs_search.py) and the
rewritten grep / glob tools.

Covers: ripgrep detection/override, backend resolution, the pure-Python engine
(pruning, git-aware ignore, thread pool), rg JSON parsing, a fake-`rg` shim that
exercises the real subprocess + early-stop + fallback path, and the tool-level
output format (context lines, include, regex, case sensitivity, caps).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from openx.tools import fs_search
from openx.tools.file_tools import GlobTool
from openx.tools.search_tools import GrepTool

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="shim needs a POSIX executable")


@pytest.fixture(autouse=True)
def _clear_rg_cache(monkeypatch):
    """每个用例重置 ripgrep 探测缓存（否则第一个用例的探测结果会粘住）。"""
    monkeypatch.setattr(fs_search, "_rg_cache", None)
    monkeypatch.delenv("OPENX_RIPGREP", raising=False)
    yield
    monkeypatch.setattr(fs_search, "_rg_cache", None)


def _has_git() -> bool:
    return shutil.which("git") is not None


def _git(ws: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=ws, check=True, capture_output=True)


# ── 后端探测 / 解析 ──────────────────────────────────────────────


class TestBackendResolution:
    def test_force_python(self):
        assert fs_search.resolve_backend("python") == "python"

    def test_auto_without_rg(self, monkeypatch):
        monkeypatch.setattr(fs_search, "find_ripgrep", lambda: None)
        assert fs_search.resolve_backend("auto") == "python"
        assert fs_search.resolve_backend("ripgrep") == "python"

    def test_auto_with_rg(self, monkeypatch):
        monkeypatch.setattr(fs_search, "find_ripgrep", lambda: "/usr/bin/rg")
        assert fs_search.resolve_backend("auto") == "ripgrep"
        assert fs_search.resolve_backend("ripgrep") == "ripgrep"

    def test_env_override_none_disables(self, monkeypatch):
        monkeypatch.setenv("OPENX_RIPGREP", "none")
        assert fs_search.find_ripgrep() is None

    def test_env_override_path(self, monkeypatch):
        monkeypatch.setenv("OPENX_RIPGREP", "/custom/rg")
        assert fs_search.find_ripgrep() == "/custom/rg"


class TestRgJsonParsing:
    def test_parses_match_events(self, tmp_path):
        stream = [
            json.dumps({"type": "begin", "data": {"path": {"text": "a.py"}}}),
            json.dumps({"type": "match", "data": {
                "path": {"text": "a.py"}, "line_number": 3,
                "lines": {"text": "hello world\n"}}}),
            "not json at all",
            json.dumps({"type": "end", "data": {}}),
        ]
        matches, truncated = fs_search.parse_rg_json_stream(stream, tmp_path, 10)
        assert len(matches) == 1 and not truncated
        assert matches[0].line == "hello world"
        assert matches[0].lineno == 3
        assert matches[0].path == tmp_path / "a.py"

    def test_truncates_at_max(self, tmp_path):
        stream = [
            json.dumps({"type": "match", "data": {
                "path": {"text": f"f{i}.py"}, "line_number": 1,
                "lines": {"text": "x\n"}}})
            for i in range(50)
        ]
        matches, truncated = fs_search.parse_rg_json_stream(stream, tmp_path, 10)
        assert len(matches) == 10 and truncated

    def test_non_utf8_path_skipped(self, tmp_path):
        evt = {"type": "match", "data": {
            "path": {"bytes": "AAAA"}, "line_number": 1, "lines": {"text": "x\n"}}}
        assert fs_search.match_from_event(evt, tmp_path) is None

    def test_missing_line_number_skipped(self, tmp_path):
        evt = {"type": "match", "data": {
            "path": {"text": "a.py"}, "lines": {"text": "x\n"}}}
        assert fs_search.match_from_event(evt, tmp_path) is None


# ── 纯 Python 引擎 ───────────────────────────────────────────────


class TestPythonEngine:
    async def test_finds_and_prunes(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "a.py").write_text("needle here\nplain\n")
        (tmp_path / "b.py").write_text("nothing\nneedle again\n")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "c.py").write_text("needle hidden\n")

        hits = await fs_search.grep_files(
            tmp_path, "needle", backend="python", respect_gitignore=False
        )
        rels = [f"{m.path.relative_to(tmp_path)}:{m.lineno}" for m in hits]
        assert rels == ["b.py:2", "sub/a.py:1"]

    async def test_include_filter(self, tmp_path):
        (tmp_path / "a.py").write_text("needle\n")
        (tmp_path / "b.txt").write_text("needle\n")
        hits = await fs_search.grep_files(
            tmp_path, "needle", include="*.py",
            backend="python", respect_gitignore=False,
        )
        assert [m.path.name for m in hits] == ["a.py"]

    async def test_max_matches_cap(self, tmp_path):
        (tmp_path / "many.py").write_text("needle\n" * 100)
        hits = await fs_search.grep_files(
            tmp_path, "needle", backend="python",
            respect_gitignore=False, max_matches=10,
        )
        assert len(hits) == 10

    async def test_single_file_search(self, tmp_path):
        f = tmp_path / "one.py"
        f.write_text("alpha\nneedle\n")
        hits = await fs_search.grep_files(
            f, "needle", backend="python", respect_gitignore=False
        )
        assert len(hits) == 1 and hits[0].lineno == 2

    async def test_binary_content_skipped(self, tmp_path):
        # 无后缀，故按后缀过滤拦不住——靠内容嗅探（含 NUL）跳过。
        (tmp_path / "blob").write_bytes(b"needle\x00more")
        (tmp_path / "ok.py").write_text("needle\n")
        hits = await fs_search.grep_files(
            tmp_path, "needle", backend="python", respect_gitignore=False
        )
        assert [m.path.name for m in hits] == ["ok.py"]

    async def test_oversized_file_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fs_search, "_MAX_FILE_BYTES", 10)
        (tmp_path / "big.py").write_text("needle\n" * 100)
        (tmp_path / "small.py").write_text("needle\n")
        hits = await fs_search.grep_files(
            tmp_path, "needle", backend="python", respect_gitignore=False
        )
        assert [m.path.name for m in hits] == ["small.py"]

    async def test_crlf_streaming_line_numbers(self, tmp_path):
        (tmp_path / "a.py").write_bytes(b"x\r\ny\r\nneedle\r\n")
        hits = await fs_search.grep_files(
            tmp_path, "needle", backend="python", respect_gitignore=False
        )
        assert len(hits) == 1
        assert hits[0].lineno == 3 and hits[0].line == "needle"


# ── git 感知 ignore ──────────────────────────────────────────────


@pytest.mark.skipif(not _has_git(), reason="git not installed")
class TestGitignore:
    def _repo(self, tmp_path: Path) -> Path:
        _git(tmp_path, "init", "-q")
        (tmp_path / ".gitignore").write_text("ignored/\n")
        (tmp_path / "ignored").mkdir()
        (tmp_path / "ignored" / "x.py").write_text("needle\n")
        (tmp_path / "keep.py").write_text("needle\n")
        return tmp_path

    async def test_grep_respects_gitignore(self, tmp_path):
        ws = self._repo(tmp_path)
        out = (await GrepTool(str(ws)).execute("needle")).output
        assert "keep.py" in out
        assert "ignored" not in out

    async def test_grep_can_disable_gitignore(self, tmp_path):
        ws = self._repo(tmp_path)
        out = (await GrepTool(str(ws), respect_gitignore=False).execute("needle")).output
        assert "keep.py" in out
        assert "ignored/x.py" in out

    async def test_glob_respects_gitignore(self, tmp_path):
        ws = self._repo(tmp_path)
        out = (await GlobTool(str(ws)).execute("**/*.py")).output
        assert "keep.py" in out
        assert "ignored" not in out

    async def test_glob_can_disable_gitignore(self, tmp_path):
        ws = self._repo(tmp_path)
        out = (await GlobTool(str(ws), respect_gitignore=False).execute("**/*.py")).output
        assert "ignored/x.py" in out


# ── 假 rg 子进程：真实验证 rg 路径 ───────────────────────────────

_FAKE_RG = '''#!/usr/bin/env python3
import sys, os, json
argv = sys.argv[1:]
spec = os.environ.get("FAKE_RG_ARGV")
if spec:
    open(spec, "w").write("\\n".join(argv))
n = int(os.environ.get("FAKE_RG_MATCHES", "2"))
if os.environ.get("FAKE_RG_STDERR"):
    sys.stderr.write(os.environ["FAKE_RG_STDERR"])
    sys.exit(2)
for i in range(n):
    sys.stdout.write(json.dumps({"type": "match", "data": {
        "path": {"text": "fake%d.py" % i}, "line_number": i + 1,
        "lines": {"text": "line %d\\n" % i}}}) + "\\n")
sys.stdout.flush()
'''


# 支持 `--files` 的假 rg：`glob` 路径用 `rg --files` 枚举（逐行相对路径）。
_FAKE_RG_FILES = '''#!/usr/bin/env python3
import sys, os
argv = sys.argv[1:]
spec = os.environ.get("FAKE_RG_ARGV")
if spec:
    open(spec, "w").write("\\n".join(argv))
if "--files" not in argv:
    sys.exit(0)
if os.environ.get("FAKE_RG_ERR"):
    sys.stderr.write("error: boom")
    sys.exit(2)
for p in os.environ.get("FAKE_RG_FILES", "").splitlines():
    if p:
        sys.stdout.write(p + "\\n")
sys.stdout.flush()
'''


@POSIX_ONLY
class TestRipgrepSubprocess:
    def _install_shim(self, tmp_path: Path, monkeypatch) -> Path:
        shim = tmp_path / "rg"
        shim.write_text(_FAKE_RG)
        shim.chmod(0o755)
        monkeypatch.setenv("OPENX_RIPGREP", str(shim))
        monkeypatch.setattr(fs_search, "_rg_cache", None)
        return shim

    async def test_streams_matches(self, tmp_path, monkeypatch):
        self._install_shim(tmp_path, monkeypatch)
        ws = tmp_path / "ws"
        ws.mkdir()
        out = (await GrepTool(str(ws), backend="ripgrep").execute("needle")).output
        assert "fake0.py:1: line 0" in out
        assert "fake1.py:2: line 1" in out

    async def test_flags_passed(self, tmp_path, monkeypatch):
        self._install_shim(tmp_path, monkeypatch)
        argv_file = tmp_path / "argv.txt"
        monkeypatch.setenv("FAKE_RG_ARGV", str(argv_file))
        ws = tmp_path / "ws"
        ws.mkdir()
        await GrepTool(str(ws), backend="ripgrep").execute("needle", include="*.py")
        argv = argv_file.read_text()
        for flag in ("--json", "--fixed-strings", "--case-sensitive", "-e", "*.py"):
            assert flag in argv
        # 剪枝目录经 -g 排除
        assert "!**/node_modules/**" in argv

    async def test_regex_and_case_flags(self, tmp_path, monkeypatch):
        self._install_shim(tmp_path, monkeypatch)
        argv_file = tmp_path / "argv.txt"
        monkeypatch.setenv("FAKE_RG_ARGV", str(argv_file))
        ws = tmp_path / "ws"
        ws.mkdir()
        await GrepTool(str(ws), backend="ripgrep").execute(
            r"def \w+", is_regex=True, case_sensitive=False
        )
        argv = argv_file.read_text()
        assert "--ignore-case" in argv
        assert "--fixed-strings" not in argv

    async def test_early_stop_cap(self, tmp_path, monkeypatch):
        self._install_shim(tmp_path, monkeypatch)
        monkeypatch.setenv("FAKE_RG_MATCHES", "5000")
        ws = tmp_path / "ws"
        ws.mkdir()
        out = (await GrepTool(str(ws), backend="ripgrep").execute("needle")).output
        assert "500 match(es) total" in out

    async def test_rg_error_falls_back(self, tmp_path, monkeypatch):
        self._install_shim(tmp_path, monkeypatch)
        monkeypatch.setenv("FAKE_RG_STDERR", "error: unrecognized flag")
        monkeypatch.setenv("FAKE_RG_MATCHES", "0")
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "real.py").write_text("needle\n")
        out = (await GrepTool(str(ws), backend="ripgrep").execute("needle")).output
        assert "real.py" in out  # 纯 Python 兜底找到了真文件


# ── 工具输出格式（两引擎一致）───────────────────────────────────


class TestToolOutputFormat:
    async def test_backends_agree(self, tmp_path):
        (tmp_path / "a.py").write_text("def foo():\n    return foo()\n")
        (tmp_path / "b.py").write_text("bar\n")
        py = (await GrepTool(str(tmp_path), backend="python").execute("foo")).output
        auto = (await GrepTool(str(tmp_path), backend="auto").execute("foo")).output
        assert py == auto

    async def test_no_match_message(self, tmp_path):
        (tmp_path / "a.py").write_text("hello\n")
        r = await GrepTool(str(tmp_path)).execute("zzz_nope")
        assert r.success and "No matches found for: zzz_nope" in r.output

    async def test_regex(self, tmp_path):
        (tmp_path / "a.py").write_text("def foo_bar():\n")
        r = await GrepTool(str(tmp_path)).execute(r"def \w+", is_regex=True)
        assert "foo_bar" in r.output

    async def test_case_insensitive(self, tmp_path):
        (tmp_path / "a.py").write_text("HELLO\n")
        assert "HELLO" not in (await GrepTool(str(tmp_path)).execute("hello")).output
        assert "HELLO" in (
            await GrepTool(str(tmp_path)).execute("hello", case_sensitive=False)
        ).output

    async def test_invalid_regex_errors(self, tmp_path):
        r = await GrepTool(str(tmp_path)).execute("(", is_regex=True)
        assert not r.success and "Invalid regex" in r.error

    async def test_context_lines_markers(self, tmp_path):
        (tmp_path / "a.py").write_text("l1\nl2\nneedle\nl4\nl5\n")
        out = (await GrepTool(str(tmp_path)).execute("needle", context_lines=1)).output
        assert "a.py:3:> needle" in out
        assert "a.py:2:  l2" in out
        assert "a.py:4:  l4" in out
        assert "---" in out

    async def test_missing_path(self, tmp_path):
        r = await GrepTool(str(tmp_path)).execute("x", path="does/not/exist")
        assert not r.success and "Path not found" in r.error


class TestGlobTool:
    async def test_basic_and_prune(self, tmp_path):
        (tmp_path / "a.py").write_text("")
        (tmp_path / "c.txt").write_text("")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "d.py").write_text("")
        out = (await GlobTool(str(tmp_path)).execute("**/*.py")).output
        assert "a.py" in out
        assert "node_modules" not in out
        assert "c.txt" not in out

    async def test_no_match_message(self, tmp_path):
        out = (await GlobTool(str(tmp_path)).execute("*.zzz")).output
        assert "No files matched pattern: *.zzz" in out


class TestGlobMatch:
    """``glob_match`` 单元：pathlib 段语义（而非 fnmatch）。"""

    def test_top_level_star(self):
        assert fs_search.glob_match("b.py", "*.py")
        assert not fs_search.glob_match("sub/a.py", "*.py")

    def test_recursive_double_star(self):
        assert fs_search.glob_match("b.py", "**/*.py")
        assert fs_search.glob_match("a/b/c.py", "**/*.py")

    def test_anchored_segment(self):
        assert fs_search.glob_match("sub/a.py", "sub/*.py")
        assert not fs_search.glob_match("x/sub/a.py", "sub/*.py")

    def test_collapses_double_star(self):
        assert fs_search.glob_match("x/sub/a.py", "**/**/*.py")

    def test_bare_double_star_matches_all(self):
        assert fs_search.glob_match("a/b/c", "**")
        assert not fs_search.glob_match("a/b/c", "*")


@POSIX_ONLY
class TestRipgrepGlob:
    """glob 走 rg（``rg --files`` 枚举）+ pathlib 匹配，与纯 Python 引擎一致。"""

    def _install(self, tmp_path: Path, monkeypatch, files, err: bool = False) -> Path:
        shim = tmp_path / "rg"
        shim.write_text(_FAKE_RG_FILES)
        shim.chmod(0o755)
        monkeypatch.setenv("OPENX_RIPGREP", str(shim))
        monkeypatch.setenv("FAKE_RG_FILES", "\n".join(files))
        if err:
            monkeypatch.setenv("FAKE_RG_ERR", "1")
        monkeypatch.setattr(fs_search, "_rg_cache", None)
        return shim

    async def test_glob_via_rg(self, tmp_path, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        self._install(
            tmp_path, monkeypatch,
            ["a.py", "sub/b.py", "c.txt", "node_modules/d.py", "logo.png"],
        )
        out = (await GlobTool(str(ws), backend="ripgrep").execute("**/*.py")).output
        assert "a.py" in out and "sub/b.py" in out
        assert "c.txt" not in out
        assert "node_modules" not in out
        assert "logo.png" not in out  # 二进制后缀被过滤

    async def test_glob_passes_files_flag(self, tmp_path, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        argv_file = tmp_path / "argv.txt"
        self._install(tmp_path, monkeypatch, ["a.py"])
        monkeypatch.setenv("FAKE_RG_ARGV", str(argv_file))
        await GlobTool(str(ws), backend="ripgrep").execute("**/*.py")
        argv = argv_file.read_text()
        assert "--files" in argv
        assert "!**/node_modules/**" in argv

    async def test_glob_parity_with_python(self, tmp_path, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "sub").mkdir()
        (ws / "a.py").write_text("")
        (ws / "sub" / "b.py").write_text("")
        (ws / "c.txt").write_text("")
        # 假 rg 枚举与真实磁盘一致的文件集。
        self._install(tmp_path, monkeypatch, ["a.py", "sub/b.py", "c.txt"])
        for pattern in ("**/*.py", "*.py", "sub/*.py"):
            rg = (await GlobTool(str(ws), backend="ripgrep").execute(pattern)).output
            py = (await GlobTool(str(ws), backend="python").execute(pattern)).output
            assert rg == py, (pattern, rg, py)

    async def test_glob_rg_error_falls_back(self, tmp_path, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "real.py").write_text("")
        self._install(tmp_path, monkeypatch, ["real.py"], err=True)
        out = (await GlobTool(str(ws), backend="ripgrep").execute("*.py")).output
        assert "real.py" in out  # 纯 Python 兜底枚举到真文件


# ── config → ToolHost → 内置工具装配 ────────────────────────────


class TestWiring:
    def test_config_defaults(self):
        from openx.config import OpenXConfig
        cfg = OpenXConfig()
        assert cfg.search_backend == "auto"
        assert cfg.respect_gitignore is True

    def test_env_override_backend(self, tmp_path, monkeypatch):
        from openx.config import OpenXConfig
        monkeypatch.setenv("OPENX_SEARCH_BACKEND", "python")
        assert OpenXConfig.load(workspace=str(tmp_path)).search_backend == "python"

    def test_host_fields(self):
        from openx.kernel.sandbox.host import ToolHost
        h = ToolHost(workspace="/tmp")
        assert h.search_backend == "auto" and h.respect_gitignore is True

    async def test_builtin_bundle_wires_knobs(self, tmp_path):
        from openx.builtin.tools import build_capability_tools
        from openx.kernel.sandbox.host import ToolHost

        host = ToolHost(
            workspace=str(tmp_path), search_backend="python", respect_gitignore=False
        )
        tools = {t.name: t for t in build_capability_tools(host)}
        assert tools["grep"].backend == "python"
        assert tools["grep"].respect_gitignore is False
        assert tools["glob"].backend == "python"
        assert tools["glob"].respect_gitignore is False


if __name__ == "__main__":  # 独立调试
    sys.exit(pytest.main([__file__, "-q"]))
