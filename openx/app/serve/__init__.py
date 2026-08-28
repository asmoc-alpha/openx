"""openx serve（P4）：长存会话服务 + Web 端。

加端 = 写一个客户端 attach 协议，内核零改动（架构详设 §5-§6）。
- ``bridge.ServeConsole``：agent/executor 可用的无终端 stub console；
- ``session.ServeSession``：agent 宿主 + 客户端注册表 + 串行回合 + 广播；
- ``server.run_serve``：aiohttp 站点（WS 事件流 + REST 只读 + 静态前端）。
"""
