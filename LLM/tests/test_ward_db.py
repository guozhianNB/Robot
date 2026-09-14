# -*- coding: utf-8 -*-
"""病房用户与管理员口令数据层测试（临时库隔离，沿用 test_memory_v4.py 模式）。"""
import os
import tempfile

import pytest

from LLM import db


@pytest.fixture()
def d():
    from LLM import db as _db
    tmp = tempfile.mkdtemp()
    old = _db.DB_PATH
    _db.DB_PATH = os.path.join(tmp, "t.db")
    _db.init_db()
    yield _db
    _db.DB_PATH = old


def test_existing_profile_defaults_to_elder(d):
    d.upsert_profile("elder_001", name="张奶奶")
    assert d.get_profile_kind("elder_001") == "elder"
    assert d.get_profile("elder_001")["ward_id"] == ""


def test_upsert_ward_with_zone_id(d):
    zid = d.add_zone(map_name="101", name="101", kind="ward", shape="polygon",
                     polygon_json=[[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0]])
    d.upsert_ward("ward_101", name="101 病房", zone_id=zid)
    wards = d.list_profiles(kind="ward")
    assert [w["uid"] for w in wards] == ["ward_101"]
    assert d.get_profile_kind("ward_101") == "ward"
    assert d.get_ward_zone("ward_101")["id"] == zid      # 经 profiles.zone_id 反查 zones
    assert d.get_zone(zid)["name"] == "101"              # 按主键查 zones
    assert d.list_wards_with_zone() == [
        {"uid": "ward_101", "name": "101 病房", "zone_id": zid, "zone": d.get_zone(zid)}]


def test_ward_without_zone_id_returns_none(d):
    d.upsert_ward("ward_102", name="102 病房")           # zone_id 默认 0 = 未关联
    assert d.get_profile("ward_102")["zone_id"] == 0
    assert d.get_ward_zone("ward_102") is None
    assert d.list_wards_with_zone() == []


def test_edit_profile_does_not_clear_ward_zone(d):
    """编辑病房档案（不带 zone_id）**不得**清掉已有的病房↔区域关联。

    zone_id 只由 upsert_ward 写；upsert_profile 的 ON CONFLICT 更新子句不含它。
    否则管理台改个病房名就会让该病房的位置自动切换静默失效。
    """
    zid = d.add_zone(map_name="101", name="101", kind="ward", shape="polygon",
                     polygon_json=[[-1.0, -1.0], [1.0, -1.0], [1.0, 1.0], [-1.0, 1.0]])
    d.upsert_ward("ward_101", name="101 病房", zone_id=zid)
    d.upsert_profile("ward_101", name="101 病房（改名）")   # 不带 zone_id
    p = d.get_profile("ward_101")
    assert p["zone_id"] == zid, "档案编辑把病房区域关联清掉了"
    assert p["name"] == "101 病房（改名）", "改名应生效"
    assert d.get_ward_zone("ward_102") is None
    # 简报原文此行为 `assert d.list_wards_with_zone() == []`，但它只在"关联被清成 0"的
    # 破损状态下成立，与上一行 `p["zone_id"] == zid` 互斥（已批准的改法正是不再清关联）；
    # 故按本缺陷的目标语义修正为：关联保留 → ward_101 仍在"已关联病房"列表里且指向同一 zone。
    assert d.list_wards_with_zone() == [
        {"uid": "ward_101", "name": "101 病房（改名）", "zone_id": zid, "zone": d.get_zone(zid)}]


def test_zone_contains_point_hits_polygon(d):
    d.add_zone(map_name="101", name="101", kind="ward", shape="polygon",
               polygon_json=[[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0]])
    assert [z["name"] for z in d.zone_contains_point("101", 1.0, 1.0, kind="ward")] == ["101"]
    assert d.zone_contains_point("101", 9.0, 9.0, kind="ward") == []


def test_zone_contains_point_uses_bbox_for_rect(d):
    d.add_zone(map_name="101", name="101", kind="ward", shape="rect",
               polygon_json=[[0.0, 0.0], [4.0, 0.0], [4.0, 2.0], [0.0, 2.0]])
    assert [z["name"] for z in d.zone_contains_point("101", 3.9, 1.9, kind="ward")] == ["101"]
    assert d.zone_contains_point("101", 5.0, 1.0, kind="ward") == []


def test_zone_lookup_degrades_safely(d):
    assert d.list_zones(map_name="不存在的地图") == []                  # 无区域 → []
    assert d.zone_contains_point("101", 0.0, 0.0, kind="ward") == []    # 还没建任何区域 → []
    d.add_zone(map_name="101", name="走廊", kind="other",
               polygon_json=[[0.0, 0.0], [9.0, 0.0], [9.0, 9.0], [0.0, 9.0]])
    assert d.zone_contains_point("101", 1.0, 1.0, kind="ward") == []    # kind 过滤：other 不算病房
    assert d.get_zone(99999) is None                                    # 主键不存在 → None


def test_zone_contains_point_tolerates_bad_geometry(d):
    """几何数据损坏（非数值坐标）→ 视为不在区域内，绝不抛异常、绝不阻塞对话（D17 fail-safe）。"""
    d.add_zone(map_name="101", name="坏区域", kind="ward",
               polygon_json=[["x", "y"], [1.0, 1.0], [2.0, 2.0]])
    assert d.zone_contains_point("101", 1.0, 1.0, kind="ward") == []
    assert d.zone_contains_point("101", "坏坐标", 1.0, kind="ward") == []


def test_set_elder_ward(d):
    d.upsert_profile("elder_101_1", name="李爷爷")
    d.set_profile_ward("elder_101_1", "ward_101")
    assert d.get_profile("elder_101_1")["ward_id"] == "ward_101"


def test_float_setting_roundtrip(d):
    d.set_settings({"ward_zone_default_r": 3.5})
    assert d.get_settings()["ward_zone_default_r"] == 3.5


def test_admin_password_hash_roundtrip(d):
    assert d.get_admin_auth() == {"required": True, "hash": "", "salt": ""}
    d.set_admin_password("246810")
    assert d.verify_admin_password("246810") is True
    assert d.verify_admin_password("000000") is False
    d.set_admin_auth_required(False)
    assert d.get_admin_auth()["required"] is False
