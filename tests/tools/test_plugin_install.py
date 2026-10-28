"""S2d ``/plugin install <git-url>``：clone → 定位 → 沙箱过闸 → 拷入 → 装载。

覆盖（本地 file:// git 仓库端到端，真实 admission 子进程）：
- 目录插件仓库全链安装：ASK 确认 → clone → admission → 拷入用户目录 →
  会话装载 + 工具重建；
- 拒绝路径：非 https/git@ URL、目标已存在、admission 失败不留残目录、
  无人可问（无 ask_permission）fail-closed。

运行：``python -m pytest tests/tools/test_plugin_install.py -q``
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from openx.app.cli import commands
from openx.kernel import get_kernel, reset_kernel


class _CapConsole:
    def __init__(self, approved: bool = True):
        self.approved = approved
        self.infos: list[str] = []
        self.successes: list[str] = []
        self.warnings: list[str] = []
        self.asked: list[str] = []
        self.raw = self

    def print(self, *a, **k):  # raw.print 兼容
        self.infos.append(" ".join(str(x) for x in a))

    def print_info(self, m):
        self.infos.append(m)

    def print_success(self, m):
        self.successes.append(m)

    def print_warning(self, m):
        self.warnings.append(m)

    async def ask_permission(self, title, body, can_remember=False):
        self.asked.append(title)
        return (self.approved, False)


class _FakeAgent:
    def __init__(self, ws):
        self.workspace = ws
        self.rebuilds = 0

    def _rebuild_tools(self):
        self.rebuilds += 1


PLUGIN_DIR_REPO = {
    "plugin.py": (
        "__openx_meta__ = {'summary': 'installed dir plugin', 'trust': 'user'}\n"
        "from pathlib import Path\n"
        "def apply(ctx):\n"
        "    assert ctx.plugin_dir is not None\n"
        "def self_test():\n"
        "    assert True\n"
    ),
    "README.md": "asset file — should be copied along",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True,
                   capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "PATH": "/usr/bin:/bin:/usr/local/bin",
                        "HOME": str(repo.parent)})


def _make_repo(base: Path, name: str, files: dict) -> Path:
    repo = base / name
    repo.mkdir(parents=True)
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def install_env(tmp_path, monkeypatch):
    import openx.config as config_mod
    import openx.kernel.global_ledger as global_ledger_mod

    settings = tmp_path / "settings.json"
    monkeypatch.setattr(config_mod, "SETTINGS_PATH", settings)
    monkeypatch.setattr(
        global_ledger_mod, "GLOBAL_LEDGER_PATH", tmp_path / "ledger.jsonl"
    )
    monkeypatch.setenv("OPENX_PLUGIN_ADMISSION", "0")
    reset_kernel()
    ws = tmp_path / "ws"
    (ws / ".openx" / "plugins").mkdir(parents=True)
    config_mod.OpenXConfig.add_trusted_dir(str(ws))
    yield tmp_path, ws
    reset_kernel()


async def test_install_directory_plugin_end_to_end(install_env, monkeypatch):
    tmp_path, ws = install_env
    repo = _make_repo(tmp_path, "myplug", PLUGIN_DIR_REPO)

    class _Agent(_FakeAgent):
        pass

    agent = _Agent(ws)
    console = _CapConsole(approved=True)
    assert await commands.handle_slash_command(
        "plugin", agent, console,
        ["install", f"file://{repo}"]) is True
    assert console.asked and "myplug" in console.asked[0]
    assert console.successes and not console.warnings, console.warnings

    # 拷入用户目录（含资产），会话已装载
    k = get_kernel()
    user_dir = settings_path().parent / "plugins" / "myplug"
    assert (user_dir / "plugin.py").is_file()
    assert (user_dir / "README.md").is_file()
    entry = next(c for c in k.list_plugins() if c["id"] == "myplug")
    assert entry["phase"] == "active"
    assert agent.rebuilds == 1


def settings_path() -> Path:
    import openx.config as config_mod

    return Path(config_mod.SETTINGS_PATH)


async def test_install_refusals(install_env):
    tmp_path, ws = install_env
    import openx.config as config_mod
    from openx.kernel.assembly.loader import user_plugins_dir

    agent = _FakeAgent(ws)

    # 非 https/git@ URL
    console = _CapConsole()
    assert await commands.handle_slash_command(
        "plugin", agent, console, ["install", "ftp://x/y"]) is True
    assert console.warnings and "https" in console.warnings[-1]

    # 目标已存在（URL 末段派生插件名 myplug → 与预建目录冲突）
    (user_plugins_dir() / "myplug").mkdir(parents=True)
    repo = _make_repo(tmp_path, "myplug", PLUGIN_DIR_REPO)
    console2 = _CapConsole()
    assert await commands.handle_slash_command(
        "plugin", agent, console2, ["install", f"file://{repo}"]) is True
    assert console2.warnings and "already exists" in console2.warnings[-1]
    assert agent.rebuilds == 0


async def test_install_admission_failure_leaves_nothing(install_env):
    tmp_path, ws = install_env
    repo = _make_repo(tmp_path, "badplug", {
        "plugin.py": "def apply(ctx):\n    pass\ndef self_test():\n    raise ValueError('no')\n",
    })
    console = _CapConsole(approved=True)
    assert await commands.handle_slash_command(
        "plugin", _FakeAgent(ws), console, ["install", f"file://{repo}"]) is True
    assert any("admission failed" in w for w in console.warnings)
    # 沙箱拒 → 不落盘、不装载
    assert not (user_plugins_dir_path() / "badplug").exists()


def user_plugins_dir_path() -> Path:
    from openx.kernel.assembly.loader import user_plugins_dir

    return user_plugins_dir()


async def test_install_without_approver_fails_closed(install_env):
    tmp_path, ws = install_env
    repo = _make_repo(tmp_path, "nope", PLUGIN_DIR_REPO)

    class _BareConsole:
        """无 ask_permission 面（如降级 console）→ fail-closed 拒绝。"""

        def __init__(self):
            self.warnings: list[str] = []

        def print_warning(self, m):
            self.warnings.append(m)

        def print_info(self, m):
            pass

    console = _BareConsole()
    assert await commands.handle_slash_command(
        "plugin", _FakeAgent(ws), console, ["install", f"file://{repo}"]) is True
    assert console.warnings and "fail-closed" in console.warnings[-1]
