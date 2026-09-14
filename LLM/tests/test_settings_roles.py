# -*- coding: utf-8 -*-
"""分层用户体系的配置项默认值（改参数先来这里看）。"""
from LLM.conf import DEFAULT_SETTINGS


def test_role_settings_have_defaults():
    assert DEFAULT_SETTINGS["admin_auth_required"] is True
    assert DEFAULT_SETTINGS["admin_session_ttl_s"] == 300
    assert DEFAULT_SETTINGS["ward_context_window"] == 10
    assert DEFAULT_SETTINGS["ward_autoswitch_enabled"] is True
    assert DEFAULT_SETTINGS["ward_switch_debounce"] == 3
    assert DEFAULT_SETTINGS["ward_zone_default_r"] == 3.0
    assert DEFAULT_SETTINGS["manual_override_sec"] == 600
    assert DEFAULT_SETTINGS["ward_map_source"] == "auto"


def test_new_float_setting_roundtrips_as_float():
    """新增的 float 键要能真的存进去并读回 float（任务 2 已给 get_settings 补 float 分支）。"""
    from LLM import db
    import os, tempfile
    tmp = tempfile.mkdtemp()
    old = db.DB_PATH
    db.DB_PATH = os.path.join(tmp, "t.db")
    db.init_db()
    try:
        db.set_settings({"ward_zone_default_r": 3.5})
        assert db.get_settings()["ward_zone_default_r"] == 3.5
    finally:
        db.DB_PATH = old


def test_does_not_shadow_existing_map_settings():
    """`current_map` 一期已有（真值是"下次启导航用哪张图"），本设计不重新定义它。"""
    assert DEFAULT_SETTINGS["current_map"] == "my_map"
    assert "rosbridge_url" not in DEFAULT_SETTINGS      # rosbridge 地址不在设置表里
    assert isinstance(DEFAULT_SETTINGS["map_topic_fingerprint_enabled"], bool)
