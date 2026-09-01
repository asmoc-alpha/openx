"""① 推理核心（microkernel-design §0 五件套）——模型接入的形状与重试。

- provider.py  模型接入槽的接口形状（Provider 协议）+ 错误契约
  （ProviderTransientError / ProviderFatalError）；实现进 llm/ 零件层
- retry.py     重试策略（RetryingProvider：瞬态重试、指数退避+抖动）
"""
