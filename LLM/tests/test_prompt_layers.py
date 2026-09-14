# -*- coding: utf-8 -*-
"""三层提示词分层 + 集体上下文单向注入（R5）测试。"""
import os
import tempfile
from pathlib import Path

import pytest

from LLM import chat
from LLM import db


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


def test_admin_gets_no_ward_context(d):
    sys_p = chat.build_system("admin", {}, "", principal=_p("admin", "admin"))
    assert "病房消息2" not in sys_p


def test_missing_role_file_degrades(d, monkeypatch):
    monkeypatch.setattr(chat, "_ROLE_PROMPT_DIR_OVERRIDE", Path("/nonexistent-dir"))
    sys_p = chat.build_system("ward_101", {}, "", principal=_p("ward", "ward_101", "ward_101"))
    assert "小护" in sys_p                          # base 仍在，没抛异常


def test_unknown_role_falls_back_to_ward_fragment(d):
    sys_p = chat.build_system("ward_101", {}, "", principal=_p("??", "ward_101", "ward_101"))
    assert "病房里" in sys_p
