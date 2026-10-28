"""S1b admission 子进程（D9 加载期 L2）+ S1e 事务化 apply。

覆盖：
- 好插件：装载前过沙箱门（**真实子进程**），ACTIVE；
- self_test 抛异常 / 死循环超时 → PHASE_FAILED，且**零残留**（S1e 事务化：
  注册表无条目、sys.modules 无模块对象、info 贡献列表清空）；
- apply 半途崩溃（注册一个工具后 raise）→ FAILED 且第一个工具不留；
- builtin 豁免（无 path 的预导入模块不走门）；
- ``OPENX_PLUGIN_ADMISSION=0`` 逃生口：门关掉，插件直接装载（self_test
  本就不在装载期跑）。

运行：``python -m pytest tests/kernel/test_load_admission.py -q``
（本文件强制 ``OPENX_PLUGIN_ADMISSION=1``，真实子进程，比常规套件慢。）
"""

from __future__ import annotations

import json
import textwrap
import threading

import pytest

import openx.config as config_mod
import openx.kernel.global_ledger as global_ledger_mod
from openx.kernel import get_kernel, reset_kernel
from openx.kernel.assembly import loader


@pytest.fixture
def admission_env(tmp_path, monkeypatch):
    """kernel_env 同款隔离，但 admission 门**强制开**（本文件主题）。"""
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(config_mod, "SETTINGS_PATH", settings)
    monkeypatch.setattr(
        global_ledger_mod, "GLOBAL_LEDGER_PATH", tmp_path / "ledger.jsonl"
    )
    monkeypatch.setenv("OPENX_PLUGIN_ADMISSION", "1")
    reset_kernel()
    ws = tmp_path / "ws"
    (ws / ".openx" / "plugins").mkdir(parents=True)
    config_mod.OpenXConfig.add_trusted_dir(str(ws))
    yield ws
    reset_kernel()


def _write(ws, name: str, body: str) -> None:
    (ws / ".openx" / "plugins" / f"{name}.py").write_text(
        textwrap.dedent(body), encoding="utf-8"
    )


GOOD = """\
    from openx.tools.base import Tool, ToolResult

    class HiTool(Tool):
        name = "hi"
        description = "hi"
        parameters = {"type": "object", "properties": {}}
        async def execute(self, **kw):
            return ToolResult(output="hi")

    def apply(ctx):
        ctx.register_tool(HiTool())

    def self_test():
        print("self-test ok")
        assert True
"""

BAD_TEST = """\
    def apply(ctx):
        pass

    def self_test():
        raise ValueError("broken on purpose")
"""

HANG_TEST = """\
    def apply(ctx):
        pass

    def self_test():
        while True:
            pass
"""

HALF_APPLY = """\
    from openx.tools.base import Tool, ToolResult

    class OkTool(Tool):
        name = "ok-first"
        description = "first"
        parameters = {"type": "object", "properties": {}}
        async def execute(self, **kw):
            return ToolResult(output="ok")

    def apply(ctx):
        ctx.register_tool(OkTool())
        raise RuntimeError("boom after registering")
"""


def _tool_names(k) -> set[str]:
    return {e.name for e in k.registry("tools").entries()}


def test_good_plugin_passes_gate_and_loads(admission_env):
    ws = admission_env
    _write(ws, "good", GOOD)
    k = get_kernel()
    k.ensure_loaded(str(ws))
    assert "hi" in _tool_names(k)
    entry = next(c for c in k.list_plugins() if c["id"] == "good")
    assert entry["phase"] == "active"


def test_failing_self_test_isolation_is_transactional(admission_env):
    ws = admission_env
    _write(ws, "badtest", BAD_TEST)
    _write(ws, "halfapply", HALF_APPLY)
    k = get_kernel()
    threads_before = threading.active_count()
    mods_before = set(n for n in __import__("sys").modules if n.startswith("_openx_"))
    k.ensure_loaded(str(ws))

    # 两个都 FAILED，错误可读
    for pid in ("badtest", "halfapply"):
        entry = next(c for c in k.list_plugins() if c["id"] == pid)
        assert entry["phase"] == "failed", (pid, entry)
    bad = next(c for c in k.list_plugins() if c["id"] == "badtest")
    assert "ValueError" in k.plugin_help("badtest")["error"]

    # S1e 事务化：半途注册的工具不留、sys.modules 无模块对象
    assert "ok-first" not in _tool_names(k)
    mods_after = set(n for n in __import__("sys").modules if n.startswith("_openx_"))
    assert mods_after == mods_before
    # daemon 线程不泄漏（旧实现超时后线程带全权限滞留）
    assert threading.active_count() == threads_before


def test_hanging_self_test_killed_at_timeout(admission_env, monkeypatch):
    """死循环 self_test：子进程被超时 kill（这里把默认 15s 收到 3s）。"""
    import openx.kernel.sandbox.admission as admission_mod

    ws = admission_env
    _write(ws, "hang", HANG_TEST)
    real = admission_mod.run_admission
    monkeypatch.setattr(
        admission_mod, "run_admission",
        lambda path, **kw: real(path, timeout=3),
    )
    k = get_kernel()
    k.ensure_loaded(str(ws))
    entry = next(c for c in k.list_plugins() if c["id"] == "hang")
    assert entry["phase"] == "failed"
    assert "timeout" in k.plugin_help("hang")["error"]


def test_builtin_plugins_exempt_from_gate(admission_env):
    """builtin 无 path，本就不走门：admission 开着也照常 ACTIVE。"""
    ws = admission_env
    k = get_kernel()
    k.ensure_loaded(str(ws))
    builtins = [p for p in k.list_plugins() if p["id"].startswith("builtin-")]
    assert builtins
    assert all(p["phase"] == "active" for p in builtins)


def test_escape_hatch_skips_gate(admission_env, monkeypatch):
    """OPENX_PLUGIN_ADMISSION=0：门整个关掉（死循环 self_test 也不再跑）。"""
    monkeypatch.setenv("OPENX_PLUGIN_ADMISSION", "0")
    ws = admission_env
    _write(ws, "hang", HANG_TEST)
    k = get_kernel()
    k.ensure_loaded(str(ws))
    entry = next(c for c in k.list_plugins() if c["id"] == "hang")
    assert entry["phase"] == "active"  # self_test 装载期本就不跑，门关了直接过


def test_retry_after_failure_recovers(admission_env):
    ws = admission_env
    path = ws / ".openx" / "plugins" / "flaky.py"
    path.write_text(textwrap.dedent(BAD_TEST), encoding="utf-8")
    k = get_kernel()
    k.ensure_loaded(str(ws))
    assert next(c for c in k.list_plugins() if c["id"] == "flaky")["phase"] == "failed"

    # 修好后重试（load_plugin 显式路径）：purge 已在失败时做过，成功即 ACTIVE
    path.write_text(textwrap.dedent(GOOD), encoding="utf-8")
    ok, msg = k.load_plugin("flaky", path=path)
    assert ok, msg
    assert "hi" in _tool_names(k)
