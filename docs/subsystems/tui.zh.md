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
> 输入行（历史 + slash 命令菜单）与权限审批 / 计划批准 / `ask_user` 模态
> 弹窗都在 app 里跑。仍存的缺口：deck（计划 / 队列 / 舰队 / 插件面板）以
> 转录文本呈现（见[限制](#限制)）。

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
│  ✻ Thinking…  (3.4s)  ·  esc to interrupt             │  状态行
├───────────────────────────────────────────────────────┤
│  Ask OpenX…  （输入 / 弹出斜杠命令菜单）              │  PromptEditor + SlashMenu
└───────────────────────────────────────────────────────┘

没有 Footer：`Ctrl+Q` / `Ctrl+R` / `Ctrl+T` 照常生效，但不在屏幕上常驻展示
（键位提示只出现在输入行占位符与弹窗的提示行里）。

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
| `ToolBlock` | 折叠 | 同上 | 结果正文；标题带状态点（`○` 运行中、`●` 完成、`✕` 出错）与 `名字 (摘要)` |

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
| `Tab` / `Shift+Tab` | slash 菜单开着：应用候选；关着：在块标题与输入行之间移动焦点 |
| `Esc` | 打断当前回合（空闲时无动作）；收起 slash 菜单；关掉当前弹窗 |

弹窗内：`↑`/`↓` 移动、`space` 勾选（仅多选）、`Enter` 确认、`Esc` 按最保守的
答案取消。弹窗里的自由输入行是**同一个** `PromptEditor`，所以 `Ctrl+J` 换行、
框随内容长高——多行答案与 `/skill add` 的 instructions 都在一个弹窗里写完。

点击块标题栏即可切换折叠；竖向滚动条与滚轮滚动转录，回看历史时视图不再
追底。任何点击都不会把焦点从输入行带走——转录容器与弹窗正文不可聚焦，
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
- 插件 UI 面板（`ctx.register_ui_slot`）、subagent fleet 面板与计划/队列 deck
  目前仍是行内：它们的渲染走 `Console`，于是输出以文本形式落进转录，而不是
  钉住的面板。`TuiStream` 把 todos/fleet/panels 参数收进构造但不使用。
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

Textual 的 `App.run_test()` + `Pilot` 直接断言**widget 状态**——这正是行内
实现做不到的（那边流式渲染靠 `pyte` 屏幕快照验证）。四个文件都用
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
