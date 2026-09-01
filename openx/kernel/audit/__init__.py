"""③ 安全审计（microkernel-design §0 五件套）——裁决管线与权限闸门。

- guard.py  七站裁决管线（Verdict 半格、只紧不松、每次裁决记账）；
  UI（弹窗）留在 executor，内核只裁决不弹窗
"""
