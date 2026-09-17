# -*- coding: utf-8 -*-
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


def _tool_call(name="search", arguments='{"query":"床位"}', call_id="call-1"):
    return SimpleNamespace(
        index=0,
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
