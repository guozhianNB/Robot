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
    db.set_admin_password("111111")           # 口令门默认开：不设口令则 login_admin 必定
                                              # 被拒（槽位 role 根本变不成 admin，分歧态构造不出来）
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


def test_divergent_slot_role_does_not_steal_private_chat(d, monkeypatch):
    """**回归（关键）**：位置判定必须用**有效角色**判断能不能改主体。

    `_expire_if_needed()`（admin TTL 到期）会把槽位原始 role 直接写成 `"ward"` 而**不重算**，
    而 `tick()` 的第一步恰好就是 `_expire_if_needed()`。拿原始 role 判定，就会把正在私聊的老人
    静默换成病房主体（D18 明令不抢私聊）。
    """
    session.set_subject("elder_101_1", slot="kiosk", source="voiceprint")
    session.login_admin("111111", slot="kiosk", ttl_s=1)      # kiosk 槽变 admin（主体仍是李爷爷）
    base = session._now_ts()
    monkeypatch.setattr(session.time, "monotonic", lambda: base + 5)
    locator.set_pose_for_test(20.0, 0.0, 0.0)
    for _ in range(5):
        session.tick()
    assert session.get_principal("kiosk")["uid"] == "elder_101_1"   # 主体不许被换掉
    assert session.current_ward() == "ward_102"                     # 背景病房照常更新


def test_manual_override_expiry_needs_full_recount(d, monkeypatch):
    """**回归**：手动覆盖期内不攒计数 —— 覆盖一到期，不许"一个采样就切"（防抖不许跨 epoch 续数）。"""
    locator.set_pose_for_test(20.0, 0.0, 0.0)
    session.tick()
    session.tick()                                            # 已攒 2 次（debounce=3）
    session.manual_set_ward("ward_101")                       # 手动覆盖 → 计数清零
    session.tick()
    assert session.current_ward() == "ward_101"               # 覆盖期内：不切也不攒（计数是空的）
    # 把时钟推过 `manual_until`（它记在快进后的时钟上）→ 手动覆盖到期。
    # 不用 `monkeypatch.undo()`：那会把 fixture 的 `_settings` 补丁一并撤掉。
    base = session.time.monotonic()
    monkeypatch.setattr(session, "_now_ts", lambda: base + 10_000)
    session.manual_set_ward("ward_101")                       # 记下 manual_until = base+10600
    monkeypatch.setattr(session, "_now_ts", lambda: base + 20_000)   # 时钟越过 manual_until
    session.tick()
    assert session.current_ward() == "ward_101"               # 第 1 次不许切
    session.tick()
    assert session.current_ward() == "ward_101"               # 第 2 次不许切
    session.tick()
    assert session.current_ward() == "ward_102"               # 从头数满 3 次才切


def test_current_map_is_cached_within_ttl(d, monkeypatch):
    """**性能红线回归**：`locator.current_map()`（含列图 + 逐图远端 stat + 全图像素统计）
    在 TTL 窗口内只准调一次 —— 它每秒跑在热路径上。"""
    monkeypatch.setattr(session, "_settings",
                        lambda: {**_SETTINGS, "ward_map_source": "auto"})
    calls = []
    monkeypatch.setattr(session.locator, "current_map",
                        lambda *a, **k: calls.append(1) or
                        {"ok": True, "source": "map_topic", "name": _MAP})
    locator.set_pose_for_test(20.0, 0.0, 0.0)
    # 光跑 30 次 tick 只要几毫秒，测不出缓存时长 → 让虚拟时钟每次走 2s。
    # 24s < TTL(30s)：整段只准调一次（TTL 若退回 10s，这段会调 3 次 → 红）。
    clock = [session._now_ts()]
    monkeypatch.setattr(session, "_now_ts", lambda: clock[0])
    for _ in range(12):
        session.tick()
        clock[0] += 2.0
    assert len(calls) == 1
