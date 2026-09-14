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
