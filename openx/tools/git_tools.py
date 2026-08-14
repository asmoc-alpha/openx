"""Git tools for OpenX — status, diff, log, branch."""

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
from pathlib import Path

from .base import Tool, ToolResult, truncate_output
from ..permissions import Permission


class GitStatusTool(Tool):
    """Show git working tree status."""

    name = "git_status"
    description = (
        "Show the working tree status — modified, staged, and untracked files. "
        "Equivalent to 'git status --short'."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def __init__(self, workspace: str):
        self.workspace = Path(workspace).resolve()

    @property
    def permission(self) -> Permission:
        return Permission.allow()

    async def execute(self) -> ToolResult:
        result = await self._run_git("status", "--short", "--branch")
        if result.error:
            return result
        if not result.output.strip():
            return ToolResult(output="Working tree clean. Nothing to commit.")
        return result

    async def _run_git(self, *args: str) -> ToolResult:
        try:
            process = await asyncio.create_subprocess_exec(
                "git", *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace),
            )
            stdout, stderr = await process.communicate()
            output = stdout.decode("utf-8", errors="replace")
            error = stderr.decode("utf-8", errors="replace")
            return ToolResult(output=output, error=error)
        except FileNotFoundError:
            return ToolResult(error="Git not found. Is git installed?")
        except Exception as e:
            return ToolResult(error=f"Git command failed: {e}")


class GitDiffTool(Tool):
    """Show git diff."""

    name = "git_diff"
    description = (
        "Show changes in the working tree. "
        "Use staged=True for staged changes, or provide a specific file path."
    )
    parameters = {
        "type": "object",
        "properties": {
            "staged": {
                "type": "boolean",
                "description": "Show staged changes (equivalent to --cached). Default: false.",
            },
            "file_path": {
                "type": "string",
                "description": "Show diff for a specific file only.",
            },
        },
        "required": [],
    }

    def __init__(self, workspace: str):
        self.workspace = Path(workspace).resolve()

    @property
    def permission(self) -> Permission:
        return Permission.allow()

    async def execute(
        self,
        staged: bool = False,
        file_path: str = "",
    ) -> ToolResult:
        args = ["diff"]
        if staged:
            args.append("--cached")
        if file_path:
            args.extend(["--", file_path])

        try:
            process = await asyncio.create_subprocess_exec(
                "git", *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace),
            )
            stdout, stderr = await process.communicate()
            output = stdout.decode("utf-8", errors="replace")
            error = stderr.decode("utf-8", errors="replace")

            if not output.strip():
                return ToolResult(output="No changes to show.")

            truncated, was_truncated, notice = truncate_output(output)
            return ToolResult(
                output=truncated,
                error=error,
                truncated=was_truncated,
                truncated_notice=notice,
            )
        except FileNotFoundError:
            return ToolResult(error="Git not found.")
        except Exception as e:
            return ToolResult(error=f"Git diff failed: {e}")


class GitLogTool(Tool):
    """Show git commit log."""

    name = "git_log"
    description = (
        "Show recent git commit history. "
        "Useful for understanding recent changes and project context."
    )
    parameters = {
        "type": "object",
        "properties": {
            "count": {
                "type": "integer",
                "description": "Number of commits to show. Default: 10.",
            },
            "oneline": {
                "type": "boolean",
                "description": "Show one-line format. Default: true.",
            },
        },
        "required": [],
    }

    def __init__(self, workspace: str):
        self.workspace = Path(workspace).resolve()

    @property
    def permission(self) -> Permission:
        return Permission.allow()

    async def execute(self, count: int = 10, oneline: bool = True) -> ToolResult:
        args = ["log", f"-n{count}"]
        if oneline:
            args.append("--oneline")
        else:
            args.append("--pretty=format:%h %ad %s (%an)")
            args.append("--date=short")

        try:
            process = await asyncio.create_subprocess_exec(
                "git", *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace),
            )
            stdout, stderr = await process.communicate()
            output = stdout.decode("utf-8", errors="replace")
            error = stderr.decode("utf-8", errors="replace")

            if not output.strip():
                return ToolResult(output="No commits yet.")

            return ToolResult(output=output, error=error)
        except FileNotFoundError:
            return ToolResult(error="Git not found.")
        except Exception as e:
            return ToolResult(error=f"Git log failed: {e}")


class GitBranchTool(Tool):
    """List git branches."""

    name = "git_branch"
    description = "List local and remote git branches."
    parameters = {
        "type": "object",
        "properties": {
            "all": {
                "type": "boolean",
                "description": "Show remote branches too. Default: false.",
            },
        },
        "required": [],
    }

    def __init__(self, workspace: str):
        self.workspace = Path(workspace).resolve()

    @property
    def permission(self) -> Permission:
        return Permission.allow()

    async def execute(self, all: bool = False) -> ToolResult:
        args = ["branch", "--list"]
        if all:
            args.append("--all")

        try:
            process = await asyncio.create_subprocess_exec(
                "git", *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace),
            )
            stdout, stderr = await process.communicate()
            output = stdout.decode("utf-8", errors="replace")
            error = stderr.decode("utf-8", errors="replace")
            return ToolResult(output=output or "No branches.", error=error)
        except FileNotFoundError:
            return ToolResult(error="Git not found.")
        except Exception as e:
            return ToolResult(error=f"Git branch failed: {e}")


if __name__ == "__main__":
    # 独立调试：临时目录 git init + commit，然后跑 status/log/branch；无 git 则降级
    import asyncio
    import subprocess
    import tempfile

    async def _self_check():
        with tempfile.TemporaryDirectory() as ws:
            try:
                def _git(*args):
                    subprocess.run(["git", *args], cwd=ws, check=True,
                                   capture_output=True)
                _git("init", "-q")
                _git("config", "user.email", "selfcheck@openx.local")
                _git("config", "user.name", "Self Check")
                (Path(ws) / "a.txt").write_text("hello\n")
                _git("add", "a.txt")
                _git("commit", "-q", "-m", "init commit")
            except Exception as e:
                print(f"git unavailable ({e}); printing schemas only")
                for t in (GitStatusTool(ws), GitLogTool(ws), GitBranchTool(ws)):
                    print(f"  {t.name}: {t.description}")
                return
            st = await GitStatusTool(ws).execute()
            lg = await GitLogTool(ws).execute(count=5)
            br = await GitBranchTool(ws).execute()
            assert st.success and "init commit" in (lg.output or ""), lg.error
            print(st.output, "|", lg.output.strip(), "|", br.output.strip())

    asyncio.run(_self_check())
    print("openx/tools/git_tools.py OK ✓")
