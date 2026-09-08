"""openx serve · 管理与资源 REST API（Web 控制台数据层）。

serve 原本只有三个只读端点（``/ws``、``/api/sessions``、复盘）。Web 端
要承担控制台职责——改模型、管插件、看产物——故补这组端点。

设计纪律：
- **settings.json 是唯一真源**：模型组 / MCP / skill / plugin 全部走
  ``OpenXConfig`` 与 ``skills`` 模块读写，不另立并行配置源（否则 Web 与
  CLI 会互相覆盖）。
- **工作区边界**：文件端点一律 ``resolve()`` 后校验落在 workspace 内，
  挡 ``../`` 穿越与符号链接逃逸。
- **秘密不出网**：apiKey 明文只回后 4 位；``env:VAR`` 引用原样返回（本身
  不含秘密）。前端回填空串 = "保持不变"。
- **失败即 4xx + reason**：写操作先校验再落盘，不做半改。
"""

from __future__ import annotations

import json
import mimetypes
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web

from ...config import OpenXConfig
from ... import model_groups as _mg
from ... import skills as _skills
from ...image import IMAGE_EXTENSIONS, image_to_base64_url
from ...orchestration.sessions import SessionStore
from .session import Upload

# ── 常量 ────────────────────────────────────────────────────────

#: 会产出文件的工具（产物面板的数据源）：从 tool_calls 入参抽 path
WRITE_TOOLS = frozenset({"write_file", "edit_file", "write_plugin"})

#: 文件树噪声目录（不展开、不展示）
IGNORE_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", "dist", "build", ".next",
    ".idea", ".vscode", ".DS_Store", "*.egg-info", ".openx",
})

#: 单层最大条目（防巨型仓库一次拖垮）
MAX_DIR_ENTRIES = 400

#: 文本预览上限（超出截断，前端提示"已截断"）
MAX_PREVIEW_BYTES = 512 * 1024

#: 原始文件（图片等）返回上限
MAX_RAW_BYTES = 8 * 1024 * 1024

#: 上传附件（web 对话附件）单文件上限：与 files_raw 预览上限一致
MAX_UPLOAD_BYTES = MAX_RAW_BYTES

#: 附件在工作区内的存放根（相对）：隐藏点目录，会话结束整目录删除
UPLOAD_REL_ROOT = ".openx/uploads"

_IMAGE_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico",
})

AGENT_KEY = web.AppKey("serve_agent", object)
WORKSPACE_KEY = web.AppKey("serve_api_workspace", object)
#: 本模块自持的 session 引用（切换模型后要广播一条提示，故需 session）
SESSION_KEY = web.AppKey("serve_api_session", object)


class WorkspaceRef:
    """可变工作区盒子：app 启动后改 App 键本身已被 aiohttp 弃用。

    serve 支持**运行时切换工作区**（web 侧栏）——App 键里存的必须是稳定
    引用（改键会触发 DeprecationWarning，未来版本可能禁止），切区只改
    ``.path``。读侧一律经 ``_workspace`` 解包，写侧在切区端点更新 ``.path``。
    """

    __slots__ = ("path",)

    def __init__(self, path: str = "") -> None:
        self.path = path


# ── 通用辅助 ────────────────────────────────────────────────────

def _ok(data: Any = None, **extra: Any) -> web.Response:
    """统一成功信封（前端只看 data，额外字段平铺进同一层）。"""
    body = {"ok": True}
    if data is not None:
        body["data"] = data
    body.update(extra)
    return web.json_response(body)


def _fail(reason: str, status: int = 400) -> web.Response:
    """统一失败信封。"""
    return web.json_response({"ok": False, "reason": reason}, status=status)


def _agent(request: web.Request) -> Any:
    return request.app[AGENT_KEY]


def _workspace(request: web.Request) -> Path:
    value = request.app.get(WORKSPACE_KEY)
    path = value.path if isinstance(value, WorkspaceRef) else str(value or "")
    return Path(path).resolve()


async def _body(request: web.Request) -> dict:
    """解析 JSON body；非法返回 {}（由调用方按缺字段报错）。"""
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_in_workspace(workspace: Path, rel: str) -> Path | None:
    """把相对路径解析到工作区内；越界/不存在返回 None。

    安全咽喉：先 join 再 resolve，最后校验前缀——挡 ``../`` 与符号链接
    逃逸（resolve 会展开 symlink，故必须在展开后再比对）。
    """
    if not isinstance(rel, str) or not rel:
        return workspace
    candidate = (workspace / rel).resolve()
    if candidate != workspace and workspace not in candidate.parents:
        return None
    return candidate


def _has_vision(agent: Any) -> bool:
    """是否有**独立**的视觉模型（modal 角色）承接图片回合。

    判定 = modal 角色解析出的 settings 与主绑定不同（``client_for("modal")``
    只在 distinct 时才新建 modal client；否则图会落到主模型）。探测失败按
    False——宁可让前端提示，也不让图片在无视觉时静默送错模型。
    """
    try:
        settings = agent.role_settings("openx-modal-model")
        return not bool(agent._same_binding(settings))
    except Exception:
        return False


def _sanitize_filename(name: str) -> str:
    """上传文件名清洗：只取 basename，剔除控制符与路径分隔符，限长。"""
    base = Path(name or "upload").name
    base = "".join(ch for ch in base if ch not in "/\\" and ord(ch) >= 32).strip()
    return (base or "upload.bin")[:120]


def _unique_upload_name(base: Path, name: str) -> str:
    """同目录重名时加短随机后缀（不覆盖已有上传）。"""
    if not (base / name).exists():
        return name
    p = Path(name)
    return f"{p.stem}-{uuid.uuid4().hex[:6]}{p.suffix}"


def _mask_secret(value: Any) -> tuple[str, bool]:
    """``(展示值, 是否明文)``——明文只留后 4 位，``env:`` 引用原样。

    Web 端要能编辑组配置，但不能把完整 key 送进浏览器（会被任何 XSS 或
    录屏带走）。故只回掩码；前端未改动时回空串表示"保持原值"。
    """
    if not isinstance(value, str) or not value:
        return "", False
    value = value.strip()
    if value.startswith("env:"):
        return value, False          # 环境引用本身不是秘密
    if len(value) <= 8:
        return "••••", True
    return f"••••{value[-4:]}", True


def _unmask_secret(incoming: str, existing: Any) -> str:
    """前端回填空串 / 掩码（未改动）→ 保留原值；否则用新值。"""
    if not isinstance(incoming, str):
        return ""
    incoming = incoming.strip()
    if not incoming or incoming.startswith("••"):
        return existing if isinstance(existing, str) else ""
    return incoming


def _tool_arguments(raw: Any) -> dict:
    """tool_calls 的 arguments（JSON 字符串或 dict）→ dict；坏值返回 {}。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _paths_from_tool_calls(name: str, arguments: Any) -> list[str]:
    """从一次写工具调用里抽出目标路径（可能是单 path 或 paths 列表）。"""
    if name not in WRITE_TOOLS:
        return []
    args = _tool_arguments(arguments)
    out: list[str] = []
    for key in ("path", "file_path", "filePath", "filename"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
    multi = args.get("paths")
    if isinstance(multi, list):
        out.extend(p for p in multi if isinstance(p, str) and p.strip())
    return out


#: 下行展示短串上限（shell 命令 / 长路径都可能很肥，事件面不扛全量）
_DISPLAY_LIMIT = 120


def _shorten(text: str) -> str:
    return text if len(text) <= _DISPLAY_LIMIT else text[: _DISPLAY_LIMIT - 1] + "…"


def tool_display(name: str, arguments: Any) -> tuple[str, str]:
    """一次工具调用 → ``(args_summary, target)``：给右栏的**派生**短串。

    - ``args_summary``：一行人类可读摘要（shell 取命令前三词，其余优先取
      搜索模式、其次路径）——任务流步骤的副标题；
    - ``target``：路径型目标（读 / 写 / glob / 列目录），非路径型工具为空串
      ——上下文面板据此收录「本会话碰过哪些文件」。

    与 ``_paths_from_tool_calls`` 的分工：那里是**产物**（只认写工具、可能
    多路径、单发 artifact 事件）；这里是**上下文**（读过也算、只取主目标、
    随 ``tool_use`` 同行下发）。

    只做只读派生，**绝不原样回传入参**——``write_file`` 的 ``content`` 可能
    含整个文件。坏 JSON / 缺字段 → 空串，绝不抛：展示字段不得拖垮事件面。
    """
    args = _tool_arguments(arguments)
    if name == "shell":
        words = str(args.get("command", "") or "").strip().split()
        return (_shorten(" ".join(words[:3])), "")

    summary = ""
    target = ""
    # 摘要优先取 pattern（grep/glob 的模式比搜索根目录更说明问题）
    for key in ("pattern", "path", "file_path", "filePath", "filename"):
        value = args.get(key)
        if not (isinstance(value, str) and value.strip()):
            continue
        text = _shorten(value.strip())
        if not summary:
            summary = text
        # 上下文只收路径型目标；glob 的模式本身就是路径通配，算路径型
        if not target and (key != "pattern" or name == "glob"):
            target = text
        if summary and target:
            break
    return (summary, target)


# ── 会话：信息 / 新建 ───────────────────────────────────────────

async def session_info(request: web.Request) -> web.Response:
    """GET /api/info → 顶栏要展示的环境事实（工作区 / 会话 / 模型）。

    工作区不在 init 事件里（init 只带会话与工具），而侧栏会话列表要靠
    workspace 过滤——故单开一个端点，避免前端从会话列表"猜"工作区。
    """
    agent = _agent(request)
    return _ok({
        "workspace": str(_workspace(request)),
        "session_id": str(getattr(agent, "session_id", "") or ""),
        "model": str(getattr(getattr(agent, "config", None), "model", "") or ""),
        "tools": sorted(getattr(agent, "tools", {}) or {}),
        # 供前端判断图片附件是否会被模型真正“看到”（无独立视觉 → 提示）
        "has_vision": _has_vision(agent),
    })


def _reset_live_session(session: Any, workspace: str) -> str:
    """在当前工作区新建一个空会话并整体重绑 agent；返回新 session_id。

    与 ``_api_workspace_switch`` 的重绑段同源，**唯一差异是不重建工具**——
    工作区没变，工具无需重根（省一次内核重载，也避免打断进行中的插件）。

    重绑面：session_store / session_id / hooks.session_id / 账本挂载 /
    历史与 todos / token 计数 / live 缓冲，最后广播 init 让已连客户端同步
    （前端侧栏据此高亮新会话）。新会话文件惰性创建（``SessionStore
    .create``）：不发言不落盘——空白会话不保存。
    """
    # 会话结束：清掉本会话的上传附件（新对话=丢当前上下文）
    session.discard_uploads()
    agent = session.agent
    model = str(getattr(getattr(agent, "config", None), "model", "") or "")
    group = str(getattr(getattr(agent, "config", None), "active_group", "") or "")
    store = SessionStore.create(workspace, model, group=group)
    had_store = getattr(agent, "session_store", None)

    agent.session_store = store
    agent.session_id = store.meta.session_id
    hooks = getattr(agent, "hooks", None)
    if hooks is not None:
        hooks.session_id = agent.session_id

    # 账本重挂到新会话文件（无旧 store 的嵌入式/测试场景跳过，保持 hermetic）
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

    session._live_events = []
    session._live_user = None
    try:
        from ...kernel import protocol

        session.broadcast(protocol.init_event(
            agent.session_id, model, sorted(getattr(agent, "tools", {}) or {})
        ))
    except Exception:
        pass  # 广播失败不影响重绑结果
    return str(agent.session_id)


async def session_new(request: web.Request) -> web.Response:
    """POST /api/session/new → 清空上下文，重开一个新会话（惰性落盘）。

    serve 是单 agent 长存进程，"新建会话"= 丢弃当前对话历史 + 整体重绑
    到新会话（**不重启进程**）：旧的转录仍在盘上（按旧 id 存），新消息
    归到新 id 下。经 ``_reset_live_session`` 与"删除当前会话"的重绑完全
    同源——session_store / 账本 / todos / token 计数一并换新（此前只换
    session_id 不换 store，新对话的消息会错写进旧会话文件）；新会话文件
    惰性创建，不发言不落盘。

    注意：不碰 activeGroup / MCP 连接——那属于配置，不属于会话。
    """
    session = request.app.get(SESSION_KEY)
    agent = _agent(request)
    if session is None:
        return _fail("serve session unavailable", status=500)
    try:
        new_id = _reset_live_session(session, str(_workspace(request)))
    except Exception as exc:                          # 上下文重置失败不应挂页
        return _fail(f"cannot reset session: {exc}", status=500)
    return _ok({
        "session_id": new_id,
        "model": str(getattr(getattr(agent, "config", None), "model", "") or ""),
    })


# ── 模型（modelGroups）──────────────────────────────────────────

def _describe_group(name: str, raw: dict) -> dict:
    """一个模型组 → 前端视图（脱敏 + 角色短别名 + main 回落）。"""
    if not isinstance(raw, dict):
        raw = {}
    main_raw = raw.get(_mg.MAIN_ROLE)
    main_model = ""
    if isinstance(main_raw, str):
        main_model = main_raw
    elif isinstance(main_raw, dict):
        main_model = str(main_raw.get("model") or "")

    roles: dict[str, str] = {}
    for role_key in _mg.ROLE_KEYS:
        value = raw.get(role_key)
        if isinstance(value, str):
            roles[_mg.role_short(role_key)] = value
        elif isinstance(value, dict):
            roles[_mg.role_short(role_key)] = str(value.get("model") or "")

    api_key, is_plain = _mask_secret(raw.get("apiKey", raw.get("api_key")))
    return {
        "name": name,
        "kind": raw.get("kind") or "",
        "apiKey": api_key,
        "apiKeyIsPlain": is_plain,
        "apiBase": raw.get("apiBase", raw.get("api_base")) or "",
        "temperature": raw.get("temperature"),
        "max_tokens": raw.get("max_tokens"),
        "main": main_model,           # 列表页主展示
        "roles": roles,               # main/exec/mini/modal 短别名
    }


async def models_get(request: web.Request) -> web.Response:
    """GET /api/models → 全部模型组（脱敏） + 当前绑定。

    ``current`` 取 agent 运行时状态（而非仅 settings）——Web 上看到的就是
    下一轮真正会用的模型。
    """
    agent = _agent(request)
    warnings: list[str] = []
    try:
        raw = OpenXConfig.load_model_groups_raw()
        _groups, active, warnings = OpenXConfig.load_model_groups()
    except Exception as exc:                       # 配置损坏不应打挂页面
        return _ok({"groups": [], "active": "", "warnings": [str(exc)], "current": {}})

    groups = [
        _describe_group(name, g)
        for name, g in sorted(raw.items())
        if isinstance(g, dict)
    ]
    current = {
        "group": getattr(agent, "_group_name", "") or active or "",
        "role": _mg.role_short(getattr(agent, "_bind_role", "") or _mg.MAIN_ROLE),
        "model": str(getattr(getattr(agent, "config", None), "model", "") or ""),
    }
    return _ok({"groups": groups, "active": active, "warnings": warnings,
                "current": current})


async def models_save(request: web.Request) -> web.Response:
    """POST /api/models → 新增或更新一个模型组。

    body: ``{name, group: {...原始组配置}}``。组名走 ``GROUP_NAME_RE``；
    main 角色必填（无 main 的组启动即不可用）。apiKey 留空/掩码 = 保持原值。
    """
    body = await _body(request)
    name = str(body.get("name") or "").strip()
    incoming = body.get("group")
    if not name:
        return _fail("group name required")
    if not _mg.validate_group_name(name):
        return _fail("invalid group name (allowed: A-Z a-z 0-9 . _ -)")
    if not isinstance(incoming, dict):
        return _fail("group config must be an object")

    raw = OpenXConfig.load_model_groups_raw()
    existing = raw.get(name) if isinstance(raw.get(name), dict) else {}

    group: dict[str, Any] = {}
    for key in ("kind", "temperature", "max_tokens", "max_retries", "retry_base_delay"):
        if key in incoming and incoming[key] not in (None, ""):
            group[key] = incoming[key]
    api_base = str(incoming.get("apiBase") or incoming.get("api_base") or "").strip()
    if api_base:
        group["apiBase"] = api_base
    # 秘密：空/掩码 → 保留原值（前端不回传完整 key）
    new_key = _unmask_secret(
        str(incoming.get("apiKey") or incoming.get("api_key") or ""),
        existing.get("apiKey", existing.get("api_key")),
    )
    if new_key:
        group["apiKey"] = new_key

    # 角色：四个长键，值为模型字符串或对象
    roles_in = incoming.get("roles")
    if not isinstance(roles_in, dict):
        roles_in = incoming  # 兼容直接给长键的形态
    for short, role_key in _mg.ROLE_ALIASES.items():
        value = roles_in.get(short, roles_in.get(role_key))
        if value in (None, ""):
            continue
        if isinstance(value, dict):
            model = str(value.get("model") or "").strip()
            if not model:
                continue
            entry: dict[str, Any] = {"model": model}
            for k in ("kind", "temperature", "max_tokens"):
                if value.get(k) not in (None, ""):
                    entry[k] = value[k]
            rk = _unmask_secret(str(value.get("apiKey") or value.get("api_key") or ""),
                                (existing.get(role_key) or {}).get("apiKey")
                                if isinstance(existing.get(role_key), dict) else None)
            if rk:
                entry["apiKey"] = rk
            rb = str(value.get("apiBase") or value.get("api_base") or "").strip()
            if rb:
                entry["apiBase"] = rb
            group[role_key] = entry
        else:
            model = str(value).strip()
            if model:
                group[role_key] = model

    if _mg.MAIN_ROLE not in group:
        return _fail("main role model is required")

    try:
        _mg.parse_group(name, group)     # 落盘前先解析一遍，坏配置不写盘
    except ValueError as exc:
        return _fail(str(exc))

    raw[name] = group
    try:
        OpenXConfig.save_model_groups(raw)
    except OSError as exc:
        return _fail(f"cannot write settings: {exc}", status=500)

    # 首个组自动激活（否则 is_configured 仍为 False，页面看着像没配）
    if not OpenXConfig._load_full_settings().get("activeGroup"):
        OpenXConfig.set_active_group(name)
    return _ok({"name": name, "group": _describe_group(name, group)})


async def models_delete(request: web.Request) -> web.Response:
    """DELETE /api/models/{name} → 删除模型组（禁止删最后一个）。"""
    agent = _agent(request)
    name = request.match_info["name"]
    raw = OpenXConfig.load_model_groups_raw()
    if name not in raw:
        return _fail(f"group not found: {name}", status=404)
    if len(raw) <= 1:
        return _fail("cannot delete the last model group")

    current_group = getattr(agent, "_group_name", "")
    del raw[name]
    OpenXConfig.save_model_groups(raw)

    # 删的是激活组 → activeGroup 指到剩下的第一个；agent 还绑着它也一起切
    settings_active = OpenXConfig._load_full_settings().get("activeGroup") or ""
    if settings_active == name or current_group == name:
        fallback = next(iter(raw), "")
        if fallback:
            OpenXConfig.set_active_group(fallback)
            if current_group == name:
                try:
                    agent.switch_group(fallback)
                except Exception:
                    pass      # 切换失败不回滚删除（配置已一致，重启即生效）
    return _ok({"deleted": name})


async def models_switch(request: web.Request) -> web.Response:
    """POST /api/models/switch → 切换会话模型。

    body: ``{group, role?, model?}``
    - 只给 ``group``：整组切换（重建 llm + 持久化 activeGroup）
    - 给 ``role`` + ``model``：只改当前组该角色的模型并重建绑定

    切换影响全局（serve 是单 agent 共享会话），故成功后广播一条 meta 提示。
    """
    agent = _agent(request)
    body = await _body(request)
    group = str(body.get("group") or "").strip()
    role = str(body.get("role") or "").strip()
    model = str(body.get("model") or "").strip()

    try:
        if model and role:
            role_key = _mg.canonical_role(role)
            if role_key is None:
                return _fail(f"unknown role: {role}")
            # 角色模型属于某组：显式给了组就先切组，否则改当前组
            if group and group != getattr(agent, "_group_name", ""):
                if not agent.switch_group(group):
                    return _fail(f"cannot switch to group: {group}")
                OpenXConfig.set_active_group(group)
            if not agent.set_role_model(role, model):
                return _fail(f"cannot set {role} model")
        elif group:
            if not agent.switch_group(group):
                return _fail(f"cannot switch to group: {group}")
            OpenXConfig.set_active_group(group)
        else:
            return _fail("group or (role + model) required")
    except Exception as exc:                        # 重建 llm 可能抛（凭据坏等）
        return _fail(f"switch failed: {type(exc).__name__}: {exc}", status=500)

    current = {
        "group": getattr(agent, "_group_name", "") or "",
        "role": _mg.role_short(getattr(agent, "_bind_role", "") or _mg.MAIN_ROLE),
        "model": str(getattr(getattr(agent, "config", None), "model", "") or ""),
    }
    session = request.app.get(SESSION_KEY)
    if session is not None and hasattr(session, "broadcast"):
        try:
            from ...kernel import protocol
            session.broadcast(protocol.text_delta(
                f"⟳ 模型已切换 → {current['group']}"
                + (f":{current['role']}" if role else "")
                + (f" ({current['model']})" if current["model"] else "")
            ))
        except Exception:
            pass                                    # 提示失败不影响切换结果
    return _ok({"current": current})


# ── MCP ─────────────────────────────────────────────────────────

def _mcp_status(agent: Any) -> dict[str, str]:
    """已连接 server → "connected"/"failed" 等状态串（best-effort）。"""
    manager = getattr(agent, "mcp", None)
    if manager is None:
        return {}
    try:
        lines = manager.status() or []
    except Exception:
        return {}
    status: dict[str, str] = {}
    for line in lines:
        if not isinstance(line, str) or ":" not in line:
            continue
        name, _, rest = line.partition(":")
        status[name.strip()] = rest.strip()
    return status


def _mcp_tools(agent: Any) -> dict[str, list[str]]:
    """server 名 → 它提供的工具名（从全局工具表按前缀归属，best-effort）。"""
    manager = getattr(agent, "mcp", None)
    tools_map: dict[str, list[str]] = {}
    tools = getattr(manager, "tools", None) if manager is not None else None
    if not isinstance(tools, dict):
        return tools_map
    for tool_name in tools:
        # MCP 工具名形如 "server__tool"；取前缀归属 server
        server = tool_name.split("__", 1)[0] if "__" in tool_name else ""
        if server:
            tools_map.setdefault(server, []).append(tool_name)
    return tools_map


async def mcp_get(request: web.Request) -> web.Response:
    """GET /api/mcp → 配置的 server + 连接状态 + 提供的工具。"""
    agent = _agent(request)
    servers = OpenXConfig.load_mcp_servers() or {}
    status = _mcp_status(agent)
    tools = _mcp_tools(agent)
    items = []
    for name, cfg in sorted(servers.items()):
        cfg = cfg if isinstance(cfg, dict) else {}
        items.append({
            "name": name,
            "command": cfg.get("command") or "",
            "args": cfg.get("args") or [],
            "env": list((cfg.get("env") or {}).keys()),   # 只回键名，不回值
            "status": status.get(name, "not connected"),
            "tools": tools.get(name, []),
        })
    return _ok({"servers": items})


async def mcp_save(request: web.Request) -> web.Response:
    """POST /api/mcp → 新增/更新一个 MCP server（需要 command）。"""
    body = await _body(request)
    name = str(body.get("name") or "").strip()
    cfg = body.get("config")
    if not name:
        return _fail("server name required")
    if not isinstance(cfg, dict):
        return _fail("config must be an object")
    if not str(cfg.get("command") or "").strip():
        return _fail("command is required (only stdio transport is supported)")

    clean = {
        "command": str(cfg["command"]).strip(),
        "args": [str(a) for a in (cfg.get("args") or [])],
    }
    env = cfg.get("env")
    if isinstance(env, dict) and env:
        clean["env"] = {str(k): str(v) for k, v in env.items()}

    try:
        OpenXConfig.save_mcp_server(name, clean)
    except OSError as exc:
        return _fail(f"cannot write settings: {exc}", status=500)
    return _ok({"name": name, "restartRequired": True})


async def mcp_delete(request: web.Request) -> web.Response:
    """DELETE /api/mcp/{name} → 删除一个 MCP server 配置。"""
    name = request.match_info["name"]
    if not OpenXConfig.delete_mcp_server(name):
        return _fail(f"server not found: {name}", status=404)
    return _ok({"deleted": name, "restartRequired": True})


# ── skill ───────────────────────────────────────────────────────

async def skills_get(request: web.Request) -> web.Response:
    """GET /api/skills → 全局 + 项目级 skill 清单。"""
    workspace = _workspace(request)
    try:
        found = _skills.load_skills(workspace)
    except Exception as exc:
        return _fail(f"cannot load skills: {exc}", status=500)
    items = [
        {
            "name": s.name,
            "description": s.description,
            "trigger": list(s.trigger),
            "level": s.level,
            "source": s.source,
            "content": s.content,          # 编辑用（前端按需展示）
        }
        for s in sorted(found.values(), key=lambda s: (s.level, s.name))
    ]
    return _ok({"skills": items})


async def skills_save(request: web.Request) -> web.Response:
    """POST /api/skills → 安装/更新一个 skill（内容为 SKILL.md 正文）。"""
    workspace = _workspace(request)
    body = await _body(request)
    name = str(body.get("name") or "").strip()
    content = str(body.get("content") or "").strip()
    if not name:
        return _fail("skill name required")
    if not content:
        return _fail("skill content required")
    level = str(body.get("level") or "global").strip()
    global_install = level != "project"

    trigger = body.get("trigger")
    if isinstance(trigger, str):
        trigger = [t.strip() for t in trigger.split(",") if t.strip()]
    if not isinstance(trigger, list):
        trigger = []

    try:
        _skills.install_skill_from_content(
            name=name,
            description=str(body.get("description") or "").strip(),
            content=content,
            trigger=trigger,
            workspace=workspace,
            global_install=global_install,
        )
    except (OSError, ValueError) as exc:
        return _fail(f"cannot install skill: {exc}", status=500)
    return _ok({"name": name, "level": "global" if global_install else "project"})


async def skills_delete(request: web.Request) -> web.Response:
    """DELETE /api/skills/{name} → 卸载一个 skill（项目级优先）。"""
    workspace = _workspace(request)
    name = request.match_info["name"]
    try:
        if not _skills.uninstall_skill(name, workspace=workspace):
            return _fail(f"skill not found: {name}", status=404)
    except OSError as exc:
        return _fail(f"cannot uninstall skill: {exc}", status=500)
    return _ok({"deleted": name})


# ── plugin（微内核清单 + 启停）──────────────────────────────────

async def plugins_get(request: web.Request) -> web.Response:
    """GET /api/plugins → 内核插件清单（phase / 注册面 / 禁用表）。"""
    try:
        from ...kernel import get_kernel
        kernel = get_kernel()
        inventory = kernel.inventory()
    except Exception as exc:
        return _fail(f"cannot read plugin inventory: {exc}", status=500)

    disabled = set((OpenXConfig.load_plugin_settings() or {}).get("disabled") or [])
    items = []
    for info in inventory:
        items.append({
            "id": info.id,
            "source": info.source,
            "phase": info.phase,                 # pending/loading/active/failed/disabled
            "builtin": bool(info.builtin),
            "error": info.error or "",
            "warnings": list(info.warnings or []),
            "tools": list(info.tools or []),
            "commands": list(info.commands or []),
            "contexts": list(info.contexts or []),
            "lifecycle": list(info.lifecycle or []),
            "ui_slots": list(info.ui_slots or []),
            "summary": info.summary or "",
            "disabled": info.id in disabled,
        })
    return _ok({"plugins": items, "disabled": sorted(disabled)})


async def plugins_toggle(request: web.Request) -> web.Response:
    """POST /api/plugins/{id}/toggle → 启用/禁用（写 plugins.disabled）。

    内置插件（builtin）失败即致命，禁用表对其无效——显式拒绝，避免给出
    "操作成功但没生效"的错觉。
    """
    plugin_id = request.match_info["id"]
    body = await _body(request)
    disabled_flag = body.get("disabled")
    if not isinstance(disabled_flag, bool):
        return _fail("disabled (bool) required")

    try:
        from ...kernel import get_kernel
        inventory = {p.id: p for p in get_kernel().inventory()}
    except Exception as exc:
        return _fail(f"cannot read plugin inventory: {exc}", status=500)
    info = inventory.get(plugin_id)
    if info is None:
        return _fail(f"plugin not found: {plugin_id}", status=404)
    if getattr(info, "builtin", False):
        return _fail("builtin plugins cannot be disabled")

    settings = OpenXConfig.load_plugin_settings() or {}
    disabled = set(settings.get("disabled") or [])
    if disabled_flag:
        disabled.add(plugin_id)
    else:
        disabled.discard(plugin_id)

    data = OpenXConfig._load_full_settings()
    data["plugins"] = {**(settings or {}), "disabled": sorted(disabled)}
    try:
        OpenXConfig._save_full_settings(data)
    except OSError as exc:
        return _fail(f"cannot write settings: {exc}", status=500)
    return _ok({"id": plugin_id, "disabled": disabled_flag, "restartRequired": True})


# ── 上传附件（web 对话图片/文件）──────────────────────────────


async def upload_create(request: web.Request) -> web.Response:
    """POST /api/upload（multipart ``file``）→ 存附件并登记，返回描述符。

    - **图片**（raster 后缀）→ 只存 base64 data-url 于 session 内存注册表，
      不落盘（项目策略：base64 图片绝不写磁盘）；模型以 ``image_url`` 承接。
    - **其它文件** → 写入 ``<workspace>/.openx/uploads/<session_id>/``，随
      会话结束整体删除；模型用 read_file 按相对路径读取。

    返回 ``{id, name, size, kind, mime, relPath}``，消息上行用 ``id`` 引用。
    """
    session = request.app.get(SESSION_KEY)
    agent = _agent(request)
    workspace = _workspace(request)
    if session is None or not workspace.is_dir():
        return _fail("upload unavailable", status=500)
    try:
        post = await request.post()
    except Exception as exc:               # 巨型/畸形 multipart → 413 而非 500
        return _fail(f"cannot read upload: {exc}", status=413)
    field = post.get("file")
    if field is None or getattr(field, "file", None) is None:
        return _fail("missing file field", status=400)
    name = _sanitize_filename(str(getattr(field, "filename", "") or "upload"))
    try:
        raw = field.file.read()
    except (OSError, ValueError) as exc:
        return _fail(f"cannot read upload: {exc}", status=400)
    if not raw:
        return _fail("empty file", status=400)
    if len(raw) > MAX_UPLOAD_BYTES:
        return _fail("file too large", status=413)

    mime, _ = mimetypes.guess_type(name)
    mime = mime or "application/octet-stream"
    if Path(name).suffix.lower() in IMAGE_EXTENSIONS:
        upload = Upload(
            name=name, size=len(raw), kind="image", mime=mime,
            data_url=image_to_base64_url(raw, mime),
        )
    else:
        sid = str(getattr(agent, "session_id", "") or "anon")
        base = workspace / UPLOAD_REL_ROOT / sid
        try:
            base.mkdir(parents=True, exist_ok=True)
            target = base / _unique_upload_name(base, name)
            target.write_bytes(raw)
        except OSError as exc:
            return _fail(f"cannot store upload: {exc}", status=500)
        upload = Upload(
            name=target.name, size=len(raw), kind="file", mime=mime,
            path=str(target),
            rel_path=str(target.relative_to(workspace)).replace("\\", "/"),
        )
    session.register_upload(upload)
    return _ok({
        "id": upload.id,
        "name": upload.name,
        "size": upload.size,
        "kind": upload.kind,
        "mime": upload.mime,
        "relPath": upload.rel_path,
    })


async def upload_delete(request: web.Request) -> web.Response:
    """DELETE /api/upload/<id> → 撤销一次待发上传（删盘 + 出注册表）。"""
    session = request.app.get(SESSION_KEY)
    upload_id = request.match_info.get("upload_id", "")
    if session is None:
        return _fail("upload unavailable", status=500)
    if not session.remove_upload(upload_id):
        return _fail("unknown upload", status=404)
    return _ok({"deleted": upload_id})


# ── 文件树 / 产物 ───────────────────────────────────────────────

def _is_ignored(path: Path) -> bool:
    name = path.name
    if name in IGNORE_DIRS:
        return True
    return name.endswith(".egg-info") or name.startswith(".")


async def files_get(request: web.Request) -> web.Response:
    """GET /api/files?path=<rel> → 单层目录条目（懒加载）。

    噪声目录不展开；超 MAX_DIR_ENTRIES 截断并回 ``truncated``。
    """
    workspace = _workspace(request)
    rel = request.rel_url.query.get("path", "")
    target = _resolve_in_workspace(workspace, rel)
    if target is None:
        return _fail("path escapes workspace", status=403)
    if not target.is_dir():
        return _fail(f"not a directory: {rel}", status=404)

    entries: list[dict] = []
    truncated = False
    try:
        children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        return _fail(f"cannot read directory: {exc}", status=500)

    for child in children:
        if len(entries) >= MAX_DIR_ENTRIES:
            truncated = True
            break
        if _is_ignored(child):
            continue
        try:
            is_dir = child.is_dir()
            size = 0 if is_dir else child.stat().st_size
        except OSError:
            continue                                  # 断裂符号链接等
        entries.append({
            "name": child.name,
            "path": str(child.relative_to(workspace)),
            "isDir": is_dir,
            "size": size,
        })
    return _ok({"path": str(target.relative_to(workspace)) if target != workspace else "",
                "entries": entries, "truncated": truncated})


async def files_content(request: web.Request) -> web.Response:
    """GET /api/files/content?path=<rel> → 文本预览（超上限截断）。"""
    workspace = _workspace(request)
    rel = request.rel_url.query.get("path", "")
    target = _resolve_in_workspace(workspace, rel)
    if target is None:
        return _fail("path escapes workspace", status=403)
    if not target.is_file():
        return _fail(f"not a file: {rel}", status=404)
    if target.suffix.lower() in _IMAGE_SUFFIXES:
        return _ok({"path": rel, "isImage": True, "text": ""})

    try:
        raw = target.read_bytes()
    except OSError as exc:
        return _fail(f"cannot read file: {exc}", status=500)
    truncated = len(raw) > MAX_PREVIEW_BYTES
    if truncated:
        raw = raw[:MAX_PREVIEW_BYTES]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _ok({"path": rel, "binary": True, "text": "", "truncated": False})
    return _ok({"path": rel, "text": text, "truncated": truncated,
                "language": _language_of(target)})


async def files_raw(request: web.Request) -> web.StreamResponse:
    """GET /api/files/raw?path=<rel> → 原始字节（图片等二进制预览）。"""
    workspace = _workspace(request)
    rel = request.rel_url.query.get("path", "")
    target = _resolve_in_workspace(workspace, rel)
    if target is None:
        return _fail("path escapes workspace", status=403)
    if not target.is_file():
        return _fail(f"not a file: {rel}", status=404)
    try:
        if target.stat().st_size > MAX_RAW_BYTES:
            return _fail("file too large to preview", status=413)
        data = target.read_bytes()
    except OSError as exc:
        return _fail(f"cannot read file: {exc}", status=500)
    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return web.Response(body=data, content_type=ctype)


def _language_of(path: Path) -> str:
    """文件后缀 → 预览用的语言标识（前端做轻量高亮）。"""
    return {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".tsx": "typescript", ".jsx": "javascript", ".json": "json",
        ".md": "markdown", ".sh": "bash", ".yaml": "yaml", ".yml": "yaml",
        ".toml": "toml", ".html": "html", ".css": "css", ".sql": "sql",
        ".go": "go", ".rs": "rust", ".java": "java", ".c": "c",
        ".cpp": "cpp", ".rb": "ruby", ".php": "php",
    }.get(path.suffix.lower(), "")


async def artifacts_get(request: web.Request) -> web.Response:
    """GET /api/artifacts?session=<id> → 该会话产出/修改过的文件。

    OpenX 内核没有 artifact 概念（会话只存消息 + 账本），产物是**读侧派
    生**：从消息流里 assistant 的 ``tool_calls`` 抽写类工具的 path 参数。

    - 有 ``session``：读该会话文件（历史复盘）
    - 无 ``session``：读当前 agent 内存历史（进行中的会话）
    """
    workspace = _workspace(request)
    session_id = request.rel_url.query.get("session", "").strip()
    items: list[dict] = []
    seen: set[str] = set()

    def add(tool: str, rel: str, root: Path) -> None:
        rel = rel.strip().replace("\\", "/")
        if not rel or rel in seen:
            return
        seen.add(rel)
        target = _resolve_in_workspace(root, rel)
        exists = bool(target and target.exists())
        items.append({
            "path": rel,
            "tool": tool,
            "exists": exists,
            "size": target.stat().st_size if (target and exists) else 0,
            "isImage": target.suffix.lower() in _IMAGE_SUFFIXES if target else False,
        })

    if session_id:
        # 复盘路径：会话可能属于**其它工作区**（侧栏跨区回放）——产物按该
        # 会话原属工作区根解析，绝不按活动根（否则 404 / 错文件 / 越界 403）。
        from ...orchestration.sessions import SessionStore
        meta = SessionStore.resolve_anywhere(session_id)
        if meta is None or meta.path is None:
            return _fail(f"session not found: {session_id}", status=404)
        root = Path(meta.workspace)
        for ev in SessionStore.iter_events(meta.path):
            if not isinstance(ev, dict) or ev.get("type") != "message":
                continue
            msg = ev.get("message")
            if not isinstance(msg, dict):
                continue
            for call in (msg.get("tool_calls") or []):
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                name = str(fn.get("name") or "")
                for p in _paths_from_tool_calls(name, fn.get("arguments")):
                    add(name, p, root)
    else:
        agent = _agent(request)
        history = getattr(agent, "history", None)
        for msg in (getattr(history, "messages", None) or []):
            if not isinstance(msg, dict):
                continue
            for call in (msg.get("tool_calls") or []):
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                name = str(fn.get("name") or "")
                for p in _paths_from_tool_calls(name, fn.get("arguments")):
                    add(name, p, workspace)

    return _ok({"session_id": session_id, "artifacts": items})


# ── 路由注册 ────────────────────────────────────────────────────

def register_api(app: web.Application, session: Any, workspace: str) -> None:
    """把管理端点挂到 app（server.create_app 调用）。

    精确路由必须在静态前缀兜底之前注册（否则 /api/* 被静态吞掉）。
    """
    app[AGENT_KEY] = session.agent
    app[WORKSPACE_KEY] = WorkspaceRef(workspace)
    app[SESSION_KEY] = session
    app.router.add_get("/api/info", session_info)
    app.router.add_post("/api/session/new", session_new)

    app.router.add_get("/api/models", models_get)
    app.router.add_post("/api/models", models_save)
    app.router.add_delete("/api/models/{name}", models_delete)
    app.router.add_post("/api/models/switch", models_switch)

    app.router.add_get("/api/mcp", mcp_get)
    app.router.add_post("/api/mcp", mcp_save)
    app.router.add_delete("/api/mcp/{name}", mcp_delete)

    app.router.add_get("/api/skills", skills_get)
    app.router.add_post("/api/skills", skills_save)
    app.router.add_delete("/api/skills/{name}", skills_delete)

    app.router.add_get("/api/plugins", plugins_get)
    app.router.add_post("/api/plugins/{id}/toggle", plugins_toggle)

    app.router.add_post("/api/upload", upload_create)
    app.router.add_delete("/api/upload/{upload_id}", upload_delete)

    app.router.add_get("/api/files", files_get)
    app.router.add_get("/api/files/content", files_content)
    app.router.add_get("/api/files/raw", files_raw)
    app.router.add_get("/api/artifacts", artifacts_get)
