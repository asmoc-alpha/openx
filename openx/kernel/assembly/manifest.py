"""插件 Manifest 校验（P-B + E1）—— 插件自描述的 schema 与词汇。

插件经 ``__openx_meta__`` 声明 manifest（type/mount/trust/summary/
permissions/cost/timeout/dependencies，外加可选的 ``scaffold`` 演进声明）。
校验分两档：

- **problems**（拒载）：形状错——字段类型不对、trust 不在已知集、脚手架
  声明缺必答项。违例 → 插件 FAILED，不进注册；
- **warnings**（不拒）：type/mount 不在已知集、permission 不在词汇表、
  脚手架 fallback 不在词汇表——P-D 协议分类/安全审计消费前先放行并记录，
  避免过度严苛拒掉合法未来插件。

词汇集随 P-D/P-C 演进消费时再收紧；这里只做声明与形状约束。

``scaffold`` 演进声明（E1，自演进详设 §3.1）：补偿模型短板的模块（压缩、
路由、子代理、记忆检索、loop 本体……）必须自带讣告——答不出
``compensates`` 与 ``exit_when`` 的模块，按 v4.1 标准三没有资格以脚手架
身份存在。声明进 manifest 后由退场评测门（E4）消费；本模块只校验形状。
"""

from __future__ import annotations

from typing import Any

# 类型（模型在目录里按它分组浏览）——P-D 协议分类消费
KNOWN_TYPES = {
    "capability.tool",
    "context.memory",
    "strategy.planning",
    "orchestration",
    "lifecycle",
    "ui.panel",
}
# 挂载点（内核 Loop 各阶段何时自动调用）——P-D 消费
KNOWN_MOUNTS = {
    "loop.tool-call",
    "loop.pre-inference",
    "loop.planning",
    "loop.post-inference",
    "lifecycle.session",
    "ingress",
    "event-bus",
    "ui.deck",
}
# 信任级（§3.1 执行隔离按它分级；auto = 模型自产）
KNOWN_TRUST = {"builtin", "third-party", "user", "auto"}
# 权限词汇表（安全审计按它审批）——P-C/P-D 消费
PERMISSION_VOCAB = {"fs:read", "fs:write", "network", "shell", "process"}

# 脚手架演进声明（E1，自演进详设 §3.1）—— 补偿模型短板的模块自带的"讣告"。
# 块内键：compensates/exit_when 是必答项（标准三），eval_set/fallback 可选。
SCAFFOLD_KEYS = {"compensates", "exit_when", "eval_set", "fallback"}
SCAFFOLD_REQUIRED = ("compensates", "exit_when")
# fallback 词汇表（模型降级时脚手架如何回挂）——未知值只警告不拒（同 type/mount）
KNOWN_SCAFFOLD_FALLBACKS = {"reinstall-on-regression"}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def scaffold_of(meta: Any) -> dict:
    """取 manifest 的 ``scaffold`` 声明块（非 dict 或缺省 → ``{}``）。

    消费方（inventory/plugin_help）读它的统一入口——不直接 ``meta["scaffold"]``，
    免得每处重判类型。
    """
    block = meta.get("scaffold") if isinstance(meta, dict) else None
    return dict(block) if isinstance(block, dict) else {}


def _validate_scaffold(block: Any, problems: list[str], warnings: list[str]) -> None:
    """校验 ``scaffold`` 块（E1）：必答项缺失/类型错 = 拒载，词汇外值只警告。"""
    if not isinstance(block, dict):
        problems.append("manifest.scaffold must be a dict")
        return
    for key in SCAFFOLD_REQUIRED:
        value = block.get(key)
        if not isinstance(value, str) or not value.strip():
            problems.append(
                f"manifest.scaffold.{key} is required and must be a non-empty str"
            )
    for key in ("eval_set", "fallback"):
        if key in block and not isinstance(block[key], str):
            problems.append(f"manifest.scaffold.{key} must be a str")
    fallback = block.get("fallback")
    if isinstance(fallback, str) and fallback not in KNOWN_SCAFFOLD_FALLBACKS:
        warnings.append(
            f"unknown manifest.scaffold.fallback {fallback!r}; "
            f"known: {sorted(KNOWN_SCAFFOLD_FALLBACKS)}"
        )
    for key in block:
        if key not in SCAFFOLD_KEYS:
            warnings.append(f"unknown manifest.scaffold key {key!r}; "
                            f"known: {sorted(SCAFFOLD_KEYS)}")


def validate_manifest(meta: Any) -> tuple[list[str], list[str]]:
    """校验 manifest → ``(problems, warnings)``。

    problems 非空 = 拒载（形状错/trust 非法）；warnings 只记不拒
    （未知 type/mount/permission，供 P-D/P-C 演进）。
    """
    problems: list[str] = []
    warnings: list[str] = []
    if not isinstance(meta, dict):
        return ["manifest must be a dict"], []
    if "summary" in meta and not isinstance(meta["summary"], str):
        problems.append("manifest.summary must be a str")
    if "cost" in meta and not isinstance(meta["cost"], dict):
        problems.append("manifest.cost must be a dict")
    if "timeout" in meta and not _is_number(meta["timeout"]):
        problems.append("manifest.timeout must be a number (seconds)")
    for key in ("permissions", "dependencies"):
        value = meta.get(key)
        if value is not None and not (
            isinstance(value, list) and all(isinstance(v, str) for v in value)
        ):
            problems.append(f"manifest.{key} must be a list of str")
    trust = meta.get("trust")
    if trust is not None and trust not in KNOWN_TRUST:
        problems.append(f"manifest.trust {trust!r} not in {sorted(KNOWN_TRUST)}")
    ptype = meta.get("type")
    if ptype is not None and ptype not in KNOWN_TYPES:
        warnings.append(f"unknown manifest.type {ptype!r}; "
                        f"known: {sorted(KNOWN_TYPES)}")
    mount = meta.get("mount")
    if mount is not None and mount not in KNOWN_MOUNTS:
        warnings.append(f"unknown manifest.mount {mount!r}; "
                        f"known: {sorted(KNOWN_MOUNTS)}")
    for perm in meta.get("permissions", []):
        if perm not in PERMISSION_VOCAB:
            warnings.append(f"unknown permission {perm!r}; "
                            f"vocabulary: {sorted(PERMISSION_VOCAB)}")
    if "scaffold" in meta:
        _validate_scaffold(meta["scaffold"], problems, warnings)
    return problems, warnings


if __name__ == "__main__":
    # 无声明：干净通过
    assert validate_manifest({"summary": "x", "trust": "auto"}) == ([], [])

    # 合法脚手架声明：通过，scaffold_of 取回
    _ok = {
        "summary": "历史压缩",
        "scaffold": {
            "compensates": "模型上下文有限",
            "exit_when": "长会话免压缩评测通过",
            "eval_set": "evals/long-session.jsonl",
            "fallback": "reinstall-on-regression",
        },
    }
    assert validate_manifest(_ok) == ([], [])
    assert scaffold_of(_ok)["compensates"] == "模型上下文有限"
    assert scaffold_of({}) == {} and scaffold_of("not-a-dict") == {}

    # 必答项缺失 → 拒载（标准三：答不出就不算脚手架）
    _p, _w = validate_manifest({"scaffold": {}})
    assert any("compensates" in p for p in _p) and any("exit_when" in p for p in _p)

    # 块非 dict / 字段类型错 → 拒载
    assert validate_manifest({"scaffold": "x"})[0] == ["manifest.scaffold must be a dict"]
    _p, _ = validate_manifest(
        {"scaffold": {"compensates": "a", "exit_when": "b", "eval_set": 1}}
    )
    assert any("eval_set" in p for p in _p)

    # 词汇外 fallback / 未知键 → 只警告不拒
    _p, _w = validate_manifest(
        {"scaffold": {"compensates": "a", "exit_when": "b", "fallback": "nope",
                      "frobnicate": 1}}
    )
    assert _p == []
    assert any("fallback" in w for w in _w) and any("frobnicate" in w for w in _w)

    print("openx/kernel/assembly/manifest.py OK ✓")
