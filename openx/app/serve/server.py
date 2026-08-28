"""aiohttp 服务器与 serve 入口（openx serve，P4）。

create_app(session)：路由——
- ``GET /ws``                          WebSocket 事件流（下行广播 + 上行意图）
- ``GET /api/sessions``                会话列表（meta，供侧栏与复盘页）
- ``GET /api/sessions/{sid}/events``   复盘：统一事件列表（消息行 + 账本行投影）
- ``GET /``（静态前端 ``web/``）       自包含 vanilla JS 客户端

run_serve(agent, console, host, port, workspace)：由 main.py 在
``asyncio.run`` **内部**调用，因此用 ``AppRunner + TCPSite`` 而非
``web.run_app``（run_app 会再起一个事件循环）。流程：agent.startup() →
建 session（接线权限桥）→ 起站点 → 等 Ctrl-C/SIGTERM → finally
session.stop() + runner.cleanup() + agent.shutdown()。

复盘语义：转录事件（text/tool/thinking）当前不进账本，回放 = 会话文件里
的消息行 + 控制/决策账本行投影（``SessionStore.iter_events``），非逐字节
重播——与架构详设 §3.3 "回放=重发" 对齐需先把转录事件入账本（后续）。
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
import logging
import signal
from pathlib import Path

from aiohttp import web

from ...core.sessions import SessionStore, resolve_by_id
from .session import ServeSession

_log = logging.getLogger("openx.serve")

# 自包含前端目录（wheel 打包经 pyproject force-include 收录）
_WEB_DIR = Path(__file__).parent / "web"

# 应用级类型安全键（aiohttp 3.9+ web.AppKey，避免 NotAppKeyWarning）
SESSION_KEY = web.AppKey("serve_session", ServeSession)
WORKSPACE_KEY = web.AppKey("serve_workspace", str)


def create_app(session: ServeSession, workspace: str = "") -> web.Application:
    """构建 aiohttp 应用：/ws + REST 只读端点 + 静态前端。"""
    app = web.Application()
    app[SESSION_KEY] = session
    app[WORKSPACE_KEY] = workspace
    # 精确路由先注册，静态前缀兜底在最后（避免 /api、/ws 被静态吞掉）
    app.router.add_get("/ws", session.handle_ws)
    app.router.add_get("/api/sessions", _api_sessions)
    app.router.add_get("/api/sessions/{sid}/events", _api_session_events)
    app.router.add_get("/", _index)
    app.router.add_static("/static/", str(_WEB_DIR))
    return app


async def _index(request: web.Request) -> web.FileResponse:
    """GET / → index.html（自包含前端入口）。"""
    return web.FileResponse(_WEB_DIR / "index.html")


async def _api_sessions(request: web.Request) -> web.Response:
    """GET /api/sessions → 该工作区会话 meta 列表（updated_at 倒序）。"""
    metas = SessionStore.list_for_workspace(request.app[WORKSPACE_KEY])
    return web.json_response([
        {
            "session_id": m.session_id,
            "workspace": m.workspace,
            "model": m.model,
            "created_at": m.created_at,
            "updated_at": m.updated_at,
            "first_user_message": m.first_user_message,
            "total_input_tokens": m.total_input_tokens,
            "total_output_tokens": m.total_output_tokens,
        }
        for m in metas
    ])


async def _api_session_events(request: web.Request) -> web.Response:
    """GET /api/sessions/{sid}/events → 复盘：按文件序的统一事件列表。

    消息行原样；账本信封行投影为 ``{**payload, seq, ts, cause, origin}``。
    """
    workspace = request.app[WORKSPACE_KEY]
    sid = request.match_info["sid"]
    meta = resolve_by_id(workspace, sid)
    if meta is None or meta.path is None:
        raise web.HTTPNotFound(text=f"session not found: {sid}")
    return web.json_response({
        "session_id": sid,
        "workspace": meta.workspace,
        "events": SessionStore.iter_events(meta.path),
    })


async def run_serve(
    agent,
    console,
    host: str = "127.0.0.1",
    port: int = 8787,
    workspace: str = "",
) -> int:
    """启动 openx serve 并阻塞至 Ctrl-C；返回退出码。

    必须在 ``asyncio.run`` 内调用（main.py 的 serve 分支）。Ctrl-C /
    SIGTERM 触发干净收尾；所有清理失败只记日志，绝不抛出。
    """
    # MCP（Phase 9）：连接配置的 MCP servers（失败只警告、不阻塞）
    await agent.startup()
    session = ServeSession(agent, console)
    session.start()
    app = create_app(session, workspace=workspace)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"\n  OpenX serve →  http://{host}:{port}\n", flush=True)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass  # 非主线程 / 平台不支持信号 → 靠 KeyboardInterrupt 兜底

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            session.stop()
        except Exception:
            _log.exception("session stop failed")
        try:
            await runner.cleanup()
        except Exception:
            _log.exception("server cleanup failed")
        try:
            await agent.shutdown()
        except Exception:
            _log.exception("agent shutdown failed")
    return 0


if __name__ == "__main__":
    # 自检：路由齐全 + 静态前端文件在位（不起真服务）
    from .bridge import ServeConsole
    from .session import ServeSession

    class _FakeAgent:
        async def startup(self):
            pass

        async def shutdown(self):
            pass

        async def stream_run(self, text):
            if False:
                yield  # noqa: 让函数成为 async generator
                return

    app = create_app(ServeSession(_FakeAgent(), ServeConsole()), workspace="/tmp/x")
    routes = [r.resource.canonical for r in app.router.routes()]
    for expected in ("/ws", "/api/sessions", "/api/sessions/{sid}/events", "/"):
        assert expected in routes, f"missing route {expected}: {routes}"
    assert (_WEB_DIR / "index.html").is_file(), "web/index.html missing"
    print(f"routes ok ({len(routes)}): {sorted(routes)}")
    print("openx/app/serve/server.py OK ✓")
