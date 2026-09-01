"""插件 Manifest 校验（P-B）—— 插件自描述的 schema 与词汇。

插件经 ``__openx_meta__`` 声明 manifest（type/mount/trust/summary/
permissions/cost/timeout/dependencies）。校验分两档：

- **problems**（拒载）：形状错——字段类型不对、trust 不在已知集。违例 →
  插件 FAILED，不进注册；
- **warnings**（不拒）：type/mount 不在已知集、permission 不在词汇表——
  P-D 协议分类/安全审计消费前先放行并记录，避免过度严苛拒掉合法未来插件。

词汇集随 P-D/P-C 演进消费时再收紧；这里只做声明与形状约束。
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


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


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
    return problems, warnings
