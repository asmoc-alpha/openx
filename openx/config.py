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
        """Check if settings.json exists with all required fields.

        两条路径任一满足即视为已配置：扁平 env 三件套（存量），或
        providers 配置的激活实例带 api_key + model（M3 新增）。
        """
        env = OpenXConfig.load_settings()
        if all(
            env.get(k, "").strip()
            for k in ("OPENX_API_KEY", "OPENX_BASE_URL", "OPENX_DEFAULT_MODEL")
        ):
            return True
        ps = OpenXConfig.load_provider_settings()
        providers = ps["providers"]
        active = ps["active_provider"]
        if active in providers:
            inst = providers[active]
            return bool(
                inst.get("api_key", "").strip() and inst.get("model", "").strip()
            )
        return False

    # ── Model profiles management ─────────────────────────────────

    @staticmethod
    def load_model_profiles() -> dict[str, dict]:
        """Load saved model profiles from settings.json.

        Returns a dict like::

            {"gpt4o": {"model": "gpt-4o", "api_base": "...", "api_key": "..."}}
        """
        return OpenXConfig._load_full_settings().get("models", {})

    @staticmethod
    def save_model_profile(name: str, profile: dict) -> None:
        """Add or update a named model profile in settings.json."""
        data = OpenXConfig._load_full_settings()
        models = data.setdefault("models", {})
        models[name] = profile
        OpenXConfig._save_full_settings(data)

    @staticmethod
    def delete_model_profile(name: str) -> bool:
        """Delete a named model profile. Returns True if it existed."""
        data = OpenXConfig._load_full_settings()
        models = data.get("models", {})
        if name in models:
            del models[name]
            OpenXConfig._save_full_settings(data)
            return True
        return False

    # ── Provider instances management（模型接入层 P3，M3）─────────
    # settings.json 顶层 ``providers``/``active_provider``：两级解耦--
    # ``providers`` 存用户配的实例（名字 -> {kind, api_key, ...}），
    # ``active_provider`` 指名激活哪个实例；``kind`` 才选内核注册表里的
    # 实现（openai-compat / anthropic...）。缺省时由扁平字段迁移合成
    # 隐式 default 实例（见 :meth:`resolve_provider`），存量配置零改动。

    @staticmethod
    def load_provider_settings() -> dict:
        """读取 providers 配置：``{"providers": {...}, "active_provider": "..."}``。"""
        data = OpenXConfig._load_full_settings()
        return {
            "providers": data.get("providers", {}) or {},
            "active_provider": data.get("active_provider", "default") or "default",
        }

    @staticmethod
    def save_provider_settings(providers: dict, active_provider: str) -> None:
        """保存 providers 配置（顶层键），保留其他顶层键。"""
        data = OpenXConfig._load_full_settings()
        data["providers"] = providers
        data["active_provider"] = active_provider
        OpenXConfig._save_full_settings(data)

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
        config = cls()

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

    # ── Provider 实例解析（M3）─────────────────────────────────

    def resolve_provider(self) -> tuple[str, dict]:
        """解析激活的 provider 实例，返回 ``(实例名, 实例配置 dict)``。

        两级解耦的落点：``kind`` 选实现（内核注册表键），外层名字是用户
        实例名。迁移规则（行为≡现状）：settings 无 ``providers`` 键时，
        扁平 ``api_key/api_base/model`` 合成隐式实例 ``default``
        （kind=openai-compat），``active_provider="default"``。

        实例配置的缺省回落：api_key/api_base/model/temperature/max_tokens
        缺省取本 config 的全局字段（CLI/环境覆盖随 ``load()`` 已并入）；
        重试字段（max_retries/retry_base_delay）**只在实例显式声明时**出现
        --策略对象读 config 实时值，构造后再改 config 依然生效。
        """
        ps = OpenXConfig.load_provider_settings()
        providers = ps["providers"]
        if not providers:
            # 迁移：扁平字段合成隐式 default 实例
            return "default", {
                "kind": "openai-compat",
                "api_key": self.api_key,
                "api_base": self.api_base,
                "model": self.model,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            }
        active = ps["active_provider"]
        if active not in providers:
            active = next(iter(providers))  # 激活名失效时回落首个实例
        resolved = dict(providers[active])
        resolved.setdefault("kind", "openai-compat")
        for key in ("api_key", "api_base", "model", "temperature", "max_tokens"):
            resolved.setdefault(key, getattr(self, key))
        return active, resolved

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
