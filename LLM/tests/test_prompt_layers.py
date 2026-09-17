# -*- coding: utf-8 -*-
"""三层提示词分层 + 集体上下文单向注入（R5）测试。"""
import json
import os
import tempfile
from pathlib import Path

import pytest

from LLM.agent import chat
from LLM.store import db


@pytest.fixture()
def d(monkeypatch):
    tmp = tempfile.mkdtemp()
    old = db.DB_PATH
    db.DB_PATH = os.path.join(tmp, "t.db")
    db.init_db()
    db.upsert_ward("ward_101", name="101 病房")
    db.upsert_ward("ward_102", name="102 病房")
    db.upsert_profile("elder_101_1", name="张奶奶")
    db.set_profile_ward("elder_101_1", "ward_101")
    db.upsert_profile("elder_102_1", name="王奶奶")
    db.set_profile_ward("elder_102_1", "ward_102")
    for i in range(3):
        db.append_history("ward_101", "user", f"病房消息{i}")
    # 离线：不让 RAG 真去调 embedding（本测试只验装配，不验召回）
    monkeypatch.setattr(chat.rag, "recall_v3", lambda uid, q: {"context": ""})
    yield db
    db.DB_PATH = old


def _p(role, uid, ward=""):
    return {"role": role, "uid": uid, "slot": "kiosk", "source": "manual",
            "locked": False, "ward_uid": ward}


def test_ward_role_fragment_is_loaded(d):
    sys_p = chat.build_system("ward_101", {}, "", principal=_p("ward", "ward_101", "ward_101"))
    assert "病房里" in sys_p                       # ward.md 正文出现
    assert "小护" in sys_p                         # 共用 base 仍在


def test_ward_prompt_has_no_personal_profile(d):
    d.upsert_profile("elder_101_1", name="张奶奶", notes="糖尿病")
    sys_p = chat.build_system("ward_101", {}, "", principal=_p("ward", "ward_101", "ward_101"))
    assert "张奶奶" not in sys_p and "糖尿病" not in sys_p


def test_elder_sees_own_ward_context(d):
    sys_p = chat.build_system("elder_101_1", {}, "",
                              principal=_p("elder", "elder_101_1", "ward_101"))
    assert "病房消息2" in sys_p


def test_ward_does_not_see_elder_private_chat(d):
    db.append_history("elder_101_1", "user", "我昨晚没睡好")
    sys_p = chat.build_system("ward_101", {}, "", principal=_p("ward", "ward_101", "ward_101"))
    assert "我昨晚没睡好" not in sys_p


def test_cross_ward_isolation(d):
    """102 病房的老人看不到 101 病房的集体上下文（R5：跨病房不可读）。"""
    db.append_history("ward_102", "user", "102 病房的消息")
    sys_p = chat.build_system("elder_102_1", {}, "",
                              principal=_p("elder", "elder_102_1", "ward_102"))
    assert "病房消息2" not in sys_p and "102 病房的消息" in sys_p


def test_admin_gets_no_ward_context_even_with_ward_uid(d):
    """admin 即使有 ward_uid 也不注入集体上下文（原用例用空 ward_uid，判据是假的）。"""
    p = _p("admin", "admin", "ward_101")
    sys_p = chat.build_system("admin", {}, "", principal=p)
    assert "病房消息2" not in sys_p and "病房里刚说过的事" not in sys_p


def test_missing_role_file_degrades(d, monkeypatch):
    monkeypatch.setattr(chat, "_ROLE_PROMPT_DIR_OVERRIDE", Path("/nonexistent-dir"))
    sys_p = chat.build_system("ward_101", {}, "", principal=_p("ward", "ward_101", "ward_101"))
    assert "小护" in sys_p                          # base 仍在，没抛异常


def test_unknown_role_falls_back_to_ward_fragment(d):
    sys_p = chat.build_system("ward_101", {}, "", principal=_p("??", "ward_101", "ward_101"))
    assert "病房里" in sys_p


def test_ward_context_denied_when_ward_uid_is_not_a_ward(d):
    """**R5 回归**：`ward_uid` 必须真的是病房档案；否则会把某位老人的私聊当"病房里刚说过的事"注入。"""
    d.upsert_profile("elder_102_1", name="王奶奶")
    db.append_history("elder_101_1", "user", "我昨晚没睡好")        # 这是私聊，不是集体层
    p = _p("elder", "elder_102_1", "elder_101_1")                  # ward_id 被误设成一位老人
    sys_p = chat.build_system("elder_102_1", {}, "", principal=p)
    assert "我昨晚没睡好" not in sys_p


def test_principal_uid_wins_over_request_uid(d):
    """**回归**：principal 给了就以它的 uid 为数据目标——客户端传过期 uid 不许注入别人的档案。"""
    d.upsert_profile("elder_101_1", name="张奶奶", notes="糖尿病")
    p = _p("elder", "elder_102_1", "ward_102")
    sys_p = chat.build_system("elder_101_1", {}, "", principal=p)   # 入参与 principal 不一致
    assert "糖尿病" not in sys_p


def test_broken_ward_context_window_does_not_crash(d):
    """设置被写坏时降级成默认 10，不许把整条对话炸掉。"""
    p = _p("elder", "elder_101_1", "ward_101")
    for bad in (None, "abc"):
        sys_p = chat.build_system("elder_101_1", {"ward_context_window": bad}, "", principal=p)
        assert "小护" in sys_p                                       # 没抛异常，base 仍在


def test_chat_history_uses_principal_uid_not_request_uid(d):
    """**回归（关键）**：入参 uid 与 principal 不一致时，滚动历史的读写以 principal 为准。

    旧实现里 `build_messages` 用入参 `uid` 取历史（`build_system` 已改用 principal）——于是
    主体是 elder_102_1、请求体传 `uid="elder_101_1"` 时，E101 的**私聊原文**会被当作 history
    注入当前上下文，回答也会被写进 E101 的历史（R5 的旁路）。
    """
    db.append_history("elder_101_1", "user", "E101 的秘密")
    p = _p("elder", "elder_102_1", "ward_102")
    msgs = chat.build_messages("elder_101_1", "在吗", False, {}, principal=p)
    joined = "\n".join(m["content"] for m in msgs)
    assert "E101 的秘密" not in joined
    # 反向确认：principal 自己的历史仍在（不是"整条 history 被吞掉"式的假通过）
    db.append_history("elder_102_1", "user", "E102 的私事")
    msgs = chat.build_messages("elder_101_1", "在吗", False, {}, principal=p)
    joined = "\n".join(m["content"] for m in msgs)
    assert "E102 的私事" in joined and "E101 的秘密" not in joined


def test_chat_stream_writes_history_to_principal_uid(d):
    """**回归（关键）**：落库也认 principal —— 回答不许写进入参 uid 的历史。"""
    class _Delta:
        content = "好的"
        reasoning_content = None
        tool_calls = None

    class _Choice:
        delta = _Delta()
        finish_reason = "stop"

    class _Chunk:
        choices = [_Choice()]

    class _Completions:
        def create(self, **kw):
            return [_Chunk()]

    class _Client:
        chat = type("C", (), {"completions": _Completions()})()

    p = _p("elder", "elder_102_1", "ward_102")
    events = list(chat.chat_stream(_Client(), "m", "elder_101_1", "在吗", "off", {},
                                   principal=p))
    # meta 报的是**实际生效**的数据主体（伪造/过期的入参 uid 被忽略）
    assert any(e["type"] == "meta" and e["router"]["uid"] == "elder_102_1" for e in events)
    assert [r["content"] for r in db.load_history("elder_102_1")] == ["在吗", "好的"]
    assert db.load_history("elder_101_1") == []


@pytest.mark.parametrize("result", [
    {"ok": True, "answer": "camera answer"},
    {"ok": False, "error": "permission denied: private prompt"},
])
def test_see_what_tool_log_fields_never_persist_result_or_prompt(result):
    args = {"image": "camera", "prompt": "private prompt", "channel": 3}
    secret_result = result.get("answer") or result.get("error")

    log_args, log_snippet = chat._tool_log_fields(
        "see_what", args, secret_result
    )

    assert log_args == {"channel": 3}
    assert log_snippet == "[视觉结果未写入日志]"
    assert "private prompt" not in str(log_args)
    assert secret_result not in log_snippet


@pytest.mark.parametrize("channel", [None, 0, 256, True, False, 3.0, "3"])
def test_see_what_tool_log_fields_rejects_invalid_channel(channel):
    args = {"image": "camera", "prompt": "private prompt", "channel": channel}

    log_args, log_snippet = chat._tool_log_fields("see_what", args, "secret result")

    assert log_args == {}
    assert log_snippet == "[视觉结果未写入日志]"


def test_non_vision_tool_log_fields_are_unchanged():
    args = {"query": "normal query"}
    snippet = "normal result"

    log_args, log_snippet = chat._tool_log_fields("search", args, snippet)

    assert log_args is args
    assert log_snippet == snippet


@pytest.mark.parametrize(
    "raw_result, expected_snippet",
    [
        (
            {
                "ok": True,
                "result": "cloud result containing private prompt",
                "answer": "real answer",
                "image": "data:image/jpeg;base64,secret-image",
            },
            "cloud result containing private prompt",
        ),
        (
            {
                "ok": False,
                "error": "permission denied: private prompt",
                "answer": "real denied answer",
                "image": "data:image/jpeg;base64,secret-image",
            },
            "permission denied: private prompt",
        ),
    ],
)
def test_chat_stream_sanitizes_only_persisted_vision_logs(
    d, monkeypatch, raw_result, expected_snippet
):
    class _Function:
        name = "see_what"
        arguments = '{"image":"camera","prompt":"private prompt","channel":7}'

    class _ToolCall:
        index = 0
        id = "call-1"
        function = _Function()

    class _Delta:
        content = None
        reasoning_content = None
        tool_calls = [_ToolCall()]

    class _Choice:
        delta = _Delta()
        finish_reason = "tool_calls"

    class _Chunk:
        choices = [_Choice()]

    class _FinalDelta:
        content = "最终回答"
        reasoning_content = None
        tool_calls = None

    class _FinalChoice:
        delta = _FinalDelta()
        finish_reason = "stop"

    class _FinalChunk:
        choices = [_FinalChoice()]

    class _Completions:
        def __init__(self):
            self.calls = 0
            self.requests = []

        def create(self, **kwargs):
            self.calls += 1
            self.requests.append(kwargs)
            return [_Chunk()] if self.calls == 1 else [_FinalChunk()]

    class _Client:
        def __init__(self):
            self.chat = type("C", (), {"completions": _Completions()})()

    logged_db = []
    logged_audit = []
    monkeypatch.setattr(chat.tool_mod, "effective_tools", lambda *_args: [{"type": "function"}])
    monkeypatch.setattr(chat.tool_mod, "run_tool", lambda *_args: raw_result)
    monkeypatch.setattr(chat.db, "log_tool", lambda *args, **kwargs: logged_db.append((args, kwargs)))
    monkeypatch.setattr(chat.audit, "log", lambda *args, **kwargs: logged_audit.append((args, kwargs)))

    client = _Client()
    events = list(chat.chat_stream(client, "m", "elder_101_1", "看一下", "off", {}))

    tool_result = next(event for event in events if event["type"] == "tool_result")
    assert tool_result["snippet"] == expected_snippet
    assert logged_db[0][0][2] == {"channel": 7}
    assert logged_db[0][0][3] == "[视觉结果未写入日志]"
    tool_audits = [entry for entry in logged_audit if entry[0] and entry[0][0] == "tool"]
    assert tool_audits[0][1]["args"] == {"channel": 7}
    persisted = json.dumps(
        {"db": logged_db[0], "audit": tool_audits[0]}, ensure_ascii=False
    )
    for secret in ("private prompt", "secret-image", "real answer", expected_snippet):
        assert secret not in persisted

    second_messages = client.chat.completions.requests[1]["messages"]
    tool_message = next(message for message in second_messages if message["role"] == "tool")
    assert raw_result["image"] in tool_message["content"]
    assert (raw_result.get("result") or raw_result.get("error")) in tool_message["content"]
    assert raw_result["answer"] in tool_message["content"]
    assert events[-1] == {"type": "done", "assistant": "最终回答"}


def test_chat_stream_keeps_non_vision_tool_log_fields_unchanged(d, monkeypatch):
    class _Function:
        name = "search"
        arguments = '{"query":"normal query"}'

    class _ToolCall:
        index = 0
        id = "call-search"
        function = _Function()

    class _Delta:
        content = None
        reasoning_content = None
        tool_calls = [_ToolCall()]

    class _Choice:
        delta = _Delta()
        finish_reason = "tool_calls"

    class _Chunk:
        choices = [_Choice()]

    class _FinalDelta:
        content = "搜索完成"
        reasoning_content = None
        tool_calls = None

    class _FinalChoice:
        delta = _FinalDelta()
        finish_reason = "stop"

    class _FinalChunk:
        choices = [_FinalChoice()]

    class _Completions:
        def __init__(self):
            self.calls = 0

        def create(self, **_kwargs):
            self.calls += 1
            return [_Chunk()] if self.calls == 1 else [_FinalChunk()]

    class _Client:
        def __init__(self):
            self.chat = type("C", (), {"completions": _Completions()})()

    original_args = {"query": "normal query"}
    original_snippet = "normal result"
    logged_db = []
    logged_audit = []
    monkeypatch.setattr(chat.tool_mod, "effective_tools", lambda *_args: [{"type": "function"}])
    monkeypatch.setattr(chat.db, "log_tool", lambda *args, **kwargs: logged_db.append((args, kwargs)))
    monkeypatch.setattr(chat.audit, "log", lambda *args, **kwargs: logged_audit.append((args, kwargs)))

    monkeypatch.setattr(
        chat.tool_mod,
        "run_tool",
        lambda *_args: {"ok": True, "result": original_snippet},
    )
    events = list(chat.chat_stream(_Client(), "m", "elder_101_1", "搜索", "off", {}))

    assert events[-1] == {"type": "done", "assistant": "搜索完成"}
    assert logged_db[0][0][2] == original_args
    assert logged_db[0][0][3] == original_snippet
    tool_audits = [entry for entry in logged_audit if entry[0] and entry[0][0] == "tool"]
    assert tool_audits[0][1]["args"] == original_args
