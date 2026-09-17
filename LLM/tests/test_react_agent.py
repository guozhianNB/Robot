# -*- coding: utf-8 -*-
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from LLM import conf
from LLM.agent import chat


def _ward_principal():
    return {"role": "ward", "uid": "ward-1", "ward_uid": ""}


def test_react_tool_round_limit_is_a_constant_not_a_setting():
    assert conf.REACT_MAX_TOOL_ROUNDS == 4
    assert "react_max_tool_rounds" not in conf.DEFAULT_SETTINGS


def test_react_prompt_file_uses_marker_and_is_loaded_after_role(monkeypatch):
    prompt_file = Path(__file__).with_name("react_prompt_marker.tmp.md")
    prompt_file.write_text("供人阅读的说明，不应发给模型\n\n<!-- PROMPT -->\n\nREACT_SENTINEL", encoding="utf-8")
    monkeypatch.setattr(chat, "REACT_PROMPT_FILE", prompt_file)
    monkeypatch.setattr(chat, "_load_role_prompt", lambda role: "ROLE_SENTINEL")
    monkeypatch.setattr(chat, "_ward_context", lambda *args: "")
    monkeypatch.setattr(chat.rag, "recall_v3", lambda *args: {"context": ""})
    monkeypatch.setattr(chat.db, "get_summary", lambda *args: "")
    monkeypatch.setattr(chat, "_expression_hint", lambda *args: "")

    try:
        system = chat.build_system("ward-1", {}, principal=_ward_principal())

        assert "供人阅读的说明" not in system
        assert "REACT_SENTINEL" in system
        assert system.index("ROLE_SENTINEL") < system.index("REACT_SENTINEL")
        assert system.index("REACT_SENTINEL") < system.index("【当前时间】")
    finally:
        prompt_file.unlink(missing_ok=True)


def test_missing_react_prompt_uses_fallback_and_warns_once_until_recovery(monkeypatch):
    prompt_file = Path(__file__).with_name("react_prompt_recovery.tmp.md")
    prompt_file.unlink(missing_ok=True)
    audits = []
    monkeypatch.setattr(chat, "REACT_PROMPT_FILE", prompt_file)
    monkeypatch.setattr(chat, "_react_prompt_warned", False)
    monkeypatch.setattr(chat.audit, "log", lambda *args, **kwargs: audits.append((args, kwargs)))

    try:
        first = chat._load_react_prompt()
        second = chat._load_react_prompt()

        assert first == second
        assert first
        assert len(audits) == 1
        assert audits[0][0] == ("chat",)
        assert audits[0][1]["action"] == "prompt_react_missing"

        prompt_file.write_text("<!-- PROMPT -->\nRECOVERED_SENTINEL", encoding="utf-8")
        assert chat._load_react_prompt() == "RECOVERED_SENTINEL"
        prompt_file.unlink()
        chat._load_react_prompt()

        assert len(audits) == 2
    finally:
        prompt_file.unlink(missing_ok=True)


def test_react_prompt_contains_required_tool_decision_rules():
    text = chat._load_react_prompt()
    for expected in (
        "仅必要时调用",
        "Observation",
        "成功和充分性",
        "改参",
        "换工具",
        "如实停止",
        "信息充分即答",
        "不输出内部 Thought",
        "协议",
        "虚构结果",
        "最多一句",
    ):
        assert expected in text


def _chunk(*, content=None, tool_calls=None, finish_reason="stop", reasoning=None):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)]
    )


def _tool_call(name="search", arguments='{"query":"床位"}', call_id="call-1", index=0):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class _SequenceCompletions:
    def __init__(self, streams):
        self.streams = streams
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.streams[len(self.requests) - 1]


def _client_with_tool_then_answer(answer="查到了"):
    completions = _SequenceCompletions([
        [_chunk(tool_calls=[_tool_call()], finish_reason="tool_calls")],
        [_chunk(content=answer)],
    ])
    return SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
    )


def _prepare_tool_stream(monkeypatch, result=None):
    monkeypatch.setattr(chat, "build_messages", lambda *args, **kwargs: [])
    monkeypatch.setattr(chat.tool_mod, "effective_tools", lambda *args: [{"type": "function"}])
    monkeypatch.setattr(chat.tool_mod, "run_tool", lambda *args: result or {"ok": True, "result": "床位 101"})
    monkeypatch.setattr(chat.db, "log_tool", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat.db, "append_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat.audit, "log", lambda *args, **kwargs: None)


_FINALIZE_PROMPT = (
    "工具调用次数已达上限。不要再调用工具；请仅根据已有工具结果直接回答用户，"
    "并如实说明尚未完成或无法确认的部分。"
)


def _budget_streams(final_stream, fourth_calls=None):
    streams = [
        [_chunk(tool_calls=[_tool_call(call_id=f"call-{round_no}")],
                finish_reason="tool_calls")]
        for round_no in range(1, 4)
    ]
    streams.append([_chunk(
        tool_calls=fourth_calls or [_tool_call(call_id="call-4")],
        finish_reason="tool_calls",
    )])
    streams.append(final_stream)
    return streams


def _prepare_budget_stream(monkeypatch, streams):
    monkeypatch.setattr(chat, "build_messages", lambda *args, **kwargs: [])
    monkeypatch.setattr(chat.tool_mod, "effective_tools",
                        lambda *args: [{"type": "function"}])
    calls = []
    monkeypatch.setattr(chat.tool_mod, "run_tool",
                        lambda name, args, principal: calls.append((name, args)) or
                        {"ok": True, "result": name})
    monkeypatch.setattr(chat.db, "log_tool", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat.db, "append_history", lambda *args, **kwargs: None)
    audits = []
    monkeypatch.setattr(chat.audit, "log",
                        lambda *args, **kwargs: audits.append((args, kwargs)))
    completions = _SequenceCompletions(streams)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client, completions, calls, audits


def test_auto_tool_observation_escalates_next_request_to_high(monkeypatch):
    _prepare_tool_stream(monkeypatch)
    client = _client_with_tool_then_answer()

    events = list(chat.chat_stream(client, "m", "elder-1", "你好", "auto", {}))

    requests = client.chat.completions.requests
    assert requests[0]["reasoning_effort"] is None
    assert requests[1]["reasoning_effort"] == "high"
    assert requests[1]["extra_body"] == chat._thinking_extra("high")
    escalations = [e for e in events if e.get("type") == "meta" and
                   e["router"].get("method") == "tool_observation"]
    assert len(escalations) == 1
    assert escalations[0]["router"] == {
        "on": True,
        "effort": "high",
        "reason": "已取得工具结果，自动加深思考",
        "method": "tool_observation",
        "mode": "auto",
        "uid": "elder-1",
    }


def test_auto_already_high_does_not_emit_duplicate_observation_meta(monkeypatch):
    _prepare_tool_stream(monkeypatch)
    client = _client_with_tool_then_answer()

    events = list(chat.chat_stream(client, "m", "elder-1", "我胸口痛", "auto", {}))

    assert client.chat.completions.requests[0]["reasoning_effort"] == "high"
    assert client.chat.completions.requests[1]["reasoning_effort"] == "high"
    assert not [e for e in events if e.get("type") == "meta" and
                 e["router"].get("method") == "tool_observation"]


@pytest.mark.parametrize("mode,expected", [
    ("none", None),
    ("low", "low"),
    ("high", "high"),
    ("max", "max"),
])
def test_manual_effort_is_preserved_after_tool_observation(monkeypatch, mode, expected):
    _prepare_tool_stream(monkeypatch)
    client = _client_with_tool_then_answer()

    list(chat.chat_stream(client, "m", "elder-1", "你好", mode, {}))

    requests = client.chat.completions.requests
    assert requests[0]["reasoning_effort"] == expected
    assert requests[1]["reasoning_effort"] == expected


def test_auto_failed_tool_observation_still_escalates(monkeypatch):
    _prepare_tool_stream(monkeypatch, {"ok": False, "error": "权限不足"})
    client = _client_with_tool_then_answer()

    events = list(chat.chat_stream(client, "m", "elder-1", "你好", "auto", {}))

    assert client.chat.completions.requests[1]["reasoning_effort"] == "high"
    assert len([e for e in events if e.get("type") == "meta" and
                e["router"].get("method") == "tool_observation"]) == 1


def test_no_tool_content_deltas_remain_immediate_events(monkeypatch):
    monkeypatch.setattr(chat, "build_messages", lambda *args, **kwargs: [])
    monkeypatch.setattr(chat.tool_mod, "effective_tools", lambda *args: [])
    monkeypatch.setattr(chat.db, "append_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat.audit, "log", lambda *args, **kwargs: None)
    completions = _SequenceCompletions([[
        _chunk(content="第一段", finish_reason=None),
        _chunk(content="第二段", finish_reason="stop"),
    ]])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    events = list(chat.chat_stream(client, "m", "elder-1", "你好", "auto", {}))

    assert [e["content"] for e in events if e["type"] == "content"] == ["第一段", "第二段"]
    assert events[-1] == {"type": "done", "assistant": "第一段第二段"}


def test_tool_progress_and_final_answer_are_both_in_done_assistant(monkeypatch):
    _prepare_tool_stream(monkeypatch)
    client = _client_with_tool_then_answer()
    client.chat.completions.streams[0] = [
        _chunk(content="我帮您看看", finish_reason=None),
        _chunk(tool_calls=[_tool_call()], finish_reason="tool_calls"),
    ]

    events = list(chat.chat_stream(client, "m", "elder-1", "查一下", "none", {}))

    assert [e["content"] for e in events if e["type"] == "content"] == ["我帮您看看", "查到了"]
    assert events[-1] == {"type": "done", "assistant": "我帮您看看查到了"}


def test_failed_tool_is_observed_then_replanned_with_new_arguments(monkeypatch):
    monkeypatch.setattr(chat, "build_messages", lambda *args, **kwargs: [])
    monkeypatch.setattr(chat.tool_mod, "effective_tools", lambda *args: [{"type": "function"}])
    calls = []

    def run_tool(name, args, principal):
        calls.append((name, args))
        if args == {"value": 1}:
            return {"ok": False, "type": "upstream_error", "error": "value 1 failed",
                    "details": {"retryable": True}}
        return {"ok": True, "result": "value 2 worked"}

    monkeypatch.setattr(chat.tool_mod, "run_tool", run_tool)
    monkeypatch.setattr(chat.db, "log_tool", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat.db, "append_history", lambda *args, **kwargs: None)
    audits = []
    monkeypatch.setattr(chat.audit, "log", lambda *args, **kwargs: audits.append((args, kwargs)))
    completions = _SequenceCompletions([
        [_chunk(tool_calls=[_tool_call(arguments='{"value":1}', call_id="call-first")],
                finish_reason="tool_calls")],
        [_chunk(tool_calls=[_tool_call(arguments='{"value":2}', call_id="call-second")],
                finish_reason="tool_calls")],
        [_chunk(content="第二次成功了")],
    ])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    events = list(chat.chat_stream(client, "m", "elder-1", "重试一下", "none", {}))

    assert calls == [("search", {"value": 1}), ("search", {"value": 2})]
    assert events[-1] == {"type": "done", "assistant": "第二次成功了"}
    third_messages = completions.requests[2]["messages"]
    observations = [message for message in third_messages if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in observations] == ["call-first", "call-second"]
    assert json.loads(observations[0]["content"]) == {
        "ok": False,
        "type": "upstream_error",
        "error": "value 1 failed",
        "details": {"retryable": True},
    }
    assert json.loads(observations[1]["content"]) == {
        "ok": True,
        "result": "value 2 worked",
    }
    turn_audit = next(kwargs for args, kwargs in audits
                      if args == ("chat",) and kwargs.get("action") == "turn")
    assert turn_audit["tool_rounds"] == 2


def test_empty_arguments_execute_as_empty_object(monkeypatch):
    monkeypatch.setattr(chat, "build_messages", lambda *args, **kwargs: [])
    monkeypatch.setattr(chat.tool_mod, "effective_tools", lambda *args: [{"type": "function"}])
    calls = []
    monkeypatch.setattr(chat.tool_mod, "run_tool",
                        lambda name, args, principal: calls.append((name, args)) or
                        {"ok": True, "result": "done"})
    monkeypatch.setattr(chat.db, "log_tool", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat.db, "append_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat.audit, "log", lambda *args, **kwargs: None)
    completions = _SequenceCompletions([
        [_chunk(tool_calls=[_tool_call(arguments="", call_id="call-empty")],
                finish_reason="tool_calls")],
        [_chunk(content="执行完成")],
    ])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    events = list(chat.chat_stream(client, "m", "elder-1", "执行无参数工具", "none", {}))

    assert calls == [("search", {})]
    assistant = next(message for message in completions.requests[1]["messages"]
                     if message["role"] == "assistant")
    assert assistant["tool_calls"][0]["function"]["arguments"] == "{}"
    assert next(event for event in events if event["type"] == "tool_result")["ok"] is True


@pytest.mark.parametrize("raw", [
    '{"value":',
    "[]",
    '"not an object"',
    '{"value":NaN}',
    '{"value":Infinity}',
    '{"value":-Infinity}',
])
def test_invalid_arguments_are_observed_without_executing_tool(monkeypatch, raw):
    monkeypatch.setattr(chat, "build_messages", lambda *args, **kwargs: [])
    monkeypatch.setattr(chat.tool_mod, "effective_tools", lambda *args: [{"type": "function"}])
    calls = []
    monkeypatch.setattr(chat.tool_mod, "run_tool",
                        lambda *args: calls.append(args) or {"ok": True})
    tool_logs = []
    monkeypatch.setattr(chat.db, "log_tool",
                        lambda *args, **kwargs: tool_logs.append((args, kwargs)))
    monkeypatch.setattr(chat.db, "append_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat.audit, "log", lambda *args, **kwargs: None)
    completions = _SequenceCompletions([
        [_chunk(tool_calls=[_tool_call(arguments=raw, call_id="call-invalid")],
                finish_reason="tool_calls")],
        [_chunk(content="参数不合法，我没有执行")],
    ])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    events = list(chat.chat_stream(client, "m", "elder-1", "执行一下", "none", {}))

    assert calls == []
    tool_starts = [event for event in events if event["type"] == "tool_start"]
    assert tool_starts == [{"type": "tool_start", "tool": "search", "args": {}}]
    tool_results = [event for event in events if event["type"] == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["ok"] is False
    assert tool_results[0]["snippet"]
    observation = next(message for message in completions.requests[1]["messages"]
                       if message["role"] == "tool")
    assert observation["tool_call_id"] == "call-invalid"
    result = json.loads(observation["content"])
    assert result["ok"] is False
    assert result["type"] == "invalid_arguments"
    assert result["error"]
    assert tool_logs[0][1]["status"] == "error"


def test_invalid_arguments_parser_contract():
    assert chat._parse_tool_arguments('{"value":1}') == ({"value": 1}, None)
    assert chat._parse_tool_arguments("") == ({}, None)
    for raw in ('{"value":', "[]", '"not an object"', '{"value":NaN}',
                '{"value":Infinity}', '{"value":-Infinity}'):
        args, error = chat._parse_tool_arguments(raw)
        assert args is None
        assert error["ok"] is False
        assert error["type"] == "invalid_arguments"
        assert error["error"]


def test_all_invalid_arguments_do_not_escalate_auto_effort(monkeypatch):
    monkeypatch.setattr(chat, "build_messages", lambda *args, **kwargs: [])
    monkeypatch.setattr(chat.tool_mod, "effective_tools", lambda *args: [{"type": "function"}])
    calls = []
    monkeypatch.setattr(chat.tool_mod, "run_tool",
                        lambda *args: calls.append(args) or {"ok": True})
    monkeypatch.setattr(chat.db, "log_tool", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat.db, "append_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat.audit, "log", lambda *args, **kwargs: None)
    completions = _SequenceCompletions([
        [_chunk(tool_calls=[_tool_call(arguments='{"value":NaN}', call_id="call-invalid")],
                finish_reason="tool_calls")],
        [_chunk(content="参数不合法")],
    ])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    events = list(chat.chat_stream(client, "m", "elder-1", "执行一下", "auto", {}))

    assert calls == []
    assert [request["reasoning_effort"] for request in completions.requests] == [None, None]
    assert not [event for event in events if event.get("type") == "meta" and
                event["router"].get("method") == "tool_observation"]


def test_multiple_tools_keep_index_order_ids_and_count_as_one_round(monkeypatch):
    monkeypatch.setattr(chat, "build_messages", lambda *args, **kwargs: [])
    monkeypatch.setattr(chat.tool_mod, "effective_tools", lambda *args: [{"type": "function"}])
    calls = []
    monkeypatch.setattr(chat.tool_mod, "run_tool",
                        lambda name, args, principal: calls.append((name, args)) or
                        {"ok": True, "result": name})
    monkeypatch.setattr(chat.db, "log_tool", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat.db, "append_history", lambda *args, **kwargs: None)
    audits = []
    monkeypatch.setattr(chat.audit, "log", lambda *args, **kwargs: audits.append((args, kwargs)))
    completions = _SequenceCompletions([
        [_chunk(tool_calls=[
            _tool_call(name="second", arguments='{"position":2}', call_id="call-2", index=1),
            _tool_call(name="first", arguments='{"position":1}', call_id="call-1", index=0),
        ], finish_reason="tool_calls")],
        [_chunk(content="都完成了")],
    ])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    events = list(chat.chat_stream(client, "m", "elder-1", "依次执行", "auto", {}))

    assert calls == [("first", {"position": 1}), ("second", {"position": 2})]
    messages = completions.requests[1]["messages"]
    assistant = next(message for message in messages if message["role"] == "assistant")
    assert [call["id"] for call in assistant["tool_calls"]] == ["call-1", "call-2"]
    observations = [message for message in messages if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in observations] == ["call-1", "call-2"]
    escalations = [event for event in events if event.get("type") == "meta" and
                   event["router"].get("method") == "tool_observation"]
    assert len(escalations) == 1
    turn_audit = next(kwargs for args, kwargs in audits
                      if args == ("chat",) and kwargs.get("action") == "turn")
    assert turn_audit["tool_rounds"] == 1


def test_empty_content_fallback_disables_tools(monkeypatch):
    monkeypatch.setattr(chat, "build_messages", lambda *args, **kwargs: [])
    monkeypatch.setattr(chat.tool_mod, "effective_tools", lambda *args: [{"type": "function"}])
    monkeypatch.setattr(chat.db, "append_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat.audit, "log", lambda *args, **kwargs: None)
    completions = _SequenceCompletions([
        [_chunk(reasoning="思考中", finish_reason="stop")],
        [_chunk(content="最终回答")],
    ])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    list(chat.chat_stream(client, "m", "elder-1", "你好", "high", {}))

    assert completions.requests[1]["reasoning_effort"] is None
    assert completions.requests[1]["tools"] is None
    assert completions.requests[1]["tool_choice"] is None


def test_budget_allows_four_tool_rounds_then_forces_tool_free_finalize(monkeypatch):
    client, completions, calls, audits = _prepare_budget_stream(
        monkeypatch, _budget_streams([_chunk(content="根据四次结果，已经完成")]))
    history = []
    monkeypatch.setattr(chat.db, "append_history",
                        lambda uid, role, content: history.append((uid, role, content)))

    events = list(chat.chat_stream(client, "m", "elder-1", "连续执行", "auto", {}))

    assert len(calls) == 4
    assert len(completions.requests) == 5
    finalize_request = completions.requests[4]
    assert finalize_request["tools"] is None
    assert finalize_request["tool_choice"] is None
    assert finalize_request["reasoning_effort"] == "high"
    assert any(message == {"role": "system", "content": _FINALIZE_PROMPT}
               for message in finalize_request["messages"])
    assert events[-1] == {"type": "done", "assistant": "根据四次结果，已经完成"}
    assert history == [
        ("elder-1", "user", "连续执行"),
        ("elder-1", "assistant", "根据四次结果，已经完成"),
    ]
    exhausted = [kwargs for args, kwargs in audits
                 if args == ("chat",) and kwargs.get("action") == "react_budget_exhausted"]
    assert exhausted == [{"action": "react_budget_exhausted", "uid": "elder-1",
                          "tool_rounds": 4, "effort": "high"}]


def test_budget_fourth_round_executes_all_calls_before_finalize(monkeypatch):
    fourth_calls = [
        _tool_call(name="fourth-a", arguments='{"part":"a"}',
                   call_id="call-4a", index=0),
        _tool_call(name="fourth-b", arguments='{"part":"b"}',
                   call_id="call-4b", index=1),
    ]
    client, completions, calls, _ = _prepare_budget_stream(
        monkeypatch,
        _budget_streams([_chunk(content="第四轮两个调用都已处理")], fourth_calls),
    )

    list(chat.chat_stream(client, "m", "elder-1", "批量执行", "auto", {}))

    assert calls[-2:] == [("fourth-a", {"part": "a"}),
                          ("fourth-b", {"part": "b"})]
    assert len(calls) == 5
    assert len(completions.requests) == 5
    observations = [message for message in completions.requests[4]["messages"]
                    if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in observations[-2:]] == [
        "call-4a", "call-4b",
    ]


def test_budget_reasoning_only_finalize_falls_back_once_without_tools(monkeypatch):
    streams = _budget_streams([_chunk(reasoning="还在整理", finish_reason="stop")])
    streams.append([_chunk(content="兜底回答")])
    client, completions, calls, audits = _prepare_budget_stream(monkeypatch, streams)

    events = list(chat.chat_stream(client, "m", "elder-1", "连续执行", "auto", {}))

    assert len(calls) == 4
    assert len(completions.requests) == 6
    fallback_request = completions.requests[5]
    assert fallback_request["tools"] is None
    assert fallback_request["tool_choice"] is None
    assert fallback_request["reasoning_effort"] is None
    assert fallback_request["extra_body"] == chat._thinking_extra(None)
    assert any(message == {"role": "system", "content": _FINALIZE_PROMPT}
               for message in fallback_request["messages"])
    assert [event["content"] for event in events if event["type"] == "reasoning"] == [
        "还在整理",
    ]
    assert events[-1] == {"type": "done", "assistant": "兜底回答"}
    fallbacks = [kwargs for args, kwargs in audits
                 if args == ("chat",) and kwargs.get("action") == "thinking_empty_fallback"]
    assert len(fallbacks) == 1


def test_budget_finalize_tool_calls_are_not_executed_and_fall_back(monkeypatch):
    streams = _budget_streams([
        _chunk(content="这段违规收尾不得落入最终回答",
               tool_calls=[_tool_call(name="forbidden", call_id="call-5")],
               finish_reason="tool_calls"),
    ])
    streams.append([_chunk(content="不再调用工具，直接回答")])
    client, completions, calls, _ = _prepare_budget_stream(monkeypatch, streams)

    events = list(chat.chat_stream(client, "m", "elder-1", "连续执行", "auto", {}))

    assert len(calls) == 4
    assert all(name != "forbidden" for name, _ in calls)
    assert len(completions.requests) == 6
    assert completions.requests[5]["tools"] is None
    assert completions.requests[5]["tool_choice"] is None
    observations = [message for message in completions.requests[5]["messages"]
                    if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in observations] == [
        "call-1", "call-2", "call-3", "call-4",
    ]
    assert events[-1] == {"type": "done", "assistant": "不再调用工具，直接回答"}


def test_budget_manual_low_finalize_preserves_low_effort(monkeypatch):
    client, completions, calls, audits = _prepare_budget_stream(
        monkeypatch, _budget_streams([_chunk(content="轻度思考后回答")]))

    list(chat.chat_stream(client, "m", "elder-1", "连续执行", "low", {}))

    assert len(calls) == 4
    assert completions.requests[4]["reasoning_effort"] == "low"
    exhausted = next(kwargs for args, kwargs in audits
                     if args == ("chat",) and kwargs.get("action") ==
                     "react_budget_exhausted")
    assert exhausted["effort"] == "low"


def test_non_budget_plain_answer_does_not_add_finalize_prompt(monkeypatch):
    monkeypatch.setattr(chat, "build_messages",
                        lambda *args, **kwargs: [{"role": "user", "content": "你好"}])
    monkeypatch.setattr(chat.tool_mod, "effective_tools", lambda *args: [])
    monkeypatch.setattr(chat.db, "append_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat.audit, "log", lambda *args, **kwargs: None)
    completions = _SequenceCompletions([[_chunk(content="你好")]])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    list(chat.chat_stream(client, "m", "elder-1", "你好", "none", {}))

    assert len(completions.requests) == 1
    assert not any(message.get("content") == _FINALIZE_PROMPT
                   for message in completions.requests[0]["messages"])
