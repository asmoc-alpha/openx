"""Configuration management for OpenX.

模型/凭据配置**唯一**来自 ``~/.openx/settings.json`` 的 ``modelGroups`` /
``activeGroup``（解析见 :mod:`openx.model_groups`）。OpenXConfig 本身只承载
非模型配置（权限/UI/指令/重试默认等）与两个解析后 echo（``model`` /
``active_group``），不再读取任何扁平旧结构（settings ``env`` 段、
``~/.openx/config.json``、``.openx.json``、``OPENAI_API_KEY`` 等直读）。
本模块同时负责 settings.json 各顶层键的读写（modelGroups/mcpServers/
hooks/plugins/trusted_dirs）。
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
    # model 只是「解析后 echo」（供 header/会话元展示），不是配置输入——
    # 模型/凭据/端点唯一来自 modelGroups（经 role_settings 解析的 settings
    # dict），此处无 env/文件默认。
    model: str = ""   # 激活组 main 模型 echo（main/agent 解析后回写）
    max_tokens: int = 8192
    temperature: float = 0.0

    # ── 模型组（modelGroups）──────────────────────────────────────
    # active_group 是当前绑定组名的投影（agent 构造/切组时回写），供 UI 展示。
    # cli_*_override 为临时 CLI 覆盖（main.py 置位），仅对 main 角色生效。
    active_group: str = ""
    cli_model_override: Optional[str] = None
    cli_api_key_override: Optional[str] = None
    cli_api_base_override: Optional[str] = None
    # load() 置位 True：实例必须走文件 modelGroups，无组即未配置（role_settings
    # 抛错，CLI 路径由 is_configured 门拦下）。手动构造（测试/嵌入）保持 False，
    # 不读全局 modelGroups（避免真实 ~/.openx 泄漏进单测），无组走内存合成。
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
    def is_configured() -> bool:
        """Check if a model group with an active main binding is configured.

        纯读判定（不再触发任何迁移）：settings.json 的 ``modelGroups`` 是否
        含激活组的 main 角色且带 model（api_key 是否齐全由启动校验另行
        要求）。无任何组时返回 False（交给 setup 向导）。
        """
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
        """加载配置。模型/凭据**不在此处读取**——只经 ``modelGroups`` 由
        ``role_settings()`` 解析。这里只合并非模型项目配置与运行旋钮：
        项目 ``<workspace>/.openx/settings.json``（顶层键，排除 model/
        active_group）+ 非 provider 环境变量（auto_approve/web_search）。
        无 modelGroups 时 ``is_configured()`` 为 False（首启走向导）；
        ``role_settings()`` 会抛错而非静默合成。
        """
        config = cls()
        config.settings_loaded = True

        if workspace:
            config.workspace = workspace

        # 项目级 config (.openx/settings.json)：只并非模型键
        # （allowed_commands / auto_approve / ...）；模型/凭据不在项目层设。
        project_settings = Path(config.workspace) / ".openx" / "settings.json"
        if project_settings.exists():
            try:
                config._merge(
                    json.loads(project_settings.read_text()),
                    exclude={"model", "active_group"},
                )
            except (json.JSONDecodeError, OSError):
                pass

        # 环境变量：只覆盖非 provider 旋钮（模型/凭据唯一来自模型组）
        if os.environ.get("OPENX_AUTO_APPROVE"):
            config.auto_approve = os.environ["OPENX_AUTO_APPROVE"].lower() == "true"
        if os.environ.get("OPENX_WEB_SEARCH"):
            config.web_search_provider = os.environ["OPENX_WEB_SEARCH"].lower()

        return config

    def _merge(self, data: dict, exclude: frozenset = frozenset()) -> None:
        """Merge a config dict into this instance.

        ``exclude`` 里的键跳过（用于阻止项目文件注入解析后 echo/模型键）。
        未知键（无对应属性）忽略；列表字段改为追加而非替换。
        """
        for key, value in data.items():
            if key in exclude or not hasattr(self, key):
                continue
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
        """手写/嵌入构造（settings_loaded=False）的极简内存组（不落盘）。

        仅测试与嵌入式构造在无磁盘组时走这里：main 绑定 = ``self.model``
        echo（可能空），kind=openai-compat，**不带任何凭据**——凭据只来自
        组配置，这里不再兜底。load() 产出的配置不合成（见 resolve_group）。
        """
        g = _mg.ModelGroup(name="default", kind="openai-compat")
        g.roles[_mg.MAIN_ROLE] = _mg.RoleBinding(_mg.MAIN_ROLE, self.model or "")
        return g

    def _file_groups(self) -> tuple[dict, str]:
        """文件里的 (解析组, 激活名)；手动构造的 config 一律不读（见字段注）。"""
        groups, active, _ = OpenXConfig.load_model_groups()
        if groups and not self.settings_loaded:
            return {}, ""
        return groups, active

    def active_group_name(self) -> str:
        """当前生效的组名（self.active_group 投影 > activeGroup > 首个）；无组返回 ""。"""
        groups, active = self._file_groups()
        if not groups:
            return ""  # 未配置：load() 路径由 is_configured / role_settings 拦下
        if self.active_group and self.active_group in groups:
            return self.active_group
        if active in groups:
            return active
        return next(iter(groups))

    def resolve_group(self, name: Optional[str] = None) -> "_mg.ModelGroup":
        """解析指定（或当前激活）模型组。

        文件无组时：手写构造（settings_loaded=False）合成极简 default 组；
        load() 产出的配置（settings_loaded=True）视为**未配置**——抛
        ValueError 及早暴露（CLI 首启已被 is_configured 门拦下走向导）。
        """
        groups, active = self._file_groups()
        if not groups:
            if self.settings_loaded:
                raise ValueError(
                    "no modelGroups configured in ~/.openx/settings.json — "
                    "run 'openx' to launch the setup wizard"
                )
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

if __name__ == "__main__":
    import tempfile

    # OpenXConfig.load：workspace 指向临时目录（不写真实 home）
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
