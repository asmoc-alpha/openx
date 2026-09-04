# 模型接入层设计 · Provider 零件化（P1 全量）

> 状态：**已定稿并实施**（2026-08-24 计划确认，2026-08-26 落地 M1~M5）。
> 范围与三个决断由用户拍板：P-A~P-D 全做；重试/退避**现在就上收内核**；
> Anthropic 原生**同批实现**。
>
> 上位文档：`openx-architecture-design.md`（v4.1 §8.2 零件与槽表：
> Provider 是零件、重试/退避/超时归内核、实现固定 chat/stream 接口）、
> `microkernel-design.md`（四职责与搁置决定）。
>
> 与 2026-08 架构图的映射：本文落地的是内核五件套中 **① 推理核心**的
> 现状面——形状进内核（`kernel/provider.py`）、重试/退避归内核
> （`kernel/retry.py`）、实现进零件（`llm/openai_compat.py`、
> `llm/anthropic.py`）；路由 / fallback / 限流 / 结构化输出约束等推理核心
> 其余面为增量（microkernel-design §5.3 N2）。协议适配一律留在 llm/，
> 内核只认识错误契约，不 import 任何 SDK。
>
> 与搁置决定的关系：沙箱执行与记账深化已搁置，本文**不依赖**它们--
> 记账只用已有的 `emit()` 出口（P-D），沙箱面完全不碰。

---

## 1. 现状基线与差距

| 部件 | 现状 | 差距 |
|---|---|---|
| 接入方式 | `agent.py:158` 硬编码 `LLMClient(config)` | 未过内核：换 provider = 改代码，不是换零件 |
| 接口形状 | `LLMClient.chat/stream_chat` 事实存在 | 不是显式契约；实现与形状混在一个类里 |
| 重试 | `llm/client.py` 内嵌（429/5xx/连接/断流、Retry-After、指数退避+抖动、60s 封顶、on_retry 可见性） | 归实现所有，与总架构"重试归内核"相悖 |
| 配置 | 扁平 `api_key/api_base/model` 三件套 | 单连接；多 provider 无处安放 |
| 格式 | 仅 OpenAI 兼容 | Anthropic 原生 ❌（对比文档已知差距） |

## 2. 接口形状：内核不变量（P-A）

**形状进内核，实现进零件。** 新增 `kernel/provider.py`，纯定义、零
SDK 依赖：

```python
# ── 流事件（从 llm/client.py 上移，llm 侧 re-export 过渡）──
@dataclass
class StreamReasoning: ...
@dataclass
class StreamDone: ...
StreamEvent = str | StreamReasoning | StreamDone

class Provider(Protocol):
    """模型接入槽的接口形状--内核不变量（槽接口形状）。"""
    async def chat(self, messages, tools=None, stream=True) -> dict: ...
    async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamEvent]: ...

# ── 错误契约 ──
class ProviderTransientError(Exception):
    """瞬态错误（连接/429/5xx）：内核可重试。retry_after 透传服务端值。"""
    retry_after: float | None
class ProviderFatalError(Exception):
    """确定性错误（401/400/…）：重试无意义，直接上抛。"""
```

实现层（`llm/`）只做协议适配，捕获 SDK 异常后翻译为上述两类。内核
重试机制只认识错误契约，不认识任何 SDK。

## 3. 重试上收内核（决断：现在做）

新增 `kernel/retry.py`，语义与现有 `LLMClient` **逐条等价**（这是硬
约束，不是"差不多"）：

```python
class RetryPolicy:
    max_retries / base_delay / cap=60s
    def delay(attempt, retry_after) -> float   # Retry-After 优先，指数退避+抖动

class RetryingProvider:
    """内核所有的重试包装：实现 Provider，组合替代继承。
    - chat()：整请求重试（无可见中间态，任何时刻可重试）
    - stream_chat()：仅在"尚未 yield 任何事件"时可透明重试；
      已产出文本/reasoning 后断流只能上抛（透明重试=重复上屏）
    - on_retry 回调可见性：异常吞掉，UI 故障不搞崩重试
    """
```

**守护策略**（重试是 763 个测试里最久经考验的一块）：

1. 语义逐条搬运：`_classify_error` 的状态码集合、`_compute_delay` 的
   退避公式、断流判据（`emitted_text`）原样保留，只换位置与错误类型；
2. 分类逻辑留在实现层（SDK 异常 -> 契约错误），重试决策与等待在内核；
3. `LLMClient` 保留为兼容门面（组 `OpenAICompatProvider` +
   `RetryingProvider`），现有 tests/llm 用例改跑门面路径，断言不变。

## 4. providers 注册项：目录字段首次被消费

`REGISTRATIONS` 加一行：

```python
PluginRegistration("providers", validate_provider,
                   conflict=CONFLICT_FIRST_WINS, hotplug=HOTPLUG_BOUNDARY)
```

- **值 = 实现工厂**：`create(provider_config: dict) -> Provider`。
  注册项的键是**实现名**（`"openai-compat"` / `"anthropic"`），不是
  用户配置名--注册表存"有哪几种实现"，settings 存"用户配了哪几个实例"。
- **`hotplug="boundary"` 首次生效**：providers 仅 boot 装配、会话边界
  换（有连接状态，不做会话内热插）；tools 的 `session` 档维持现状。
  这是注册目录里 `conflict`/`hotplug` 字段第一次真正分叉消费。
- `validate_provider`：工厂可调用 + 实现名合法（`[a-z0-9-]+`）。

## 5. base bundle：两个内置 provider 插件

base bundle 从单插件扩为**内置插件列表**（kernel 加载处由写死
`BUILTIN_TOOLS_ID` 改为遍历 `BUILTIN_PLUGINS`，先见者赢语义不变）：

| 内置插件 | 注册 | 失败语义 |
|---|---|---|
| `builtin-tools`（现有） | 工具工厂 | 致命（不变） |
| `builtin-providers`（新） | `openai-compat` 实现；SDK 缺失时 anthropic **跳过注册而非失败**（可选依赖不是产品缺陷） | openai-compat 致命；anthropic 装不上=零贡献 |

`OpenAICompatProvider` = 现 `LLMClient` 去掉重试循环后的单次实现
（重试由内核 `RetryingProvider` 包装）；`LLMClient` 名字保留为门面。

## 6. 配置：多 provider 与迁移（P-C）

> ⚠️ **已废弃（superseded）**：§6 的 `active_provider`+`providers` 扁平实例与
> 迁移规则已被 modelGroups 取代（见 `docs/user/guide/configuration.md`）。代码不再
> 读取或迁移这些旧结构——模型/凭据只经 `settings.json` 的 `modelGroups` 表达。以下
> 仅作历史设计存档。

```json
{
  "active_provider": "deepseek",
  "providers": {
    "deepseek":  {"kind": "openai-compat", "api_key": "…", "api_base": "…", "model": "…"},
    "claude":    {"kind": "anthropic",     "api_key": "…", "model": "…"}
  }
}
```

- **两级解耦**：`kind` 选实现（注册表键）；外层名字是用户的实例名。
- **迁移规则（行为≡现状）**：`providers` 缺失时，扁平
  `api_key/api_base/model` 合成隐式实例 `default`（kind=
  openai-compat），`active_provider="default"`。存量配置零改动。
- 实例可覆盖 `max_tokens/temperature/max_retries/retry_base_delay`，
  缺省回落全局字段；`/model` 继续只改激活实例的 model。
- 新命令 `/provider`：无参=列出实例与激活态；`/provider <name>` 切换
  （校验存在性，切换即重建 agent 的 provider 绑定）。setup wizard 增加
  provider 类型选择（OpenAI 兼容 / Anthropic）。

## 7. Anthropic 原生适配（决断：同批做）

> 📌 **已演进**：Anthropic 实现现为 **anthropic-compat**（Anthropic-format 兼容协议，
> 不只官方 Claude）——base URL 可经组/角色的 `apiBase` 指向任意兼容端点（如 DeepSeek），
> 留空默认 Anthropic 官方；旧 kind `anthropic` 注册为别名。见 `docs/user/guide/configuration.md`。

新增 `llm/anthropic.py`：`AnthropicProvider` 实现 Provider 接口，
**在边界做消息格式双向转换**，系统其余部分继续说 OpenAI 格式：

| 方向 | 转换 |
|---|---|
| 入（OpenAI -> Anthropic） | system 消息抽为 `system` 参数；assistant `tool_calls` -> `tool_use` content block；`role=tool` 消息 -> user 的 `tool_result` block；多模态 `image_url`(base64) -> image block |
| 出（Anthropic -> OpenAI） | content blocks 组装回 `{role, content, tool_calls}` dict；thinking block -> `StreamReasoning`；usage 字段对齐 |
| 流式 | SSE 事件（content_block_delta 等）映射为 `StreamEvent`：text delta -> str、thinking delta -> StreamReasoning、tool_use 分片缓冲、message_delta 带 stop_reason/usage |

- 依赖：`anthropic` SDK 走**可选 extra**（`pip install openx[anthropic]`），
  缺失时该 provider 不注册（见 §5），不是报错。
- 测试全部离线：双向转换单测 + 流事件映射单测（伪 SSE 序列驱动），
  绝不真实联网。

## 8. 记账接线（P-D，轻量）

只用已有 `emit()` 出口，不新增机制：`provider_selected` 事件--
agent 绑定 provider 与 `/provider` 切换时各记一条（payload：实例名、
kind、model、origin=user|kernel）。切换留痕是将来"为什么这次回答换了
模型"的答案来源。

## 9. 落地切片（每步可独立验收，前三步行为≡现状）

1. ~~**M1 形状与重试上收**~~ **已完成**（2026-08-26）：`kernel/provider.py`
   （形状+错误契约）+ `kernel/retry.py`（策略+`RetryingProvider`）；
   `llm/openai_compat.py` 重构为单次实现 + 门面；`llm/base.py` 收口实现
   层的共享编排面（chat/stream_chat 骨架 + SDK 异常->契约翻译，openai_compat
   与 anthropic 共用）；重试测试迁移至内核层，语义断言逐条不变。
2. ~~**M2 注册与内置迁移**~~ **已完成**（2026-08-26）：providers 注册项；
   `builtin-providers` 插件；base bundle 插件列表化；`agent.llm` 经内核
   解析（flat 配置 -> default 实例，行为≡现状）。
3. ~~**M3 多 provider 配置**~~ **已完成**（2026-08-26）：config
   `providers`/`active_provider` 与迁移（`resolve_provider` 合成隐式
   default 实例）；`/provider` 命令（列出/切换即重建绑定）；`/model` 只改
   激活实例的 model；setup wizard 增加 provider 类型选择。
4. ~~**M4 Anthropic 适配**~~ **已完成**（2026-08-26）：`llm/anthropic.py`
   （边界双向转换 + 流事件映射 + 错误契约）+ 可选 extra
   （`pip install openx[anthropic]`）+ 离线转换测试。
5. ~~**M5 记账**~~ **已完成**（2026-08-26）：`provider_selected` 事件
   （agent 绑定 origin=kernel、/provider 切换 origin=user）。

依赖序：M1 -> M2 -> M3 ->（M4、M5 可并行）。

## 10. 风险与守护

| 风险 | 守护 |
|---|---|
| 重试上收改坏久经考验的语义 | 语义逐条搬运 + 门面保旧 API + 既有 llm 测试改跑门面路径且断言不变 |
| Anthropic 转换错误（格式细节多） | 转换层纯函数化、双向单测全覆盖、伪 SSE 流测试；不碰联网 |
| 配置迁移破坏存量用户 | 无 `providers` 键走 default 合成路径，扁平字段优先级不变；迁移路径专项测试 |
| 内核膨胀（违背"小到可审计"） | `kernel/provider.py`/`retry.py` 只含形状与策略（~250 行，零 SDK import）；协议适配全部留在 llm/ |

---

*实施中机制变更须先改本文再改代码；与 `openx-architecture-design.md`
§8.2 的零件表冲突时以本文为准并回写。*
