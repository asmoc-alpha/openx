# 全屏 TUI（Textual）

[English](tui.md) | 中文

默认界面是**全屏 TUI**（`openx/tui/`，基于 Textual）：屏幕是 widget 树，
折叠一个 thinking 块只是控件属性而非状态机，布局、焦点、滚动都交给框架。

`openx --inline` 则跑**行内 REPL**——TUI 之前的旧界面：输出累积进终端
scrollback，输入框就地重绘。这个模型代价小、但天花板低：一行进了
scrollback 就再也改不了，于是"折叠这段 thinking"、"窗口大小变了重排"、
"显示实时 subagent 面板"统统变成对一块程序并不拥有的区域做光标算术。它
作为逃生门保留（`--inline`），不再是平级界面。

> **状态：默认界面。** 流式、可折叠的 thinking/工具块、`Esc` 打断、多行
> 输入行（历史 + slash 命令菜单）、钉住的状态层（计划 / 队列 / Agents /
> 插件面板）、可键盘导航的 Agents 面板与子代理详情视图，以及权限审批 /
> 计划批准 / `ask_user` 模态弹窗都在 app 里跑。仍存的缺口见[限制](#限制)。

```bash
openx                          # 全屏界面（默认）
openx --inline                 # 行内 REPL（TUI 之前的旧界面）
openx --tui                    # 无操作兼容别名（等价默认）
python -m openx.tui            # 离线演示：不起模型、不联网
```

## 布局

```
┌ 转录 ─────────────────────────────── VerticalScroll ──┐
│  ❯ 讲讲流式渲染这一层是怎么做的                          │
│  ▸ ● Thought for 3.4s            ← 默认折叠            │
│  ▾ ● read_file (services/…)      ← 点击展开            │
│      1 2200 行 / 水印                                  │
│  ▸ ● grep (pattern=…)                                  │
│  答案（Rich Markdown，数学公式预转换）                   │
├───────────────────────────────────────────────────────┤
│   Plan 1/3                                ← #plan-deck│  计划 + 队列
│     ✓ 读完流式渲染层                                  │
│     ✻ 接 deck 面板                                    │
│   Queue (1)                                           │
│     ▸ 顺便把文档也更新了                              │
├───────────────────────────────────────────────────────┤
│  ✻ Thinking…  (3.4s)  ·  esc to interrupt             │  状态行
├───────────────────────────────────────────────────────┤
│  Ask OpenX…  （输入 / 弹出斜杠命令菜单）              │  PromptEditor + SlashMenu
├───────────────────────────────────────────────────────┤
│   Agents (2)                            ← #agents-deck│  Agents + 插件面板
│  ❯1 explore streaming · 3 tools · 4s · ^O switch      │
│        ⎿  read_file(services/streaming.py)            │
│   2 ✓ review docs · 5 tools · 9s                      │
│   0 main · main answer                                │
│  (^_^) demo-pet · 第 12 帧                            │
└───────────────────────────────────────────────────────┘

没有 Footer：`Ctrl+Q` / `Ctrl+R` / `Ctrl+T` 照常生效，但不在屏幕上常驻展示
（键位提示只出现在输入行占位符与弹窗的提示行里）。

`Ctrl+O` 把上面那块转录整个换成某个子代理的详情视图（`#detail`——屏幕的另
一个 `1fr` 子控件，两者因此不会抢高度）：

```
┌ 详情 ─────────────────────────────── VerticalScroll ──┐
│  Agent 1: explore streaming · ✻ running · 3 tools · 4s │  ← #detail-head
│  ● read_file (services/streaming.py)                   │  ← #detail-body
│    ⎿ 1 2200 行 / 水印                                  │
│    正在核对 deck 预算与视口裁剪…                        │
└───────────────────────────────────────────────────────┘
```

状态行、输入行与两块 deck 都留在原位——看着**这个**子代理时，插一句跟进仍
然只差一次回车。

## 三条缝

全屏界面不重写 agent；它只是改道了三处行内实现本就抽出来的接缝。

| 缝 | 行内（`--inline`） | 默认（TUI） |
|---|---|---|
| 输出 | `Console._console` 是写 stdout 的 Rich `Console` | `Console._console` 是 `TuiSink`：`print()` 投递 renderable，由 app 挂成 widget |
| 输入 | `print_user_prompt` 画输入框、跑 prompt_toolkit 编辑器 | `print_user_prompt` 转交 `app.ask()`——等 `PromptEditor` 控件的下一次提交 |
| 会话 | `asyncio.run(run_interactive(...))` | `run_tui` 让 app 与**同一个** REPL 协程在**一个事件循环**里并发跑 |
| 弹窗 | tty 上的 raw 模式菜单（`ui/_components/dialogs.py`） | 浮在转录上的 `ModalScreen`（`openx/tui/modals.py`） |

**输出。** `TuiSink`（`openx/tui/sink.py`）让整个渲染层零改动：约 90 处
`console.print_*` 调用点照旧交出 Rich renderable，每一条变成一个 `Static`。
`print()` 只入队——有界队列（4000 条，溢出丢最旧并计数，绝不阻塞调用方）——
由 app 线程按 30Hz 定时器排空。因此**改 widget 的永远只有单一消费者**，与
行内实现给 `Live` 区域定的纪律相同，只是把线程换成了 app 线程。量测面
（`render_lines` / `capture` / `width` / `height`）由一个 file 指向 `StringIO`
的内部 Rich console 承担：漏网的直接写调用落进缓冲区，不会打到屏幕上。

**输入。** `Console.attach_tui(app)` 记下 `_tui`，`print_user_prompt` 于是改为
`await app.ask()`，不再画框。`ask()` 解析为下一次提交的文本（可能是多行）；
界面关闭时返回 `None`——与行内路径 EOF 语义完全一致，所以 `interactive.py`
不需要任何特判，`/quit` 仍照常干净退出。背后的控件是
[`PromptEditor`](#输入行)，不是 Textual 的 `Input`。

**会话。** `run_tui` 把 app 与 `run_interactive` 作为两个 task 放进同一个 loop，
任一结束就收掉另一个：Ctrl+Q 用 `None` 唤醒正在等的 `ask()`（REPL 正常退出），
`/quit` 或 EOF 则关闭 app。

`Console.detach_tui()` 还原原 Rich console 与行内输入路径。这就是迁移期的
逃生门——行内实现一直保持接线可用，直到阶段 6 删除它。

## 块

`openx/tui/blocks.py` 对应行内实现最费光标算术的两件事：

| 块 | 默认 | 切换方式 | 内容 |
|---|---|---|---|
| `ThinkingBlock` | 折叠 | 点标题栏，或聚焦标题栏后按 Enter | 纯 dim `Text`，**绝不**走 Markdown（围栏与反引号会被重排乱） |
| `ToolBlock` | 折叠 | 同上 | 结果正文，文本操作**按 diff 着色**：`+` 绿、`-` 红、`@@`/`---`/`+++` dim。有变更预览时（`edit_file` / `write_file`，执行前探测）正文换成 `⎿ +A −D` + 语法高亮 diff，不再回显原始输出——判定来自 `ui/_diff.py`，与行内转录、`openx serve` 的 `diff` 载荷同一份，所以不会出现"一个界面染了色、另一个留白"。标题带状态点（`○` 运行中、`●` 完成、`✕` 出错）与 `名字 (摘要)` |

`feed()` 追加 token，`freeze(elapsed)` 把标题改成 `● Thought for Ns`。与行内
实现不同，冻结后的块**仍然可折叠**——widget 还在，重渲即可。

## 流式

行内实现把一个回合渲染进一块 Rich `Live` 区域，代价是视口预算、逐行固化水印
与光标算术。全屏管线吃**同一份事件流**，但不付这些账：`TuiStream`
（`openx/tui/stream.py`）与 `StreamingService` 同面（`start` / `feed` /
`done` / `cancel` / `pause` / `set_cancel_target` / `esc_interrupted`），
所以 `interactive.py` 只在一处选后端，下面的接线一行不用改。

`feed()` 只做分类与入队；`StreamView`（`openx/tui/view.py`）把事件落成
widget，而且**只由 app 的 30Hz 排空驱动**——与输出 sink 同一条单一消费者纪律。

| `agent.stream_run` 的 chunk | 落成的 widget |
|---|---|
| `str` | 追加进当前答案 entry（Rich `Markdown`，数学公式预转换），最多 10 次/秒重渲 |
| `StreamReasoning(text)` | 首个 token 时 mount 一个 `ThinkingBlock`；首个非推理 chunk 把它冻结成 `● Thought for Ns` |
| `ToolStartEvent(name, arguments, call_id)` | 一个 `ToolBlock`，标题 = 状态点 + 名字 + 参数摘要 |
| `ToolResultEvent(name, output, is_error, call_id)` | 对应块翻成完成/出错并显示输出 |

工具块会**结束当前答案段**：工具调用之后的文本另起一条 entry，等价于行内的
text/tool 交替——否则新文本会被拼进旧 Markdown、把工具块埋在中间。

工具结果在 provider 给了 `call_id` 时按 id 精确配对，否则退回"同名首个 running"
（行内规则）。并行调用同一个工具时因此各自落到对的那一块，这一点行内实现做不到。

打断：`Esc` 是 app 的 key binding（带 priority，输入框里打字时也有效）。它调用
`TuiStream.interrupt`，与行内 `_interrupt` 逐点同义——单回合闩、**只**取消登记
的流消费子任务、真正的取消经 `loop.call_soon_threadsafe`、`esc_interrupted`
供 `interactive.py` 决定吞掉取消（回提示符）还是上抛。收尾时回调解绑，回合
结束后误按绝不会波及下一回合。

流式期回车**排队**而不是立刻发送：行进 `console._input_queue`——行内 REPL 本
就在每轮之间弹的那个通道——转录同时回显 `↳ queued: …`，让用户看见排了什么。

## 输入行

输入行是 `PromptEditor`（`openx/tui/prompt.py`），`TextArea` 的子类，不是
Textual 的 `Input`。理由只有一条，但很硬：`Input` 的文档模型里没有 `\n`，多行
粘贴会被压成一行、光标永远进不了第二行。`TextArea` 有真正的文档模型——软折行、
选区、撤销、括号粘贴全部现成——代价是每条键位语义都得在这里自己定。

| 按键 | 含义 |
|---|---|
| `Enter` | 提交（流式期：排队）；slash 菜单开着时先应用候选再提交 |
| `Ctrl+J` | 插入字面换行 |
| `Alt+Enter` / `Shift+Enter` | 同上（`ESC CR` 是可区分的字节对；`Shift+Enter` 需要能报出它的终端，如 kitty 键盘协议） |
| `↑` / `↓` | 菜单开着：移动候选光标；否则**只在首/末行**翻历史，其余位置是光标移动 |
| `Tab` | 菜单开着：应用候选 + 尾随空格；否则走框架的焦点链 |
| `Esc` | 菜单开着：只收菜单；否则冒泡上去打断回合 |

行内编辑器是 `multiline=False` + `("escape", "enter")` 绑成插入 `\n`，所以
"回车提交、`Alt+Enter` 换行"在两侧是同一个契约；TUI 多的只是 `Ctrl+J`
（不需要终端支持修饰键）。

**框随内容长高。** `_sync_height` 设 `styles.height = 行数 + 2`（上下两条框线
各占一行），行数取 `wrapped_document.height`——**软折行之后**的行数，长单行与
多行粘贴一视同仁。上限 `MAX_ROWS = 8`，超出后内部滚动；没有这个上限，一次粘贴
就能把转录顶出屏幕。高度在这里自己算，不赌框架的 auto-height 语义——对一个还带
边框的控件，那是不值得下的注。

**历史就是行内 REPL 那个文件。** `tui/history.py` 按 prompt_toolkit
`FileHistory` 的格式读写 `~/.openx/history`——每条前面一行 `# <时间戳>` 头，
随后每条逻辑行带 `+` 前缀——于是在 `--inline` 里敲过的命令回到全屏界面按 `↑`
照样能翻到，反之亦然。写只追加（两个界面同时开着也不会互相截断），空行与相邻
重复按行内 `OpenXHistory.store_string` 的规则过滤，任何 IO 失败静默降级：历史
是锦上添花，绝不是输入行坏掉的理由。游标保留草稿——打了一半按 `↑` 翻历史，
再按 `↓` 越过最新一条就把你打的字还回来。

**斜杠菜单保持两条键路的分工。** `↑`/`↓`/`Tab`/`Esc` 的绑定用 `check_action`
门控（菜单关时整体失效），而不是在 `_on_key` 里 `stop()`：Textual 的键路是两条
——① MRO 分发的 `_on_key`（`TextArea` 在这里插字符、移动光标），② 冒泡到 app
后的绑定求值。`prevent_default()` 只断 ①，`stop()` 把 ② 也断掉，按键于是永远
到不了绑定 action。所以覆盖默认行为时用 `prevent_default()`，**完整处理完**一个
键时才 `stop()`。这道门也正是 `Esc` 能有两种含义的原因：有菜单就收菜单，没有就
打断回合。

`Ctrl+R` / `Ctrl+T` 是界面级动作，而 `TextArea` 自带一整套编辑绑定（`ctrl+z`、
`ctrl+k`…），所以这两个是 **priority** 绑定——将来 `TextArea` 若给 `ctrl+r`
也绑一件事，也偷不走"折叠全部 thinking"。

`TextScreen`（自由文本弹窗）挂的就是**同一个控件**：同一套键位、同一副长相、
同样会随内容长高。`/skill add` 的多行 instructions 因此在一个弹窗里写完，而不是
一行一个弹窗。它不接历史——一次性弹窗里翻转录的命令只会让人莫名其妙——并保留
保守契约：空输入视为未作答，`Esc` 返回 `None`。

## 状态层（deck）

行内把四块状态面板塞进 `Live` 区域一帧一构——**计划**（agent 的 todos）、
**队列**（流式期敲下的跟进消息）、**Agents**（子代理舰队）与**插件 `ui.panel`
槽位**——全屏这边把它们做成常驻 widget：`#plan-deck` 在状态行之上、
`#agents-deck` 在输入行之下。**数据一点没搬家**：`TuiStream.start()` 把
`_make_display` 给行内的同三个 provider 交给 `app.attach_decks`，由 app 的 5Hz
定时器渲染（与行内 `Live` 同节拍）。

**行格式只有一份。** `openx/ui/_deck.py` 负责拼行——计划的三种状态（进行中用
`activeForm`）、全部完成时折叠成单行汇总、队列的 FIFO 全列、Agents 的主条目
（编号 0）+ 每个**运行中**子代理下面的活动行（`⎿ 工具(摘要)`）、插件面板跳过
`svg:` 行并把坏 markup 隔离在单个面板内。两个界面的差别只剩**预算**（能花几
行）与**摆放**（框上还是框下）：`StreamingService._deck_renderable` 那几个方法
现在是同一批函数的薄包装，于是"两个界面看到的 deck 一样"是代码的性质，而不是
一条要靠人守的维护承诺。转帧字形也同理下沉到了 `ui/_style.py`。

**预算。** 行内是纯减法，因为 Rich `Live` 区域是绝对行数；全屏取
`height // 4` 与 `(height - 固定件 - 间隙 - 3) // 2` 的较小者——比例保证两个
deck 合计不吃掉半屏，减法保证转录至少三行（12 行终端上只按比例算，转录只剩一
行）。`DeckSource` 还会把每段行**硬裁**到预算内：共享的插件渲染允许折叠行比预算
多一行（那是行内被测试钉住的既有行为），"严格不超"这条由持有视口的那一侧补上。

**可见性与行内故意不同。** 行内回合结束全部消失——那是 `Live` 区域的副作用，
不是需求。这里：

| 面板 | 何时显示 |
|---|---|
| Plan / Queue | 只要有内容就显示——待办列表本就跨轮存活 |
| Agents | 只在回合进行中（`fleet.reset()` 每轮清空） |
| 插件面板 | 只要渲染得出来就显示——`ui.panel` 的宠物/监控件按定义该常驻 |

**几何是显式的。** `DeckView` 把 `styles.height` 写成 `len(rows)`，不赌框架的
auto-height（deck 行恒 `no_wrap`，一行就是一个终端行）；空时用 `display: none`
藏起来——margin 也随之消失，正是行内"`_DECK_SPINNER_GAP` 仅在 deck 在场时占位"
的等价物；运行期**绝不增删子控件**（`SlashMenu` 踩过的坑：容器短暂以空 visual
参与渲染会让 `render_strips` 崩）。deck 高度变化后 app 会补一次贴底滚动，
免得"长高了就不再跟随"。

### 计划新鲜度（`▲ stale` 标记）

Plan 面板逐字渲染 `agent.todos`——而这份清单只在模型决定调 `todo_write` 时才变。
一个长回合里它能连着跑十几轮工具才想起更新，或者干脆不想。于是面板继续以
**现在进行时**渲染旧清单：`in_progress` 那行显示的是 `activeForm`，它的语义
就是"我正在做什么"，所以它一直在说早就干完的活。**过期计划比没有计划更坏**：
没有计划什么也没说，过期计划在说谎。

两道闸，因为有两个读者：

| 读者 | 闸 | 位置 |
|---|---|---|
| 模型 | 过期后把一段旁注拼进 `message[0]` | `agent.py`（`_TODO_STALE_NOTE`、`TODO_STALE_ROUNDS`） |
| 用户 | Plan 头行标 `▲ stale` | `ui/_deck.py`（`plan_queue_lines(stale=…)`） |

**计数口径。** `agent._todo_rounds_since_write` 数的是**工具往返**（一次产出
工具调用的模型请求 = 一个决策点），不是单个工具调用——并行批次里 6 个调用只是
一次"更新计划的机会"，而"你有 6 次机会都没更新"才是值得钉住的事实。批次里有
`todo_write` 就归零，否则加一。它**刻意跨轮累加**：上一轮留下的未完成计划，
和这一轮写下的一样会过期。

**`todos_stale`** 只在以下全部成立时为真，每一条都必要：

- agent 是顶层——子代理也带 `todo_write`，但没有任何界面渲染它的清单，
  给它发提醒纯粹是白付 token（与"模式指令仅顶层注入"同一口径）；
- 确实有清单；
- 至少一项不是 `completed`——全部完成时面板折叠成 `✓ Plan N/N`，那是已收尾的
  记录，不是进度声明；
- 计数达到 `TODO_STALE_ROUNDS`（6）。

**旁注是字节稳定的。** 它不含轮数字面量：带数字就会让字符串逐轮变化，
`message[0]` 跟着变，整个会话的 provider 前缀缓存就断了。稳定还有一个好处：
`_sync_system_message` 只在"新鲜 ↔ 过期"翻转时才重写消息。旁注只拼进**下发的**
`message[0]`，绝不烘进 `self._system_prompt`——那份字段是装配变化才重建的可缓存
身份，不该为一个逐轮计数重算。

**标记不占行。** 它拼在已有的 `Plan` 头行里，所以"1 deck 行 ≡ 1 终端行"的几何、
视口预算、行内的擦除算术全都不受影响（`stale` 默认 `False`，既有调用点输出
逐字节不变）。折叠态刻意不标：全部完成的 deck 是汇总，没有要维护的东西。

**两个循环、两个界面。** `_note_tool_round` 在 `_run_inner` **和** `_turn_stream`
里都调用——TUI 与 REPL 走的都是流式，只接 `run()` 等于把用户真正看得见的路径
漏掉。`todos_stale` 作为**独立一路 provider** 传给面板（`StreamingService` /
`TuiStream` → `DeckSource`），而不是塞进 todo 字典里当合成字段：那个字典的每个
键都会被回显进系统提示，塞在里面的标记会漏进模型上下文。

`/todos` 用文字说同一件事：`print_todos(..., stale=…, stale_rounds=…)` 会补一行
`▲ 12 tool rounds since the last update — this plan may be behind what the agent
actually did`。**判定归 `agent.todos_stale`**，不归 console——`openx/ui` 不
import `Agent`，阈值只存在于一处。

### 子代理详情视图

`Ctrl+O` 循环切换观看目标（`Alt+0…9` 直选）：目标 `0` 是主回答、`1..N` 是第 N
个子代理。选中时隐藏 `#transcript`、在原位显示 `#detail`——正是行内"正文槽切到
那个子代理"，而输入行与两块 deck 原地不动，于是看着它的时候仍能插一句跟进。

**为什么不用 `↓` 和 `Enter`？** 行内 deck 是"`↓` 选中 + `Enter` 确认"，那能成立
只因为行内输入行在流式期不接收编辑。全屏的输入行始终可编辑，而 `↑`/`↓` 的第一
含义已经是"翻历史 / 移光标"（`tests/tui/test_input.py` 两条都钉着），所以按键是
`Ctrl+O`（循环）与 `Alt+0…9`（直选）。Agents 头行写的是 `· ^O switch`——共享渲染
函数把提示文案做成参数（`fleet_lines(hints=…)` 接受 `True` / `False` / 字符串）：
头行绝不能承诺按不动的键，而"不动"并不是行内文案之外唯一的选择。

**头行与正文是两个 `Static`。** 头行带转帧与耗时，每 5Hz 一拍都要重写；正文是
子代理的捕获流，只有来了新行才变（按 `plain` 签名去重，与 `DeckView` 同款）。
合成一个控件就等于逼后者跟着前者的节奏重排——200 行 × 5Hz 纯属浪费。正文行是
本文件里**唯一允许折行**的地方：那是子代理的原始输出，截断比折行更糟；它们住在
滚动容器里，不参与 deck 的"一行 ≡ 一个终端行"几何。

行从 `ui/_deck.py` 来（`detail_header` / `detail_body`），与行内 `_detail_view`
共用。也正因为如此，子代理输出里一个裸 `[` 不再能炸掉整帧：`format_stream_event`
返回模型文本时不做转义，行内界面此前会因此抛 `MarkupError`；现在 `detail_body`
把那一行降级成纯文本。

**生命周期。** 每个回合开始时目标归 `0`（`attach_decks`），新回合绝不会一上来就
停在已经不存在的视图上。舰队在背后变短（`reset()`、跑完）时，下一拍钳回主回答。
模态弹窗在场时**不切**：详情视图会顶掉弹窗正压着的转录。`Esc` 不关详情（`Esc`
已经是"打断"与"取消弹窗"）——要回去就 `Alt+0` 或继续循环。

## 弹窗

行内提示框是旧界面里 widget 树**无法共用**的那部分：`ui/_components/dialogs.py`
把 tty 切进 raw 模式、直接写转义序列、靠数行数定位光标。stdin 与屏幕都归
Textual 之后，这套等于必然花屏。于是 agent 真正会阻塞等待的三个问题变成了
`ModalScreen`（`openx/tui/modals.py`）。

| 问题 | 屏 | 取消（`Esc`） |
|---|---|---|
| 权限审批（`Console.ask_permission`） | `ChoiceScreen` | `(False, False)`——不运行 |
| 计划批准（`confirm_plan`） | `ChoiceScreen`（Markdown 正文） | `False`——不执行 |
| `ask_user` 提问（`ask_user_question`） | `ChoiceScreen`（可多选）/ `TextScreen`（"Other"） | `""` / `[]`——未作答 |

三种弹窗只由两个控件覆盖：一个选择屏、一个文本屏。差异全是数据（标题、正文
renderable、选项表）——加第四种弹窗不该需要写第三个屏。

**视觉是行内的开放式弹窗**：模态屏不铺半透明遮罩（背景透明，转录在弹窗后面
照常可见），内容是停靠在屏幕底部的通栏左对齐一列——亮字标题 → 正文 → 裸
`●` 选项列（选中行亮粗、其余 dim）→ 尾部 `(↑/↓ to choose, …)` 提示行，
没有卡片、边框与盒子。

**工具侧接线零改动。** `Console.ask_permission` 先看 `_tui`，命中就转交
`app.ask_permission`；`ask_user` / `confirm_plan` 走**异步优先**入口
（`ask_user_question_async` / `confirm_plan_async`）——`tools/console_dialog.py`
本来就优先探这两个名字，浏览器（`--serve`）面用的是同一对。工具代码一行没改；
没挂 app 时这两个方法逐字委托回同步的行内实现。

**取消一律 fail-closed**，且取的是屏自身的值而非调用方的猜测：`Esc`、app 关闭、
屏丢失，三者都解析成最保守的答案（拒绝 / 不执行 / 未作答）。被关掉的弹窗绝不
可能变成一次隐式授权。权限选项次序也与行内一致：*允许一次 / 不再询问 / 拒绝*，
多按一次 `↓` 落到的是**更窄**的授权，而不是把拒绝按成永久放行。

**弹窗绝不留悬空的 `await`。** 每个模态屏 resolve 一个调用方 await 的 `Future`；
`App._close()` 在退出前把所有挂起的 future 按默认值 resolve，收摊之后再来问一句
也立刻返回默认值。没有这一条，卡在 `ask_permission` 里的工具会比界面活得久
（那是死锁，不是退出）。

弹窗期间状态行改说 `Waiting for you…` 而不是 `Answering…`：回合正停在工具结果
上，继续显示"在回答"会被读成"已经答完了"。

### slash 命令菜单也走同一对屏

`/config`、`/model <组>:<角色>`、`/mcp add|remove`、`/skill add|remove`、
`/release` 原先走行内选择器，在 app 下会抛 `TuiInputNeeded`。现在它们统一走
`Console` 上的两个**异步优先**入口——`select_async`（选择）与
`ask_text_async`（自由输入）：挂了 TUI 就转交 `app.select` / `app.ask_text`，
没挂就逐字委托行内实现。`app.select` 只是又一个 `ChoiceScreen`——第四种问题
依旧不需要第三个屏。取消统一返回 `None` / `""`，与各菜单既有的
"Cancel/Done" 语义对齐。`/clear` 则移除转录的全部 widget
（`app.clear_transcript`）——`raw.clear()` 在 sink 下只会落进量测用的
`StringIO`，屏清不掉。

`Esc` 刻意**不再**是 app 级 priority 绑定：输入行没有 `Esc` 绑定，按键照样冒泡
上来打断回合；而模态屏自己的绑定天然优先于 app 级绑定——这正是两种含义共存所需
的分层。

## 按键

| 按键 | 动作 |
|---|---|
| `Enter` | 提交输入行（流式期：排进下一回合） |
| `Ctrl+J` / `Alt+Enter` | 在输入行内换行（输入框最高长到 8 行） |
| `↑` / `↓` | slash 菜单开着：移动候选光标；关着：在首/末行翻历史 |
| `Ctrl+Q` | 退出（用 EOF 唤醒正在等的输入） |
| `Ctrl+R` | 切换所有 thinking 块 |
| `Ctrl+T` | 全部展开；若已有折叠则全部折叠 |
| `Ctrl+O` | 循环切换观看目标：主回答 → 子代理 1 → … → 回主回答 |
| `Alt+0` … `Alt+9` | 直选目标（`0` = 主回答；越界钳到最后一个） |
| `Tab` / `Shift+Tab` | slash 菜单开着：应用候选；关着：在块标题与输入行之间移动焦点 |
| `Esc` | 打断当前回合（空闲时无动作）；收起 slash 菜单；关掉当前弹窗 |

弹窗内：`↑`/`↓` 移动、`space` 勾选（仅多选）、`Enter` 确认、`Esc` 按最保守的
答案取消。弹窗里的自由输入行是**同一个** `PromptEditor`，所以 `Ctrl+J` 换行、
框随内容长高——多行答案与 `/skill add` 的 instructions 都在一个弹窗里写完。

点击块标题栏即可切换折叠；滚轮滚动转录，回看历史时视图不再追底。**没有
滚动条**——两个滚动容器都是 `scrollbar-size-vertical: 0`。终端滚动条是一列
可拖动的滑块，键位里没有任何东西去拖它，滚轮又按指针位置派发事件（不需要
焦点、也不需要可见滑块）。它还白占一列：Textual 会给可见滚动条留出槽位，
隐藏后这列归正文（80 列终端从 77 列变 78 列可用）。任何点击都不会把焦点
从输入行带走——转录容器与弹窗正文不可聚焦，
主屏点击后焦点回到输入行，点完照常打字（`Tab` 仍走焦点链；聚焦块标题后
`Enter` 照旧切换）。

## 限制

- **输入行是编辑器，但不是行内那个 ptk 编辑器。** 多行编辑、括号多行粘贴、
  软折行与 `↑`/`↓` 历史（与行内 REPL 共用 `~/.openx/history`）都已就位；
  仍缺行内编辑器的 `Ctrl+R` 反搜与终端鼠标选区。`/skill add` 的多行
  instructions 现在一个弹窗就能写完。
- **TUI 需要真终端。** 管道/CI 下 CLI 打印警告并回退到行内 REPL；
  `python -m openx.tui` 则直接以 2 退出。`textual` 本身缺失时，默认路径
  同样警告后回退；显式传 `--tui` 才以 2 退出并给安装指引。
- TUI 与 `--serve`、以及一次性 prompt 参数互斥——那两条路径对 stdout 的
  占用方式，全屏 app 无法共享（要行内界面配 prompt 就加 `--inline`）。
- **Agents 面板可键盘导航，但点不动。** `Ctrl+O` / `Alt+0…9` 能到达子代理详情
  视图（见[子代理详情视图](#子代理详情视图)）；仍缺的是行内那套两步式 `↓` 选中 +
  `Enter` 确认、点舰队行开详情、以及用 `Esc` 关掉详情（现在是 `Alt+0` 回去）。
  头行只写真正能按的键。
- 长答案流式期按 10Hz 重渲 Markdown；慢终端上的超长答案因此比行内的逐行固化
  路径更重。

## 测试

`tests/tui/test_spike.py` 钉住承重行为：点标题栏与 Enter 切换、`Ctrl+T`、
thinking 保持纯文本、工具结果三态、`Static` 里的 Rich/Markdown/数学保真度、
sink 投递、attach/detach，以及 `ask()` 契约（返回提交的文本，退出时为 `None`）。

`tests/tui/test_streaming.py` 用**真实事件对象**（`StreamReasoning` /
`ToolStartEvent` / `ToolResultEvent`）驱动管线：答案分段、并行同名按 `call_id`
配对、孤立结果不丢、`task` 起始抑制与结果回显、完成/打断收尾行、`Esc` 闩与
解绑、排队输入路由，以及一次 `_stream_response` 端到端——断言 widget 有内容
**且**没有起行内 `InputCapture`（cbreak 会与 Textual 抢 stdin）。
`TestToolBlockDiff` 钉住 diff 着色：`edit_file` 结果里 `-` 红 / `+` 绿 / 头行
dim；探测到预览时正文换成 `⎿ +A −D` + 高亮 diff 且**不重复回显**原始输出；
失败的操作绝不展示未发生的 diff；其余工具输出不被误着色；`tool_lookup` 抛错
或缺失时退回无预览路径。

`tests/tui/test_dialogs.py` 覆盖迁移后的弹窗：控件形态（两个屏，不给第四种弹窗
写第三个屏）、fail-closed 取消语义、权限选项次序、审批正文里的 diff 预览、
多选勾选、"Other" 分支进 `TextScreen`、三条缝（`Console.ask_permission`、
`tools.console_dialog` 的异步优先入口）、detach 后回退行内，以及两条收摊契约——
挂起的弹窗按默认值 resolve、关掉 app **真的会退出来**（有界等待，回归时是失败
而不是挂死）。桥接测试覆盖 `select_async` / `ask_text_async`：挂上 app 走模态
屏、detach 后逐字委托行内选择器、行内路径的 Ctrl-C/EOF 归一成取消值。未迁移
入口的守卫（挂上 app 后裸调 `_interactive_select` / `paste_aware_input` 抛
`TuiInputNeeded`）保留——它是任何绕过异步入口的新调用点的绊线。

`tests/tui/test_input.py` 端到端覆盖输入行。`PromptHistory`（不需要 Textual）
对着行内 ptk 格式钉死：`# <时间戳>` 头 + `+` 前缀行、多行往返、垃圾行与坏字节
容错、只追加写入、空行/相邻重复过滤、`max_entries` 只裁内存不裁文件，以及
`↑`/`↓` 游标与草稿语义。控件侧：`Enter` 提交整段多行文本并清空（框缩回一行）、
`Ctrl+J` / `Alt+Enter` / `Shift+Enter` 都能换行、多行粘贴原样进入、高度跟随
`wrapped_document` 并封顶于 `MAX_ROWS`、`↑`/`↓` 只在首/末行翻历史（文本中间是
光标移动）、不接历史的弹窗输入框无视方向键、菜单关时 `Tab` 仍走焦点链且 `Esc`
仍冒泡成打断，提交落进历史文件——包括重复与空行不落盘。
`tests/tui/conftest.py` 把 `HISTORY_PATH` 指到 `tmp_path`（整个目录自动生效），
因此没有用例会写进用户真实的 `~/.openx/history`。

`tests/tui/test_deck.py` 覆盖状态层的两半。纯函数那半（`ui/_deck.py`，不需要
app）钉住行语义：计划三态与 `activeForm`、全完成折叠成一行、`+N more` 折叠、
队列 FIFO 与计数、计划/队列共享预算（且较大的块先让位）、舰队行（活动行计入
预算）、`hints` 按界面给提示文案（关掉或替换 `↓ select · ↵ view`）、插件面板
跳 `svg:` 且坏 markup 只影响那一个面板、以及每个 provider 抛异常时的 fail-open。
详情视图的行构造同样钉死：头行的身份/状态/耗时与按界面的提示、正文窗口
（`budget`）、尾部未成行的 `pending` 片段、`(no output yet)`、坏 markup 回落
纯文本。控件那半钉住 `DeckView`（空则隐藏且不占行、高度等于行数、不折行、点击
不夺输入行焦点）、compose 顺序（`transcript` → `detail` → `plan-deck` → 状态行
→ 输入行 → `agents-deck`）、回合生命周期（`TuiStream.start()` 后四块都在、
`TurnEnd` 让 Agents 退场而计划与插件面板留存、`todos[:] = …` 下一拍可见、舰队
每轮归零）以及 12 行小终端上两个 deck 都在预算内、转录仍留三行。
`TestFleetNavigation` 用真实按键驱动详情视图：`Ctrl+O` 循环并隐藏转录、
`Alt+N` 直选与钳位、Agents 行的 `❯` 标记跟随目标、新回合归零、舰队清空后钳回
主回答、正文未变时不重写、点详情不夺输入行焦点。

Textual 的 `App.run_test()` + `Pilot` 直接断言**widget 状态**——这正是行内
实现做不到的（那边流式渲染靠 `pyte` 屏幕快照验证）。五个文件都用
`pytest.importorskip("textual")` 守卫，默认路径不会因缺少可选依赖而红。

依赖：`textual>=8,<9`（它要求 `rich>=14.2`，与本仓 `rich>=14,<16` 相容）。
所依赖的 API 面——`Collapsible` 的 reactive `title`/`collapsed`、
`CollapsibleTitle` 的点击与 Enter 处理、`run_async`、`run_test`、
`Static.update`/`visual`、`OptionList` 的 `replace_option_prompt_at_index`、
`TextArea`（`load_text`、`document`、`wrapped_document`、`Changed`、
`placeholder`、`styles.height`）及其 `prevent_default` 与 `stop` 的两条键路、
`ModalScreen`/`push_screen(screen, callback)`、`VerticalScroll`——逐条列在
`pyproject.toml` 的依赖注释里；那边的改名应先在 `tests/tui/` 变红。

> 一个值得记住的命名雷：`MessagePump._closed` 是 Textual 自己的"消息队列已关闭"
> 开关，它为真时 `post_message` 会**静默丢弃**消息。所以 app 自己的收摊标记叫
> `_tui_closed`；早先复用了 `_closed`，`_close()` 一置位就把随后的 `ExitApp` 吃
> 掉了，app 于是永久停在 `_process_messages` 里（表现为测试挂死而非失败）。

## 参见

- [架构](../architecture.zh.md)——`openx/tui/` 在模块树里的位置
- [模式与权限](../user/guide/modes-permissions.zh.md)——今天就要用 `--tui` 时该开的 auto 模式
- [Subagents](subagents.zh.md)——将来由全屏界面渲染成真面板的 fleet 面板
- [插件](plugins.zh.md)——`ui.panel` 槽位，目前仍是行内
