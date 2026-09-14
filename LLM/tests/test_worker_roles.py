# -*- coding: utf-8 -*-
import os
import tempfile

import pytest

from LLM import db
from LLM import session
from LLM import voice_api


@pytest.fixture()
def d():
    tmp = tempfile.mkdtemp()
    old = db.DB_PATH
    db.DB_PATH = os.path.join(tmp, "t.db")
    db.init_db()
    session.reset_for_test()
    db.upsert_ward("ward_101", name="101 病房")
    db.upsert_profile("elder_101_1", name="李爷爷")
    db.set_profile_ward("elder_101_1", "ward_101")
    yield db
    db.DB_PATH = old


def test_voice_api_session_roundtrips_through_session(d):
    # 先有"当前病房"（位置自动切换/手动切病房后的常态）：解锁 = 回集体层（§4.5），
    # 主体取当前病房而不是传进来的 uid —— 见 test_unlock_returns_to_collective_layer
    session.set_subject("ward_101", False, slot="kiosk", source="location")
    res = voice_api.set_session_uid("ward_101", False)
    assert res["uid"] == "ward_101" and res["role"] == "ward"
    got = voice_api.get_session_uid()
    assert got["uid"] == "ward_101" and got["role"] == "ward"
    assert got["locked"] is False
    assert "autoswitch" in got and "ttl_remain" in got


def test_voice_api_is_the_same_session_as_module(d):
    voice_api.set_session_uid("elder_101_1", True)
    assert session.get_principal("kiosk")["role"] == "elder"
    assert session.get_principal("kiosk")["locked"] is True
    assert session.get_principal("kiosk")["ward_uid"] == "ward_101"   # 跟随老人


def test_empty_uid_reads_as_none_for_old_callers(d):
    """老前端拿 uid=null 表示"没选人"；集体层没有病房时保持这个形状。"""
    session.reset_for_test()
    assert voice_api.get_session_uid()["uid"] in (None, "")


def test_worker_admin_is_not_downgraded(d):
    """管理员在车前说话 → 声纹认到老人也不改主体（D8 提权只升不降）。"""
    from LLM.voice import worker as worker_mod
    d.set_admin_password("111111")
    session.login_admin("111111", slot="kiosk")
    w = worker_mod.VoiceWorker(stream_fn=lambda uid, text: iter(()))
    w._apply_role_subject("elder_101_1")
    assert session.get_principal("kiosk")["role"] == "admin"
    assert session.get_principal("kiosk")["uid"] == "admin"


def test_worker_sets_voiceprint_subject_when_not_admin(d):
    """非管理员：认出谁就切到谁（source=voiceprint）。"""
    from LLM.voice import worker as worker_mod
    calls = []
    orig = session.set_subject

    def spy(uid, locked=False, slot="kiosk", source="manual"):
        calls.append((uid, locked, slot, source))
        return orig(uid, locked, slot, source)

    session.set_subject = spy                       # 函数级替换，测完还原
    try:
        w = worker_mod.VoiceWorker(stream_fn=lambda uid, text: iter(()))
        w._apply_role_subject("elder_101_1")
    finally:
        session.set_subject = orig
    assert calls and calls[0][0] == "elder_101_1" and calls[0][3] == "voiceprint"
    assert session.get_principal("kiosk")["role"] == "elder"
    assert session.get_principal("kiosk")["ward_uid"] == "ward_101"   # 当前病房跟随该老人


def test_no_fabricated_elder_from_fallback(d, monkeypatch):
    """**回归（关键）**：没有主体时，兜底常量不许被回灌成"声纹识别结果"。"""
    from LLM.voice import worker as worker_mod

    session.reset_for_test()                      # 无病房、无主体
    w = worker_mod.VoiceWorker(stream_fn=lambda uid, text: iter(()))
    w._apply_role_subject(None)
    w.current_uid = "elder_001"                   # 模拟"应答用了兜底 uid"
    w._apply_role_subject(None)                   # 第二轮：不许把它当识别结果
    assert session.get_principal("kiosk")["uid"] == ""
    assert session.get_principal("kiosk")["source"] != "voiceprint"

    # 真链路复核（上面两行只是守栏）：连续两句都没认出人时，`_handle_speech` 不许把
    # 兜底常量写回 `current_uid`——写回了，第二轮 `effective_uid()` 就会把它当声纹结果。
    from LLM.voice import session as voice_session_mod
    monkeypatch.setattr(worker_mod.audit, "log", lambda event, **kw: None)
    session.reset_for_test()
    w2 = worker_mod.VoiceWorker(stream_fn=lambda uid, text: iter(()))
    w2.session = voice_session_mod.Session()
    w2.fusion = type("Fusion", (), {
        "resolve": lambda self, seg: type(
            "Vote", (), {"candidate_uid": None, "confidence": 0.0})()})()
    w2._start_answer = lambda uid, text, settings: None     # 应答编排不在本用例范围
    w2._handle_speech("seg", "第一句", {"tts_enabled": True})
    w2._handle_speech("seg", "第二句", {"tts_enabled": True})
    assert w2.current_uid in (None, "")           # 兜底 uid 没被回灌成内存态
    assert session.get_principal("kiosk")["uid"] == ""
    assert session.get_principal("kiosk")["source"] != "voiceprint"


def test_role_subject_degrades_on_db_error(d, monkeypatch):
    """DB 异常不许把整句应答掐掉：落审计 + 退回上一主体。"""
    from LLM.voice import worker as worker_mod
    seen = []
    original = session.set_subject

    def boom(*a, **k):
        raise RuntimeError("db down")

    session.set_subject = boom
    monkeypatch.setattr(worker_mod.audit, "log",
                        lambda ev, **kw: seen.append((ev, kw)))
    try:
        w = worker_mod.VoiceWorker(stream_fn=lambda uid, text: iter(()))
        w._apply_role_subject("elder_101_1")      # 不抛异常即通过
    finally:
        session.set_subject = original
    assert any(ev == "voice_error" for ev, _ in seen)          # 出问题只记审计
    assert session.get_principal("kiosk")["uid"] == ""         # 退回上一主体（未改内存态）


def test_unidentified_speech_does_not_revert_subject(d):
    """**回归**：没认出人来时，不许把"旧主体"当声纹结果回写（否则位置自动切换会被回退）。"""
    from LLM.voice import worker as worker_mod
    session.set_subject("ward_102", slot="kiosk")          # 位置自动切换后的主体
    w = worker_mod.VoiceWorker(stream_fn=lambda uid, text: iter(()))
    w.current_uid = "ward_101"                             # 内存里的旧主体
    w._apply_role_subject(None)                            # 没认出来
    assert session.get_principal("kiosk")["uid"] == "ward_102"   # 主体不动
    assert session.get_principal("kiosk")["source"] != "voiceprint"


def test_unlock_returns_to_collective_layer(d):
    """**回归**：解锁 = 回集体层（不许把传进来的 uid 钉成主体，否则位置自动切换停摆）。"""
    session.set_subject("ward_101", slot="kiosk")
    res = voice_api.set_session_uid("elder_001", False)    # 老前端会传兜底 uid
    assert res["locked"] is False
    assert res["role"] == "ward" and res["uid"] == "ward_101"
