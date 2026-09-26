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
    """R3：急停/呼救不受**权限**影响（白名单/矩阵/服务器 roles 都挡不住）。

    两者都是 MCP 工具（car / notice），故开关是 `mcp_enabled`；**红锁不越过整机开关**——
    那是运维拔能力的开关，不是权限（规格 §4.1 顺序说明）。
    """
    from LLM.agent import permissions
    for role in ("ward", "elder"):
        for tool in ("robot_stop", "notify_nurse"):
            out = permissions.decide({"role": role}, tool, server="car", local=False,
                                     settings={"mcp_enabled": True},
                                     grants={}, ignore_switch=False)
            assert out["allow"] is True and out["source"] == "lock"
            out = permissions.decide({"role": role}, tool, server="car", local=False,
                                     settings={"mcp_enabled": False},
                                     grants={(role, tool): False})
            assert out["allow"] is False and out["source"] == "switch"


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
    # R3 优先于 matrix/factory/tool_roles：即便服务器只声明 admin、矩阵也写了拒绝，
    # 急停对老人层仍放行（整机开关关掉才会被 switch 拦下，见上一条用例）
    out = permissions.decide({"role": "elder"}, "robot_stop", server="srv", local=False,
                             settings={"mcp_enabled": True},
                             grants={("elder", "robot_stop"): False})
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


# ---------------------------------------------------------------- 接入 tools.py
def _mcp(schema_name: str, server: str) -> dict:
    return {schema_name: {"server": server, "schema": {
        "type": "function",
        "function": {"name": schema_name, "description": "", "parameters": {}}}}}


def test_effective_tools_matrix_grants_mcp_tool(d, monkeypatch):
    """矩阵放行后，老人层**看得见也调得到** tavily-search（出厂只给 admin，见 D5）。"""
    from LLM.agent import tools
    monkeypatch.setattr(tools.mcp_client, "tools", lambda: _mcp("tavily-search", "tavily"))
    monkeypatch.setattr(tools.mcp_client, "call_tool", lambda n, a: {"ok": True})
    d.set_settings({"mcp_enabled": True})          # run_tool 读真实 settings，总开关必须打开
    settings = {"mcp_enabled": True}
    p = {"uid": "elder_101_1", "role": "elder", "slot": "kiosk"}

    names = lambda: [t["function"]["name"] for t in tools.effective_tools(settings, p)]
    assert "tavily-search" not in names()                       # 出厂：只有 admin
    d.set_role_grant("elder", "tavily-search", True, by="admin")
    assert "tavily-search" in names()                           # 勾上即生效（无缓存）
    assert tools.run_tool("tavily-search", {}, p)["ok"] is True


def test_effective_tools_matrix_denies_admin_tool(d, monkeypatch):
    """反向：矩阵显式拒绝能盖掉 admin 的"全部"出厂默认。"""
    from LLM.agent import tools
    monkeypatch.setattr(tools.mcp_client, "tools", lambda: _mcp("robot_move", "car"))
    monkeypatch.setattr(tools, "_mcp_roles", lambda server: {"admin"})
    monkeypatch.setattr(tools.mcp_client, "call_tool", lambda n, a: {"ok": True})
    d.set_settings({"mcp_enabled": True})
    settings = {"mcp_enabled": True}
    admin = {"uid": "admin", "role": "admin", "slot": "admin"}

    names = lambda: [t["function"]["name"] for t in tools.effective_tools(settings, admin)]
    assert "robot_move" in names()
    d.set_role_grant("admin", "robot_move", False, by="admin")
    assert "robot_move" not in names()
    res = tools.run_tool("robot_move", {}, admin)
    assert res["ok"] is False and "不允许" in res["error"]


def test_run_tool_audits_matrix_source(d, monkeypatch):
    """越权审计要带上第一因（source=matrix），否则排查时分不清是哪道闸门拦的。"""
    from LLM.agent import tools
    d.set_settings({"mcp_enabled": True})
    monkeypatch.setattr(tools.mcp_client, "tools", lambda: _mcp("robot_move", "car"))
    seen = []
    monkeypatch.setattr("LLM.core.log.log", lambda ev, **kw: seen.append((ev, kw)))
    d.set_role_grant("admin", "robot_move", False, by="admin")
    tools.run_tool("robot_move", {}, {"role": "admin", "slot": "admin"})
    assert seen[-1][1]["reason"] == "matrix_deny"
    assert seen[-1][1]["source"] == "matrix"


# ---------------------------------------------------------------- 快照与写入口
def test_snapshot_matches_factory_when_table_empty(d):
    """对账：空表时 allowed 必须逐格等于 factory（防"改了默认忘了矩阵"）。"""
    from LLM.agent import permissions
    snap = permissions.matrix_snapshot(settings={"mcp_enabled": True})
    assert snap["roles"] == ["ward", "elder", "admin"]
    assert snap["grants"] == []
    assert snap["tools"], "注册表里至少有 create_plan，工具行不该为空"
    for t in snap["tools"]:
        for role in snap["roles"]:
            assert t["allowed"][role] == t["factory"][role]
            assert t["overridden"][role] is False


def test_snapshot_marks_locked_and_override(d, monkeypatch):
    from LLM.agent import permissions, tools
    monkeypatch.setattr(tools.mcp_client, "tools",
                        lambda: {**_mcp("tavily-search", "tavily"), **_mcp("robot_stop", "car")})
    d.set_role_grant("elder", "tavily-search", True, by="admin")
    snap = permissions.matrix_snapshot(settings={"mcp_enabled": True})
    by_name = {t["name"]: t for t in snap["tools"]}

    assert by_name["robot_stop"]["locked"]["ward"] is True       # R3 红锁
    assert by_name["robot_stop"]["locked"]["admin"] is False
    tavily = by_name["tavily-search"]
    assert tavily["factory"]["elder"] is False                   # 出厂只给 admin
    assert tavily["overridden"]["elder"] is True
    assert tavily["allowed"]["elder"] is True                    # 覆盖生效
    assert [g for g in snap["grants"] if g["tool"] == "tavily-search"] == [
        {"role": "elder", "tool": "tavily-search", "allowed": True}]


def test_snapshot_marks_orphan_grant(d):
    """库里留着已下线工具的行 → 标 orphan，让人知道它不生效。"""
    from LLM.agent import permissions
    d.set_role_grant("elder", "早就没了的工具", True, by="admin")
    snap = permissions.matrix_snapshot(settings={})
    orph = [t for t in snap["tools"] if t["orphan"]]
    assert [t["name"] for t in orph] == ["早就没了的工具"]
    assert orph[0]["allowed"] == {"ward": False, "elder": False, "admin": False}


def test_set_grants_rejects_locked_and_unknown_role(d):
    from LLM.agent import permissions
    out = permissions.set_grants([
        {"role": "ward", "tool": "robot_stop", "allowed": False},      # R3 锁
        {"role": "nope", "tool": "robot_move", "allowed": True},       # 非法角色
        {"role": "elder", "tool": "tavily-search", "allowed": True},   # 合法
    ], actor={"uid": "admin", "slot": "admin"})
    assert out["ok"] is False                              # 有格被拒 → 整体 ok False
    by_key = {(r["role"], r["tool"]): r for r in out["results"]}
    assert by_key[("ward", "robot_stop")]["action"] == "rejected"
    assert by_key[("ward", "robot_stop")]["reason"] == "locked"
    assert by_key[("nope", "robot_move")]["action"] == "rejected"
    assert by_key[("elder", "tavily-search")]["action"] == "set"
    assert d.get_role_grants() == {("elder", "tavily-search"): True}   # 合法那格照常落库


def test_set_grants_clears_row_equal_to_factory(d):
    """写回出厂值 = 删行（D2：只存差额，别留冗余）。"""
    from LLM.agent import permissions
    d.set_role_grant("admin", "robot_move", False, by="admin")
    out = permissions.set_grants([{"role": "admin", "tool": "robot_move", "allowed": True}],
                                 actor={"uid": "admin", "slot": "admin"})
    assert out["results"][0]["action"] == "cleared"
    assert d.get_role_grants() == {}
    # 本来就是出厂值再写一次 = noop（不留行、不报错）
    out = permissions.set_grants([{"role": "admin", "tool": "robot_move", "allowed": True}],
                                 actor={"uid": "admin", "slot": "admin"})
    assert out["results"][0]["action"] == "noop"
    assert d.get_role_grants() == {}


def test_set_grants_writes_audit(d, monkeypatch):
    from LLM.agent import permissions
    seen = []
    monkeypatch.setattr("LLM.core.log.log", lambda ev, **kw: seen.append((ev, kw)))
    d.set_role_grant("admin", "robot_move", False, by="admin")
    permissions.set_grants([{"role": "admin", "tool": "robot_move", "allowed": True}],
                           actor={"uid": "admin", "slot": "admin"})
    assert seen[-1][0] == "policy_change"
    assert seen[-1][1]["role"] == "admin" and seen[-1][1]["tool"] == "robot_move"
    assert seen[-1][1]["from"] is False and seen[-1][1]["to"] is True
    assert seen[-1][1]["by"] == "admin" and seen[-1][1]["via"] == "api"
