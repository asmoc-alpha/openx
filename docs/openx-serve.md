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
- **多端 attach**：开多个标签页同看一会话，事件广播；迟到客户端收到
  `init` + `history` 快照 + 当轮已广播内容；
- **会话列表 + 复盘**：侧栏列出历史会话，点击回放对话与权限决策；
- **interrupt**：回合进行中可停止（等价终端 Esc）。

`--auto-approve` 会切到 auto 模式（跳过批准，仍受危险命令闸门约束）。

## 协议（线格式单一真源 = `openx/core/protocol.py`）

下行（服务端 → 端）：

| 事件 | 说明 |
|---|---|
| `system/init` | 开场：session_id / model / tools |
| `history` | attach 快照：既有对话消息列表 |
| `user_message` | 一条用户消息 |
| `text_delta` / `thinking_delta` | 流式文本 / 推理增量（`[dim]` 标签已剥） |
| `tool_use` / `tool_result` | 工具开始 / 结果（输出上限 2000 字符） |
| `permission_request` | 权限请求：request_id / tool / reason / can_remember |
| `result` | 单轮终局：subtype / duration_ms / num_turns / usage |
| `interrupted` | 回合被打断 |
| `plan_request` / `ask_user` | 同步弹窗的通知（MVP 不交互，见限制） |

上行（端 → 服务端）：

| 意图 | 格式 |
|---|---|
| 消息 | `{"type":"message","text":...}` |
| 打断 | `{"type":"interrupt"}` |
| 权限裁决 | `{"type":"permission_response","request_id":...,"allowed":...,"remember":...}` |

## REST（只读）

| 端点 | 说明 |
|---|---|
| `GET /` · `/static/*` | 自包含前端（无构建、无 CDN） |
| `GET /ws` | WebSocket 事件流（下行广播 + 上行意图） |
| `GET /api/sessions` | 会话 meta 列表（updated_at 倒序） |
| `GET /api/sessions/{id}/events` | 复盘：消息行 + 账本行（seq 有序投影） |

## 已知限制（MVP，后续跟进）

- **plan 审批 / ask_user 弹窗**在 web 上非交互：`confirm_plan` 广播
  `plan_request` 后按拒绝处理（模型修订计划重交）；`ask_user_question`
  广播 `ask_user` 后取保守默认（mode 询问默认留在 manual，绝不自动切
  auto）。异步化（`asyncio.iscoroutinefunction` 分支）列 P4.1。
- **复盘是投影**：转录事件（text/tool/thinking）当前不进账本，回放 =
  会话文件消息行 + 控制/决策账本行，非逐字节重播。转录事件入账本后升级
  为"回放 = 账本重发"（架构详设 §3.3）。
- 首启设置向导 / 工作区信任仍需 TTY；headless + 未信任时先 `openx` 跑一次。

## 架构

```
aiohttp app（server.py）
├── /ws            ServeSession.handle_ws：上行分发 message/interrupt/permission_response
├── ServeSession   串行回合队列（REPL 语义）→ agent.stream_run → 事件投影 → 广播
│   ├── 每客户端 downlink 队列任务独占 ws.send_json（并发广播不撕裂帧）
│   └── 权限桥 WebPermissionBridge：广播 permission_request，fail-closed 等待
├── ServeConsole   agent 的无 TUI console：ask_permission → 桥；print_* → no-op
└── 静态前端 web/  纯函数 reducer + XSS 安全渲染（先转义后 markdown）
```
