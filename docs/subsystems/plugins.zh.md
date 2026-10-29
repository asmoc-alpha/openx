# 插件

[English](plugins.md) | 中文

插件是一段通过 `apply(ctx)` 向内核注册能力（工具/命令/上下文片段/生命周期钩子/
UI 面板）的代码单元。内置之外 agent 的一切能力都从这条通道进来——包括模型
自产的插件。

```
<name>.py                       # 单文件插件
<plugins>/<name>/plugin.py      # 目录插件（数据资产与入口同目录）
```

发现顺序（同 id 先见者赢）：`~/.openx/plugins/` → `<ws>/.openx/plugins/` →
pip entry-points 组 `openx.plugins`。内置插件（`openx/builtin/*`）是预导入
模块，不走此路径。

## 最小插件

```python
__openx_meta__ = {
    "type": "capability.tool",
    "mount": "loop.tool-call",
    "trust": "user",
    "summary": "模型在 /plugins 里看到的一句话",
    "permissions": ["fs:read"],   # 能力上限声明——见下
}

from openx.tools.base import Tool, ToolResult

class MyTool(Tool):
    name = "my_tool"
    description = "..."
    parameters = {"type": "object", "properties": {...}}

    @property
    def permission(self):
        from openx.permissions import Permission, PermissionLevel
        return Permission(level=PermissionLevel.ASK, reason="...")

    async def execute(self, **kwargs):
        return ToolResult(output="...")

def apply(ctx):
    ctx.register_tool(MyTool())

def self_test():
    assert True
```

不想手打就：`/plugin new <name>` 生成骨架。

## Manifest 字段参考

| 键 | 类型 | 含义 |
|---|---|---|
| `type` | str | 协议路由：`capability.tool`、`context.memory`、`lifecycle`、`ui.panel`。未知 → 默认路由 + 警告。`strategy.planning`/`orchestration` 保留、尚无协议 |
| `mount` | str | 仅文档性（真实挂载点由协议表派生） |
| `trust` | str | `builtin` / `third-party` / `user` / `auto`（模型自产） |
| `summary` | str | `/plugins` 与模型目录里展示 |
| `permissions` | list | 能力**声明（attestation）**——见下 |
| `timeout` | number | 工具单次调用超时（缺省 60s） |
| `cost` | dict | `schemaTokens` 进预算记账 |
| `dependencies` | list | 装载时必须已 ACTIVE 的插件 id |
| `api` | int | 目标内核插件 API（当前 1）；不符拒载 |
| `protocol` | str | 显式协议钉扎（如 `tool/v1`），与 type 强校验 |
| `scaffold` | dict | E1 退场声明（`compensates`/`exit_when` 必答）——见 [scaffolds.zh.md](scaffolds.zh.md) |
| `config` | dict | 用户配置的 JSON Schema（settings.json 的 `plugins.config.<id>`）；装载期校验，`ctx.config` 注入（归并 default） |

顶层未知键**警告**（拼错的键绝不静默失效）；形状错误**拒载**。

## 注册面（`apply(ctx)`）

| 方法 | 注册内容 |
|---|---|
| `ctx.register_tool(tool)` | `Tool` 实例（校验 + `ProtectPluginTool` 包装：超时/输出上限/3 次崩溃熔断） |
| `ctx.register_tool_factory(name, factory)` | `factory(host) -> list[Tool]`；`host` 是只读 `ToolHost` 快照 |
| `ctx.register_command(name, handler, description, aliases)` | `/slash` 命令：`async (agent, console, args) -> bool` |
| `ctx.register_context(name, contribute, priority)` | 系统提示片段；priority 升序、5s 超时、3 次熔断 |
| `ctx.register_lifecycle(name, on_session_start=, on_checkpoint=, on_resume=, on_unload=)` | 会话生命周期钩子 |
| `ctx.register_ui_slot(name, render, refresh_hz)` | 状态 deck 面板（≤8 行；渲染 1s 超时；3 次熔断） |
| `ctx.plugin_dir` | 目录插件的根——数据资产相对它解析（单文件/内置为 `None`） |
| `ctx.config` | 本插件的用户配置：按 schema 校验 + default 归并（经 `/plugin config <name> [json|reset]` 读写） |

## 安全模型

- **boot 信任门**：`<ws>/.openx/plugins` 只在工作区受信任时装载（交互 CLI
  启动时问；`serve` fail-closed——用 `/trust`）。用户目录与 pip
  entry-points 默认可信（决策 #5）。
- **admission 沙箱**：插件代码的首次执行（import + `self_test`）在沙箱
  子进程里（macOS `sandbox-exec`：拒网、/tmp 外拒写；POSIX rlimits；15s
  超时即 kill）。内置与 entry-points 豁免。`OPENX_PLUGIN_ADMISSION=0`
  关门——仅调试。
- **权限上限**：`permissions` 是声明。不声明任何权限的插件，其工具的
  自声明级别被压到 ASK、`auto_allowed` 失效；声明了则维持自声明级别
  （高危仍被 Guard 强制 ASK）。词汇表：`fs:read`、`fs:write`、`network`、
  `shell`、`process`。
- **失败隔离**：插件崩溃绝不带死会话——PHASE_FAILED + 条目事务化清残 +
  模块卸载；工具崩溃走熔断自动摘除。
- 诚实边界：工具**运行期**是进程内执行（L1）；沙箱只覆盖 admission 阶段。
  权限上限是声明性的，不是行为拦截。

## 生命周期

`/plugin load|unload|reload <name>`（session 作用域）；boot 作用域由组合决定；
`on_unload` 在条目清理前执行。`auto-*` 插件（模型自产）boot 默认不装载，
可晋升（`promote_plugin`）经 overlay 持久，或一键批量回滚。

## 测试与调试

```
/plugin test <name>     # 在 admission 沙箱里重跑 self_test
/plugin reload <name>   # 显式开发重载（boot 作用域原地刷新）
/plugins、/plugin_help  # 清单 + 单插件详情（含警告）
OPENX_PLUGIN_AUTORELOAD=1  # 插件文件变化自动重组
```

`self_test` 是 admission 的放行前提（「自测是放行的前提，不是放行的替代」）
——写就有意义的：真实调用工具、断言结果、打印进度。

## 与 Skills 的边界

[Skills](skills.zh.md)（`SKILL.md`）是**指令包**：提示词层、渐进披露、
没有注册面——只骑在一条内置 context 片段上。插件是**代码**：注册能力。
两者不合并；协作方式 = skill 教模型"何时调用某插件工具"。

## 协议钉扎

插件可以钉住它编写时所依据的契约：

```python
__openx_meta__ = {"type": "capability.tool", "protocol": "tool/v1", ...}
```

声明会被**强制执行**：未知协议拒载；协议路由到的 type 与 `manifest.type`
不符拒载（message 写明两侧 type；检查器是 `kernel/assembly/protocols.py`
的 `find()`）。不声明 `protocol` 则按 type 表默认路由——存量插件零影响。
`strategy.planning` / `orchestration` 是保留 type 名、尚无协议行：暂不可
钉扎。清单投影（`/plugins`、`plugin_help`）在钉扎之外还暴露 `api`
（内核支持的插件 API 版本，当前 1）。
