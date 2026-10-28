# openx serve · Web 端（P4）

> 会话长存服务端 + 浏览器客户端。对齐架构详设（`openx-architecture-design.md`）
> P4：**会话共享、远程批准、复盘回放**；"加端 = 写一个客户端 attach 协议，内核零改动"。

## 启动

```bash
pip install 'openx[web]'      # aiohttp 是 web optional extra
openx serve                   # 默认 http://127.0.0.1:8787
openx serve --host 0.0.0.0 --port 9000
```

首启仍需工作区信任（终端一次确认）；未配 API key 时走设置向导。服务启动后
终端停留，Ctrl-C 干净收尾（打断当前回合 → 停服务 → 关 MCP）。

## Web UI

- **聊天**：流式文本、thinking 折叠/展开、工具调用块（点击展开输出）；
- **远程权限批准**：写工具逐项弹窗（手动模式默认）——允许一次 / 允许并记住 /
  拒绝；**fail-closed**：无客户端 / 断流 / 超时 / 未匹配 request_id 一律拒绝；
- **交互弹窗**：`ask_user`（单选/多选 + Other 自由输入 + Skip）与计划审批
  （Approve / Reject）都在页面上真交互；任一客户端应答即唤醒，无人应答
  （超时 / 断流 / Skip）服务端落保守默认或拒绝；
- **多端 attach**：开多个标签页同看一会话，事件广播；迟到客户端收到
  `init` + `history` 快照 + 当轮已广播内容；
- **会话列表 + 复盘**：侧栏列出历史会话，点击回放对话与权限决策；
- **interrupt**：回合进行中可停止（等价终端 Esc）。
- **任务面板**（右栏「任务流」）：一次回合 = 一个任务，步骤 = 本轮真实发生
  的工具调用。工具按服务端下发的 `call_id` **配对**（并行批次里同名工具不会
  错配）、按 `round` **分轮**；耗时只用服务端实测值（测不到就不显示，不当 0）；
  点一行展开原始输出，失败默认展开并显示首行错误。权限 / 提问 / 计划审批
  期间回合标「等待你处理」并显示在等谁批什么，裁决结果留成时间线夹注
  （已允许 / 已拒绝 / 超时 / 断流 / 随回合取消，系统行为不冒充人的决定）。
  **三条诚实性纪律**：不猜进度百分比（总步数未知时那是编的，进行中走不确定
  态动画）、不猜结果（历史未持久化失败标记就不标成功/失败）、没有数据就空态。
  刷新 / 回放不再整片空掉：`GET /api/flow` 从会话消息派生同一份视图。
- **插件面板**：`ui.panel` 类插件（如桌面宠物）的常驻面板区，输入框上方
  动画/小部件；坏面板服务端隔离，端只做哑渲染（纯文本行）。

`--auto-approve` 会切到 auto 模式（跳过批准，仍受危险命令闸门约束）。

## 协议（线格式单一真源 = `openx/kernel/protocol.py`）

下行（服务端 → 端）：

| 事件 | 说明 |
|---|---|
| `system/init` | 开场：session_id / model / tools |
| `history` | attach 快照：既有对话消息列表 |
| `user_message` | 一条用户消息 |
| `text_delta` / `thinking_delta` | 流式文本 / 推理增量（`[dim]` 标签已剥） |
| `tool_use` | 工具开始：name / args_summary（shell 给**完整命令**）/ target / `call_id`（配对键）/ `round`（本回合第几次模型请求产生这批调用） |
| `tool_result` | 工具结果：name / is_error / output（上限 2000 字符）/ `call_id` / `round` / `duration_ms`（**服务端实测**，测不到则**不出现该键**）/ `output_chars`（截断前原始长度）/ `truncated` |
| `permission_request` | 权限请求：request_id / tool / reason / can_remember |
| `panels` | 插件 UI 面板快照（ui/v1）：`{name, lines}`，行已剥 rich 标签；变化才广播（常驻 ticker ~4Hz，有客户端才跑），attach 快照含当前面板 |
| `result` | 单轮终局：subtype / duration_ms / num_turns / usage（`input_tokens` / `output_tokens` / `cached_tokens` / `plugin_tokens`，均**会话累计**口径） |
| `interrupted` | 回合被打断：kind（serve 目前恒为 `client`——Esc / 停止钮；信号路径走 CLI） |
| `ask_user` | 交互提问：request_id / question / options（`{label, description}`）/ multi_select；端以同 request_id 的 `ask_user_response` 应答，超时 / 断流 / 空答服务端落保守默认 |
| `plan_request` | 计划审批：request_id / plan；端以同 request_id 的 `plan_response` 应答，超时 / 断流按拒绝 |
| `interaction` | 人工闸门**可观测性**：`phase=open`（kind = permission / ask_user / plan，带 tool / detail ="在等谁批什么"）→ `phase=close`（outcome = approved / denied / timeout / answered / skipped / rejected / cancelled / disconnected）。**紧跟**在对应请求事件之后（首个事件仍是请求——老客户端只认它），是任务面板"卡在哪 / 谁批的"的唯一真源 |

上行（端 → 服务端）：

| 意图 | 格式 |
|---|---|
| 消息 | `{"type":"message","text":...}` |
| 打断 | `{"type":"interrupt"}` |
| 权限裁决 | `{"type":"permission_response","request_id":...,"allowed":...,"remember":...}` |
| 提问应答 | `{"type":"ask_user_response","request_id":...,"answers":[...]}`（answers 为空 → 服务端落保守默认，不等待超时） |
| 计划裁决 | `{"type":"plan_response","request_id":...,"approved":bool}` |

## REST（只读）

| 端点 | 说明 |
|---|---|
| `GET /` · `/static/*` | 自包含前端（无构建、无 CDN） |
| `GET /ws` | WebSocket 事件流（下行广播 + 上行意图） |
| `GET /api/sessions` | 会话 meta 列表（updated_at 倒序） |
| `GET /api/sessions/{id}/events` | 复盘：消息行 + 账本行（seq 有序投影） |
| `GET /api/trace?session=<id>` | 任务路径：按回合分组的执行轨迹（步骤 + 输出分块，逐段全文） |
| `GET /api/flow?session=<id>` | 任务流：与 `/api/trace` **同源派生**（输出按 `call_id` 合进步骤），任务面板刷新 / 回放的数据源；缺 `session` = 当前实时会话 |

## 已知限制（MVP，后续跟进）

- **复盘是投影**：转录事件（text/tool/thinking）当前不进账本，回放 =
  会话文件消息行 + 控制/决策账本行，非逐字节重播。转录事件入账本后升级
  为"回放 = 账本重发"（架构详设 §3.3）。
- **历史不回填失败与耗时**：会话文件只存 tool 消息正文（没有 `is_error`、
  没有单步耗时），故任务面板对"上一轮"不给耗时、失败状态如实留空（不冒充
  成功），无对应结果的调用标「未返回结果」。
- 首启设置向导 / 工作区信任仍需 TTY；headless + 未信任时先 `openx` 跑一次。

## 架构

```
aiohttp app（server.py）
├── /ws            ServeSession.handle_ws：上行分发 message/interrupt/permission_response/ask_user_response/plan_response
├── ServeSession   串行回合队列（REPL 语义）→ agent.stream_run → 事件投影 → 广播
│   ├── 每客户端 downlink 队列任务独占 ws.send_json（并发广播不撕裂帧）
│   └── 权限桥 WebPermissionBridge：广播 permission_request/ask_user/plan_request，fail-closed 等待
├── ServeConsole   agent 的无 TUI console：ask_permission/ask_user_question_async/confirm_plan_async → 桥；print_* → no-op
│   └── 工具侧 console_dialog（tools/）：async 优先——serve 走 bridge 应答通道，TUI 回退同步版
└── 静态前端 web/  纯函数 reducer + XSS 安全渲染（先转义后 markdown）；交互弹窗 renderAskOptions/renderPlan；
    任务面板 task-panel.js：call_id 配对 + round 分轮 + interaction 留痕，历史走 /api/flow
```
