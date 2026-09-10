"""容灾 · 旁挂存储——崩溃安全的 checkpoint 落盘（只做持久化，不做裁决）。

**落点**：``~/.openx/sessions/<workspace_hash>/<session_id>.ckpt.json``，
与会话文件 ``<session_id>.jsonl`` 同目录。旁挂文件是**覆盖式最新态**
（last-writer-wins），账本事件才是 append-only 的审计真源。

**为什么不是"只往账本写一条 checkpoint 事件"**：账本只能追加，而 checkpoint
的语义是"最新态取代旧态"。把整个快照塞进事件流会让"读最新 checkpoint"
退化成全文件扫描，且事件体积随消息数膨胀--``iter_events`` 会把 payload
原样喂给 Web 回放，几百 KB 的工具输出会把每次回放撑爆。故分工：
**账本事件存事实（seq + 摘要），旁挂文件存体**。

**原子写**：``tmp -> fsync(tmp) -> os.replace -> fsync(dir)``。
``os.replace`` 在 POSIX 上是原子的，因此**不存在撕裂的旁挂文件**--
要么是旧的完整内容，要么是新的完整内容。崩溃只可能留下 ``.tmp`` 垃圾。
"""

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

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from .model import CheckpointRecord, record_from_json, record_to_json

_log = logging.getLogger("openx.kernel")

# 快照体积上限：超限**拒绝落盘**而不是裁剪。裁剪会破坏"逐字节保真"，
# 让恢复后的 prompt 与崩溃前不同源--宁可退化为"锚在上一轮"。
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024

# 旁挂文件后缀（与 .jsonl 同目录；list_for_workspace 只 glob *.jsonl，不会误列）
SIDECAR_SUFFIX = ".ckpt.json"

# 残留 .tmp 的清理门限：超过一天视为孤儿（活着的写者不会留这么久）
_STALE_TMP_SECONDS = 24 * 3600

# 失败原因（写盘返回值；"" = 成功）
REASON_OK = ""
REASON_OVERSIZE = "oversize"
REASON_IO = "io"


def sidecar_path(jsonl_path: Path) -> Path:
    """由会话文件路径推出旁挂文件路径（同目录、同 stem）。

    从会话路径推导而非重新拼目录，是为了让"monkeypatch SESSIONS_DIR"
    这类测试隔离**自动生效**--旁挂文件永远跟着会话文件走。
    """
    jsonl_path = Path(jsonl_path)
    return jsonl_path.with_name(f"{jsonl_path.stem}{SIDECAR_SUFFIX}")


class CheckpointStore:
    """单会话的旁挂检查点存储。所有方法**永不抛**（持久化是优化，不是关键路径）。

    与 ``Ledger`` 的 sink 隔离同一条纪律：证据/优化系统故障不该成为单点。
    """

    def __init__(self, jsonl_path: Path) -> None:
        self.jsonl_path = Path(jsonl_path)
        self.path = sidecar_path(self.jsonl_path)

    # ── 写 ──────────────────────────────────────────────────

    def write(self, record: CheckpointRecord) -> str:
        """原子落盘。成功返回 ``""``，失败返回原因（``"oversize"`` / ``"io"``）。"""
        try:
            raw = record_to_json(record)
        except Exception:
            _log.exception("checkpoint serialization failed")
            return REASON_IO
        if len(raw.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            # 拒绝而非裁剪：裁剪过的快照恢复出来的 prompt 与崩溃前不同源
            _log.warning(
                "checkpoint exceeds %d bytes; skipped (not truncated)",
                MAX_SNAPSHOT_BYTES,
            )
            return REASON_OVERSIZE
        if not _atomic_write_text(self.path, raw):
            return REASON_IO
        _sweep_stale_tmp(self.path)
        return REASON_OK

    # ── 读 ──────────────────────────────────────────────────

    def read(self) -> Optional[CheckpointRecord]:
        """读取并反序列化；不存在/损坏/版本不符一律 None（绝不抛）。"""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            _log.exception("checkpoint read failed")
            return None
        record = record_from_json(raw)
        if record is None:
            _log.warning("checkpoint unreadable or unsupported: %s", self.path)
        return record

    def peek(self) -> Optional[CheckpointRecord]:
        """轻量探测（当前与 ``read`` 同实现；保留给未来的"只看头部"优化）。"""
        return self.read()

    # ── 删 ──────────────────────────────────────────────────

    def delete(self) -> bool:
        """删除旁挂文件。返回是否真的删掉了。"""
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            _log.exception("checkpoint delete failed")
            return False


def ledger_last_checkpoint(jsonl_path: Path) -> Optional[dict[str, Any]]:
    """会话账本里**最后一条** ``checkpoint`` 信封行（倒序扫描，找到即停）。

    这是"账本为准"的落点：旁挂文件丢失/陈旧时，账本仍能告诉我们最后
    一次提交到了哪一轮。倒序扫描让常见路径只读文件尾部附近的量。

    落点是"最后一条"而非"摘要最大"：seq 单调，文件顺序即 seq 顺序，
    倒序首条命中就是最新。损坏行跳过（与 ``SessionStore.load`` 同纪律）。
    """
    jsonl_path = Path(jsonl_path)
    try:
        lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            line = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(line, dict):
            continue
        if line.get("type") != "checkpoint":
            continue
        payload = line.get("payload")
        if not isinstance(payload, dict):
            continue
        # 账本行 → 裁决需要的字段（payload 自带 type，此处拍平便于读取）
        return {
            "seq": line.get("seq", 0),
            "digest": line.get("digest", ""),
            "cause": line.get("cause"),
            "session": line.get("session", ""),
            **payload,
        }
    return None


def ledger_envelope_count(jsonl_path: Path) -> int:
    """账本信封行数（= 已有 seq 数）。

    与 ``SessionStore.ledger_start_seq`` 同口径，本地重算一份是为了让
    内核层（不 import orchestration）也能做"引用是否越界"的裁决。
    """
    jsonl_path = Path(jsonl_path)
    try:
        lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    count = 0
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            line = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(line, dict) and "seq" in line and "digest" in line:
            count += 1
    return count


# ── 内部：原子写 ────────────────────────────────────────────


def _atomic_write_text(path: Path, text: str) -> bool:
    """``tmp -> fsync -> replace -> fsync(dir)``；失败返回 False，永不抛。"""
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, text.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(path))
    except Exception:
        _log.exception("checkpoint write failed: %s", path)
        _best_effort_unlink(tmp)
        return False
    _fsync_dir(path.parent)
    return True


def _fsync_dir(directory: Path) -> None:
    """fsync 父目录，保证 rename 本身持久化。

    部分文件系统不支持对目录 fsync--失败无妨（rename 已原子生效，
    最坏是断电后回到旧内容，仍是完整快照而非撕裂块）。
    """
    try:
        fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        pass


def _best_effort_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _sweep_stale_tmp(keep: Path) -> None:
    """清理同目录下的孤儿 ``.tmp``（进程崩溃留下的）。

    只删**超过一天**的--同进程刚写的 tmp 可能在别的线程里还没 replace，
    按时间而不是按 pid 判定更保守（pid 会被复用）。
    """
    try:
        now = time.time()
        for candidate in keep.parent.glob(f"{keep.name}.tmp.*"):
            if candidate == keep:
                continue
            try:
                if now - candidate.stat().st_mtime > _STALE_TMP_SECONDS:
                    candidate.unlink()
            except OSError:
                continue
    except OSError:
        pass


if __name__ == "__main__":
    import tempfile

    from .model import CheckpointRecord, TurnSnapshot

    with tempfile.TemporaryDirectory() as tmpdir:
        jsonl = Path(tmpdir) / "abc123.jsonl"
        assert sidecar_path(jsonl).name == "abc123.ckpt.json"

        store = CheckpointStore(jsonl)
        # 不存在时读 → None（不抛）
        assert store.read() is None
        assert not store.delete()

        # 原子写：内容可读、无残留 tmp
        rec = CheckpointRecord(
            session_id="abc123", workspace="/ws", ledger_seq=3,
            snapshot=TurnSnapshot(tool_rounds=1, new_turn=[{"role": "user", "content": "x"}]),
        )
        assert store.write(rec) == ""
        back = store.read()
        assert back is not None and back.ledger_seq == 3
        assert back.snapshot.tool_rounds == 1
        assert list(Path(tmpdir).glob("*.tmp.*")) == []

        # 覆盖写：旧态被取代（last-writer-wins，非追加）
        rec.ledger_seq = 5
        assert store.write(rec) == ""
        assert store.read().ledger_seq == 5

        # 损坏文件 → None，不抛
        store.path.write_text('{"kind": "openx.turn_checkpoint", "ver', encoding="utf-8")
        assert store.read() is None

        # 超限 → 拒绝落盘（不裁剪）
        huge = CheckpointRecord(
            session_id="abc123",
            snapshot=TurnSnapshot(new_turn=[{"content": "x" * (MAX_SNAPSHOT_BYTES + 1)}]),
        )
        assert store.write(huge) == REASON_OVERSIZE

        # 写失败（目录只读）→ 返回 io，不抛
        ro = Path(tmpdir) / "ro"
        ro.mkdir()
        ro_store = CheckpointStore(ro / "s.jsonl")
        assert ro_store.write(rec) == ""
        os.chmod(ro, 0o500)
        try:
            assert ro_store.write(rec) == REASON_IO
        finally:
            os.chmod(ro, 0o700)

        # 删除
        assert store.delete() and not store.path.exists()

        # 账本倒序扫描：取最后一条 checkpoint 信封
        ledger = Path(tmpdir) / "sess.jsonl"
        ledger.write_text(
            "\n".join([
                json.dumps({"type": "meta", "session_id": "s"}),
                json.dumps({"seq": 1, "digest": "d1", "type": "registered",
                            "payload": {"type": "registered"}}),
                json.dumps({"seq": 2, "digest": "d2", "cause": 1, "type": "checkpoint",
                            "payload": {"type": "checkpoint", "phase": "committed",
                                        "tool_rounds": 1}}),
                "not json at all",
                json.dumps({"type": "message", "message": {"role": "user"}}),
            ]) + "\n",
            encoding="utf-8",
        )
        last = ledger_last_checkpoint(ledger)
        assert last is not None and last["seq"] == 2 and last["tool_rounds"] == 1
        assert ledger_last_checkpoint(Path(tmpdir) / "nope.jsonl") is None
        assert ledger_envelope_count(ledger) == 2  # 两行带 seq+digest
    print("openx/kernel/recovery/store.py OK ✓")
