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


def test_manual_switch_to_admin_uid_is_denied(d):
    """**R1 红线回归**：提权只能走 login_admin（口令）。

    主体切换若能把角色变成 admin，`POST /api/session/user {"uid":"admin"}` 就是免口令后门
    （该端点只拒绝 role 字段、CORS 全开）。
    """
    session.set_subject("admin", slot="kiosk", source="manual")
    p = session.get_principal("kiosk")
    assert p["role"] == "ward" and p["uid"] != "admin"


def test_expired_admin_slot_does_not_swallow_voiceprint(d, monkeypatch):
    """过期的 admin 槽不许吞掉一次声纹认人（D8 守卫只对"有效期内"的管理员会话生效）。"""
    session.set_subject("ward_101", slot="kiosk")
    session.login_admin("111111", slot="kiosk", ttl_s=1)
    base = session._now_ts()
    monkeypatch.setattr(session.time, "monotonic", lambda: base + 5)
    session.set_subject("elder_101_1", slot="kiosk", source="voiceprint")
    p = session.get_principal("kiosk")
    assert p["role"] == "elder" and p["uid"] == "elder_101_1"


def test_unknown_slot_is_rejected(d):
    """槽位名非法必须报错，绝不静默回落到 kiosk（否则会把车前屏提权、审计也失真）。"""
    with pytest.raises(ValueError):
        session.get_principal("TABLET")
    with pytest.raises(ValueError):
        session.login_admin("111111", slot="TABLET")


def test_admin_ignored_voiceprint_is_audited(d, monkeypatch):
    calls = []
    monkeypatch.setattr(session.audit, "log", lambda ev, **kw: calls.append((ev, kw)))
    session.login_admin("111111", slot="kiosk")
    session.set_subject("elder_101_1", slot="kiosk", source="voiceprint")
    assert calls and calls[-1][0] == "voice_spk"
    assert calls[-1][1]["action"] == "ignored_in_admin"
    assert calls[-1][1]["slot"] == "kiosk"


def test_auth_disabled_login_needs_no_password(d):
    """口令门关着时：无需口令直接进 admin，且**不再自动降权**（D13）。"""
    d.set_admin_auth_required(False)
    r = session.login_admin(password=None, slot="kiosk")
    assert r["ok"] is True and r["source"] == "auth_disabled" and r["until"] is None
    assert session.get_principal("kiosk")["role"] == "admin"


def test_unknown_uid_does_not_hijack_current_ward(d):
    """fail-closed 判成 ward，但不许把"当前病房"顶成那个不存在的 uid。"""
    session.set_subject("ward_101", slot="kiosk")
    session.set_subject("ghost_9", slot="kiosk", source="voiceprint")
    assert session.get_principal("kiosk")["ward_uid"] == "ward_101"


def test_change_password_requires_old(d):
    d.set_admin_password("111111")
    assert session.change_admin_password("wrong", "222222")["ok"] is False
    assert session.change_admin_password("111111", "222222")["ok"] is True
    assert d.verify_admin_password("222222") is True
    assert d.verify_admin_password("111111") is False


def test_change_password_rejects_too_short(d):
    assert session.change_admin_password("111111", "12")["ok"] is False
    assert d.verify_admin_password("111111") is True          # 失败不改动原口令


def test_toggle_auth_required_and_login_without_password(d):
    assert session.set_admin_auth(False)["required"] is False
    r = session.login_admin(password=None, slot="kiosk")
    assert r["ok"] is True and r["source"] == "auth_disabled"
    assert r["ttl_remain"] is None                            # 无口令保护时不再自动降权
    assert session.set_admin_auth(True)["required"] is True


def test_re_enabling_lock_kills_existing_admin_sessions(d):
    """重新开启口令门 = 现有 admin 会话立即作废（强制下次要口令）。"""
    session.login_admin("111111", slot="admin", ttl_s=600)
    session.set_admin_auth(False)
    session.login_admin(password=None, slot="admin")          # 无保护状态下进的管理员
    session.set_admin_auth(True)
    assert session.get_principal("admin")["role"] == "ward"    # 已被踢回


def test_first_boot_generates_random_password(d):
    d._set_setting_raw("admin_password_hash", "")
    d._set_setting_raw("admin_password_salt", "")
    pw = session.ensure_admin_password()
    assert isinstance(pw, str) and len(pw) == 6 and pw.isdigit()
    assert d.verify_admin_password(pw) is True
    assert session.ensure_admin_password() is None             # 已有口令 → 不再生成
