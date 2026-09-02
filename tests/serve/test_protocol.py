"""serve 协议扩展测试（openx/kernel/protocol.py 的 P4 增量）。

覆盖：上行 message/interrupt/permission_response(remember) 解析、畸形行
容错、未知类型容忍；下行 user_message/history/result/permission_request
(can_remember) 形状；存量 permission_response 行为不变（向后兼容）。
"""

from __future__ import annotations

from openx.kernel import protocol


# ── 上行解析 ─────────────────────────────────────────────────────


def test_uplink_user_message():
    m = protocol.parse_uplink('{"type": "message", "text": "hello"}')
    assert isinstance(m, protocol.UserMessage)
    assert m.text == "hello"


def test_uplink_interrupt():
    assert isinstance(protocol.parse_uplink('{"type": "interrupt"}'), protocol.Interrupt)


def test_uplink_permission_response_with_remember():
    m = protocol.parse_uplink(
        '{"type": "permission_response", "request_id": "r1",'
        ' "allowed": true, "remember": true}'
    )
    assert isinstance(m, protocol.PermissionResponse)
    assert m.allowed and m.remember


def test_uplink_malformed_tolerated():
    assert protocol.parse_uplink("not json") is None
    assert protocol.parse_uplink("") is None
    assert protocol.parse_uplink('{"type": "message"}') is None       # 缺 text
    assert protocol.parse_uplink('{"type": "message", "text": 5}') is None  # text 非字符串
    assert protocol.parse_uplink('[1, 2]') is None                     # 非对象


def test_uplink_unknown_type_tolerated():
    assert isinstance(protocol.parse_uplink('{"type": "nope"}'), protocol.UplinkUnknown)


def test_permission_response_missing_allowed_defaults_to_deny():
    """存量兼容：缺 allowed 字段 → request_id 合法则解析为拒绝（fail-closed）。"""
    m = protocol.parse_uplink('{"type": "permission_response", "request_id": "x"}')
    assert isinstance(m, protocol.PermissionResponse)
    assert m.allowed is False and m.remember is False


# ── P4.1 交互化上行：ask_user_response / plan_response ──────────


def test_uplink_ask_user_response():
    m = protocol.parse_uplink(
        '{"type": "ask_user_response", "request_id": "r1", "answers": ["A"]}'
    )
    assert isinstance(m, protocol.AskUserResponse)
    assert m.request_id == "r1" and m.answers == ["A"]


def test_uplink_ask_user_response_single_string_and_empty():
    """单字符串答案归一为列表；空列表合法（服务端落保守默认）。"""
    m = protocol.parse_uplink(
        '{"type": "ask_user_response", "request_id": "r1", "answers": "A"}'
    )
    assert m.answers == ["A"]
    m2 = protocol.parse_uplink(
        '{"type": "ask_user_response", "request_id": "r1", "answers": []}'
    )
    assert m2.answers == []


def test_uplink_ask_user_response_malformed_tolerated():
    """缺 request_id / 非字符串元素 / 非列表 → 拒绝解析（不断流）。"""
    assert protocol.parse_uplink(
        '{"type": "ask_user_response", "answers": ["A"]}'
    ) is None
    assert protocol.parse_uplink(
        '{"type": "ask_user_response", "request_id": "r", "answers": [1]}'
    ) is None
    assert protocol.parse_uplink(
        '{"type": "ask_user_response", "request_id": "r", "answers": [""]}'
    ) is None
    assert protocol.parse_uplink(
        '{"type": "ask_user_response", "request_id": "r", "answers": {}}'
    ) is None


def test_uplink_plan_response():
    m = protocol.parse_uplink(
        '{"type": "plan_response", "request_id": "r2", "approved": true}'
    )
    assert isinstance(m, protocol.PlanResponse)
    assert m.approved is True
    # 缺 approved 字段 → 拒绝（fail-closed）；缺 request_id → 解析失败
    m2 = protocol.parse_uplink('{"type": "plan_response", "request_id": "r2"}')
    assert m2.approved is False
    assert protocol.parse_uplink(
        '{"type": "plan_response", "approved": true}'
    ) is None


# ── serve 下行事件形状 ──────────────────────────────────────────


def test_user_message_event():
    assert protocol.user_message("hi") == {"type": "user_message", "text": "hi"}


def test_serve_history_shape():
    ev = protocol.serve_history([{"role": "user", "content": "hi"}])
    assert ev["type"] == "history"
    assert ev["messages"][0]["content"] == "hi"


def test_serve_panels_shape():
    """ui/v1 面板快照事件：{type, panels:[{name, lines}]}；空列表合法。"""
    ev = protocol.serve_panels(
        [{"name": "pet", "lines": ["(=^-^=)  pet is happy"]}]
    )
    assert ev == {
        "type": "panels",
        "panels": [{"name": "pet", "lines": ["(=^-^=)  pet is happy"]}],
    }
    assert protocol.serve_panels([]) == {"type": "panels", "panels": []}


def test_result_event_success_and_error():
    ok = protocol.result_event("done", False, 5, 2, "s1",
                               {"input_tokens": 1, "output_tokens": 2})
    assert ok["type"] == "result"
    assert ok["subtype"] == "success"
    assert not ok["is_error"]
    assert ok["duration_ms"] == 5 and ok["num_turns"] == 2
    assert ok["session_id"] == "s1"
    assert "error" not in ok

    err = protocol.result_event(None, True, 5, 0, "s1", {}, error="boom")
    assert err["subtype"] == "error"
    assert err["error"] == "boom"


def test_permission_request_can_remember():
    p = protocol.permission_request("r2", "shell", "run", can_remember=False)
    assert p["type"] == "permission_request"
    assert p["can_remember"] is False
    # 缺省 True：存量 NDJSON 消费者零改动
    assert protocol.permission_request("r2", "shell", "run")["can_remember"] is True


# ── P4.1 交互化下行：serve_ask_user / serve_plan_request ─────────


def test_serve_ask_user_shape():
    ev = protocol.serve_ask_user(
        "req-1", "Pick one",
        [{"label": "A", "description": "first"}, {"label": "B"}],
        multi_select=True,
    )
    assert ev["type"] == "ask_user"
    assert ev["request_id"] == "req-1"
    assert ev["question"] == "Pick one"
    assert ev["multi_select"] is True
    # 对象选项带 description；纯字符串选项补空 description
    assert ev["options"] == [
        {"label": "A", "description": "first"},
        {"label": "B", "description": ""},
    ]


def test_serve_ask_user_string_options_normalized():
    ev = protocol.serve_ask_user("r", "q", ["A", "B"])
    assert ev["options"] == [
        {"label": "A", "description": ""},
        {"label": "B", "description": ""},
    ]


def test_serve_plan_request_shape():
    ev = protocol.serve_plan_request("req-2", "# Plan")
    assert ev == {"type": "plan_request", "request_id": "req-2", "plan": "# Plan"}
    assert protocol.serve_plan_request("req-2") == {
        "type": "plan_request", "request_id": "req-2", "plan": "",
    }
