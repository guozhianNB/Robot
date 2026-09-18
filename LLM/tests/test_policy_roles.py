# -*- coding: utf-8 -*-
from LLM.agent import policy

# 集体层白名单的唯一真相（用例里三处复用，改策略只改这里）。
# notify_nurse = "把话传到护士台"：与 robot_stop 同属**呼救**一族，未识别的说话人落到这层时
# 也必须有求援手段（规格 docs/superpowers/specs/2026-09-18-llm-notify-nurse-mcp-design.md D5）。
WARD_TOOLS = ["robot_status", "robot_stop", "notify_nurse"]


def test_policy_keys_cover_three_roles():
    assert set(policy.POLICY_DEFAULTS) == {"admin", "ward", "elder"}


def test_ward_has_only_safety_tools_and_no_personal_scope():
    p = policy.POLICY_DEFAULTS["ward"]
    assert p["data_scope"] == "none"        # 不注入任何老人档案/私人记忆
    assert p["ward_context"] is True        # 但读得到本病房的集体上下文
    # 集体层只接"安全 + 只读"：急停（R3 永远允许）+ 状态只读播报（规格 §3.3 矩阵）
    # + 喊护士（notify_nurse，R3 的呼救一族）。
    # 未识别的说话人会回落到这一层，所以这里**不能**是空列表。
    assert p["allowed_tools"] == WARD_TOOLS


def test_role_policy_returns_copy_not_the_shared_table():
    """策略表是模块级共享常量：调用方原地 append 会污染全局白名单（往低权限角色里长工具）。"""
    a, b = policy.role_policy("ward"), policy.role_policy("ward")
    a["allowed_tools"].append("__注入__")
    assert b["allowed_tools"] == WARD_TOOLS
    assert policy.POLICY_DEFAULTS["ward"]["allowed_tools"] == WARD_TOOLS


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
