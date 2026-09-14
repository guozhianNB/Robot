# -*- coding: utf-8 -*-
"""病房位置自动切换测试：全部注入假位姿，**不需要 ROS / rosbridge / 板卡**。"""
import json
import os
import tempfile

import pytest

from LLM import db
from LLM import locator
from LLM import session

_MAP = "my_map"

_SETTINGS = {"ward_autoswitch_enabled": True, "ward_switch_debounce": 3,
             "manual_override_sec": 600, "ward_map_source": "setting",
             "current_map": _MAP, "admin_session_ttl_s": 300}


def _seed_zones(map_name: str, zones: list[dict]) -> None:
    """整图重建缓存（`replace_map_tags` 是整图级的：一次把该图所有区域一起传）。"""
    db.replace_map_tags(
        map_name,
        {"file_mtime": 0, "file_size": 0, "sha1": "", "resolution": 0.05,
         "origin": {"x": 0.0, "y": 0.0}, "warnings": []},
        [],
        zones,
    )


def _zone(uid: str, name: str, poly: list) -> dict:
    return {"uid": uid, "name": name, "kind": "ward", "shape": "polygon",
            "polygon_json": json.dumps(poly), "parent": "", "note": "",
            "created_at": "", "updated_at": ""}


@pytest.fixture()
def d(monkeypatch):
    tmp = tempfile.mkdtemp()
    old = db.DB_PATH
    db.DB_PATH = os.path.join(tmp, "t.db")
    db.init_db()
    session.reset_for_test()
    locator.clear_injection()
    _seed_zones(_MAP, [
        _zone("z1", "101", [[-3.0, -3.0], [3.0, -3.0], [3.0, 3.0], [-3.0, 3.0]]),
        _zone("z2", "102", [[17.0, -3.0], [23.0, -3.0], [23.0, 3.0], [17.0, 3.0]]),
    ])
    db.upsert_ward("ward_101", name="101 病房", ward_map=_MAP, ward_zone="z1")
    db.upsert_ward("ward_102", name="102 病房", ward_map=_MAP, ward_zone="z2")
    db.upsert_profile("elder_101_1", name="李爷爷")
    db.set_profile_ward("elder_101_1", "ward_101")
    monkeypatch.setattr(session, "_settings", lambda: dict(_SETTINGS))
    session.set_subject("ward_101", slot="kiosk")
    locator.set_pose_for_test(0.0, 0.0, 0.0)          # 起点在 101 里
    yield db
    db.DB_PATH = old
    locator.clear_injection()


def test_debounce_requires_repeated_ticks(d):
    locator.set_pose_for_test(20.0, 0.0, 0.0)          # 进入 102
    session.tick()
    assert session.get_principal("kiosk")["uid"] == "ward_101"    # 第 1 次不切
    session.tick()
    assert session.get_principal("kiosk")["uid"] == "ward_101"    # 第 2 次不切
    session.tick()
    assert session.get_principal("kiosk")["uid"] == "ward_102"    # 第 3 次才切
    assert session.current_ward() == "ward_102"


def test_debounce_resets_when_leaving_zone(d):
    locator.set_pose_for_test(20.0, 0.0, 0.0)
    session.tick(); session.tick()
    locator.set_pose_for_test(99.0, 99.0, 0.0)         # 走廊
    session.tick()
    locator.set_pose_for_test(20.0, 0.0, 0.0)          # 又回 102
    session.tick(); session.tick()
    assert session.get_principal("kiosk")["uid"] == "ward_101"    # 计数被清零，重新数满 3 次
    session.tick()
    assert session.get_principal("kiosk")["uid"] == "ward_102"


def test_leaving_zone_does_not_switch(d):
    locator.set_pose_for_test(99.0, 99.0, 0.0)
    for _ in range(5):
        session.tick()
    assert session.get_principal("kiosk")["uid"] == "ward_101"


def test_ward_without_zone_never_switches(d):
    """病房没关联区域（清空 ward_map/ward_zone）→ 位置判定不命中（fail-safe）。"""
    db.set_ward_zone("ward_102", "", "")          # 显式清空关联（空串在 upsert_ward 里=保持原值）
    locator.set_pose_for_test(20.0, 0.0, 0.0)
    for _ in range(5):
        session.tick()
    assert session.get_principal("kiosk")["uid"] == "ward_101"


def test_ward_zone_on_other_map_never_switches(d):
    """病房区域绑在**别的地图**上 → 不判命中（防止换图后静默切错病房）。"""
    db.upsert_ward("ward_102", name="102 病房", ward_map="other_map", ward_zone="z2")
    locator.set_pose_for_test(20.0, 0.0, 0.0)
    for _ in range(5):
        session.tick()
    assert session.get_principal("kiosk")["uid"] == "ward_101"


def test_does_not_steal_elder_private_chat(d):
    session.set_subject("elder_101_1", slot="kiosk", source="voiceprint")
    locator.set_pose_for_test(20.0, 0.0, 0.0)
    for _ in range(5):
        session.tick()
    p = session.get_principal("kiosk")
    assert p["role"] == "elder" and p["uid"] == "elder_101_1"     # 私聊不被打断
    assert session.current_ward() == "ward_102"                   # 但"当前病房"已更新
    # 退出私聊回集体层时，用的是新病房
    session.set_subject("ward_102", slot="kiosk")
    assert session.get_principal("kiosk")["uid"] == "ward_102"


def test_locked_session_holds_even_in_ward_layer(d):
    session.set_subject("ward_101", locked=True, slot="kiosk")
    locator.set_pose_for_test(20.0, 0.0, 0.0)
    for _ in range(5):
        session.tick()
    assert session.get_principal("kiosk")["uid"] == "ward_101"
    assert session.current_ward() == "ward_102"
    assert session.autoswitch_state()["reason"] == "holding_session"


def test_no_pose_disables_autoswitch(d):
    locator.set_pose_for_test(None)
    for _ in range(5):
        session.tick()
    assert session.get_principal("kiosk")["uid"] == "ward_101"
    assert session.autoswitch_state() == {"enabled": False, "reason": "no_pose"}


def test_disabled_by_setting(d, monkeypatch):
    monkeypatch.setattr(session, "_settings",
                        lambda: {**_SETTINGS, "ward_autoswitch_enabled": False})
    locator.set_pose_for_test(20.0, 0.0, 0.0)
    for _ in range(5):
        session.tick()
    assert session.current_ward() == "ward_101"
    assert session.autoswitch_state()["reason"] == "disabled"


def test_manual_override_blocks_location(d):
    session.manual_set_ward("ward_101")
    locator.set_pose_for_test(20.0, 0.0, 0.0)
    for _ in range(5):
        session.tick()
    assert session.current_ward() == "ward_101"
    assert session.autoswitch_state()["reason"] == "manual_override"


def test_auto_map_source_uses_fingerprint(d, monkeypatch):
    """默认 ward_map_source='auto'：地图名来自 locator.current_map()（/map 指纹反查）。"""
    monkeypatch.setattr(session, "_settings",
                        lambda: {**_SETTINGS, "ward_map_source": "auto"})
    monkeypatch.setattr(session.locator, "current_map",
                        lambda *a, **k: {"ok": True, "source": "map_topic", "name": _MAP})
    locator.set_pose_for_test(20.0, 0.0, 0.0)
    for _ in range(3):
        session.tick()
    assert session.current_ward() == "ward_102"


def test_unknown_map_disables_autoswitch(d, monkeypatch):
    """指纹认不出当前图 → 不切（宁可不切也不误切）。"""
    monkeypatch.setattr(session, "_settings",
                        lambda: {**_SETTINGS, "ward_map_source": "auto"})
    monkeypatch.setattr(session.locator, "current_map",
                        lambda *a, **k: {"ok": True, "source": "unknown", "name": None})
    locator.set_pose_for_test(20.0, 0.0, 0.0)
    for _ in range(5):
        session.tick()
    assert session.current_ward() == "ward_101"
    assert session.autoswitch_state()["reason"] == "map_unknown"
