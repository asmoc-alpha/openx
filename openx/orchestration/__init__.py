"""编排机制包（硬连线，P2+ 逐步插件化）。

本包不是内核：微内核 TCB 在 ``openx.kernel``（五件套）。这里收容 kernel
详设 §1.1 coordination 的硬连线实现——subagent / workflow / tasks /
fleet / sessions / history；按 microkernel-design 的插件分类，终局是
orchestration / lifecycle / compaction 类插件，随 P2+ 逐步迁出。

历史：原名 ``core``，2026-09-02 改名 ``orchestration``——"core" 与
"kernel" 语义重叠（两个"内核"），且协议（→ kernel/protocol.py）与
hooks（→ kernel/audit/hooks.py）归核后本包只剩编排机制，名随实走。
"""
