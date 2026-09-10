"""aiohttp 服务器与 serve 入口（openx serve，P4）。

create_app(session)：路由——
- ``GET /ws``                          WebSocket 事件流（下行广播 + 上行意图）
- ``GET /api/sessions``                会话列表（meta，供侧栏与复盘页）
- ``GET /api/sessions/{sid}/events``   复盘：统一事件列表（消息行 + 账本行投影）
- ``DELETE /api/sessions/{sid}``       删除会话（当前活动会话 → 重绑新会话）
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
import json
import logging
import signal
from pathlib import Path

from aiohttp import web

from ...orchestration.sessions import SessionStore
from .api import WORKSPACE_KEY as API_WORKSPACE_KEY
from .api import WorkspaceRef, register_api, _reset_live_session
from .session import ServeSession
from .web_plugins import register_web_plugins

_log = logging.getLogger("openx.serve")

# 自包含前端目录（wheel 打包经 pyproject force-include 收录）
_WEB_DIR = Path(__file__).parent / "web"

# 应用级类型安全键（aiohttp 3.9+ web.AppKey，避免 NotAppKeyWarning）
SESSION_KEY = web.AppKey("serve_session", ServeSession)
#: 存 WorkspaceRef 盒子（app 启动后改键本身已弃用；切区只改盒子的 .path）
WORKSPACE_KEY = web.AppKey("serve_workspace", object)


def _active_workspace(request: web.Request) -> str:
    """当前 serve 工作区绝对路径串（解包 WorkspaceRef；兼容旧字符串键）。"""
    ref = request.app.get(WORKSPACE_KEY)
    if isinstance(ref, WorkspaceRef):
        return str(ref.path)
    return str(ref or "")


async def _api_dirs(request: web.Request) -> web.Response:
    """GET /api/dirs?path=… → 列出目录的直接子目录（目录选择器导航）。

    浏览器出于安全拿不到本地绝对路径，目录选择须由本地 serve 侧枚举。
    返回 ``{path, basename, parent, dirs:[{name, path}]}``；跳过点目录与
    符号链接。给定路径无效/缺失时回落当前活动工作区。
    """
    start = _active_workspace(request) or str(Path.home())
    raw = (request.query.get("path") or "").strip()
    candidate = Path(raw).expanduser().resolve() if raw else Path(start)
    if not candidate.is_dir():
        candidate = Path(start)
    dirs: list[dict] = []
    try:
        for child in sorted(candidate.iterdir(), key=lambda p: (p.name.lower())):
            if child.name.startswith("."):
                continue
            try:
                if child.is_dir() and not child.is_symlink():
                    dirs.append({"name": child.name, "path": str(child)})
            except OSError:
                continue
    except (PermissionError, OSError):
        dirs = []
    parent = candidate.parent
    return web.json_response({"ok": True, "data": {
        "path": str(candidate),
        "basename": candidate.name or str(candidate),
        "parent": str(parent) if parent != candidate else "",
        "dirs": dirs,
    }})


def create_app(session: ServeSession, workspace: str = "") -> web.Application:
    """构建 aiohttp 应用：/ws + REST 端点 + 静态前端。

    ``client_max_size``：aiohttp 默认 1 MiB 会卡掉 web 图片/文件上传，提到
    32 MiB（单文件另有 8 MiB 上限，见 api.MAX_UPLOAD_BYTES）。
    """
    app = web.Application(client_max_size=32 * 1024 * 1024)
    app[SESSION_KEY] = session
    app[WORKSPACE_KEY] = WorkspaceRef(workspace)
    # 精确路由先注册，静态前缀兜底在最后（避免 /api、/ws 被静态吞掉）
    app.router.add_get("/ws", session.handle_ws)
    app.router.add_get("/api/sessions", _api_sessions)
    app.router.add_get("/api/sessions/{sid}/events", _api_session_events)
    app.router.add_delete("/api/sessions/{sid}", _api_session_delete)
    app.router.add_get("/api/workspaces", _api_workspaces)
    app.router.add_post("/api/workspace/switch", _api_workspace_switch)
    app.router.add_get("/api/dirs", _api_dirs)
    # 管理端点（模型 / MCP / skill / plugin / 文件产物）——见 api.py
    register_api(app, session, workspace)
    # web 插件端点（发现/启停/静态 iframe 卡片）——见 web_plugins.py
    register_web_plugins(app)
    app.router.add_get("/", _index)
    # 静态前端：统一 no-store，杜绝浏览器缓存旧版 JS/CSS（前端零构建、改动即生效）
    app.router.add_get("/static/{name}", _static_file)
    return app


async def _static_file(request: web.Request) -> web.FileResponse:
    """GET /static/{name} → web/ 下文件；扁平目录 + no-store 防缓存旧稿。"""
    name = request.match_info.get("name", "")
    if not name or "/" in name or "\\" in name:
        raise web.HTTPNotFound()
    path = (_WEB_DIR / name).resolve()
    if not str(path).startswith(str(_WEB_DIR.resolve())) or not path.is_file():
        raise web.HTTPNotFound()
    resp = web.FileResponse(path)
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def _index(request: web.Request) -> web.FileResponse:
    """GET / → index.html（自包含前端入口）。"""
    return web.FileResponse(_WEB_DIR / "index.html")


def _session_meta_payload(m) -> dict:
    """一个 ``SessionMeta`` → 会话列表条目（/api/sessions 与 /api/workspaces 共用）。

    ``title`` = 首条用户消息（读侧回填，见 ``_load_meta_only``）——列表要
    显示"这个会话在聊什么"，session-id（``5732de02c8e3``）对人是噪声。
    空标题（新会话尚未发过消息）由前端回退。
    """
    return {
        "session_id": m.session_id,
        "title": m.first_user_message,
        "workspace": m.workspace,
        "model": m.model,
        "group": m.group,
        "created_at": m.created_at,
        "updated_at": m.updated_at,
        "first_user_message": m.first_user_message,
        "total_input_tokens": m.total_input_tokens,
        "total_output_tokens": m.total_output_tokens,
    }


async def _api_sessions(request: web.Request) -> web.Response:
    """GET /api/sessions → 该工作区会话 meta 列表（updated_at 倒序）。"""
    metas = SessionStore.list_for_workspace(_active_workspace(request))
    return web.json_response([_session_meta_payload(m) for m in metas])


async def _api_workspaces(request: web.Request) -> web.Response:
    """GET /api/workspaces → 侧栏「工作区」树：全部工作区及其会话。

    分组视图 ``[{workspace, active, sessions:[…]}]``：``active`` = 等于当前
    serve 工作区；排序活动组置首，其余按组内最新会话倒序（catalog 原生序）。
    会话条目形状与 /api/sessions 完全一致。
    """
    active = str(Path(_active_workspace(request)).resolve())
    groups: list[dict] = []
    for workspace, metas in SessionStore.catalog():
        groups.append({
            "workspace": workspace,
            "active": str(Path(workspace).resolve()) == active,
            "sessions": [_session_meta_payload(m) for m in metas],
        })
    groups.sort(key=lambda g: (not g["active"]))  # 稳定排序：活动组置首、其余保原序
    return web.json_response(groups)


async def _api_session_events(request: web.Request) -> web.Response:
    """GET /api/sessions/{sid}/events → 复盘：按文件序的统一事件列表。

    消息行原样；账本信封行投影为 ``{**payload, seq, ts, cause, origin}``。
    按 id **跨工作区**定位（``resolve_anywhere``）——侧栏可回放任意工作区
    会话（只读）；meta.workspace 让前端知道该会话原属目录。
    """
    sid = request.match_info["sid"]
    meta = SessionStore.resolve_anywhere(sid)
    if meta is None or meta.path is None:
        raise web.HTTPNotFound(text=f"session not found: {sid}")
    return web.json_response({
        "session_id": sid,
        "workspace": meta.workspace,
        "events": SessionStore.iter_events(meta.path),
    })


async def _api_session_delete(request: web.Request) -> web.Response:
    """DELETE /api/sessions/{sid} → 删除一条会话（侧栏会话项的删除按钮）。

    按 id **跨工作区**定位（同复盘端点），两种情形：

    - **历史会话**：直接删文件（append-only 转录，无其它引用）。
    - **当前活动会话**：先 ``_reset_live_session`` 重绑到同工作区的新会话，
      **再**删旧文件。顺序不可颠倒——先删的话，重绑窗口内一次 append 就会
      用 ``open("a")`` 把文件复活成无 meta 行的空壳（见 ``SessionStore
      .delete`` 的调用方契约）。响应回带新的 ``session_id``，前端据此同步。

    回合进行中删除当前会话 → 409：删除会换 session_id，进行中的回合会把
    消息写进一个已被摘掉的 store。
    """
    sid = request.match_info["sid"]
    meta = SessionStore.resolve_anywhere(sid)
    if meta is None or meta.path is None:
        return web.json_response(
            {"ok": False, "reason": f"session not found: {sid}"}, status=404
        )

    session = request.app[SESSION_KEY]
    agent = getattr(session, "agent", None)
    is_live = agent is not None and str(getattr(agent, "session_id", "") or "") == sid
    new_id = ""
    if is_live:
        if session.is_busy():
            return web.json_response(
                {"ok": False,
                 "reason": "a turn is in progress — stop it before deleting "
                           "the current session"},
                status=409,
            )
        try:
            new_id = _reset_live_session(session, _active_workspace(request))
        except Exception as exc:                    # 建文件 / 重绑失败 → 不删
            return web.json_response(
                {"ok": False, "reason": f"cannot reset current session: {exc}"},
                status=500,
            )

    if not SessionStore.delete(sid):
        return web.json_response(
            {"ok": False, "reason": f"session not found: {sid}"}, status=404
        )
    return web.json_response(
        {"ok": True, "data": {"deleted": sid, "live": is_live, "session_id": new_id}}
    )


async def _api_workspace_switch(request: web.Request) -> web.Response:
    """POST /api/workspace/switch → 切换 serve 当前工作区（live 重根 + 新会话）。

    body: ``{"workspace": "/abs/path"}``。镜像 CLI ``/workspace`` 的工具重绑
    先例 + 会话持久化重绑：在目标工作区新建一个会话文件，把 ``session_store``
    / ``session_id`` / 内核账本 / token 计数 / todos 全部指到新会话，随后更新
    两处 app 级 ``WORKSPACE_KEY``（server 与 api），广播 init 让已连客户端同步。

    **原子性纪律**：除首部一次 ``await``（读 body）外，校验 → 重绑 → 广播全
    程不再有 await——在单事件循环内 check/mutate 原子，杜绝与回合 worker 交错
    （队列里的消息若在切区后才消费，会在新工作区误执行）。
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"ok": False, "reason": "invalid json body"}, status=400)
    raw = (body or {}).get("workspace") if isinstance(body, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return web.json_response({"ok": False, "reason": "workspace required"}, status=400)

    candidate = Path(raw.strip()).resolve()
    if not candidate.is_dir():
        return web.json_response(
            {"ok": False, "reason": f"directory not found: {candidate}"}, status=404
        )
    # 目录选择是 Web 端用户的显式动作（等同在此跑过 openx）：允许切到**尚无
    # 会话**的目录——SessionStore.create 会重开首条会话（惰性：首条消息
    # 才落盘）。不再要求 has_sessions，以支持“新对话输入框选任意新目录”的
    # 体验。
    session = request.app[SESSION_KEY]
    if session.is_busy():
        return web.json_response(
            {"ok": False, "reason": "a turn is in progress — wait for it to finish first"},
            status=409,
        )
    agent = getattr(session, "agent", None)
    if agent is None:
        return web.json_response({"ok": False, "reason": "agent unavailable"}, status=500)
    tasks = getattr(agent, "tasks", None)
    try:
        active_bg = [h for h in tasks.all() if getattr(h, "running", False)]
    except Exception:
        active_bg = []
    if active_bg:
        return web.json_response(
            {"ok": False, "reason": "background tasks are running — stop them first"},
            status=409,
        )

    # ── 重绑（以下无 await）──────────────────────────────────
    session.discard_uploads()  # 离开旧工作区：其会话上传附件目录一并清理
    model = str(getattr(getattr(agent, "config", None), "model", "") or "")
    group = str(getattr(getattr(agent, "config", None), "active_group", "") or "")
    store = SessionStore.create(str(candidate), model, group=group)

    had_store = getattr(agent, "session_store", None)
    agent.config.workspace = str(candidate)
    agent.workspace = candidate
    agent.session_store = store
    agent.session_id = store.meta.session_id
    hooks = getattr(agent, "hooks", None)
    if hooks is not None:
        hooks.session_id = agent.session_id

    # 账本在 _build_tools() **之前**重挂：插件重载产生的组合事件落入新会话
    # 文件（与 OpenXAgent.__init__ 序一致）。无旧 store（测试/嵌入式）则跳过，
    # 保持 hermetic 测试不碰进程级 kernel。
    if had_store is not None:
        try:
            from ...kernel import get_kernel

            get_kernel().attach_ledger(
                store.append_event,
                session=agent.session_id,
                start_seq=store.ledger_start_seq(),  # 新建文件恒 0
            )
        except Exception:
            pass  # 账本是证据系统；挂接失败不阻断切换

    # 会话状态就地重置（保共享引用：todos 被 ToolHost/TodoWriteTool 共享）
    clear = getattr(agent, "clear_history", None)
    if callable(clear):
        clear()
    todos = getattr(agent, "todos", None)
    if isinstance(todos, list):
        todos.clear()
    for attr in (
        "total_input_tokens",
        "total_output_tokens",
        "total_cached_tokens",
        "total_plugin_tokens",
    ):
        if hasattr(agent, attr):
            setattr(agent, attr, 0)

    # 工具重根：ensure_loaded(新 workspace) 整体重载内核插件（同 CLI /workspace）
    agent.tools = agent._build_tools()
    agent.tool_schemas = agent._compute_tool_schemas()
    agent.reload_instructions()

    session._live_events = []
    session._live_user = None
    # 两处 app-key 各持 WorkspaceRef 盒子：只改 .path，不重设 App 键
    # （aiohttp 应用运行后改键已弃用）。盒缺失（老形态）才回落直接赋值。
    for key in (WORKSPACE_KEY, API_WORKSPACE_KEY):
        ref = request.app.get(key)
        if isinstance(ref, WorkspaceRef):
            ref.path = str(candidate)
        else:
            request.app[key] = str(candidate)

    try:
        from ...kernel import protocol

        session.broadcast(protocol.init_event(
            agent.session_id,
            model,
            sorted(getattr(agent, "tools", {}) or {}),
        ))
    except Exception:
        pass  # 广播失败不影响切换结果

    return web.json_response({"ok": True, "data": {
        "workspace": str(candidate),
        "session_id": agent.session_id,
        "model": model,
    }})


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

    def _request_stop() -> None:
        """收到停止信号：先把在途回合落成可续跑的 checkpoint，再停服务。

        次序是有意的--反序就是"先丢了状态再想存"。``flush_checkpoint`` 是
        同步落盘、不抛异常（失败只降级），所以它不会挡住关停。

        serve 用 ``add_signal_handler`` 而非 ``signal.signal``（事件循环
        线程内回调，符合 asyncio 惯例）；CLI 侧的 InterruptController 因此
        不需要在这里装信号--两处各管一段，互不干扰。
        """
        try:
            agent.flush_checkpoint("signal")
        except Exception:
            _log.debug("checkpoint flush on shutdown failed", exc_info=True)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            pass  # 非主线程 / 平台不支持信号 → 靠 KeyboardInterrupt 兜底

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        # 兜底路径（信号处理器装不上）：同样先落盘再退
        try:
            agent.flush_checkpoint("signal")
        except Exception:
            _log.debug("checkpoint flush on interrupt failed", exc_info=True)
    finally:
        # 关停前再兜一次：即使 stop 由其它路径触发，也不让在途进展丢掉
        try:
            agent.flush_checkpoint("signal")
        except Exception:
            _log.debug("checkpoint flush on shutdown failed", exc_info=True)
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
    for expected in (
        "/ws", "/api/sessions", "/api/sessions/{sid}/events",
        "/api/sessions/{sid}", "/",
    ):
        assert expected in routes, f"missing route {expected}: {routes}"
    assert (_WEB_DIR / "index.html").is_file(), "web/index.html missing"
    print(f"routes ok ({len(routes)}): {sorted(routes)}")
    print("openx/app/serve/server.py OK ✓")
