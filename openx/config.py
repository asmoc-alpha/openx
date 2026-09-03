"""Configuration management for OpenX.

Supports config from:
0. ~/.openx/settings.json (env section — new!)
1. Environment variables
2. ~/.openx/config.json
3. .openx.json (project-level)
4. Command-line arguments
"""

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
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import model_groups as _mg

# ── Settings path ────────────────────────────────────────────────

SETTINGS_PATH = Path.home() / ".openx" / "settings.json"


@dataclass
class OpenXConfig:
    """OpenX configuration."""

    # ── LLM settings ─────────────────────────────────────────────
    # No hardcoded defaults — must be set via settings.json, env, or CLI.
    api_key: str = field(default_factory=lambda: os.environ.get("OPENAI_API_KEY", ""))
    api_base: str = field(
        default_factory=lambda: os.environ.get("OPENAI_API_BASE", "")
    )
    model: str = field(
        default_factory=lambda: os.environ.get("OPENX_MODEL", "")
    )
    max_tokens: int = 8192
    temperature: float = 0.0

    # ── 模型组（modelGroups）──────────────────────────────────────
    # active_group 是当前绑定组的投影（agent 构造/切组时回写），供 UI 展示。
    # cli_*_override 为临时 CLI 覆盖（main.py 置位），仅对 main 角色生效。
    active_group: str = ""
    cli_model_override: Optional[str] = None
    cli_api_key_override: Optional[str] = None
    cli_api_base_override: Optional[str] = None
    # load() 置位：手动构造的 OpenXConfig（测试/嵌入，字段直给）不读全局
    # modelGroups——保持与旧 resolve_provider 相同的隔离（避免真实 ~/.openx
    # 泄漏进单测）。由 load() 产出、或经 save/ensure 消费的实例才走文件组。
    settings_loaded: bool = False

    # ── Retry settings ───────────────────────────────────────────
    # LLM 请求重试（429/5xx/连接错误/流中断）。0 = 不重试，出错即抛。
    # retry_base_delay 为指数退避基数（秒）：delay ≈ base·2^attempt + jitter，
    # 429 带 Retry-After 头时优先采用服务端值；两者均以 60s 封顶。
    max_retries: int = 4
    retry_base_delay: float = 1.0

    # ── Agent settings ───────────────────────────────────────────
    max_tool_rounds: int = 30  # max back-and-forth tool calls per message
    workspace: str = field(default_factory=lambda: os.getcwd())

    # ── Permission settings ──────────────────────────────────────
    auto_approve: bool = False  # skip all permission prompts
    allow_read_outside_workspace: bool = True
    allow_write_outside_workspace: bool = False
    dangerous_commands: list[str] = field(
        default_factory=lambda: [
            "rm -rf",
            "sudo rm",
            "mkfs.",
            "dd if=",
            ":(){ :|:& };:",  # fork bomb
            "chmod -R 777",
            "> /dev/sda",
        ]
    )
    allowed_commands: list[str] = field(
        default_factory=lambda: [
            "ls", "cat", "head", "tail", "wc", "find", "grep",
            "git", "python", "python3", "pip", "pip3",
            "mkdir", "touch", "cp", "mv", "echo", "curl",
            "npm", "npx", "node", "cargo", "go", "rustc",
            "docker", "make", "cmake",
            "pytest", "ruff", "mypy", "black",
        ]
    )

    # ── Output settings ──────────────────────────────────────────
    stream: bool = True  # streaming output; CLI --no-stream sets this False

    # ── Web search settings ──────────────────────────────────────
    # web_search 后端："auto" = DDG 优先、网络错误自动降级 Bing（并粘住
    # 成功的后端）；"ddg" / "bing" = 固定单一后端。大陆网络 DDG 被墙，
    # auto 首次搜索付一次 ≤5s 连接超时后即粘住 Bing。
    web_search_provider: str = "auto"

    # ── UI settings ──────────────────────────────────────────────
    show_token_usage: bool = True
    syntax_theme: str = "monokai"
    max_history_tokens: int = 100_000  # truncate history above this

    # ── Instruction file settings ─────────────────────────────────
    instructions_file: str = "OPENX.md"  # filename to look for in workspace
    global_instructions_path: str = ""   # blank = use default (~/.openx/OPENX.md)

    # ── Settings.json management ──────────────────────────────────

    @staticmethod
    def _load_full_settings() -> dict:
        """Load entire settings.json as a dict. Returns {} if missing."""
        if not SETTINGS_PATH.exists():
            return {}
        try:
            return json.loads(SETTINGS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    @staticmethod
    def _save_full_settings(data: dict) -> None:
        """Save entire settings dict to ~/.openx/settings.json."""
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(data, indent=2))

    @staticmethod
    def load_settings() -> dict:
        """Load env settings from ~/.openx/settings.json."""
        return OpenXConfig._load_full_settings().get("env", {})

    @staticmethod
    def save_settings(env: dict) -> None:
        """Save env settings, preserving other top-level keys."""
        data = OpenXConfig._load_full_settings()
        data["env"] = env
        OpenXConfig._save_full_settings(data)

    @staticmethod
    def is_configured() -> bool:
        """Check if a model group with an active main binding is configured.

        先跑一次迁移（存量 env/providers/models 折成模型组），再判定激活组
        的 main 角色是否带 model（api_key 可经运行时 env 兜底，启动校验
        另行要求）。无任何组时返回 False（交给 setup 向导 / 内存合成）。
        """
        OpenXConfig.ensure_model_groups()
        data = OpenXConfig._load_full_settings()
        groups_raw = data.get("modelGroups") or {}
        active = data.get("activeGroup") or ""
        if active not in groups_raw:
            active = next(iter(groups_raw), "")
        if not active:
            return False
        raw_group = groups_raw.get(active)
        if not isinstance(raw_group, dict):
            return False
        main = raw_group.get(_mg.MAIN_ROLE)
        if isinstance(main, str):
            return bool(main.strip())
        if isinstance(main, dict):
            return bool(str(main.get("model") or "").strip())
        return False

    # ── 模型组（modelGroups）存取 ────────────────────────────────

    @staticmethod
    def load_model_groups_raw() -> dict[str, dict]:
        """读取 settings.json 里 ``modelGroups`` 原始 dict（name -> raw）。"""
        return OpenXConfig._load_full_settings().get("modelGroups", {}) or {}

    @staticmethod
    def load_model_groups() -> tuple[dict, str, list[str]]:
        """解析全部模型组 → ``(groups, active_name, warnings)``。

        ``groups: dict[name, model_groups.ModelGroup]``（结构错误的组跳过并
        记 warning）；``active_name`` 为激活组名（失效回落首个，无组时 ""）。
        """
        raw = OpenXConfig.load_model_groups_raw()
        groups: dict = {}
        warnings: list[str] = []
        for name, r in raw.items():
            try:
                groups[name] = _mg.parse_group(name, r)
            except ValueError as exc:
                warnings.append(str(exc))
        active = str(OpenXConfig._load_full_settings().get("activeGroup") or "")
        if active not in groups:
            active = next(iter(groups), "")
        return groups, active, warnings

    @staticmethod
    def save_model_groups(groups: dict) -> None:
        """覆盖保存 ``modelGroups``（camel 规范形），保留其他顶层键。"""
        data = OpenXConfig._load_full_settings()
        data["modelGroups"] = {
            name: _mg.to_raw(g) if isinstance(g, _mg.ModelGroup) else _mg._canonicalize_raw(g)
            for name, g in groups.items()
        }
        OpenXConfig._save_full_settings(data)

    @staticmethod
    def set_active_group(name: str) -> None:
        """持久化激活组名 ``activeGroup``。"""
        data = OpenXConfig._load_full_settings()
        data["activeGroup"] = name
        OpenXConfig._save_full_settings(data)

    @staticmethod
    def ensure_model_groups() -> list[str]:
        """modelGroups 缺失且存在旧结构时，自动迁移并落盘一次。

        返回迁移说明（无迁移返回空列表）。已含 modelGroups 或没有任何
        旧结构（留给内存合成兜底）时不动文件。
        """
        data = OpenXConfig._load_full_settings()
        if data.get("modelGroups"):
            return []
        new_data, notes = _mg.migrate_legacy(data)
        if not notes:
            return []
        OpenXConfig._save_full_settings(new_data)
        return notes

    # ── Plugin management（微内核 P1）─────────────────────────

    @staticmethod
    def load_plugin_settings() -> dict:
        """Plugin settings from the top-level ``plugins`` key.

        Example::

            {"plugins": {"disabled": ["noisy_plugin"]}}
        """
        return OpenXConfig._load_full_settings().get("plugins", {})

    # ── MCP server management ──────────────────────────────────────

    @staticmethod
    def load_mcp_servers() -> dict[str, dict]:
        """Load configured MCP servers from settings.json (global)."""
        return OpenXConfig._load_full_settings().get("mcpServers", {})

    @staticmethod
    def save_mcp_server(name: str, config: dict) -> None:
        """Add or update an MCP server in settings.json."""
        data = OpenXConfig._load_full_settings()
        servers = data.setdefault("mcpServers", {})
        servers[name] = config
        OpenXConfig._save_full_settings(data)

    @staticmethod
    def delete_mcp_server(name: str) -> bool:
        """Remove an MCP server from settings.json. Returns True if it existed."""
        data = OpenXConfig._load_full_settings()
        servers = data.get("mcpServers", {})
        if name in servers:
            del servers[name]
            OpenXConfig._save_full_settings(data)
            return True
        return False

    # ── Trust management ─────────────────────────────────────────

    @staticmethod
    def is_trusted(workspace: str) -> bool:
        """Check if a workspace directory has been trusted by the user."""
        data = OpenXConfig._load_full_settings()
        trusted: list[str] = data.get("trusted_dirs", [])
        workspace_abs = str(Path(workspace).resolve())
        return workspace_abs in trusted

    @staticmethod
    def add_trusted_dir(workspace: str) -> None:
        """Mark a workspace directory as trusted."""
        data = OpenXConfig._load_full_settings()
        trusted: list[str] = data.get("trusted_dirs", [])
        workspace_abs = str(Path(workspace).resolve())
        if workspace_abs not in trusted:
            trusted.append(workspace_abs)
        data["trusted_dirs"] = trusted
        OpenXConfig._save_full_settings(data)

    @classmethod
    def load(cls, workspace: Optional[str] = None) -> "OpenXConfig":
        """Load config from all sources, merging in priority order.

        Priority (lowest → highest):
        0. settings.json env values
        1. ~/.openx/config.json
        2. .openx.json (project-level)
        3. Environment variable overrides
        """
        # 存量结构（扁平 env / providers / profiles）首次加载时迁移为模型组
        OpenXConfig.ensure_model_groups()

        config = cls()
        config.settings_loaded = True

        if workspace:
            config.workspace = workspace

        # 0. settings.json env
        settings_env = cls.load_settings()
        if settings_env.get("OPENX_API_KEY"):
            config.api_key = settings_env["OPENX_API_KEY"]
        if settings_env.get("OPENX_BASE_URL"):
            config.api_base = settings_env["OPENX_BASE_URL"]
        if settings_env.get("OPENX_DEFAULT_MODEL"):
            config.model = settings_env["OPENX_DEFAULT_MODEL"]

        # 1. Global user config
        global_config = Path.home() / ".openx" / "config.json"
        if global_config.exists():
            try:
                config._merge(json.loads(global_config.read_text()))
            except (json.JSONDecodeError, OSError):
                pass

        # 2. Project-level config (.openx/settings.json)
        project_settings = Path(config.workspace) / ".openx" / "settings.json"
        if project_settings.exists():
            try:
                config._merge(json.loads(project_settings.read_text()))
            except (json.JSONDecodeError, OSError):
                pass

        # 2b. Legacy project config (.openx.json, deprecated)
        legacy_project = Path(config.workspace) / ".openx.json"
        if legacy_project.exists():
            try:
                config._merge(json.loads(legacy_project.read_text()))
            except (json.JSONDecodeError, OSError):
                pass

        # 3. Environment variable overrides (highest priority)
        if os.environ.get("OPENAI_API_KEY"):
            config.api_key = os.environ["OPENAI_API_KEY"]
        if os.environ.get("OPENAI_API_BASE"):
            config.api_base = os.environ["OPENAI_API_BASE"]
        if os.environ.get("OPENX_MODEL"):
            config.model = os.environ["OPENX_MODEL"]
        if os.environ.get("OPENX_AUTO_APPROVE"):
            config.auto_approve = os.environ["OPENX_AUTO_APPROVE"].lower() == "true"
        if os.environ.get("OPENX_WEB_SEARCH"):
            config.web_search_provider = os.environ["OPENX_WEB_SEARCH"].lower()

        return config

    def _merge(self, data: dict) -> None:
        """Merge a config dict into this instance."""
        for key, value in data.items():
            if hasattr(self, key):
                if isinstance(value, list) and isinstance(getattr(self, key), list):
                    # For lists, extend (don't replace)
                    getattr(self, key).extend(value)
                else:
                    setattr(self, key, value)

    # ── 模型组解析（modelGroups，唯一咽喉点）────────────────────

    def _cli_overrides(self) -> dict:
        """当前临时 CLI 覆盖（仅 main 角色消费）。"""
        out: dict = {}
        if self.cli_model_override:
            out["model"] = self.cli_model_override
        if self.cli_api_key_override:
            out["api_key"] = self.cli_api_key_override
        if self.cli_api_base_override:
            out["api_base"] = self.cli_api_base_override
        return out

    def _synthesize_default_group(self) -> "_mg.ModelGroup":
        """无任何组时的内存合成 default 组（不落盘）。

        保留「手动设 config 字段 / 无 settings 文件」用法（测试与嵌入），
        main 绑定 = 本 config 的 model/key/base。
        """
        g = _mg.ModelGroup(
            name="default",
            kind="openai-compat",
            api_key=self.api_key or None,
            api_base=self.api_base or None,
        )
        g.roles[_mg.MAIN_ROLE] = _mg.RoleBinding(_mg.MAIN_ROLE, self.model or "")
        return g

    def _file_groups(self) -> tuple[dict, str]:
        """文件里的 (解析组, 激活名)；手动构造的 config 一律不读（见字段注）。"""
        groups, active, _ = OpenXConfig.load_model_groups()
        if groups and not self.settings_loaded:
            return {}, ""
        return groups, active

    def active_group_name(self) -> str:
        """当前生效的组名（self.active_group 投影 > activeGroup > 首个 > default）。"""
        groups, active = self._file_groups()
        if self.active_group and self.active_group in groups:
            return self.active_group
        if active in groups:
            return active
        if groups:
            return next(iter(groups))
        return "default"

    def resolve_group(self, name: Optional[str] = None) -> "_mg.ModelGroup":
        """解析指定（或当前激活）模型组；无组时返回内存合成 default。"""
        groups, active = self._file_groups()
        if not groups:
            return self._synthesize_default_group()
        target = name or self.active_group or active
        if target not in groups:
            target = active if active in groups else next(iter(groups))
        return groups[target]

    def role_settings(
        self, role: str, group_name: Optional[str] = None
    ) -> tuple[str, dict]:
        """解析 (组, 角色) 的 provider 设置 → ``(生效组名, settings dict)``。

        角色可为长键或别名（main/exec/mini/modal）。dict 键与 provider 工厂
        读取一致（kind/api_key/api_base/model/temperature/max_tokens，retry
        字段仅在组/角色显式声明时出现）——消费方零改动。CLI 临时覆盖仅对
        main 角色生效（历史 ``-m`` 最大的语义保留）。
        """
        role_key = _mg.canonical_role(role) or role
        group = self.resolve_group(group_name)
        return group.name, _mg.resolve_role_settings(
            self, group, role_key, self._cli_overrides()
        )

    def save_global(self) -> None:
        """Save global config."""
        global_dir = Path.home() / ".openx"
        global_dir.mkdir(parents=True, exist_ok=True)
        config_file = global_dir / "config.json"

        data = {
            "api_key": self.api_key,
            "api_base": self.api_base,
            "model": self.model,
            "temperature": self.temperature,
            "auto_approve": self.auto_approve,
        }
        config_file.write_text(json.dumps(data, indent=2))


if __name__ == "__main__":
    import tempfile

    # OpenXConfig.load：workspace 指向临时目录（不调用 save_global，不写真实 home）
    with tempfile.TemporaryDirectory() as _td:
        _cfg = OpenXConfig.load(workspace=_td)
        print(f"workspace     = {_cfg.workspace}")
        print(f"model         = {_cfg.model or '(unset)'}")
        print(f"auto_approve  = {_cfg.auto_approve}")
        print(f"max_tool_rounds = {_cfg.max_tool_rounds}, allowed_commands[:5] = {_cfg.allowed_commands[:5]}")
        assert _cfg.workspace == _td

        # _merge 演示：标量覆盖、列表追加、未知字段忽略
        _cfg._merge({"temperature": 0.7, "allowed_commands": ["rg"], "bogus_field": 1})
        assert _cfg.temperature == 0.7
        assert _cfg.allowed_commands[-1] == "rg"
        assert not hasattr(_cfg, "bogus_field")
        print(f"_merge: temperature={_cfg.temperature}, 'rg' appended ✓, unknown key ignored ✓")

    print("openx/config.py OK ✓")
