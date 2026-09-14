# -*- coding: utf-8 -*-
"""工具角色白名单（闸门 2）测试。

⚠️ `LLM/tool/` 目前**没有任何 `@tool` 注册**（只有两个 `_` 开头的 demo），`_TOOL_REGISTRY` 是空的
—— 直接断言"哪些工具可见"会**空转变绿**。所以下面注册一个只在测试期存在的探针工具，测完摘干净。

【与简报的两处偏差（已记录在 task-9-report.md）】
1. 简报的 helper 写作 `_names(settings, principal)`，但 `test_unknown_role_falls_back_to_ward`
   又调 `_names({})` → 必然 `TypeError`。这里给 `principal` 补默认 `None`（缺省 → 集体层 R2），
   正是该断言注释所要求的语义；实测 `session.get_principal("kiosk")["role"] == "ward"`，故与真实缺省一致。
2. `test_tool_roles_field_filters_even_for_admin` 在简报里**与简报的实现互相矛盾**：它断言
   `"__probe_elder_only__" in _names({}, _p("elder"))`，而 elder 的 `allowed_tools ==
   ["robot_status", "robot_stop"]` 不含该探针 → 被角色白名单先拒掉，实测恒为 `[]`。
   与硬性约束自洽的读法是「`roles` 只能**收窄**、不能**扩展**角色白名单」（交集语义），
   那样该探针永不可见。故此处按同一意图改判：**第二道闸门只对未被角色白名单裁剪的 admin 可观测**，
   并对 elder 断言其被白名单挡住 —— 判别力落在 `test_mcp_tools_default_to_admin_only`（MCP admin-only）。
"""
import pytest

from LLM import tools


def _p(role):
    return {"role": role, "uid": "u", "slot": "kiosk", "ward_uid": "", "locked": False}


def _names(settings, principal=None):
    return [t["function"]["name"] for t in tools.effective_tools(settings, principal)]


@pytest.fixture()
def probe():
    """注册一个测试期探针工具（默认 enabled=True、不限角色），测完摘干净。"""
    tools.tool("__probe__", "探针（仅测试）", {})(lambda **kw: {"ok": True, "message": "probe"})
    yield "__probe__"
    tools._TOOL_REGISTRY.pop("__probe__", None)
    tools.TOOL_DEFAULTS.pop("__probe___enabled", None)


def test_ward_gets_only_safety_tools(probe):
    """集体层（含未识别说话人的兜底）只拿安全工具：急停 + 状态只读（R3 + 规格 §3.3 矩阵）。"""
    names = set(_names({}, _p("ward")))
    assert "__probe__" not in names
    assert names <= {"robot_status", "robot_stop"}


def test_elder_only_gets_whitelisted_tools(probe):
    names = set(_names({}, _p("elder")))
    assert "__probe__" not in names
    assert names <= {"robot_status", "robot_stop"}


def test_admin_sees_registered_tool(probe):
    """admin 的 allowed_tools=None → 不被角色裁剪（仍受 per-tool 开关约束）。"""
    assert "__probe__" in _names({}, _p("admin"))


def test_global_switch_still_applies(probe):
    assert "__probe__" not in _names({"__probe___enabled": False}, _p("admin"))


def test_tool_roles_field_filters_even_for_admin():
    """`@tool(roles=...)` 是闸门 2 的第二道：admin 不被角色白名单裁剪，但仍受工具自己的 roles 约束。

    注：`roles` 只能**收窄**、不能**扩展** —— elder 的角色白名单不含任何探针名，
    所以 `roles={"elder"}` 也不可能让探针进入 elder 可见集（交集语义）。

    判别力设计：`__probe__`（roles=None）与 `__probe_ward_only__`（roles={"ward"}）在 admin 下
    唯一的差异就是 roles 字段，故断言 "前者可见、后者不可见" 直接证明第二道闸门在起作用。
    """
    tools.tool("__probe__", "探针", {})(lambda **kw: {"ok": True})
    tools.tool("__probe_ward_only__", "探针", {}, roles={"ward"})(lambda **kw: {"ok": True})
    try:
        admin_names = _names({}, _p("admin"))
        assert "__probe__" in admin_names                 # 对照组：roles=None 的探针 admin 可见
        assert "__probe_ward_only__" not in admin_names   # 第二道闸门生效（否则两者都会在）
        # 非 admin 的角色白名单不含任何探针名 → 交集下 roles 无法把工具送进来
        assert "__probe_ward_only__" not in _names({}, _p("elder"))
        assert "__probe_ward_only__" not in _names({}, _p("ward"))
    finally:
        for _n in ("__probe__", "__probe_ward_only__"):
            tools._TOOL_REGISTRY.pop(_n, None)
            tools.TOOL_DEFAULTS.pop(f"{_n}_enabled", None)


def test_unknown_role_falls_back_to_ward(probe):
    assert _names({}, {"role": "??"}) == _names({}, _p("ward"))
    assert _names({}) == _names({}, _p("ward"))     # principal 缺省 → 集体层（R2）


def test_run_tool_denies_out_of_whitelist(probe):
    res = tools.run_tool("__probe__", {}, _p("ward"))
    assert res["ok"] is False and "不允许" in (res.get("error") or res.get("message") or "")
    assert tools.run_tool("__probe__", {}, _p("admin"))["ok"] is True


def test_mcp_tools_default_to_admin_only(monkeypatch):
    """MCP 工具未声明 roles → 只有 admin 看得到（不受控外部能力，默认从严）。"""
    monkeypatch.setattr(tools.mcp_client, "tools", lambda: {
        "fetch_html": {"server": "fetch", "schema": {
            "type": "function",
            "function": {"name": "fetch_html", "description": "", "parameters": {}}}}})
    assert "fetch_html" in _names({"mcp_enabled": True}, _p("admin"))
    assert "fetch_html" not in _names({"mcp_enabled": True}, _p("ward"))
    assert "fetch_html" not in _names({"mcp_enabled": True}, _p("elder"))


def test_mcp_tools_intersect_role_whitelist(monkeypatch):
    """**回归（重要 1）**：白名单是天花板 —— 服务器 roles 不能把白名单外的工具送给某角色。"""
    def fake_tools():
        return {
            # 名字不在任何角色白名单里、服务器只允许 elder
            "mcp_probe": {"server": "srv", "schema": {"type": "function",
                          "function": {"name": "mcp_probe", "description": "", "parameters": {}}}},
            # 名字**在** elder 白名单里、服务器只允许 elder → 两侧都满足才该可见
            "robot_stop": {"server": "srv", "schema": {"type": "function",
                           "function": {"name": "robot_stop", "description": "", "parameters": {}}}},
        }
    monkeypatch.setattr(tools.mcp_client, "tools", fake_tools)
    monkeypatch.setattr(tools, "_mcp_roles", lambda server: {"elder"})
    names_elder = _names({"mcp_enabled": True}, _p("elder"))
    assert "mcp_probe" not in names_elder          # 白名单外 → 不许可见（旧实现在此会红）
    assert "robot_stop" in names_elder             # 白名单内 + 服务器 roles 命中 → 可见
    assert "mcp_probe" not in _names({"mcp_enabled": True}, _p("admin"))   # admin 不在服务器 roles 里
    assert "mcp_probe" not in _names({"mcp_enabled": True}, _p("ward"))


def test_run_tool_denies_mcp_outside_server_roles(monkeypatch):
    """**回归（重要 2）**：看不见的 MCP 工具也不许调到（可见性与可调用性对称）。"""
    called = []
    monkeypatch.setattr(tools.mcp_client, "tools", lambda: {
        "mcp_probe": {"server": "srv", "schema": {"type": "function",
                      "function": {"name": "mcp_probe", "description": "", "parameters": {}}}}})
    monkeypatch.setattr(tools, "_mcp_roles", lambda server: {"admin"})
    monkeypatch.setattr(tools.mcp_client, "call_tool",
                        lambda name, args: called.append(name) or {"ok": True})
    res = tools.run_tool("mcp_probe", {}, _p("elder"))
    assert res["ok"] is False and called == []
    assert tools.run_tool("mcp_probe", {}, _p("admin"))["ok"] is True and called == ["mcp_probe"]


def test_policy_deny_audit_has_decision_field(monkeypatch, probe):
    """规格 §9：策略判定至少要带 decision 字段（否则下游按 decision 聚合会漏统计）。"""
    seen = []
    monkeypatch.setattr("LLM.log.log", lambda ev, **kw: seen.append((ev, kw)))
    tools.run_tool("__probe__", {"a": 1}, _p("ward"))
    assert seen and seen[-1][0] == "policy_deny"
    assert seen[-1][1]["decision"] == "deny"
    assert seen[-1][1]["tool"] == "__probe__" and seen[-1][1]["role"] == "ward"
    # Minor：原始 role 与归一后的 resolved_role 一起记（否则 fail-closed 的兜底会被算成
    # "集体层越权"）。未知取值：role 保留原样，resolved_role 归一成 ward。
    seen.clear()
    tools.run_tool("__probe__", {}, {"role": "??"})
    assert seen[-1][1]["role"] == "??" and seen[-1][1]["resolved_role"] == "ward"
    seen.clear()
    tools.run_tool("__probe__", {}, None)
    assert seen[-1][1]["role"] is None and seen[-1][1]["resolved_role"] == "ward"


def test_run_tool_denies_whitelisted_tool_excluded_by_server_roles(monkeypatch):
    """**回归（Minor）**：白名单**内** + 服务器 roles 排除该角色 → 仍拒。

    与 `test_run_tool_denies_mcp_outside_server_roles` 的差别：那条用的 `mcp_probe` 不在任何
    角色白名单里，拒因落在**白名单**（`allow_ok=False`）而非服务器 roles，判别力弱。
    这里用 **elder 白名单内**的 `robot_stop`，只有"服务器 roles 也参与判定"才会被拒。
    """
    from LLM import policy
    called = []
    seen = []
    monkeypatch.setattr(tools.mcp_client, "tools", lambda: {
        "robot_stop": {"server": "srv", "schema": {"type": "function",
                       "function": {"name": "robot_stop", "description": "", "parameters": {}}}}})
    monkeypatch.setattr(tools, "_mcp_roles", lambda server: {"admin"})
    monkeypatch.setattr(tools.mcp_client, "call_tool",
                        lambda name, args: called.append(name) or {"ok": True})
    monkeypatch.setattr("LLM.log.log", lambda ev, **kw: seen.append((ev, kw)))
    assert "robot_stop" in policy.role_policy("elder")["allowed_tools"]   # 前提：白名单内
    assert "robot_stop" not in _names({"mcp_enabled": True}, _p("elder"))  # 看不见
    res = tools.run_tool("robot_stop", {}, _p("elder"))                   # 也调不到
    assert res["ok"] is False and called == []
    assert seen[-1][1]["reason"] == "tool_roles_mismatch"                 # 拒因 = 服务器 roles
    assert tools.run_tool("robot_stop", {}, _p("admin"))["ok"] is True
    assert called == ["robot_stop"]


def test_unknown_tool_is_not_counted_as_denied(monkeypatch):
    """未知工具名不该落 policy_deny（别把模型拼错算成越权）。"""
    seen = []
    monkeypatch.setattr("LLM.log.log", lambda ev, **kw: seen.append((ev, kw)))
    res = tools.run_tool("__nope__", {}, _p("ward"))
    assert res["ok"] is False and "未知工具" in (res.get("message") or "")
    assert [ev for ev, _ in seen if ev == "policy_deny"] == []
