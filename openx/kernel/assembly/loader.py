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
    path: Optional[Path] = None          # 文件插件（入口 .py）
    entry_point: Optional[object] = None  # importlib.metadata EntryPoint
    loaded: Optional[object] = None       # 已导入模块（base bundle 内置插件）
    builtin: bool = False                 # 内置：失败=致命，禁用表对其无效
    root: Optional[Path] = None           # S2a：目录插件的根（数据资产相对它解析）


def user_plugins_dir() -> Path:
    """~/.openx/plugins —— 调用期读 SETTINGS_PATH，测试可 monkeypatch。"""
    from ... import config

    return Path(config.SETTINGS_PATH).parent / "plugins"


def project_plugins_dir(workspace: str) -> Path:
    return Path(workspace) / ".openx" / "plugins"


def project_plugins_untrusted(workspace: str) -> bool:
    """项目插件目录存在但工作区未获信任（S1a boot 信任门）。

    信任判定复用 CLI 层的 ``OpenXConfig.is_trusted``（trusted_dirs 持久化
    在 settings.json；交互 CLI 启动时信任屏先行）。内核只消费判定结果——
    未信任 = 该目录的插件代码一行都不 import（clone 恶意仓库 + 打开会话
    不再等于静默执行其插件）。用户目录与 entry-points 默认可信（决策 #5）。
    """
    from ... import config

    directory = project_plugins_dir(workspace)
    if not directory.is_dir():
        return False
    return not config.OpenXConfig.is_trusted(str(Path(workspace).resolve()))


def untrusted_project_plugin_ids(workspace: str) -> list[str]:
    """未信任项目目录里**会被跳过**的插件 id（plugin_skipped 记账用）。"""
    if not project_plugins_untrusted(workspace):
        return []
    directory = project_plugins_dir(workspace)
    return sorted(p.stem for p in directory.glob("*.py")
                  if not p.name.startswith("_"))


def discover(workspace: str) -> list[PluginSpec]:
    """发现插件指定符；同 id 先见者赢（用户级先于项目级）。

    S1a：项目目录未获工作区信任 → 整目录跳过（一个 spec 都不产出）。
    S2a：除单文件 ``*.py`` 外，还发现**目录插件**——``<dir>/<name>/plugin.py``
    存在即一个插件（数据资产放同目录，经 ``ctx.plugin_dir`` 解析）；
    同 id 单文件优先。
    """
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
        if directory == project_plugins_dir(workspace) and project_plugins_untrusted(workspace):
            # S1a：项目目录未信任 → 一个 spec 都不产出（记账由 kernel 侧
            # _reload 发 plugin_skipped，loader 只负责沉默地跳过）。
            _log.warning(
                "project plugin dir untrusted (workspace not trusted); skipped: %s",
                directory,
            )
            continue
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            add(PluginSpec(id=path.stem, source=str(directory), path=path))
        # S2a：目录插件（<name>/plugin.py）
        for subdir in sorted(directory.iterdir()):
            if not subdir.is_dir() or subdir.name.startswith("_"):
                continue
            entry = subdir / "plugin.py"
            if entry.is_file():
                add(PluginSpec(id=subdir.name, source=str(directory),
                               path=entry, root=subdir))

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
    if spec.loaded is not None:  # base bundle 内置插件：模块已导入
        return spec.loaded
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


def unload_module(spec: PluginSpec) -> None:
    """把文件插件的模块对象从 ``sys.modules`` 摘除（S1e）。

    此前 unload 只清注册项，模块对象（类/线程/模块级状态）永久滞留——
    卸载是名义的，重载会生成第二个模块对象。调用前提：该插件的注册项
    已按 provenance 清空（模块仅被注册项引用时 pop 才安全）。builtin 与
    entry-point 插件不走这里（模块不是本加载器创建的）。
    """
    if spec.path is None:
        return
    sys.modules.pop(f"_openx_plugin_{spec.id}", None)
