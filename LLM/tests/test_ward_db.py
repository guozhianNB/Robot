# -*- coding: utf-8 -*-
"""病房用户 / 管理员口令 / 区域缓存的数据层测试（临时库隔离，沿用 test_memory_v4.py 模式）。"""
import json
import os
import tempfile

import pytest

from LLM.store import db


@pytest.fixture()
def d():
    tmp = tempfile.mkdtemp()
    old = db.DB_PATH
    db.DB_PATH = os.path.join(tmp, "t.db")
    db.init_db()
    yield db
    db.DB_PATH = old


def _seed_zones(map_name: str, zones: list[dict]) -> None:
    """往**只读缓存表**里种区域（等价于 maptags.sync_map 的结果；测试直接铺数据）。

    注意 `replace_map_tags` 是「整图重建」——**一次要把该图所有区域一起传进来**，
    分两次调用会把前一次的行删掉。
    """
    db.replace_map_tags(
        map_name,
        {"file_mtime": 0, "file_size": 0, "sha1": "", "resolution": 0.05,
         "origin": {"x": 0.0, "y": 0.0}, "warnings": []},
        [],
        zones,
    )


def _zone(uid: str, name: str, poly: list, kind: str = "ward", shape: str = "polygon") -> dict:
    return {"uid": uid, "name": name, "kind": kind, "shape": shape,
            "polygon_json": json.dumps(poly), "parent": "", "note": "",
            "created_at": "", "updated_at": ""}


def test_existing_profile_defaults_to_elder(d):
    d.upsert_profile("elder_001", name="张奶奶")
    assert d.get_profile_kind("elder_001") == "elder"
    assert d.get_profile("elder_001")["ward_id"] == ""
    assert d.get_profile("elder_001")["ward_map"] == ""
    assert d.get_profile("elder_001")["ward_zone"] == ""


def test_unknown_uid_kind_is_empty(d):
    assert d.get_profile_kind("ghost_9") == ""       # 查不到 → ""，由 session 兜底成 ward（R2）


def test_upsert_ward_and_list(d):
    d.upsert_ward("ward_101", name="101 病房", ward_map="my_map", ward_zone="z1")
    wards = d.list_wards()
    assert [w["uid"] for w in wards] == ["ward_101"]
    assert d.get_profile_kind("ward_101") == "ward"
    assert wards[0]["ward_map"] == "my_map" and wards[0]["ward_zone"] == "z1"
    assert d.list_profiles(kind="elder") == []       # kind 过滤生效


def test_upsert_ward_does_not_wipe_linkage_on_rename(d):
    """重复 upsert_ward（只改名）不许把已有关联清掉——调用方传空串=保持原值。"""
    d.upsert_ward("ward_101", name="101", ward_map="my_map", ward_zone="z1")
    d.upsert_ward("ward_101", name="101 病房")
    p = d.get_profile("ward_101")
    assert p["name"] == "101 病房"
    assert p["ward_map"] == "my_map" and p["ward_zone"] == "z1"


def test_upsert_ward_keeps_name_when_omitted(d):
    """**回归**：只改关联（不传名字）时，已有病房名不许被抹成空串。"""
    d.upsert_ward("ward_101", name="101 病房", ward_map="my_map", ward_zone="z1")
    d.upsert_ward("ward_101", ward_map="my_map2", ward_zone="z9")
    p = d.get_profile("ward_101")
    assert p["name"] == "101 病房"
    assert (p["ward_map"], p["ward_zone"]) == ("my_map2", "z9")


def test_upsert_profile_never_touches_ward_linkage(d):
    """**核心回归**：档案编辑（upsert_profile）绝不许清掉病房关联（f6f6e54 的教训）。"""
    d.upsert_ward("ward_101", name="101 病房", ward_map="my_map", ward_zone="z1")
    d.upsert_profile("elder_101_1", name="李爷爷")
    d.set_profile_ward("elder_101_1", "ward_101")
    d.upsert_profile("elder_101_1", name="李爷爷（改名）", age=79)
    p = d.get_profile("elder_101_1")
    assert p["name"] == "李爷爷（改名）" and p["ward_id"] == "ward_101"
    w = d.get_profile("ward_101")
    assert w["ward_map"] == "my_map" and w["ward_zone"] == "z1"
    assert w["kind"] == "ward"


def test_set_ward_zone_roundtrip(d):
    d.upsert_ward("ward_101", name="101 病房")
    assert d.get_profile("ward_101")["ward_zone"] == ""
    d.set_ward_zone("ward_101", "my_map", "z3")
    p = d.get_profile("ward_101")
    assert (p["ward_map"], p["ward_zone"]) == ("my_map", "z3")


def test_set_profile_ward(d):
    d.upsert_profile("elder_101_1", name="李爷爷")
    d.set_profile_ward("elder_101_1", "ward_101")
    assert d.get_profile("elder_101_1")["ward_id"] == "ward_101"


def test_upsert_ward_promotes_existing_profile(d):
    """**回归（历史隐患①）**：把一个已存在的 uid 提升成病房，`kind` 必须真的变成 ward。

    旧版实现用 `kind=COALESCE(profiles.kind, excluded.kind)` 兜底，而旧行早已被回填成
    `'elder'`、永不为空 → 兜底分支**永不触发** → 提升静默失败却照写区域关联（半状态）。
    本版用 `SET kind='ward'` 直写，这条测试就是钉住它。
    """
    d.upsert_profile("ward_101", name="其实是先建错的老人档案")   # 先以 elder 存在
    assert d.get_profile_kind("ward_101") == "elder"
    d.upsert_ward("ward_101", name="101 病房", ward_map="my_map", ward_zone="z1")
    assert d.get_profile_kind("ward_101") == "ward"
    assert d.get_profile("ward_101")["ward_map"] == "my_map"


def test_list_zones_kind_filter_and_get_zone_with_map(d):
    _seed_zones("my_map", [
        _zone("z1", "101", [[0, 0], [2, 0], [2, 2], [0, 2]], kind="ward"),
        _zone("z2", "走廊", [[0, 0], [9, 0], [9, 9], [0, 9]], kind="other"),
    ])
    _seed_zones("my_map2", [_zone("z1", "102", [[10, 10], [12, 10], [12, 12], [10, 12]])])

    assert [z["uid"] for z in d.list_zones(map_name="my_map", kind="ward")] == ["z1"]
    assert len(d.list_zones(map_name="my_map")) == 2
    assert d.list_zones(map_name="不存在的地图") == []          # 无缓存 → []（降级）

    # get_zone 必须能按地图限定：三张图各有一个 z1，只按 uid 查会串（既有隐患已修）
    assert d.get_zone("z1", "my_map")["name"] == "101"
    assert d.get_zone("z1", "my_map2")["name"] == "102"
    assert d.get_zone("z1", "nope") is None
    assert d.get_zone("z9", "my_map") is None
    assert d.get_zone("z1")["name"] in ("101", "102")           # 不传地图名：兼容旧行为


def test_zone_rows_expose_parsed_polygon(d):
    _seed_zones("my_map", [_zone("z1", "101", [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0]])])
    z = d.list_zones(map_name="my_map")[0]
    assert z["polygon"] == [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0]]
    assert "polygon_json" not in z


def test_float_setting_roundtrip(d):
    """float 设置项读回来必须是 float 不是 "0.45"。

    这里用**一期末尾就已存在**的 `map_boundary_margin_m`（默认 0.3，float）来验类型转换分支——
    本任务不依赖任务 3 新增的键（`ward_zone_default_r` 的 float 往返由任务 3 自己的测试覆盖）。
    """
    d.set_settings({"map_boundary_margin_m": 0.45})
    assert d.get_settings()["map_boundary_margin_m"] == 0.45


def test_admin_password_hash_roundtrip(d):
    assert d.get_admin_auth() == {"required": True, "hash": "", "salt": ""}
    d.set_admin_password("246810")
    a = d.get_admin_auth()
    assert a["hash"] and a["salt"] and "246810" not in a["hash"]  # 绝不落明文
    assert d.verify_admin_password("246810") is True
    assert d.verify_admin_password("000000") is False
    d.set_admin_auth_required(False)
    assert d.get_admin_auth()["required"] is False
    d.set_admin_auth_required(True)
    assert d.get_admin_auth()["required"] is True


def test_get_settings_never_leaks_password_keys(d):
    """口令哈希/盐存在 settings 表里，但绝不许随 get_settings() 外泄（GET /api/settings 会透出）。"""
    d.set_admin_password("246810")
    leaked = [k for k in d.get_settings() if k.startswith("admin_password_")]
    assert leaked == []
    # 口令通路本身仍然可用
    assert d.get_admin_auth()["hash"] and d.verify_admin_password("246810") is True


def test_verify_admin_password_without_hash_is_false(d):
    assert d.verify_admin_password("任意") is False


def test_verify_password_with_corrupt_salt_is_false(d):
    """盐被写坏（非十六进制）也必须只回"拒绝"，不许抛异常把登录端点打成 500。"""
    d._set_setting_raw("admin_password_hash", "deadbeef")
    d._set_setting_raw("admin_password_salt", "不是十六进制")
    assert d.verify_admin_password("任意") is False
