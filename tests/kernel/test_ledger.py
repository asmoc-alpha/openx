"""记账（K2a/K2b）：事件信封、突变记账、账本挂接、会话账本接线。

运行：``python -m pytest tests/kernel/test_ledger.py -q``
"""

from __future__ import annotations

import json

import openx.orchestration.sessions as sessions_mod
from openx.kernel.protocol import Event, digest_of, project
from openx.orchestration.sessions import SessionStore
from openx.kernel import get_kernel

from ._helpers import BAD_SRC, HELLO_SRC, NOVALID_SRC, write_plugin


class Sink:
    """收集事件的账本 sink。"""

    def __init__(self):
        self.events: list[Event] = []

    def __call__(self, event: Event) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]

    def of(self, type_: str) -> list[Event]:
        return [e for e in self.events if e.type == type_]


class TestEmit:
    def test_seq_monotonic_and_digest_chain(self, kernel_env):
        ws, _ = kernel_env
        sink = Sink()
        k = get_kernel()
        k.attach_ledger(sink, session="s1")
        k.ensure_loaded(str(ws))
        assert [e.seq for e in sink.events] == list(
            range(1, len(sink.events) + 1)
        )
        # digest 链可复算：digest = h(prev || canonical(event))
        prev = ""
        for e in sink.events:
            assert e.digest == digest_of(prev, e)
            prev = e.digest
        assert all(e.session == "s1" for e in sink.events)
        # 归因：装载/组合事件 origin=kernel，注册事件 origin=plugin:<id>
        assert all(
            e.origin == "kernel" or e.origin.startswith("plugin:")
            for e in sink.events
        )
        assert any(e.origin.startswith("plugin:") for e in sink.events)

    def test_sink_failure_does_not_crash(self, kernel_env):
        ws, _ = kernel_env

        def bad_sink(event):
            raise OSError("disk full")

        k = get_kernel()
        k.attach_ledger(bad_sink)
        k.ensure_loaded(str(ws))  # 记账降级为丢弃，加载照常完成
        assert len(k.inventory()) == 2  # 两个内置插件

    def test_no_sink_still_counts(self, kernel_env):
        ws, _ = kernel_env
        k = get_kernel()
        event = k.emit("probe", {"type": "probe"})
        assert event.seq == 1
        k.ensure_loaded(str(ws))  # 未挂接：emit 不炸，事件落空
        # seq 仍在计数（Ledger 内部状态，经公共 emit 观察）
        assert k.emit("probe2", {"type": "probe"}).seq >= 2


class TestMutationEvents:
    def test_composition_and_load_events(self, kernel_env):
        ws, _ = kernel_env
        write_plugin(ws, "hello", HELLO_SRC)
        sink = Sink()
        k = get_kernel()
        k.attach_ledger(sink)
        k.ensure_loaded(str(ws))
        types = sink.types()
        # 注册发生在 apply 内、plugin_loaded 在 apply 完成后：
        # 首条事件是内置工厂的 registered，紧随其后的才是 plugin_loaded
        assert types[0] == "registered"
        assert sink.events[0].payload["name"] == "core-tools"
        assert types.count("plugin_loaded") == 3  # 两个内置 + hello
        assert sink.events[1].payload["plugin"] == "builtin-tools"
        # 工具工厂 + 各 provider 工厂（openai-compat 恒在、anthropic 视 SDK）+ hello 工具 + hi 命令
        n_providers = len(get_kernel().registry("providers"))
        assert types.count("registered") == 3 + n_providers
        comp = sink.of("composition_resolved")
        assert len(comp) == 1
        assert comp[0].payload["plugins"] == [
            "builtin-tools", "builtin-providers", "hello",
        ]
        assert comp[0].payload["disabled"] == []

    def test_idempotent_reload_emits_nothing(self, kernel_env):
        ws, _ = kernel_env
        write_plugin(ws, "hello", HELLO_SRC)
        sink = Sink()
        k = get_kernel()
        k.attach_ledger(sink)
        k.ensure_loaded(str(ws))
        n = len(sink.events)
        k.ensure_loaded(str(ws))  # 键未变：幂等跳过，不重记（D6）
        assert len(sink.events) == n

    def test_rejected_event_with_provenance(self, kernel_env):
        ws, _ = kernel_env
        write_plugin(ws, "novalid", NOVALID_SRC)
        sink = Sink()
        k = get_kernel()
        k.attach_ledger(sink)
        k.ensure_loaded(str(ws))
        rejected = sink.of("rejected")
        assert len(rejected) == 1
        assert rejected[0].payload["kind"] == "tools"
        assert rejected[0].payload["plugin"] == "novalid"
        assert rejected[0].origin == "plugin:novalid"
        assert any("permission" in p for p in rejected[0].payload["problems"])

    def test_failed_event(self, kernel_env):
        ws, _ = kernel_env
        write_plugin(ws, "bad", BAD_SRC)
        sink = Sink()
        k = get_kernel()
        k.attach_ledger(sink)
        k.ensure_loaded(str(ws))
        failed = sink.of("plugin_failed")
        assert len(failed) == 1
        assert failed[0].payload["plugin"] == "bad"
        assert "boom" in failed[0].payload["error"]

    def test_entry_seq_backfilled(self, kernel_env):
        """Entry.seq（inserted_at_seq）指回 registered 事件。"""
        ws, _ = kernel_env
        write_plugin(ws, "hello", HELLO_SRC)
        sink = Sink()
        k = get_kernel()
        k.attach_ledger(sink)
        k.ensure_loaded(str(ws))
        entry = k.registry("tools").get("hello")
        assert entry is not None and entry.seq is not None
        event = next(e for e in sink.events if e.seq == entry.seq)
        assert event.type == "registered"
        assert event.payload["name"] == "hello"


class TestSessionLedger:
    def test_events_persist_and_resume_seq(self, kernel_env, monkeypatch, tmp_path):
        ws, _ = kernel_env
        write_plugin(ws, "hello", HELLO_SRC)
        monkeypatch.setattr(sessions_mod, "SESSIONS_DIR", tmp_path)
        store = SessionStore.create(str(ws), "test-model")
        k = get_kernel()
        k.attach_ledger(
            store.append_event, session="sess1", start_seq=store.ledger_start_seq()
        )
        k.ensure_loaded(str(ws))
        n = store.ledger_start_seq()
        assert n > 0
        # 恢复语义：load() 只恢复消息，账本行不干扰会话恢复
        meta, messages = SessionStore.load(store.path)
        assert messages == [] and meta.session_id == store.meta.session_id
        # 续接：新内核进程从既有条目数起，seq 不重号
        k.attach_ledger(
            store.append_event, session="sess1", start_seq=store.ledger_start_seq()
        )
        event = k.emit("probe", {"type": "probe"})
        assert event.seq == n + 1
        # 信封行落盘且字段完整
        lines = [
            json.loads(line)
            for line in store.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        envelope = lines[-1]
        assert envelope["seq"] == n + 1 and envelope["digest"]
        assert envelope["payload"] == {"type": "probe"}

    def test_downlink_projection_is_payload(self, kernel_env):
        """协议 = 账本外化：下行投影与 payload 逐字段一致。"""
        ws, _ = kernel_env
        k = get_kernel()
        event = k.emit(
            "text_delta",
            {"type": "text_delta", "text": "hi"},
            origin="model",
        )
        assert project(event) == {"type": "text_delta", "text": "hi"}
