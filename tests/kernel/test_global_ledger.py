"""全局账本 + 决策事件族（K5，kernel 详设 §3.2）。

覆盖：
- 决策事件全文落**全局账本**，会话账本只留 ``decision_ref`` 引用（不复制内容）；
- 引用 ``seq`` 对齐全局条目、``ledger="global"``、归因 ``session`` 透传；
- 默认文件 sink（``GLOBAL_LEDGER_PATH``）惰性自挂接 + **跨进程续接** seq/哈希链；
- §3.4 校验工具：篡改中段 → ``verify()`` 报断链；
- 晋升 → 卸载 = 回滚（``plugin_promoted`` + ``plugin_rolled_back`` 均上全局账本）。

环境：kernel_env 已把 ``GLOBAL_LEDGER_PATH`` 与 ``SETTINGS_PATH`` 隔离到 tmp，
绝不触碰真实 ``~/.openx``。运行：``python -m pytest tests/kernel/test_global_ledger.py -q``
"""

from __future__ import annotations

import json

from openx.kernel import get_kernel, reset_kernel
from openx.kernel.global_ledger import GlobalLedgerStore
from openx.kernel.protocol import DECISION_EVENTS, decision_ref

from ._helpers import HELLO_SRC, write_plugin


class Sink:
    """收集事件的账本 sink（test_ledger 同款）。"""

    def __init__(self):
        self.events: list = []

    def __call__(self, event) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]

    def of(self, type_: str) -> list:
        return [e for e in self.events if e.type == type_]


# ── 决策：全文上全局、引用留会话 ──────────────────────────────────


def test_decision_lands_in_global_ledger_with_session_ref(kernel_env):
    k = get_kernel()
    session_sink, global_sink = Sink(), Sink()
    k.attach_ledger(session_sink, session="s1")
    k.attach_global_ledger(global_sink)

    g = k.emit_decision(
        "plugin_promoted",
        {"type": "plugin_promoted", "plugin": "auto-x", "trust": "user"},
        origin="user",
    )

    # 全文（含归因 session）落全局账本
    promoted = global_sink.of("plugin_promoted")
    assert len(promoted) == 1
    assert promoted[0].payload["plugin"] == "auto-x"
    assert promoted[0].payload["session"] == "s1"  # 归因：哪次会话做的决定
    assert promoted[0].seq == g.seq == 1

    # 会话账本只留引用，不复制内容
    refs = session_sink.of("decision_ref")
    assert len(refs) == 1
    assert refs[0].payload == {
        "type": "decision_ref", "decision": "plugin_promoted",
        "ledger": "global", "seq": 1, "session": "s1",
    }
    assert not session_sink.of("plugin_promoted")  # 决策本体不在会话账本


def test_default_sink_writes_to_configured_path(kernel_env):
    """不显式挂接时惰性用默认文件 sink，落 GLOBAL_LEDGER_PATH（测试内已隔离）。"""
    k = get_kernel()
    k.emit_decision("ratchet_tightened", {"type": "ratchet_tightened", "rule": "x"})

    store = GlobalLedgerStore()
    assert store.path.name == "ledger.jsonl"
    events = store.read_all()
    assert [e.type for e in events] == ["ratchet_tightened"]
    assert store.verify() == []  # 链完好


def test_chain_resumes_across_process(kernel_env):
    """模拟进程重启：新内核重新扫描账本 → seq 续起、哈希链不断。"""
    k1 = get_kernel()
    e1 = k1.emit_decision("plugin_promoted", {"type": "plugin_promoted", "plugin": "a"})
    e2 = k1.emit_decision("plugin_promoted", {"type": "plugin_promoted", "plugin": "b"})
    assert (e1.seq, e2.seq) == (1, 2)

    reset_kernel()  # "重启"：新内核，内存计数器归零，但账本文件还在
    k2 = get_kernel()
    e3 = k2.emit_decision("plugin_rolled_back", {"type": "plugin_rolled_back", "plugin": "a"})
    assert e3.seq == 3, e3.seq  # 从既有条目数续起，不重号

    store = GlobalLedgerStore()
    assert [e.seq for e in store.read_all()] == [1, 2, 3]
    assert store.verify() == []  # 跨进程续接后链仍完好


# ── 校验工具（§3.4）─────────────────────────────────────────────


def test_verify_detects_mid_tamper(kernel_env):
    k = get_kernel()
    for name in ("a", "b", "c"):
        k.emit_decision("plugin_promoted", {"type": "plugin_promoted", "plugin": name})

    store = GlobalLedgerStore()
    assert store.verify() == []

    # 篡改中段（第 2 条）的 payload → 该条起链断
    lines = store.path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[1])
    row["payload"]["plugin"] = "evil"
    lines[1] = json.dumps(row, ensure_ascii=False)
    store.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert store.verify() == [2]


def test_verify_detects_deletion(kernel_env):
    k = get_kernel()
    for name in ("a", "b", "c"):
        k.emit_decision("plugin_promoted", {"type": "plugin_promoted", "plugin": name})

    store = GlobalLedgerStore()
    lines = store.path.read_text(encoding="utf-8").splitlines()
    del lines[1]  # 删中段 → 后继那条复算不匹配
    store.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert store.verify() == [3]


# ── 端到端：晋升 → 卸载 = 回滚 ────────────────────────────────────


def test_promote_then_unload_records_rollback(kernel_env):
    ws, _ = kernel_env
    k = get_kernel()
    global_sink = Sink()
    k.attach_global_ledger(global_sink)
    k.ensure_loaded(str(ws))  # 先装载（此时无插件文件）
    write_plugin(ws, "auto-greet", HELLO_SRC)  # 再落一个 auto-* 插件
    assert k.load_plugin("auto-greet")[0]

    assert k.promote_plugin("auto-greet")[0]
    assert "plugin_promoted" in global_sink.types()

    # 卸载一个曾晋升的插件 = 回滚，落全局账本（"这个插件为什么没了"）
    assert k.unload_plugin("auto-greet")[0]
    assert "plugin_rolled_back" in global_sink.types()


def test_rollback_not_emitted_for_unpromoted(kernel_env):
    """未晋升的会话插件卸载只是会话记账，不产跨会话决策。"""
    ws, _ = kernel_env
    k = get_kernel()
    global_sink = Sink()
    k.attach_global_ledger(global_sink)
    k.ensure_loaded(str(ws))
    write_plugin(ws, "plain", HELLO_SRC)
    assert k.load_plugin("plain")[0]
    assert k.unload_plugin("plain")[0]
    assert global_sink.events == []  # 无任何决策事件


# ── 族常量与引用 builder ──────────────────────────────────────────


def test_decision_family_and_ref_shape():
    assert DECISION_EVENTS == frozenset({
        "plugin_promoted", "plugin_rolled_back",
        "scaffold_retired", "scaffold_restored", "ratchet_tightened",
    })
    ref = decision_ref("scaffold_retired", 7, session="s9")
    assert ref["type"] == "decision_ref" and ref["ledger"] == "global"
    assert ref["decision"] == "scaffold_retired" and ref["seq"] == 7
    assert ref["session"] == "s9"
