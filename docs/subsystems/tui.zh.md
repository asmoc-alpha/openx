# 全屏 TUI（Textual）

[English](tui.md) | 中文

默认界面是**行内 REPL**：输出累积进终端 scrollback，输入框就地重绘。这个
模型的代价小、但天花板低——一行进了 scrollback 就再也改不了，于是"折叠这段
thinking"、"窗口大小变了重排"、"显示实时 subagent 面板"统统变成对一块程序
并不拥有的区域做光标算术。

`openx --tui` 改为跑**全屏界面**（`openx/tui/`，基于 Textual）：屏幕是
widget 树，折叠一个块只是控件属性而非状态机，布局、焦点、滚动都交给框架。

> **状态：阶段 0（实验性）。** 界面已存在且有测试，但默认仍是行内 REPL，
> 下文未列出的一切仍以行内实现为准。`--tui` 下回合走**非流式**（见[限制](#限制)）。

```bash
openx --tui                     # 全屏界面
openx --tui --auto-approve      # ……且不弹 ASK 弹窗（见「限制」）
openx --tui --continue          # 照常恢复会话
python -m openx.tui             # 离线演示：不起模型、不联网
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
│  Ask OpenX…                                           │  Input
├───────────────────────────────────────────────────────┤
│  ^Q quit   ^R thinking   ^T expand all                │  Footer
└───────────────────────────────────────────────────────┘
```

## 三条缝

全屏界面不重写 agent；它只是改道了三处行内实现本就抽出来的接缝。

| 缝 | 行内 | `--tui` |
|---|---|---|
| 输出 | `Console._console` 是写 stdout 的 Rich `Console` | `Console._console` 是 `TuiSink`：`print()` 投递 renderable，由 app 挂成 widget |
| 输入 | `print_user_prompt` 画输入框、跑 prompt_toolkit 编辑器 | `print_user_prompt` 转交 `app.ask()`——等 `Input` 控件的下一次提交 |
| 会话 | `asyncio.run(run_interactive(...))` | `run_tui` 让 app 与**同一个** REPL 协程在**一个事件循环**里并发跑 |

**输出。** `TuiSink`（`openx/tui/sink.py`）让整个渲染层零改动：约 90 处
`console.print_*` 调用点照旧交出 Rich renderable，每一条变成一个 `Static`。
`print()` 只入队——有界队列（4000 条，溢出丢最旧并计数，绝不阻塞调用方）——
由 app 线程按 30Hz 定时器排空。因此**改 widget 的永远只有单一消费者**，与
行内实现给 `Live` 区域定的纪律相同，只是把线程换成了 app 线程。量测面
（`render_lines` / `capture` / `width` / `height`）由一个 file 指向 `StringIO`
的内部 Rich console 承担：漏网的直接写调用落进缓冲区，不会打到屏幕上。

**输入。** `Console.attach_tui(app)` 记下 `_tui`，`print_user_prompt` 于是改为
`await app.ask()`，不再画框。`ask()` 解析为下一次提交的那一行；界面关闭时返回
`None`——与行内路径 EOF 语义完全一致，所以 `interactive.py` 不需要任何特判，
`/quit` 仍照常干净退出。

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

## 按键

| 按键 | 动作 |
|---|---|
| `Enter` | 提交输入行 |
| `Ctrl+Q` | 退出（用 EOF 唤醒正在等的输入） |
| `Ctrl+R` | 切换所有 thinking 块 |
| `Ctrl+T` | 全部展开；若已有折叠则全部折叠 |
| `Tab` / `Shift+Tab` | 在块标题与输入行之间移动焦点 |
| `Esc` | 预留给"打断回合"——**尚未接线**（阶段 2） |

点击块标题栏即可切换折叠；竖向滚动条与滚轮滚动转录，回看历史时视图不再
追底。

## 限制

- **回合是非流式的。** 行内 `StreamingService` 驱动一块 Rich `Live` 区域，
  会与 Textual 争同一个终端，因此 `--tui` 强制 `config.stream = False`；
  流式 widget 管线是阶段 2 的交付物。可感知的后果：没有逐字答案、没有实时
  thinking 块、`Esc` 不能打断。
- **弹窗尚未迁移。** 权限 ASK、计划批准、`ask_user`、设置浮层仍来自行内实现，
  它会把 tty 切到 raw 模式并直接写转义序列——与"app 独占屏幕"不兼容。
  在阶段 5 的 modal screen 落地前，请用 `-y` / `--auto-approve`（或 auto 模式）
  跑 `--tui`，让流程不需要弹窗。
- **输入行是单行的。** 暂无历史、暂无补全、暂无多行粘贴（阶段 3）。
- **`--tui` 需要真终端。** 管道/CI 下打印警告并回退到行内 REPL；
  `python -m openx.tui` 则直接以 2 退出。
- `--tui` 与 `--serve`、以及一次性 prompt 参数互斥——那两条路径对 stdout 的
  占用方式，全屏 app 无法共享。
- 插件 UI 面板（`ctx.register_ui_slot`）与 subagent fleet 面板目前仍是行内：
  它们的渲染走 `Console`，于是输出以文本形式落进转录，而不是钉住的面板。

## 测试

`tests/tui/test_spike.py` 钉住承重行为：点标题栏与 Enter 切换、`Ctrl+T`、
thinking 保持纯文本、工具结果三态、`Static` 里的 Rich/Markdown/数学保真度、
sink 投递、attach/detach，以及 `ask()` 契约（返回提交的文本，退出时为 `None`）。

Textual 的 `App.run_test()` + `Pilot` 直接断言**widget 状态**——这正是行内
实现做不到的（那边流式渲染靠 `pyte` 屏幕快照验证）。该文件用
`pytest.importorskip("textual")` 守卫，默认路径不会因缺少可选依赖而红。

依赖：`textual>=8,<9`（它要求 `rich>=14.2`，与本仓 `rich>=14,<16` 相容）。
所依赖的 API 面——`Collapsible` 的 reactive `title`/`collapsed`、
`CollapsibleTitle` 的点击与 Enter 处理、`run_async`、`run_test`、
`Static.update`、`VerticalScroll`——逐条列在 `pyproject.toml` 的依赖注释里；
那边的改名应先在 `tests/tui/` 变红。

## 参见

- [架构](../architecture.zh.md)——`openx/tui/` 在模块树里的位置
- [模式与权限](../user/guide/modes-permissions.zh.md)——今天就要用 `--tui` 时该开的 auto 模式
- [Subagents](subagents.zh.md)——将来由全屏界面渲染成真面板的 fleet 面板
- [插件](plugins.zh.md)——`ui.panel` 槽位，目前仍是行内
