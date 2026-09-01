"""② 插件装配器（microkernel-design §0 五件套）——插件维护的单一门与装配。

发现 → 解析 → apply(ctx) → 校验 → 激活；目录表驱动的注册目录与注册表
（IPC）；P-B Manifest 自描述与 P-F PluginSpec 编写契约。
- loader.py        发现/加载五阶段（discover/load_module/extract_apply）
- registry.py      注册表（每条带 provenance；unregister）
- registrations.py 注册目录（枚举：tools/commands/contexts/lifecycle/providers）
- context.py       apply(ctx) 给予面（特权分隔）
- validate.py      形状校验（约束即代码）
- manifest.py      P-B 自描述 schema 校验
- protocols.py     P-D 协议目录（类别->协议->装配层路由）
- plugin_spec.py   P-F 模型可读的插件编写契约
"""
