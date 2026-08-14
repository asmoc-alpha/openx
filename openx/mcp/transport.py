"""MCP stdio transport — NDJSON JSON-RPC 2.0 over subprocess pipes.

MCP 的 stdio 帧格式是**换行分隔 JSON**（每行一条 JSON-RPC 消息），
**不是** LSP 的 Content-Length 头。本模块提供最小的客户端传输层：

- ``start()`` 拉起子进程并起两个后台 task：reader（解析响应、按 id
  分发 future）与 stderr drainer（诊断用的环形缓冲，绝不记录 env 值）；
- ``request()`` 发请求、等 future（超时抛 ``asyncio.TimeoutError``，
  服务端 ``error`` 响应抛 ``JSONRPCError``）；
- ``notify()`` fire-and-forget 写入（无 id、不等响应）；
- ``close()`` 幂等：取消后台 task → terminate → 2s 后 kill → 关管道。

仅 stdlib（asyncio/json/os）——零新依赖。
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

import asyncio
import collections
import json
import os

# stderr 诊断环形缓冲保留的行数（仅用于报错信息，绝不记录 env 值）
STDERR_TAIL_LINES = 50


class JSONRPCError(Exception):
    """JSON-RPC 2.0 错误：服务端 ``error`` 响应或 isError 工具结果。"""

    def __init__(self, code: int, message: str, data=None) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"[{code}] {message}")


class StdioTransport:
    """以子进程 stdin/stdout 上的 NDJSON 承载 JSON-RPC 2.0 往返。"""

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict | None = None,
        name: str = "server",
    ) -> None:
        self.command = command
        self.args = list(args or [])
        # env 值只传给子进程，绝不写进日志/异常文本
        self._env = dict(env or {})
        self.name = name
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 0  # 单调递增请求 id
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail: collections.deque = collections.deque(
            maxlen=STDERR_TAIL_LINES
        )
        self._closed = False

    # ── lifecycle ───────────────────────────────────────────────

    async def start(self) -> None:
        """拉起子进程并启动 reader / stderr drainer 后台 task。"""
        if self._proc is not None:
            raise JSONRPCError(-32000, f"MCP transport '{self.name}' already started")
        self._proc = await asyncio.create_subprocess_exec(
            self.command, *self.args,
            env={**os.environ, **self._env},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._closed = False
        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"mcp-reader-{self.name}"
        )
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name=f"mcp-stderr-{self.name}"
        )

    async def _read_loop(self) -> None:
        """逐行读 stdout：按 id 分发响应；通知（无 id）忽略；坏行跳过。"""
        proc = self._proc
        try:
            while proc is not None and proc.stdout is not None:
                line = await proc.stdout.readline()
                if not line:  # EOF —— 服务端已退出
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue  # 非法 JSON 行 → 跳过
                if not isinstance(msg, dict):
                    continue
                msg_id = msg.get("id")
                if msg_id is None:
                    continue  # 服务端通知（无 id）→ 忽略
                fut = self._pending.pop(msg_id, None)
                if fut is None or fut.done():
                    continue  # 已超时清理过的请求
                error = msg.get("error")
                if isinstance(error, dict):
                    fut.set_exception(JSONRPCError(
                        int(error.get("code") or -1),
                        str(error.get("message") or ""),
                        error.get("data"),
                    ))
                else:
                    fut.set_result(msg.get("result"))
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # 管道异常视同 EOF：唤醒所有等待者后退出
        finally:
            # EOF / 异常退出 → 让挂起的 request() 立即失败，绝不挂到超时
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(JSONRPCError(
                        -32000, f"MCP server '{self.name}' closed the connection"
                    ))
            self._pending.clear()

    async def _drain_stderr(self) -> None:
        """把 stderr 行收进环形缓冲（诊断用，绝不打日志、不记录 env）。"""
        proc = self._proc
        try:
            while proc is not None and proc.stderr is not None:
                line = await proc.stderr.readline()
                if not line:
                    break
                self._stderr_tail.append(line.decode("utf-8", "replace").rstrip())
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    # ── messaging ───────────────────────────────────────────────

    def _write(self, payload: dict) -> None:
        """把一条 JSON 消息写进子进程 stdin（一行一消息）。"""
        proc = self._proc
        if proc is None or proc.stdin is None or self._closed:
            raise JSONRPCError(-32000, f"MCP transport '{self.name}' not started")
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        proc.stdin.write(data)

    async def request(
        self,
        method: str,
        params: dict | None = None,
        timeout: float = 30.0,
    ) -> dict:
        """发送请求并等待响应。

        - 超时 → 清理 pending 并抛 ``asyncio.TimeoutError``；
        - 响应含 ``error`` → reader 已把 future 置为 ``JSONRPCError``。
        """
        self._next_id += 1
        msg_id = self._next_id
        payload: dict = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            payload["params"] = params
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        try:
            self._write(payload)
            await self._proc.stdin.drain()  # type: ignore[union-attr]
        except Exception as e:
            self._pending.pop(msg_id, None)
            if not fut.done():
                fut.cancel()
            if isinstance(e, JSONRPCError):
                raise
            raise JSONRPCError(
                -32000, f"write to MCP server '{self.name}' failed: {e}"
            ) from e
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise

    def notify(self, method: str, params: dict | None = None) -> None:
        """发送通知（无 id、fire-and-forget，写入失败抛 JSONRPCError）。"""
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

    # ── teardown ────────────────────────────────────────────────

    async def close(self) -> None:
        """幂等关闭：唤醒挂起请求 → 取消后台 task → 终止进程 → 关管道。

        每一步单独兜底异常——清理绝不允许抛给调用方。
        """
        if self._closed:
            return
        self._closed = True

        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(JSONRPCError(
                    -32000, f"MCP transport '{self.name}' closed"
                ))
        self._pending.clear()

        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                try:
                    task.cancel()
                except Exception:
                    pass
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._reader_task = None
        self._stderr_task = None

        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except (ProcessLookupError, Exception):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except (ProcessLookupError, Exception):
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except Exception:
                    pass
            except Exception:
                pass
        if proc is not None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except Exception:
                pass
            try:  # 关闭子进程 transport 顺带收掉 stdout/stderr 管道
                proc._transport.close()
            except Exception:
                pass
        self._proc = None

    @property
    def stderr_tail(self) -> str:
        """最近若干行 stderr（供错误信息展示，不含 env 值）。"""
        return "\n".join(self._stderr_tail)


if __name__ == "__main__":
    import sys
    import tempfile
    from pathlib import Path

    # JSONRPCError 基本属性
    _err = JSONRPCError(-32601, "method not found", {"x": 1})
    assert _err.code == -32601 and _err.message == "method not found"
    assert _err.data == {"x": 1} and "-32601" in str(_err)

    # 真实子进程往返：内联一个最小 NDJSON JSON-RPC 服务器
    _SERVER = (
        "import json, sys\n"
        "while True:\n"
        "    line = sys.stdin.readline()\n"
        "    if not line:\n"
        "        break\n"
        "    line = line.strip()\n"
        "    if not line:\n"
        "        continue\n"
        "    msg = json.loads(line)\n"
        "    if msg.get('method') == 'ping':\n"
        "        out = {'jsonrpc': '2.0', 'id': msg['id'], 'result': {'pong': True}}\n"
        "    elif msg.get('method') == 'bad':\n"
        "        out = {'jsonrpc': '2.0', 'id': msg['id'],\n"
        "               'error': {'code': -32601, 'message': 'nope'}}\n"
        "    else:\n"
        "        continue\n"
        "    sys.stdout.write(json.dumps(out) + '\\n')\n"
        "    sys.stdout.flush()\n"
    )

    async def _main(script: str) -> None:
        t = StdioTransport(sys.executable, [script], name="selftest")
        await t.start()
        try:
            assert await t.request("ping", {}, timeout=5) == {"pong": True}
            t.notify("some/notification")  # 无响应、不挂起
            try:
                await t.request("bad", timeout=5)
                raise AssertionError("expected JSONRPCError")
            except JSONRPCError as e:
                assert e.code == -32601 and e.message == "nope"
        finally:
            await t.close()
            await t.close()  # 幂等

    with tempfile.TemporaryDirectory() as _td:
        _path = Path(_td) / "srv.py"
        _path.write_text(_SERVER)
        asyncio.run(_main(str(_path)))

    print("openx/mcp/transport.py OK ✓")
