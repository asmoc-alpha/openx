r"""P6 组合输入（Composition）—— model_profile × 用户 overlay × 项目 overlay。

设计（`docs/openx-kernel-design.md` §1.3）：

```
model_profile（按模型版本的能力面）
  × 用户 overlay（~/.openx/openx.json，补丁原语 enable/disable/add/remove/replace）
  × 项目 overlay（<ws>/.openx/openx.json，同原语）
  = 应载清单（computed bundle）—— loader 只装载清单内插件
```

- **补丁语义**（cordis.patch.yml 式）：overlay 只作用于 f(档案) 的计算结果；
  同键冲突**用户级赢项目级**。
- **不加载 ≡ 现状**（标准四）：overlay 为空、档案未声明任何要求时，应载清单 =
  内置插件 + 目录/entry-points 全集，行为与引入前逐字节等价。
- **迁移语义**：settings.json 顶层 ``plugins.disabled`` 升格为「用户级 overlay
  disable 的语法糖」——双读（并集生效），写只走 overlay（不出现两个写真相源）。
- **auto-\* 出厂默认**：模型自产插件（§2.4 独立信任档）**默认不进 boot**——
  「先 session 后 persistent」的灰度就落在这一步：经 overlay ``enable``（晋升
  写回）或用户显式 enable 才进应载清单。非 auto-* 插件不受此影响。

本模块只做**纯计算**（:func:`resolve`）+ 薄文件 IO（overlay/profile 的读写）：
``resolve`` 不碰盘，入参是已加载对象，便于单测；路径助手**调用期**读
``config.SETTINGS_PATH``（镜像 ``assembly.loader.user_plugins_dir`` 模式），
测试 monkeypatch 即隔离真实 ``~/.openx``。
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
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger("openx.kernel.composition")

#: 组合跳过（disabled 轴）与退场（retired 轴）的原因标签。
REASON_AUTO = "auto-unpromoted"
REASON_SETTINGS = "settings-disabled"
REASON_PROJECT_DISABLE = "project-overlay-disable"
REASON_USER_DISABLE = "user-overlay-disable"
REASON_LEDGER_RETIRED = "scaffold-retired"
REASON_PROFILE_RETIRED = "profile-retired"


def is_auto(plugin_id: str) -> bool:
    """模型自产插件命名域（§2.4）：``auto-*`` 前缀，可一键批量回滚。"""
    return plugin_id.startswith("auto-")


# ── 路径（调用期读 SETTINGS_PATH，测试可 monkeypatch 隔离）──────────


def user_overlay_path() -> Path:
    """用户级 overlay：``~/.openx/openx.json``（与 settings.json 同目录）。"""
    from ... import config

    return Path(config.SETTINGS_PATH).parent / "openx.json"


def project_overlay_path(workspace: str) -> Path:
    """项目级 overlay：``<workspace>/.openx/openx.json``（只读，绝不自动创建）。"""
    return Path(workspace) / ".openx" / "openx.json"


def profiles_dir() -> Path:
    """模型档案目录：``~/.openx/profiles``。"""
    from ... import config

    return Path(config.SETTINGS_PATH).parent / "profiles"


def profile_path(name: str) -> Path:
    return profiles_dir() / f"{name}.json"


def active_profile_name() -> str:
    """当前激活档案名：settings.json 顶层 ``plugins.profile``（缺省 ""）。"""
    try:
        from ...config import OpenXConfig

        return str(OpenXConfig.load_plugin_settings().get("profile") or "").strip()
    except Exception:
        return ""


# ── 数据模型 ─────────────────────────────────────────────────────


@dataclass
class Overlay:
    """overlay 补丁（插件轴）。``add/remove/replace`` 为保留原语（解析记录、
    暂无消费方——loader 只认 enable/disable）。"""

    enable: set[str] = field(default_factory=set)
    disable: set[str] = field(default_factory=set)
    add: set[str] = field(default_factory=set)
    remove: set[str] = field(default_factory=set)
    replace: set[str] = field(default_factory=set)

    def is_empty(self) -> bool:
        return not (self.enable or self.disable or self.add or self.remove or self.replace)

    def ops(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for key in ("enable", "disable", "add", "remove", "replace"):
            values = sorted(getattr(self, key))
            if values:
                out[key] = values
        return out


@dataclass
class ModelProfile:
    """模型能力档案：声明该模型版本「需要 / 不再需要」哪些脚手架（§3.3 档案联动）。

    ``retire`` 命中的脚手架按 ``profile-retired`` 跳过硬装载（代码与注册仍在，
    可回挂）；``require`` 命中则撤销 ``profile-retired``（模型降级自动回挂）。
    **不覆盖**由账本决策的退场（那是用户裁决，须 ``restore_scaffold``）。
    """

    name: str = ""
    retire: set[str] = field(default_factory=set)
    require: set[str] = field(default_factory=set)

    def ops(self) -> dict[str, list[str]]:
        return {"retire": sorted(self.retire), "require": sorted(self.require)}


@dataclass
class Composition:
    """组合决议结果：应载清单 + 两支跳过明细 + 输入摘要（供记账/展示）。"""

    load: list[str] = field(default_factory=list)
    disabled: dict[str, str] = field(default_factory=dict)   # id -> reason（disabled 轴）
    retired: dict[str, str] = field(default_factory=dict)    # id -> reason（retired 轴）
    profile: str = ""
    ops: dict[str, Any] = field(default_factory=dict)        # 输入摘要（记账用）

    def skipped(self) -> dict[str, str]:
        merged = dict(self.retired)
        merged.update(self.disabled)
        return merged


# ── 文件 IO ──────────────────────────────────────────────────────


def _read_json(path: Path) -> dict:
    """读 JSON 对象；缺失/坏文件返回 {}（组合容错，绝不因坏 overlay 炸启动）。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        _log.warning("ignoring malformed overlay/profile: %s", path)
        return {}
    return data if isinstance(data, dict) else {}


def _str_set(value: Any) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        return {str(v) for v in value if str(v).strip()}
    return set()


def load_overlay(path: Path) -> Overlay:
    """从 JSON 读一个 overlay（不存在/坏文件 → 空 overlay，无副作用）。"""
    data = _read_json(Path(path))
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        return Overlay()
    return Overlay(
        enable=_str_set(plugins.get("enable")),
        disable=_str_set(plugins.get("disable")),
        add=_str_set(plugins.get("add")),
        remove=_str_set(plugins.get("remove")),
        replace=_str_set(plugins.get("replace")),
    )


def save_overlay(path: Path, overlay: Overlay) -> None:
    """把 overlay 写回 JSON（原子写：临时文件 + ``replace``）。

    保留文件内其它顶层键（只覆盖 ``plugins.enable`` / ``plugins.disable``）；
    写失败抛 ``OSError`` 由调用方决定降级（组合是证据系统，不该单点）。
    """
    path = Path(path)
    data = _read_json(path)
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}
    plugins["enable"] = sorted(overlay.enable)
    plugins["disable"] = sorted(overlay.disable)
    data["plugins"] = plugins
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def enable_in_overlay(path: Path, name: str) -> Overlay:
    """便捷写：把 *name* 加入 overlay 的 ``enable``（并从 ``disable`` 移除）。

    E7 晋升写回组合的落点——返回写后的 overlay（供记账）。
    """
    overlay = load_overlay(path)
    overlay.enable.add(name)
    overlay.disable.discard(name)
    save_overlay(path, overlay)
    return overlay


def disable_in_overlay(path: Path, name: str) -> Overlay:
    """便捷写：把 *name* 加入 overlay 的 ``disable``（并从 ``enable`` 移除）。"""
    overlay = load_overlay(path)
    overlay.disable.add(name)
    overlay.enable.discard(name)
    save_overlay(path, overlay)
    return overlay


def load_profile(name: str) -> ModelProfile:
    """按名读模型档案（缺失/坏文件 → 空档案，名前缀保留）。"""
    if not name:
        return ModelProfile(name="")
    data = _read_json(profile_path(name))
    return ModelProfile(
        name=str(data.get("name") or name),
        retire=_str_set(data.get("retire")),
        require=_str_set(data.get("require")),
    )


# ── 组合计算（纯函数）────────────────────────────────────────────


def resolve(
    ids: Iterable[str],
    *,
    retired_by_ledger: Iterable[str] | None = None,
    user_overlay: Overlay | None = None,
    project_overlay: Overlay | None = None,
    settings_disabled: Iterable[str] = (),
    profile: ModelProfile | None = None,
) -> Composition:
    """计算应载清单（纯函数，不碰盘）。

    ``ids`` = 已发现的**非内置**插件 id（加载序）。内置插件恒载，不经此表。
    优先级（后写覆盖）：auto-* 默认 → settings.disabled → 项目 overlay →
    用户 overlay → 模型档案。退场（retired 轴）与禁用（disabled 轴）分开：
    账本退场是用户裁决（``require`` 不撤销）；``profile.retire`` 是派生状态
    （``require`` 可撤销 = 档案联动自动回挂）。
    """
    ids = list(ids)
    known = set(ids)
    retired: dict[str, str] = {}
    disabled: dict[str, str] = {}

    # 退场轴：账本决策（权威，先落）
    for pid in retired_by_ledger or ():
        retired[str(pid)] = REASON_LEDGER_RETIRED

    # 禁用轴：auto-* 出厂默认 + settings.json（迁移期双读）
    for pid in ids:
        if is_auto(pid):
            disabled[pid] = REASON_AUTO
    for pid in settings_disabled or ():
        if str(pid) in known:
            disabled[str(pid)] = REASON_SETTINGS

    # overlay：项目级先、用户级后（同键用户赢）；同文件内 enable 后于 disable
    proj = project_overlay or Overlay()
    for pid in sorted(proj.disable):
        disabled[pid] = REASON_PROJECT_DISABLE
    for pid in sorted(proj.enable):
        disabled.pop(pid, None)
    user = user_overlay or Overlay()
    for pid in sorted(user.disable):
        disabled[pid] = REASON_USER_DISABLE
    for pid in sorted(user.enable):
        disabled.pop(pid, None)

    # 档案：retire 先（派生退场 + 清禁用），require 后（可撤销 profile 派生退场）
    # ——同一 id 同列两端时 **require 赢**（显式"需要"强于"不需要"）。
    prof = profile or ModelProfile()
    for pid in sorted(prof.retire):
        if pid not in retired:  # 不覆盖账本决策的退场
            disabled.pop(pid, None)
            retired[pid] = REASON_PROFILE_RETIRED
    for pid in sorted(prof.require):
        if retired.get(pid) == REASON_PROFILE_RETIRED:
            retired.pop(pid, None)
        disabled.pop(pid, None)

    load = [pid for pid in ids if pid not in disabled and pid not in retired]
    ops = {
        "profile": prof.name,
        "user": user.ops(),
        "project": proj.ops(),
    }
    return Composition(
        load=load, disabled=disabled, retired=retired, profile=prof.name, ops=ops
    )


def resolve_for_workspace(workspace: str, ids: Iterable[str], **kwargs: Any) -> Composition:
    """便捷封装：从盘上读三份输入（用户/项目 overlay + 激活档案）再 :func:`resolve`。

    ``retired_by_ledger`` / ``settings_disabled`` 由调用方（kernel）传入——
    它们各有惰性真源，不在本模块重复读取。
    """
    return resolve(
        ids,
        user_overlay=load_overlay(user_overlay_path()),
        project_overlay=load_overlay(project_overlay_path(workspace)),
        profile=load_profile(active_profile_name()),
        **kwargs,
    )


if __name__ == "__main__":
    import tempfile

    # ── resolve：纯函数 ──────────────────────────────────────────
    # 空 overlay + 无档案 ⇒ 全集（「不加载 ≡ 现状」）
    base = resolve(["alpha", "beta", "auto-x"])
    assert base.load == ["alpha", "beta"], base.load
    assert base.disabled == {"auto-x": REASON_AUTO}  # auto-* 出厂默认不进 boot
    assert base.retired == {}

    # 用户 overlay enable ⇒ auto-* 进 boot
    uv = Overlay(enable={"auto-x"})
    c = resolve(["alpha", "beta", "auto-x"], user_overlay=uv)
    assert c.load == ["alpha", "beta", "auto-x"]

    # 同键冲突：用户级赢项目级
    c = resolve(
        ["alpha"],
        user_overlay=Overlay(enable={"alpha"}),
        project_overlay=Overlay(disable={"alpha"}),
    )
    assert c.load == ["alpha"]

    # settings.disabled 作语法糖（并集）；用户 overlay enable 可覆盖
    c = resolve(["alpha"], settings_disabled=["alpha"])
    assert c.disabled == {"alpha": REASON_SETTINGS}
    c = resolve(["alpha"], settings_disabled=["alpha"], user_overlay=Overlay(enable={"alpha"}))
    assert c.load == ["alpha"]

    # 档案：retire 跳过硬装载（retired 轴），require 撤销
    prof = ModelProfile(name="m1", retire={"agent-beta"}, require=set())
    c = resolve(["agent-beta", "histcompact"], profile=prof)
    assert c.retired == {"agent-beta": REASON_PROFILE_RETIRED}
    assert c.load == ["histcompact"]
    prof2 = ModelProfile(name="m2", retire={"histcompact"}, require={"agent-beta"})
    c = resolve(["agent-beta", "histcompact"], profile=prof2)
    assert c.retired == {"histcompact": REASON_PROFILE_RETIRED}
    assert c.load == ["agent-beta"]

    # 账本决策的退场不被 profile.require 撤销（用户裁决权威）
    c = resolve(["scaf"], retired_by_ledger=["scaf"], profile=ModelProfile(require={"scaf"}))
    assert c.retired == {"scaf": REASON_LEDGER_RETIRED} and c.load == []

    # ── 文件 IO：临时目录，绝不碰真实 ~/.openx ─────────────────────
    with tempfile.TemporaryDirectory() as td:
        ov_path = Path(td) / "openx.json"
        assert load_overlay(ov_path).is_empty()
        enable_in_overlay(ov_path, "auto-x")
        ov = load_overlay(ov_path)
        assert ov.enable == {"auto-x"} and ov.disable == set()
        # disable 移除同名 enable
        disable_in_overlay(ov_path, "auto-x")
        ov = load_overlay(ov_path)
        assert ov.disable == {"auto-x"} and ov.enable == set()
        # 保留其它顶层键
        ov_path.write_text(json.dumps({"other": 1, "plugins": {"disable": ["x"]}}))
        enable_in_overlay(ov_path, "y")
        data = json.loads(ov_path.read_text())
        assert data["other"] == 1 and set(data["plugins"]["enable"]) == {"y"}

        # 坏文件 → 空 overlay（不抛）
        (Path(td) / "bad.json").write_text("{not json")
        assert load_overlay(Path(td) / "bad.json").is_empty()

        # 档案读：坏文件 → 空档案
        assert load_profile("nope").retire == set()

    print("openx/kernel/assembly/composition.py OK ✓")
