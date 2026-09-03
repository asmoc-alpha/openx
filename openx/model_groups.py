"""模型组（modelGroups）配置：schema 解析 / 旧配置迁移 / per-role 解析。

模型组是 OpenX 的唯一模型配置入口（替换旧的扁平 ``model/api_key/api_base``
与 ``providers``/``active_provider``/``models`` profiles）。每个组可定义至多
四个角色模型：

- ``openx-main-model``：主推理模型（规划/拆解/设计，主回合）；
- ``openx-exec-model``：执行模型（子代理/任务委派）；
- ``openx-mini-model``：最简模型（压缩等简单任务）；
- ``openx-modal-model``：多模模型（带图回合）。

一个组可整体共享一套 apiKey/apiBase，也允许逐角色覆盖（含换 kind/端点/key）。
组内 main 必填，exec/mini/modal 缺席时运行时回落 main 绑定。

本模块**只做纯逻辑**（不 import config、不碰文件 I/O）：解析/校验/迁移/
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


# ── 迁移（旧结构 -> modelGroups，纯 dict → dict） ────────────────

# settings.json ``env`` 里与模型相关的旧键（迁移后删除）
_LEGACY_ENV_KEYS = ("OPENX_API_KEY", "OPENX_BASE_URL", "OPENX_DEFAULT_MODEL")


def _group_from_flat(key: str, base: str, model: str) -> dict:
    g: dict[str, Any] = {"kind": "openai-compat", "openx-main-model": model}
    if key:
        g["apiKey"] = key
    if base:
        g["apiBase"] = base
    return g


def _group_from_provider(name: str, inst: dict) -> dict:
    g: dict[str, Any] = {
        "kind": inst.get("kind") or "openai-compat",
        "openx-main-model": str(inst.get("model") or ""),
    }
    for src, dst in (("api_key", "apiKey"), ("api_base", "apiBase")):
        v = inst.get(src)
        if v:
            g[dst] = v
    for k in ("temperature", "max_tokens", "max_retries", "retry_base_delay"):
        if k in inst and inst[k] is not None:
            g[k] = inst[k]
    return g


def migrate_legacy(data: dict) -> tuple[dict, list[str]]:
    """旧 settings.json 结构 -> modelGroups/activeGroup。

    返回 ``(新 data, 迁移说明列表)``；``modelGroups`` 已存在时原样返回
    （幂等）。迁移源优先级：扁平 env 三件套 < providers < models profiles；
    各来源映射成独立组（名字见内），冲突时靠前的来源优先。
    """
    if not isinstance(data, dict):
        return data, []
    if data.get("modelGroups"):
        return data, []
    notes: list[str] = []
    groups: dict[str, dict] = {}
    env = data.get("env") or {}
    providers = data.get("providers") or {}
    profiles = data.get("models") or {}
    active_group: Optional[str] = None

    # 1) 扁平 env 三件套（存量最深、保底）
    flat_key = str(env.get("OPENX_API_KEY") or "").strip()
    flat_base = str(env.get("OPENX_BASE_URL") or "").strip()
    flat_model = str(env.get("OPENX_DEFAULT_MODEL") or "").strip()
    if flat_model:
        groups["default"] = _group_from_flat(flat_key, flat_base, flat_model)
        active_group = "default"
        notes.append(
            "migrated flat env config into model group 'default' "
            f"(main={flat_model})"
        )

    # 2) providers：每个实例保留为独立组（1:1 对应 /model <组> 切换）
    for pname, inst in providers.items():
        if not isinstance(inst, dict):
            continue
        if pname in groups:  # 名字冲突：扁平 default 优先，其余跳过保活
            notes.append(f"skipped provider '{pname}' (group name taken)")
            continue
        groups[pname] = _group_from_provider(pname, inst)
        if data.get("active_provider") == pname:
            active_group = pname
    if providers:
        notes.append(
            f"migrated {len(groups)} provider instance(s) into model groups"
        )

    # 3) models profiles：每个折成独立组
    for mname, prof in profiles.items():
        if not isinstance(prof, dict):
            continue
        if mname in groups:
            notes.append(f"skipped model profile '{mname}' (group name taken)")
            continue
        g: dict[str, Any] = {
            "kind": "openai-compat",
            "openx-main-model": str(prof.get("model") or ""),
        }
        for src, dst in (("api_key", "apiKey"), ("api_base", "apiBase")):
            v = prof.get(src)
            if v:
                g[dst] = v
        if prof.get("model"):
            groups[mname] = g
            if active_group is None:
                active_group = mname
            notes.append(f"migrated model profile '{mname}' into a model group")

    if not groups:
        return data, []  # 无任何存量：不写空 groups，留给内存合成兜底

    out = dict(data)
    # 写进规范 raw（camel），并清掉旧键
    out["modelGroups"] = {n: _canonicalize_raw(g) for n, g in groups.items()}
    if active_group and active_group in out["modelGroups"]:
        out["activeGroup"] = active_group
    else:
        out["activeGroup"] = next(iter(out["modelGroups"]))
    out.pop("providers", None)
    out.pop("active_provider", None)
    out.pop("models", None)
    if env:
        kept_env = {k: v for k, v in env.items() if k not in _LEGACY_ENV_KEYS}
        if kept_env:
            out["env"] = kept_env
        else:
            out.pop("env", None)
    return out, notes


def _canonicalize_raw(g: dict) -> dict:
    """把 snake 键归一成 camel（api_key->apiKey 等），供落盘与保存路径统一。

    ``migrate_legacy`` 与保存侧可能带 snake 键（provider/profile 旧字段），
    归一后 settings 里只存 camel 规范形。角色对象同样递归归一。
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
    cli: Optional[dict] = None,
) -> dict:
    """把 (组, 角色) 解析成 provider 工厂读的设置 dict。

    优先级：role 显式 > group 默认 > ``cfg``（env/CLI 已并入的全局兜底）。
    - model：main 角色还会被 CLI ``cli['model']`` 临时覆盖（历史 ``-m`` 最大）；
      非 main 角色不回落到 ``cfg.model``（那是主模型语义，缺席已整体回落 main）。
    - api_key/base 逐级合并并展开 ``env:VAR``。
    - retry 字段只在组/角色显式声明时进 dict（策略对象仍晚绑定读 config）。
    """
    b = resolve_binding(group, role_key)
    cli = cli or {}

    kind = b.kind or group.kind or "openai-compat"

    api_key = _pick_secret(b.api_key, group.api_key, getattr(cfg, "api_key", ""))
    api_base = _pick_secret(b.api_base, group.api_base, getattr(cfg, "api_base", ""))

    if role_key == MAIN_ROLE:
        cli_model = cli.get("model")
        model = cli_model or b.model or getattr(cfg, "model", "") or ""
        api_key = cli.get("api_key") or api_key
        api_base = cli.get("api_base") or api_base
    else:
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
    # 独立自检：解析 + env:VAR 展开 + 迁移三类旧结构
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

    # 迁移
    class _Cfg:
        api_key = ""; api_base = ""; model = ""; temperature = 0.0; max_tokens = 100

    _mig = {"env": {"OPENX_BASE_URL": "https://x", "OPENX_DEFAULT_MODEL": "dm"},
            "trusted_dirs": ["/a"]}
    _new, _notes = migrate_legacy(_mig)
    assert _new["modelGroups"]["default"]["openx-main-model"] == "dm"
    assert _new["activeGroup"] == "default"
    # env 里 LLM 键已移除（此处 env 全被清空 → 整节删除）
    assert "OPENX_DEFAULT_MODEL" not in str(_new)
    assert _new["trusted_dirs"] == ["/a"]
    # 幂等：已含 modelGroups 不再迁移
    _again, _n2 = migrate_legacy(_new)
    assert _again is _new and _n2 == []
    _p = {"providers": {"a": {"kind": "anthropic", "api_key": "k", "model": "cl1"},
                        "b": {"kind": "openai-compat", "api_key": "k2",
                              "api_base": "https://b", "model": "bm"}},
          "active_provider": "b"}
    _pv, _ = migrate_legacy(_p)
    assert set(_pv["modelGroups"]) == {"a", "b"} and _pv["activeGroup"] == "b"
    assert _pv["modelGroups"]["a"]["kind"] == "anthropic"

    _s = resolve_role_settings(_Cfg(), _g, "openx-mini-model")
    assert _s["model"] == "m3" and _s["kind"] == "openai-compat"
    _cli_ovr = {"model": "clivm"}
    assert resolve_role_settings(_Cfg(), _g, MAIN_ROLE, _cli_ovr)["model"] == "clivm"
    print("openx/model_groups.py OK ✓")
