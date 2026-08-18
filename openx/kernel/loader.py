"""插件加载器 —— 发现 / 解析 / apply(ctx) / 阶段跃迁。

P1 组合输入 = 用户目录 + 项目目录 + pip entry-points（base bundle 的
YAML 组合留作后续）。失败语义：解析/apply 失败 = 该插件 failed，主进
程不死（用户插件隔离）；内置插件不经此路径，失败即致命。

用户目录从 ``config.SETTINGS_PATH`` 在调用期推导——测试 monkeypatch
SETTINGS_PATH 即隔离，不碰真实 ~/.openx。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_log = logging.getLogger("openx.kernel.loader")

ENTRY_POINT_GROUP = "openx.plugins"


@dataclass
class PluginSpec:
    id: str
    source: str                 # 展示用：目录路径 / entry-point 值
    path: Optional[Path] = None          # 文件插件
    entry_point: Optional[object] = None  # importlib.metadata EntryPoint


def user_plugins_dir() -> Path:
    """~/.openx/plugins —— 调用期读 SETTINGS_PATH，测试可 monkeypatch。"""
    from .. import config

    return Path(config.SETTINGS_PATH).parent / "plugins"


def project_plugins_dir(workspace: str) -> Path:
    return Path(workspace) / ".openx" / "plugins"


def discover(workspace: str) -> list[PluginSpec]:
    """发现插件指定符；同 id 先见者赢（用户级先于项目级）。"""
    specs: list[PluginSpec] = []
    seen: set[str] = set()

    def add(spec: PluginSpec) -> None:
        if spec.id in seen:
            _log.warning("duplicate plugin id %r; first wins", spec.id)
            return
        seen.add(spec.id)
        specs.append(spec)

    for directory in (user_plugins_dir(), project_plugins_dir(workspace)):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            add(PluginSpec(id=path.stem, source=str(directory), path=path))

    # 包插件：pip entry-points group openx.plugins（远期的市场入口，P1 先接通）
    try:
        from importlib.metadata import entry_points

        for ep in entry_points(group=ENTRY_POINT_GROUP):
            add(PluginSpec(id=ep.name, source=f"entry-point:{ep.value}", entry_point=ep))
    except Exception as exc:  # 元数据后端异常不阻断启动
        _log.warning("entry-point discovery failed: %s", exc)
    return specs


def load_module(spec: PluginSpec) -> object:
    """解析指定符为模块/apply 可调用；失败抛异常由调用方记 failed。"""
    if spec.entry_point is not None:
        loaded = spec.entry_point.load()
        return loaded
    assert spec.path is not None
    mod_name = f"_openx_plugin_{spec.id}"
    mod_spec = importlib.util.spec_from_file_location(mod_name, spec.path)
    if mod_spec is None or mod_spec.loader is None:
        raise ImportError(f"cannot resolve module spec for {spec.path}")
    module = importlib.util.module_from_spec(mod_spec)
    sys.modules[mod_name] = module
    mod_spec.loader.exec_module(module)
    return module


def extract_apply(loaded: object) -> Optional[object]:
    """模块 → 其 apply 导出；本身可调用（entry-point 直指函数）亦可。"""
    apply_fn = getattr(loaded, "apply", None)
    if callable(apply_fn):
        return apply_fn
    if callable(loaded):
        return loaded
    return None
