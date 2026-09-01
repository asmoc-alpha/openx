"""PluginSpec（P-F + P-D）-- 模型可读的插件编写契约（唯一真源）。

写插件只读这一份就够：单文件格式、三协议契约（tool / context /
lifecycle）、最小示例。write_plugin 生成的落盘文件就是本格式；admit
管线按本契约校验（语法 + apply/self_test 存在性 + manifest 校验 + 按
type 的注册面契约检查 + 进程内自测）。

格式（一个 .py 文件）::

    __openx_meta__ = { "type": ..., "mount": ..., "trust": "auto",
                       "summary": "一句话描述", "permissions": [...],
                       "cost": {...}, "timeout": 30 }
    <协议实现 + apply(ctx)>
    def self_test():
        <断言；进程内跑，绿了才放行>

type 决定协议（mount 与注册面由内核协议表派生，不必也不应手填）：
- ``capability.tool``  工具类（tool/v1）    -> ctx.register_tool_factory
- ``context.memory``   上下文类（context/v1）-> ctx.register_context
- ``lifecycle``        生命周期类（lifecycle/v1）-> ctx.register_lifecycle
- ``ui.panel``         界面面板类（ui/v1）  -> ctx.register_ui_slot

约束：
- ``apply(ctx)`` 内必须调用 type 对应的注册方法（admit 按此校验）；
- ``self_test()`` 抛异常或超时（>10s，疑似死循环）= 测试失败。
"""

PLUGIN_SPEC = """\
插件编写格式（write_plugin 工具按此生成，单文件 .py；type 决定协议）：

  __openx_meta__ = { "type": "capability.tool", "trust": "auto",
                     "summary": "一句话描述",
                     "permissions": ["fs:read"], "cost": {"schemaTokens": 400},
                     "timeout": 30 }
  from openx.tools.base import Tool, ToolResult

  class MyTool(Tool):
      name = "my_tool"
      description = "做什么"
      parameters = {"type": "object", "properties": {}}
      async def execute(self, **kw):
          return ToolResult(output="结果")

  def factory(host):            # host 是只读投影（ToolHost），拿不到 agent
      return [MyTool()]

  def apply(ctx):
      ctx.register_tool_factory("my_tool", factory)

  def self_test():              # 进程内跑；抛异常 = 测试失败
      assert factory(None)[0].name == "my_tool"

四种协议（type 取值决定，mount 由内核派生勿手填）：

1. capability.tool（工具类，多例）--注册能力工具，模型经 schema 调用：
   apply 里 ctx.register_tool_factory(name, factory)，factory(host) 返回
   [Tool,...]；Tool 必须声明 name / description / parameters / permission。

2. context.memory（上下文类，多例）--往系统提示贡献上下文片段：
     def contribute():
         return "一段注入系统提示的上下文（如召回的记忆/检索结果）"
     def apply(ctx):
         ctx.register_context("my_context", contribute, priority=100)
   priority 小者先征集；contribute 每次提示重建都会调用，只做纯读取。

3. lifecycle（生命周期类，多例）--挂会话生命周期钩子（至少一个）：
     def apply(ctx):
         ctx.register_lifecycle("my_hooks",
                                on_session_start=start,
                                on_unload=save_state)
   可用钩子：on_session_start / on_checkpoint / on_resume / on_unload。
   on_unload 是卸载时的状态落盘契约（有状态插件在此收尾）。

4. ui.panel（界面面板类，多例）--在输入框下方的状态层显示自定义面板
   （如桌面宠物、监控小部件）：
     _FRAMES = ["[dim](^_^)[/dim]", "[dim](o_o)[/dim]"]
     _N = {"i": 0}
     def render():                    # 每次刷新调用一次（默认 5Hz）
         _N["i"] = (_N["i"] + 1) % len(_FRAMES)
         return _FRAMES[_N["i"]]
     def apply(ctx):
         ctx.register_ui_slot("pet", render, refresh_hz=2)
   render() 返回一行或多行（str 或 list[str]，支持 Rich markup 颜色）；
   必须快速返回（同步、无阻塞 IO）。行数上限 8、连续 3 次崩溃自动摘除--
   面板再坏也不会影响主界面。

要点：
- 工具 name 用下划线小写，不与现有工具重名；
- self_test 只做纯内存断言，不做文件/网络副作用；
- 权限按需声明（fs:read / fs:write / network / shell / process）；
- timeout 只对工具类有意义（插件工具的执行超时秒数）。
"""
