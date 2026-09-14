# -*- coding: utf-8 -*-
"""会话主体与角色推导测试（临时库隔离，沿用 test_memory_v4.py 模式）。"""
import os
import tempfile

import pytest

from LLM import db
from LLM import session


@pytest.fixture()
def d():
    tmp = tempfile.mkdtemp()
    old = db.DB_PATH
    db.DB_PATH = os.path.join(tmp, "t.db")
    db.init_db()
    session.reset_for_test()
    db.upsert_profile("elder_101_1", name="李爷爷")
    db.upsert_ward("ward_101", name="101 病房")
    db.set_profile_ward("elder_101_1", "ward_101")
    db.set_admin_password("111111")
    yield db
    db.DB_PATH = old


def test_derive_role_by_kind(d):
    assert session.derive_role("elder_101_1") == "elder"
    assert session.derive_role("ward_101") == "ward"
    assert session.derive_role("admin") == "admin"
    assert session.derive_role("ghost_9") == "ward"      # R2 fail-closed
    assert session.derive_role("") == "ward"
    assert session.derive_role(None) == "ward"


def test_derive_role_not_fooled_by_uid_prefix(d):
    d.upsert_profile("ward_fake", name="其实是老人")      # kind 默认 elder
    assert session.derive_role("ward_fake") == "elder"    # 权威判定查 profiles.kind


def test_two_slots_are_isolated(d):
    session.set_subject("ward_101", locked=False, slot="kiosk")
    session.login_admin("111111", slot="admin")
    assert session.get_principal("kiosk")["role"] == "ward"
    assert session.get_principal("admin")["role"] == "admin"
    assert session.get_principal("kiosk")["uid"] == "ward_101"     # 车前屏不被提权
    assert session.get_principal("admin")["uid"] == "admin"


def test_admin_never_downgraded_by_voiceprint(d):
    session.login_admin("111111", slot="kiosk")
    session.set_subject("elder_101_1", locked=False, slot="kiosk", source="voiceprint")
    p = session.get_principal("kiosk")
    assert p["role"] == "admin" and p["uid"] == "admin"            # D8 提权只升不降


def test_wrong_password_does_not_elevate(d):
    assert session.login_admin("000000", slot="kiosk")["ok"] is False
    assert session.get_principal("kiosk")["role"] == "ward"


def test_recognizing_elder_follows_his_ward(d):
    d.upsert_ward("ward_102", name="102 病房")
    d.upsert_profile("elder_102_1", name="王奶奶")
    d.set_profile_ward("elder_102_1", "ward_102")
    session.set_subject("ward_101", slot="kiosk")
    session.set_subject("elder_102_1", slot="kiosk", source="voiceprint")
    p = session.get_principal("kiosk")
    assert p["role"] == "elder" and p["ward_uid"] == "ward_102"     # D18：当前病房跟随老人


def test_elder_without_ward_keeps_current(d):
    d.upsert_profile("elder_999", name="未分配病房的老人")
    session.set_subject("ward_101", slot="kiosk")
    session.set_subject("elder_999", slot="kiosk", source="voiceprint")
    assert session.get_principal("kiosk")["ward_uid"] == "ward_101"


def test_ttl_expiry_falls_back_to_ward(d, monkeypatch):
    session.set_subject("ward_101", locked=False, slot="kiosk")
    session.login_admin("111111", slot="kiosk", ttl_s=1)
    base = session._now_ts()
    monkeypatch.setattr(session.time, "monotonic", lambda: base + 5)
    p = session.get_principal("kiosk")
    assert p["role"] == "ward" and p["uid"] == "ward_101"          # 回落集体层
    assert p["source"] == "expired"


def test_ttl_remain_counts_down(d):
    session.login_admin("111111", slot="kiosk", ttl_s=60)
    assert 0 < session.ttl_remain("kiosk") <= 60
    session.logout("kiosk")
    assert session.ttl_remain("kiosk") is None                      # 非管理员 → None


def test_logout_returns_to_ward_layer(d):
    session.set_subject("ward_101", slot="kiosk")
    session.login_admin("111111", slot="kiosk")
    session.logout("kiosk")
    p = session.get_principal("kiosk")
    assert p["role"] == "ward" and p["uid"] == "ward_101" and p["locked"] is False
