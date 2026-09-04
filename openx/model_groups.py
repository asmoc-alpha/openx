"""模型组（modelGroups）配置：schema 解析 / 序列化 / per-role 解析。

模型组是 OpenX 的**唯一**模型配置入口——模型、凭据、端点只经 settings.json
的 ``modelGroups``/``activeGroup`` 表达（不再读取任何扁平旧结构）。每个组可
定义至多四个角色模型：

- ``openx-main-model``：主推理模型（规划/拆解/设计，主回合）；
- ``openx-exec-model``：执行模型（子代理/任务委派）；
- ``openx-mini-model``：最简模型（压缩等简单任务）；
- ``openx-modal-model``：多模模型（带图回合）。

一个组可整体共享一套 apiKey/apiBase，也允许逐角色覆盖（含换 kind/端点/key）。
组内 main 必填，exec/mini/modal 缺席时运行时回落 main 绑定。凭据字段支持
``env:VAR`` 间接（运行时从进程环境取值）——这是唯一允许的外部凭据来源。

本模块**只做纯逻辑**（不 import config、不碰文件 I/O）：解析/校验/序列化/
解析合并都以 ``dict`` 或鸭子类型对象进出；settings.json 的读写由
:mod:`openx.config` 的 ``OpenXConfig`` 委托调用（其 ``SETTINGS_PATH`` 是
测试 monkeypatch 点，I/O 必须留在那里）。

独立调试：``python openx/model_groups.py``。
"""

from __future__ import annotations

# ── 独立调试支持：允许直接运行本文件 ──────────────────────────
if __name__ == "__main__" and not __package__:
    import sys as _sys
    from pathlib import Path as _Path
    _file = _Path(__file__).resolve()
    _root = _file.parent
    while _root != _root.parent and not (_root / "pyproject.toml").exists():
        _root = _root.parent
    _sys.path.insert(0, str(_root))
    __package__ = ".".join(_file.relative_to(_root).parts[:-1])

import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

# ── 角色常量 ────────────────────────────────────────────────────

#: 组内可定义的角色键（与用户命名一致，恒定四个）
ROLE_KEYS = (
    "openx-main-model",
    "openx-exec-model",
    "openx-mini-model",
    "openx-modal-model",
)
MAIN_ROLE = ROLE_KEYS[0]

#: 角色友好别名（CLI /model group:role）-> 长键
ROLE_ALIASES = {
    "main": MAIN_ROLE,
    "exec": ROLE_KEYS[1],
    "mini": ROLE_KEYS[2],
    "modal": ROLE_KEYS[3],
}
#: 长键 -> 短别名（展示用）
ROLE_SHORT = {v: k for k, v in ROLE_ALIASES.items()}

#: 组名合法字符（禁 ``:`` —— /model group:role 按首冒号切分）
GROUP_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# 组级可声明键（除角色键外）
_GROUP_KEYS = frozenset({
    "kind", "apiKey", "api_key", "apiBase", "api_base",
    "temperature", "max_tokens", "max_retries", "retry_base_delay",
})

# 角色对象可选字段
_ROLE_FIELDS = frozenset({
    "model", "kind", "apiKey", "api_key", "apiBase", "api_base",
    "temperature", "max_tokens", "max_retries", "retry_base_delay",
})

_CAMEL = {"apiKey": "api_key", "apiBase": "api_base"}


# ── dataclasses（已解析视图） ──────────────────────────────────


@dataclass
class RoleBinding:
    """一个角色的模型绑定（解析后的规范字段，已去 env: 别名归一）。"""

    role: str                  # 长键（openx-*-model）
    model: str
    kind: Optional[str] = None          # 覆盖/继承 resolve 时再合并
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    max_retries: Optional[int] = None
    retry_base_delay: Optional[float] = None


@dataclass
class ModelGroup:
    """一个解析后的模型组。roles 只含**显式声明**的角色；缺席者回落 main。"""

    name: str
    kind: Optional[str] = None          # 组级实现默认
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    max_retries: Optional[int] = None
    retry_base_delay: Optional[float] = None
    roles: dict[str, RoleBinding] = field(default_factory=dict)


# ── 秘密展开 ───────────────────────────────────────────────────


def expand_secret(value: Any) -> str:
    """``env:VAR`` 间接：运行时从进程环境取；未设返回 ``""``。其余原样字符串。"""
    if not isinstance(value, str):
        return "" if value is None else str(value)
    value = value.strip()
    if value.startswith("env:"):
        return os.environ.get(value[4:], "")
    return value


# ── 名字 / 字段归一 ─────────────────────────────────────────────


def validate_group_name(name: Any) -> bool:
    """组名是否合法（字符串 + GROUP_NAME_RE）。"""
    return isinstance(name, str) and bool(GROUP_NAME_RE.match(name))


def _norm_field(raw: dict, *names: str) -> Any:
    """按 camel/snake 别名顺序取值。"""
    for n in names:
        if n in raw:
            return raw[n]
    return None


def role_short(role_key: str) -> str:
    """长键 -> 短别名（如 openx-exec-model -> exec）；未知键原样返回。"""
    return ROLE_SHORT.get(role_key, role_key)


def canonical_role(name: str) -> Optional[str]:
    """短别名/长键 -> 长键；未知返回 None。"""
    if name in ROLE_ALIASES:
        return ROLE_ALIASES[name]
    if name in ROLE_KEYS:
        return name
    return None


# ── 解析 ────────────────────────────────────────────────────────


def _parse_role(role_key: str, raw: Any, group_name: str) -> RoleBinding:
    """把角色值（字符串简写或对象）解析成 RoleBinding。坏值抛 ValueError。"""
    if isinstance(raw, str):
        model = raw.strip()
        if not model:
            raise ValueError(f"group '{group_name}' role '{role_key}' empty model")
        return RoleBinding(role=role_key, model=model)
    if not isinstance(raw, dict):
        raise ValueError(
            f"group '{group_name}' role '{role_key}' must be a model string "
            "or an object"
        )
    model = _norm_field(raw, "model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError(
            f"group '{group_name}' role '{role_key}' object missing 'model'"
        )
    api_key = _norm_field(raw, "apiKey", "api_key")
    api_base = _norm_field(raw, "apiBase", "api_base")
    temperature = _norm_field(raw, "temperature")
    max_tokens = _norm_field(raw, "max_tokens")
    max_retries = _norm_field(raw, "max_retries")
    retry_base_delay = _norm_field(raw, "retry_base_delay")
    return RoleBinding(
        role=role_key,
        model=model.strip(),
        kind=_norm_field(raw, "kind"),
        api_key=None if api_key in (None, "") else str(api_key),
        api_base=None if api_base in (None, "") else str(api_base),
        temperature=_as_opt_float(temperature, role_key),
        max_tokens=_as_opt_int(max_tokens, role_key),
        max_retries=_as_opt_int(max_retries, role_key),
        retry_base_delay=_as_opt_float(retry_base_delay, role_key),
    )


def _as_opt_int(v: Any, where: str) -> Optional[int]:
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{where}: 'max_tokens/max_retries' must be an int")


def _as_opt_float(v: Any, where: str) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{where}: temperature/retry_base_delay must be a number")


def parse_group(name: Any, raw: Any) -> ModelGroup:
    """把一个原始组 dict 解析成 ModelGroup。结构性问题抛 ValueError。"""
    if not validate_group_name(name):
        raise ValueError(f"invalid group name {name!r}")
    if not isinstance(raw, dict):
        raise ValueError(f"group '{name}' must be an object")
    kind = _norm_field(raw, "kind")
    api_key = _norm_field(raw, "apiKey", "api_key")
    api_base = _norm_field(raw, "apiBase", "api_base")
    group = ModelGroup(
        name=name,
        kind=None if kind in (None, "") else str(kind),
        api_key=None if api_key in (None, "") else str(api_key),
        api_base=None if api_base in (None, "") else str(api_base),
        temperature=_as_opt_float(_norm_field(raw, "temperature"), name),
        max_tokens=_as_opt_int(_norm_field(raw, "max_tokens"), name),
        max_retries=_as_opt_int(_norm_field(raw, "max_retries"), name),
        retry_base_delay=_as_opt_float(_norm_field(raw, "retry_base_delay"), name),
    )
    for role_key in ROLE_KEYS:
        if role_key in raw:
            group.roles[role_key] = _parse_role(role_key, raw[role_key], name)
    if MAIN_ROLE not in group.roles:
        raise ValueError(f"group '{name}' missing required role '{MAIN_ROLE}'")
    return group


def group_warnings(name: str, raw: dict) -> list[str]:
    """组内未知键（非组级、非角色键）警告——向前兼容，不拦截。"""
    known = _GROUP_KEYS | set(ROLE_KEYS)
    unknown = [k for k in raw if k not in known]
    return [
        f"group '{name}': ignoring unknown key {k!r}" for k in unknown
    ]


# ── 序列化（ModelGroup -> 规范 raw，落盘） ─────────────────────


def to_raw(group: ModelGroup) -> dict:
    """ModelGroup -> 规范原始 dict（snake 键；空可选字段不写）。"""
    out: dict[str, Any] = {}
    if group.kind:
        out["kind"] = group.kind
    if group.api_key:
        out["apiKey"] = group.api_key
    if group.api_base:
        out["apiBase"] = group.api_base
    if group.temperature is not None:
        out["temperature"] = group.temperature
    if group.max_tokens is not None:
        out["max_tokens"] = group.max_tokens
    if group.max_retries is not None:
        out["max_retries"] = group.max_retries
    if group.retry_base_delay is not None:
        out["retry_base_delay"] = group.retry_base_delay
    for role_key, b in group.roles.items():
        entry: dict[str, Any] = {"model": b.model}
        if b.kind:
            entry["kind"] = b.kind
        if b.api_key:
            entry["apiKey"] = b.api_key
        if b.api_base:
            entry["apiBase"] = b.api_base
        if b.temperature is not None:
            entry["temperature"] = b.temperature
        if b.max_tokens is not None:
            entry["max_tokens"] = b.max_tokens
        if b.max_retries is not None:
            entry["max_retries"] = b.max_retries
        if b.retry_base_delay is not None:
            entry["retry_base_delay"] = b.retry_base_delay
        out[role_key] = entry
    return out


def _canonicalize_raw(g: dict) -> dict:
    """把 snake 键归一成 camel（api_key->apiKey 等），供落盘与保存路径统一。

    保存侧可能带 snake 键；归一后 settings 里只存 camel 规范形。角色对象
    同样递归归一。
    """
    def _one(v: Any) -> Any:
        if not isinstance(v, dict):
            return v
        return {_camelize(k): _one(x) for k, x in v.items()}

    return _one(g)


def _camelize(key: str) -> str:
    return {"api_key": "apiKey", "api_base": "apiBase"}.get(key, key)


# ── per-role 解析合并（唯一咽喉点，鸭子类型 cfg） ───────────────


def _pick_secret(*values: Optional[str]) -> str:
    """按序取第一个非空值并展开 env:VAR；全空返回 ""。"""
    for v in values:
        if v is None:
            continue
        expanded = expand_secret(v)
        if expanded:
            return expanded
    return ""


def resolve_binding(group: ModelGroup, role_key: str) -> RoleBinding:
    """取角色绑定；缺席/未定义的角色回落 **整 main 绑定**（含凭据/kind）。"""
    if role_key in group.roles:
        return group.roles[role_key]
    if MAIN_ROLE in group.roles:
        main = group.roles[MAIN_ROLE]
        return RoleBinding(
            role=role_key, model=main.model, kind=main.kind,
            api_key=main.api_key, api_base=main.api_base,
            temperature=main.temperature, max_tokens=main.max_tokens,
            max_retries=main.max_retries, retry_base_delay=main.retry_base_delay,
        )
    raise ValueError(f"group '{group.name}' has no main role binding")


def resolve_role_settings(
    cfg: Any,
    group: ModelGroup,
    role_key: str,
) -> dict:
    """把 (组, 角色) 解析成 provider 工厂读的设置 dict。

    优先级：role 显式 > group 默认。凭据/端点/模型只来自组（含 ``env:VAR``
    展开），没有任何 config 扁平兜底与 CLI 覆盖——模型配置唯一入口就是模型组
    （main 模型由 parse 保证非空）。
    - temperature/max_tokens/retry 在组/角色未声明时回落 ``cfg`` 通用默认
      （这些是运行期旋钮，不是扁平旧结构兼容）。
    """
    b = resolve_binding(group, role_key)

    kind = b.kind or group.kind or "openai-compat"

    api_key = _pick_secret(b.api_key, group.api_key)
    api_base = _pick_secret(b.api_base, group.api_base)

    model = b.model or ""

    settings: dict[str, Any] = {
        "kind": kind,
        "api_key": api_key,
        "api_base": api_base,
        "model": model,
        "temperature": (
            b.temperature if b.temperature is not None
            else group.temperature if group.temperature is not None
            else getattr(cfg, "temperature", 0.0)
        ),
        "max_tokens": (
            b.max_tokens if b.max_tokens is not None
            else group.max_tokens if group.max_tokens is not None
            else getattr(cfg, "max_tokens", 8192)
        ),
    }
    # retry 晚绑定：只在组/角色显式声明时给覆盖
    retry = b.max_retries if b.max_retries is not None else group.max_retries
    if retry is not None:
        settings["max_retries"] = retry
    retry_delay = (
        b.retry_base_delay if b.retry_base_delay is not None
        else group.retry_base_delay
    )
    if retry_delay is not None:
        settings["retry_base_delay"] = retry_delay
    return settings


if __name__ == "__main__":
    # 独立自检：解析 + env:VAR 展开 + per-role 解析（凭据/模型只来自组）
    _raw = {
        "kind": "openai-compat",
        "openx-main-model": "m1",
        "openx-exec-model": "m2",
        "openx-mini-model": {"model": "m3", "apiKey": "env:NO_SUCH_ENV_ZZZ"},
    }
    _g = parse_group("dev", _raw)
    assert _g.roles[MAIN_ROLE].model == "m1"
    assert _g.roles[ROLE_KEYS[2]].api_key == "env:NO_SUCH_ENV_ZZZ"
    assert expand_secret("env:NO_SUCH_ENV_ZZZ") == ""
    assert expand_secret("sk-abc") == "sk-abc"
    assert ROLE_ALIASES["exec"] == "openx-exec-model"
    assert canonical_role("mini") == "openx-mini-model"
    # 缺席 exec → resolve 回落 main 绑定
    _only = ModelGroup(name="o", roles={MAIN_ROLE: RoleBinding(MAIN_ROLE, "mm")})
    assert resolve_binding(_only, "openx-exec-model").model == "mm"

    # per-role 解析：凭据/模型只来自组；temperature/max_tokens 回落 cfg 默认
    class _Cfg:
        temperature = 0.0
        max_tokens = 100

    _s = resolve_role_settings(_Cfg(), _g, "openx-mini-model")
    assert _s["model"] == "m3" and _s["kind"] == "openai-compat"
    # mini 自带 env: 间接 key 未设 → 空（不回落任何扁平字段）
    assert _s["api_key"] == ""
    # 缺组级 key/base → 空（无 config/CLI 扁平兜底）
    _only_grp = ModelGroup(name="k", roles={MAIN_ROLE: RoleBinding(MAIN_ROLE, "mm")})
    _no_cred = resolve_role_settings(_Cfg(), _only_grp, MAIN_ROLE)
    assert _no_cred["api_key"] == "" and _no_cred["api_base"] == ""
    assert _no_cred["model"] == "mm"
    print("openx/model_groups.py OK ✓")
