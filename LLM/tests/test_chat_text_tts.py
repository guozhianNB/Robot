# -*- coding: utf-8 -*-
import asyncio

import pytest
from fastapi.testclient import TestClient

from LLM import chat, server, voice_api


def _patch_request_side_effects(monkeypatch):
    monkeypatch.setattr(server.db, "get_settings", lambda: {})
    monkeypatch.setattr(server._bg, "submit", lambda *args, **kwargs: None)


def test_chat_defaults_to_silent_text_reply(monkeypatch):
    _patch_request_side_effects(monkeypatch)

    def stream(*args, **kwargs):
        yield {"type": "content", "content": "静音回复"}
        yield {"type": "done", "assistant": "静音回复"}

    monkeypatch.setattr(chat, "chat_stream", stream)
    monkeypatch.setattr(
        voice_api,
        "begin_text_reply",
        lambda: pytest.fail("默认请求不应启动文本播报"),
    )
    monkeypatch.setattr(voice_api, "feed_text_reply", lambda handle, delta: None)
    monkeypatch.setattr(
        voice_api, "end_text_reply", lambda handle, flush_tail=True: None
    )

    response = TestClient(server.app).post(
        "/api/chat", json={"uid": "elder_001", "message": "你好"}
    )

    assert response.status_code == 200
    assert '"type": "content"' in response.text
    assert '"content": "静音回复"' in response.text


def test_chat_speak_feeds_content_and_flushes_after_done(monkeypatch):
    _patch_request_side_effects(monkeypatch)
    handle = object()
    calls = []

    def stream(*args, **kwargs):
        yield {"type": "content", "content": "第一句。"}
        yield {"type": "content", "content": "尾句"}
        yield {"type": "done", "assistant": "第一句。尾句"}

    monkeypatch.setattr(chat, "chat_stream", stream)
    monkeypatch.setattr(
        voice_api,
        "begin_text_reply",
        lambda: (calls.append(("begin",)), handle)[1],
    )
    monkeypatch.setattr(
        voice_api,
        "feed_text_reply",
        lambda speech, delta: calls.append(("feed", speech, delta)),
    )
    monkeypatch.setattr(
        voice_api,
        "end_text_reply",
        lambda speech, flush_tail=True: calls.append(
            ("end", speech, flush_tail)
        ),
    )

    response = TestClient(server.app).post(
        "/api/chat",
        json={"uid": "elder_001", "message": "你好", "speak": True},
    )

    assert response.status_code == 200
    assert calls == [
        ("begin",),
        ("feed", handle, "第一句。"),
        ("feed", handle, "尾句"),
        ("end", handle, True),
    ]


def test_chat_speak_does_not_flush_tail_when_stream_fails(monkeypatch):
    _patch_request_side_effects(monkeypatch)
    handle = object()
    calls = []

    def stream(*args, **kwargs):
        yield {"type": "content", "content": "半句话"}
        raise RuntimeError("stream failed")

    monkeypatch.setattr(chat, "chat_stream", stream)
    monkeypatch.setattr(voice_api, "begin_text_reply", lambda: handle)
    monkeypatch.setattr(
        voice_api,
        "feed_text_reply",
        lambda speech, delta: calls.append(("feed", speech, delta)),
    )
    monkeypatch.setattr(
        voice_api,
        "end_text_reply",
        lambda speech, flush_tail=True: calls.append(
            ("end", speech, flush_tail)
        ),
    )

    with pytest.raises(RuntimeError, match="stream failed"):
        TestClient(server.app).post(
            "/api/chat",
            json={"uid": "elder_001", "message": "你好", "speak": True},
        )

    assert calls == [
        ("feed", handle, "半句话"),
        ("end", handle, False),
    ]


def test_chat_submits_completed_turn_when_body_closes_after_done(monkeypatch):
    handle = object()
    end_calls = []
    submit_calls = []

    def stream(*args, **kwargs):
        yield {"type": "content", "content": "完整回复"}
        yield {"type": "done", "assistant": "完整回复"}

    monkeypatch.setattr(server.db, "get_settings", lambda: {})
    monkeypatch.setattr(chat, "chat_stream", stream)
    monkeypatch.setattr(voice_api, "begin_text_reply", lambda: handle)
    monkeypatch.setattr(voice_api, "feed_text_reply", lambda speech, delta: None)
    monkeypatch.setattr(
        voice_api,
        "end_text_reply",
        lambda speech, flush_tail=True: end_calls.append(
            (speech, flush_tail)
        ),
    )
    monkeypatch.setattr(
        server._bg,
        "submit",
        lambda *args, **kwargs: submit_calls.append((args, kwargs)),
    )

    async def consume_until_done_then_close():
        response = await server.chat_route(
            server.ChatRequest(
                uid="elder_done",
                message="请回复",
                speak=True,
            )
        )
        body = response.body_iterator
        try:
            while True:
                chunk = await anext(body)
                if '"type": "done"' in chunk:
                    break
        finally:
            await body.aclose()

    asyncio.run(consume_until_done_then_close())

    assert end_calls == [(handle, True)]
    assert len(submit_calls) == 1
    args, kwargs = submit_calls[0]
    # 第 4 个参数 = 本轮 principal 的角色（集体层不沉淀，规格 §5.3；任务 11 起 /api/chat
    # 必须把它带给后台沉淀任务）。角色值直接与会话层对齐断言，不写死字面量。
    assert args[:4] == (server._post_chat_jobs, "elder_done", "请回复", "完整回复")
    assert args[4] == server.session.get_principal("kiosk")["role"]
    assert kwargs == {}
