# -*- coding: utf-8 -*-
from LLM import policy


def test_policy_keys_cover_three_roles():
    assert set(policy.POLICY_DEFAULTS) == {"admin", "ward", "elder"}


def test_ward_reads_own_ward_but_no_personal_scope():
    p = policy.POLICY_DEFAULTS["ward"]
    assert p["data_scope"] == "none"        # 不注入任何老人档案/私人记忆
    assert p["ward_context"] is True        # 但读得到本病房的集体上下文
    assert p["allowed_tools"] == []         # 集体层无任何工具


def test_elder_reads_self_plus_ward_context():
    p = policy.POLICY_DEFAULTS["elder"]
    assert p["data_scope"] == "self"
    assert p["ward_context"] is True
    assert "robot_stop" in p["allowed_tools"]     # R3：安全动作永远在列


def test_admin_reads_all_without_ward_context():
    p = policy.POLICY_DEFAULTS["admin"]
    assert p["data_scope"] == "all"
    assert p["allowed_tools"] is None             # None = 全部
    assert p["ward_context"] is False


def test_prompt_files_point_into_llm_prompt_dir():
    for role in ("ward", "elder", "admin"):
        f = policy.POLICY_DEFAULTS[role]["prompt_file"]
        assert f.name == f"{role}.md" and f.parent.name == "prompt"


def test_unknown_role_falls_back_to_ward():
    assert policy.role_policy("nope") == policy.POLICY_DEFAULTS["ward"]
    assert policy.role_policy(None) == policy.POLICY_DEFAULTS["ward"]
    assert policy.role_policy("") == policy.POLICY_DEFAULTS["ward"]
