# -*- coding: utf-8 -*-
"""权限矩阵测试（临时库隔离，直接改 db.DB_PATH，不 reload）。

规格：docs/superpowers/specs/2026-09-26-permission-matrix-design.md
"""
import os
import tempfile

import pytest


@pytest.fixture()
def d():
    from LLM.store import db
    tmp = tempfile.mkdtemp()
    old = db.DB_PATH
    db.DB_PATH = os.path.join(tmp, "t.db")
    db.init_db()
    yield db
    db.DB_PATH = old


def test_grants_start_empty(d):
    assert d.get_role_grants() == {}
    assert d.list_role_grants() == []


def test_set_grant_is_idempotent_and_readable(d):
    d.set_role_grant("elder", "tavily-search", True, by="admin")
    d.set_role_grant("elder", "tavily-search", True, by="admin")
    assert d.get_role_grants() == {("elder", "tavily-search"): True}
    assert len(d.list_role_grants()) == 1


def test_set_grant_overwrites_and_delete_removes(d):
    d.set_role_grant("admin", "robot_move", False, by="admin")
    assert d.get_role_grants()[("admin", "robot_move")] is False
    d.set_role_grant("admin", "robot_move", True, by="admin")
    assert d.get_role_grants()[("admin", "robot_move")] is True
    assert d.delete_role_grant("admin", "robot_move") == 1
    assert d.get_role_grants() == {}
    assert d.delete_role_grant("admin", "robot_move") == 0      # 已无行 → 0


# ---------------------------------------------------------------- 判定入口
def test_decide_lock_allows_safety_tools(d):
    """R3：急停/呼救不受任何权限影响，即便整机开关关着也放行。"""
    from LLM.agent import permissions
    for role in ("ward", "elder"):
        for tool in ("robot_stop", "notify_nurse"):
            out = permissions.decide({"role": role}, tool,
                                     settings={"robot_stop_enabled": False,
                                               "notify_nurse_enabled": False}, grants={})
            assert out["allow"] is True and out["source"] == "lock"


def test_decide_switch_denies_before_matrix(d):
    """整机开关关着时，矩阵勾了也不生效（规格 §4.1 顺序：switch 先于 matrix）。"""
    from LLM.agent import permissions
    out = permissions.decide({"role": "elder"}, "robot_move",
                             settings={"robot_move_enabled": False},
                             grants={("elder", "robot_move"): True})
    assert out["allow"] is False and out["source"] == "switch"
    assert out["reason"] == "tool_disabled"


def test_decide_mcp_switch_is_global(d):
    from LLM.agent import permissions
    out = permissions.decide({"role": "admin"}, "mcp_probe", server="srv", local=False,
                             settings={"mcp_enabled": False}, grants={})
    assert out["allow"] is False and out["reason"] == "mcp_disabled"


def test_decide_matrix_overrides_factory_both_ways(d):
    """矩阵既能放行出厂拒绝的，也能拒绝出厂允许的。"""
    from LLM.agent import permissions
    allowed = permissions.decide({"role": "elder"}, "tavily-search", server="tavily",
                                 local=False, settings={"mcp_enabled": True},
                                 grants={("elder", "tavily-search"): True})
    assert allowed["allow"] is True and allowed["source"] == "matrix"
    denied = permissions.decide({"role": "admin"}, "robot_move",
                               settings={}, grants={("admin", "robot_move"): False})
    assert denied["allow"] is False and denied["reason"] == "matrix_deny"


def test_decide_unknown_role_and_tool_fail_closed(d):
    from LLM.agent import permissions
    assert permissions.normalize_role("??") == "ward"
    assert permissions.normalize_role(None) == "ward"
    out = permissions.decide({"role": "??"}, "robot_move", settings={}, grants={})
    assert out["allow"] is False and out["reason"] == "out_of_role_whitelist"
    out = permissions.decide({"role": "elder"}, "根本不存在", settings={}, grants={})
    assert out["allow"] is False and out["reason"] == "out_of_role_whitelist"


def test_decide_reason_priority_matches_legacy(d, monkeypatch):
    """白名单外 + 服务器 roles 排除 → reason 必须是 out_of_role_whitelist（与旧实现一致）。"""
    from LLM.agent import permissions, tools
    monkeypatch.setattr(tools, "_mcp_roles", lambda server: {"admin"})
    out = permissions.decide({"role": "elder"}, "mcp_probe", server="srv", local=False,
                             settings={"mcp_enabled": True}, grants={})
    assert out["reason"] == "out_of_role_whitelist"
    # see_what 在 elder 白名单内 → 拒因才轮到服务器 roles。
    # **不能用 robot_stop 当探针**：它是 R3 红锁，任何闸门都拦不住（见下一条断言）。
    out = permissions.decide({"role": "elder"}, "see_what", server="srv", local=False,
                             settings={"mcp_enabled": True}, grants={})
    assert out["reason"] == "tool_roles_mismatch"
    # R3 优先于一切：即便服务器只声明 admin、开关也关着，急停对老人层仍放行
    out = permissions.decide({"role": "elder"}, "robot_stop", server="srv", local=False,
                             settings={"mcp_enabled": False}, grants={})
    assert out["allow"] is True and out["source"] == "lock"


def test_decide_falls_back_to_factory_when_db_raises(d, monkeypatch):
    """规格 D7：读库异常必须回落出厂政策 + 审计，绝不放行未知工具。"""
    from LLM.agent import permissions
    seen = []
    monkeypatch.setattr("LLM.core.log.log", lambda ev, **kw: seen.append((ev, kw)))
    monkeypatch.setattr(permissions.db, "get_role_grants",
                        lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    permissions._matrix_error_at = 0.0
    grants = permissions.load_grants()
    assert grants == {}
    assert seen and seen[-1][0] == "policy_deny"
    assert seen[-1][1]["action"] == "matrix_unavailable"
