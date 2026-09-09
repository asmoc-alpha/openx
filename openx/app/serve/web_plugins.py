"""Web UI 插件——用户自定义 openx web 页面的静态扩展（serve 数据层）。

内核插件（``~/.openx/plugins`` 的 Python 模块）扩展的是**模型能力**；
web 插件扩展的是**web 页面本身**：用户把一个自包含的静态小页面
（``index.html`` + 任意 css/js/图片资源）放进约定目录，前端把它作为
**沙箱 iframe 卡片**嵌进任务面板的「插件」标签——自定义仪表盘、监视
器、工具面板都由此承载，无需改 openx 一行代码。

目录约定（镜像内核插件的发现面：用户级先于项目级，同 id 先见者赢）::

    ~/.openx/web-plugins/<name>/manifest.json   # 可选；损坏静默跳过
                                  index.html    # 卡片入口（必需）
                                  ...           # 任意静态资源（相对引用）
    <workspace>/.openx/web-plugins/<name>/...

manifest.json 字段（全部可选，缺省取目录名/默认值）::

    {"name": "...", "title": "...", "description": "...", "height": 320}

设计纪律：
- **沙箱隔离**：前端 iframe 用 ``sandbox="allow-scripts"``（无
  ``allow-same-origin``）——插件页拿到唯一 origin，读不到父页 DOM /
  localStorage；对 serve API 的 fetch 因跨源无 CORS 头而被拒。插件 =
  自包含展示页，不是扩展 openx 权限的通道。
- **服务端只做静态只读**：目录 resolve 后前缀校验（挡 ``../`` 与符号
  链接逃逸，与 api._resolve_in_workspace 同咽喉）；manifest 只信白名单
  字段，name 一律按目录名回退（不信 manifest 自报 id，防混淆）。
- **settings.json 唯一真源**：启停写 ``webPlugins.disabled``（与内核插件
  的 ``plugins.disabled`` 同模式），切换即时生效（前端重拉清单），无需
  重启 serve。
- **失败即 4xx + reason**：坏清单/坏路径不半改。
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from aiohttp import web

from ...config import OpenXConfig, SETTINGS_PATH
from .api import _fail, _ok

#: manifest 单文件读取上限（防一个巨型 json 拖垮清单端点）
MAX_MANIFEST_BYTES = 16 * 1024

#: iframe 默认 / 上限高度（px）——卡片高度来自 manifest，超限截到上限
DEFAULT_HEIGHT = 320
MIN_HEIGHT = 80
MAX_HEIGHT = 1200

#: 目录名（=插件 id）的合法字符集：挡路径穿越与 glob 注入（同 sessions
#: 的 resolve_anywhere 校验语义）
_NAME_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
)

WP_LIST_KEY = web.AppKey("serve_web_plugins", object)


@dataclass
class WebPlugin:
    """一个已发现的 web 插件（发现即只读，启停在 settings 侧）。"""

    name: str          # 目录名（id；manifest 不改写它）
    title: str
    description: str
    height: int
    source: str        # "user" | "project"
    root: Path         # 插件目录绝对路径（静态服务的根）

    def describe(self, disabled: bool) -> dict[str, Any]:
        """前端视图（enabled 由调用方按 settings 计算）。"""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "height": self.height,
            "source": self.source,
            "enabled": not disabled,
        }


# ── 发现 ────────────────────────────────────────────────────────


def user_web_plugins_dir() -> Path:
    """~/.openx/web-plugins——调用期读 SETTINGS_PATH，测试可 monkeypatch。"""
    return Path(SETTINGS_PATH).parent / "web-plugins"


def project_web_plugins_dir(workspace: str) -> Path:
    """<workspace>/.openx/web-plugins（项目级；同名先见者赢语义见 discover）。"""
    return Path(workspace) / ".openx" / "web-plugins"


def _valid_name(name: str) -> bool:
    """目录名合法性：非空、无路径分量、无特殊字符（单分量白名单）。"""
    return (
        bool(name)
        and name not in (".", "..")
        and not name.startswith(".")
        and all(c in _NAME_CHARS for c in name)
    )


def _read_manifest(root: Path) -> dict[str, Any]:
    """manifest.json → 白名单字段 dict；缺失/损坏/超限 → {}（静默）。"""
    path = root / "manifest.json"
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def discover(workspace: str) -> list[WebPlugin]:
    """扫描两级目录 → 插件列表（用户级先；同 id 先见者赢）。

    坏条目（无 index.html / 名字非法 / manifest 损坏）直接跳过——清单
    端点必须永不抛；跳过本身已是最诚实的"该插件不可用"表达。
    """
    out: list[WebPlugin] = []
    seen: set[str] = set()
    for source, directory in (
        ("user", user_web_plugins_dir()),
        ("project", project_web_plugins_dir(workspace)),
    ):
        if not directory.is_dir():
            continue
        try:
            children = sorted(directory.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or not _valid_name(child.name):
                continue
            if child.name in seen:
                continue
            if not (child / "index.html").is_file():
                continue
            manifest = _read_manifest(child)
            height = manifest.get("height")
            try:
                height = int(height)
            except (TypeError, ValueError):
                height = DEFAULT_HEIGHT
            height = max(MIN_HEIGHT, min(MAX_HEIGHT, height))
            out.append(WebPlugin(
                name=child.name,
                title=str(manifest.get("title") or child.name),
                description=str(manifest.get("description") or ""),
                height=height,
                source=source,
                root=child.resolve(),
            ))
            seen.add(child.name)
    return out


def _find(plugins: list[WebPlugin], name: str) -> Optional[WebPlugin]:
    for p in plugins:
        if p.name == name:
            return p
    return None


def _disabled_set() -> set[str]:
    settings = OpenXConfig._load_full_settings()
    raw = (settings.get("webPlugins") or {}).get("disabled")
    return {str(x) for x in raw} if isinstance(raw, list) else set()


# ── handlers ────────────────────────────────────────────────────


async def web_plugins_list(request: web.Request) -> web.Response:
    """GET /api/web-plugins → 全部 web 插件清单（含启停态与来源）。"""
    from .api import _workspace

    plugins = discover(str(_workspace(request)))
    disabled = _disabled_set()
    return _ok({
        "plugins": [p.describe(p.name in disabled) for p in plugins],
        "userDir": str(user_web_plugins_dir()),
    })


async def web_plugins_toggle(request: web.Request) -> web.Response:
    """POST /api/web-plugins/{name}/toggle → 启用/禁用一个 web 插件。

    body: ``{"disabled": bool}``。写 ``settings.json`` 的
    ``webPlugins.disabled``（与内核插件 ``plugins.disabled`` 同模式）；
    即时生效（前端重拉清单，iframe 随卡片增删），无需重启 serve。
    """
    from .api import _workspace

    name = request.match_info["name"]
    if not _valid_name(name):
        return _fail("invalid plugin name")
    plugins = discover(str(_workspace(request)))
    if _find(plugins, name) is None:
        return _fail(f"web plugin not found: {name}", status=404)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = {}
    disabled_flag = body.get("disabled") if isinstance(body, dict) else None
    if not isinstance(disabled_flag, bool):
        return _fail("disabled (bool) required")

    settings = OpenXConfig._load_full_settings()
    section = settings.get("webPlugins")
    if not isinstance(section, dict):
        section = {}
    disabled = {str(x) for x in section.get("disabled") or []}
    if disabled_flag:
        disabled.add(name)
    else:
        disabled.discard(name)
    section["disabled"] = sorted(disabled)
    settings["webPlugins"] = section
    try:
        OpenXConfig._save_full_settings(settings)
    except OSError as exc:
        return _fail(f"cannot write settings: {exc}", status=500)
    return _ok({"name": name, "disabled": disabled_flag})


async def web_plugins_static(request: web.Request) -> web.FileResponse:
    """GET /web-plugins/{name}/{file} → 插件目录内静态文件（只读）。

    安全咽喉（与 api._resolve_in_workspace 同构）：join 后 resolve 展开
    符号链接，再校验前缀落在该插件目录内——挡 ``../`` 穿越与软链逃逸。
    插件名走单分量白名单（discover 同款校验），双重防注入。no-store 与
    主前端一致（插件改动即生效）。
    """
    name = request.match_info["name"]
    rel = request.match_info.get("file", "")
    if not _valid_name(name):
        raise web.HTTPNotFound(text=f"no such plugin: {name}")

    from .api import _workspace

    plugins = discover(str(_workspace(request)))
    plugin = _find(plugins, name)
    if plugin is None:
        raise web.HTTPNotFound(text=f"no such plugin: {name}")

    candidate = (plugin.root / rel).resolve()
    if candidate != plugin.root and plugin.root not in candidate.parents:
        raise web.HTTPNotFound(text="not found")
    if not candidate.is_file():
        raise web.HTTPNotFound(text="not found")

    resp = web.FileResponse(candidate)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def register_web_plugins(app: web.Application) -> None:
    """把 web 插件端点挂到 app（server.create_app 调用，先于静态兜底）。"""
    app.router.add_get("/api/web-plugins", web_plugins_list)
    app.router.add_post("/api/web-plugins/{name}/toggle", web_plugins_toggle)
    app.router.add_get("/web-plugins/{name}/{file:.+}", web_plugins_static)


if __name__ == "__main__":
    # 自检：发现（用户级/项目级/覆盖/坏条目跳过）+ 静态路由注册面
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        ws = Path(td) / "ws"
        user_dir = home / ".openx" / "web-plugins"
        proj_dir = ws / ".openx" / "web-plugins"
        user_dir.mkdir(parents=True)
        proj_dir.mkdir(parents=True)

        def _mk(base: Path, name: str, manifest: str | None = None) -> None:
            d = base / name
            d.mkdir()
            (d / "index.html").write_text("<p>ok</p>")
            if manifest is not None:
                (d / "manifest.json").write_text(manifest)

        _mk(user_dir, "clock", json.dumps({"title": "时钟", "height": 240}))
        _mk(user_dir, "notes")  # 无 manifest → 全缺省
        _mk(proj_dir, "clock", json.dumps({"title": "项目覆盖"}))  # 同名覆盖
        _mk(proj_dir, "dashboard", "{broken json")  # manifest 损坏 → 缺省
        (user_dir / "nohtml").mkdir()               # 无 index.html → 跳过
        (user_dir / "bad..name").mkdir()            # 非法目录名 → 跳过
        (user_dir / "weird").mkdir()                # 合法名但无入口 → 跳过
        (user_dir / "weird").joinpath("readme.txt").write_text("x")

        saved = SETTINGS_PATH
        SETTINGS_PATH = home / ".openx" / "settings.json"
        try:
            plugins = discover(str(ws))
        finally:
            SETTINGS_PATH = saved
        by_name = {p.name: p for p in plugins}
        assert set(by_name) == {"clock", "notes", "dashboard"}, set(by_name)
        # 同名：用户级先见者赢（镜像内核插件发现语义）
        assert by_name["clock"].source == "user"
        assert by_name["clock"].title == "时钟"
        assert by_name["clock"].height == 240
        # 缺省：title 回退目录名、高度回退默认
        assert by_name["notes"].title == "notes"
        assert by_name["notes"].height == DEFAULT_HEIGHT
        # manifest 损坏 → 不炸，全缺省
        assert by_name["dashboard"].height == DEFAULT_HEIGHT

    # 路由注册面：create_app 后三个端点在位（不起真服务）
    from .api import WorkspaceRef
    from .bridge import ServeConsole
    from .server import create_app
    from .session import ServeSession

    class _FakeAgent:
        async def startup(self):
            pass

        async def shutdown(self):
            pass

        async def stream_run(self, text):
            if False:
                yield

    app = create_app(ServeSession(_FakeAgent(), ServeConsole()), workspace="/tmp/x")
    routes = [r.resource.canonical for r in app.router.routes()]
    for expected in (
        "/api/web-plugins",
        "/api/web-plugins/{name}/toggle",
        "/web-plugins/{name}/{file}",   # resource.canonical 略去 ":+" 正则
    ):
        assert expected in routes, f"missing route {expected}: {routes}"
    print("openx/app/serve/web_plugins.py OK ✓")
