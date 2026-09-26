# -*- coding: utf-8 -*-
r"""权限矩阵：三层 × 每工具的**唯一判定入口**。

规格：docs/superpowers/specs/2026-09-26-permission-matrix-design.md

出厂默认 = `policy.py` 的角色白名单（`None` = 不裁剪）∩ 工具自身 roles
（本地 `@tool(roles=…)` / MCP 服务器 `roles`，未声明 = `{"admin"}`）；
运行期覆盖 = `brain.db.role_tool_grants`（只存与出厂不同的格子）。

红线：
  R1 角色只从 principal 取，前端传的一律不可信；
  R2 fail-closed —— 未知角色 / 未知工具 / 无记录 → 最保守那一侧（出厂政策）；
  R3 急停与呼救**永不**被权限挡住（`LOCKED`，写入口硬拒，见规格 §7）。

判定顺序（规格 §4.1）：``switch → lock → matrix → factory(白名单) → tool_roles``。
两条顺序都是**故意的**：
  * `switch` 在 `lock` 之前 —— 整机开关（`mcp_enabled` / `<工具>_enabled`）是**运维把能力拔掉**
    的开关，不是权限；R3 保护的是"身份/权限不能挡住急停与呼救"，不是"绕过全局 kill switch"；
  * `factory` 在 `tool_roles` 之前 —— tools.py 旧实现的审计 reason 优先级就是"白名单先"
    （`out_of_role_whitelist if not allow_ok else tool_roles_mismatch`），换顺序会改动审计口径、
    并让按 reason 聚合的既有用例回归；允许/拒绝的结论完全相同。
"""
import time

from ..core import log as audit
from ..store import db
from .policy import role_policy

ROLES = ("ward", "elder", "admin")

# R3：急停（robot_stop）与呼救（notify_nurse）在**权限层面**任何层都不可取消。
# **故意不进 DB** —— 否则"能改这一格的人"就等于"能绕过 R3 的人"。
# 它们仍然服从整机开关（见上面的顺序说明）：`mcp_enabled` 关掉时车控/通知子进程根本没起，
# 红锁也无从调用——那条路是运维开关，不是权限分歧。
LOCKED: dict[str, frozenset[str]] = {
    "ward": frozenset({"robot_stop", "notify_nurse"}),
    "elder": frozenset({"robot_stop", "notify_nurse"}),
}

_matrix_error_at = 0.0          # 读库异常的审计节流（每 30s 最多一条，别刷爆审计）


def normalize_role(role: str | None) -> str:
    """未知 / None / 空串 → `"ward"`（R2，与 `role_policy` 同口径）。"""
    return role if role in ROLES else "ward"


def is_locked(role: str | None, tool: str) -> bool:
    """该格是不是 R3 红线（不可取消）。"""
    return tool in LOCKED.get(normalize_role(role), frozenset())


def load_grants() -> dict[tuple[str, str], bool]:
    """读覆盖表；异常时回落空表（= 全走出厂默认）并审计（规格 D7）。

    热路径约束（规格 §4.2）：**每轮对话只调一次**，把结果传给每个 `decide()`；
    绝不要在 `decide()` 内部查库。
    """
    global _matrix_error_at
    try:
        return db.get_role_grants()
    except Exception as e:                      # noqa: BLE001  读库失败绝不等于放行
        now = time.monotonic()
        if now - _matrix_error_at > 30:
            _matrix_error_at = now
            audit.log("policy_deny", action="matrix_unavailable", error=str(e),
                      decision="fallback_factory")
        return {}


def tool_roles_ok(role: str, tool: str, *, server: str = "", local: bool = True) -> bool:
    """工具自身的角色声明：本地 `@tool(roles=…)`（None = 不限）/ MCP 服务器 `roles`。"""
    from . import tools
    if local:
        reg = tools._TOOL_REGISTRY.get(tool)
        roles = (reg or {}).get("roles")
    else:
        roles = tools._mcp_roles(server)
    return roles is None or role in roles


def factory_allows(role: str, tool: str, *, server: str = "", local: bool = True) -> bool:
    """出厂默认 = policy 白名单 ∩ 工具自身 roles。只服务矩阵快照与对账，不参与判定顺序。"""
    allow = role_policy(role)["allowed_tools"]
    if allow is not None and tool not in allow:
        return False
    return tool_roles_ok(role, tool, server=server, local=local)


def decide(principal: dict | None, tool: str, *, server: str = "", local: bool = True,
           settings: dict | None = None, grants: dict | None = None,
           ignore_switch: bool = False) -> dict:
    """唯一判定入口 → ``{"allow": bool, "reason": str, "source": str}``。

    `ignore_switch=True` 供矩阵页快照使用：跳过整机开关，回答"若开关打开，这一层允许吗"。
    """
    role = normalize_role((principal or {}).get("role"))

    if not ignore_switch:
        st = settings if settings is not None else db.get_settings()
        if local:
            from . import tools
            reg = tools._TOOL_REGISTRY.get(tool) or {}
            if not st.get(f"{tool}_enabled", reg.get("enabled", True)):
                return {"allow": False, "reason": "tool_disabled", "source": "switch"}
        elif not st.get("mcp_enabled"):
            return {"allow": False, "reason": "mcp_disabled", "source": "switch"}

    if is_locked(role, tool):
        return {"allow": True, "reason": "", "source": "lock"}

    g = grants if grants is not None else load_grants()
    if (role, tool) in g:
        allowed = bool(g[(role, tool)])
        return {"allow": allowed, "reason": "" if allowed else "matrix_deny",
                "source": "matrix"}

    allow = role_policy(role)["allowed_tools"]
    if allow is not None and tool not in allow:
        return {"allow": False, "reason": "out_of_role_whitelist", "source": "factory"}
    if not tool_roles_ok(role, tool, server=server, local=local):
        return {"allow": False, "reason": "tool_roles_mismatch", "source": "tool_roles"}
    return {"allow": True, "reason": "", "source": "factory"}


# ---------------------------------------------------------------- 矩阵快照与写入口
def _registry_rows() -> list[dict]:
    """矩阵的行 = 注册表里**现在存在**的工具（本地 + 已在线的 MCP）。

    MCP 工具只有在后端拉起子进程后才出现在 `mcp_client.tools()` 里 —— 后端没在线/子进程没起时
    矩阵页会少这几行，这是如实反映"现在有什么"，不是 bug。
    """
    from . import mcp_client, tools
    rows = []
    for name, reg in tools._TOOL_REGISTRY.items():
        rows.append({"name": name, "server": "local", "local": True,
                     "switch_key": f"{name}_enabled",
                     "switch_default": bool(reg.get("enabled", True))})
    for name, entry in mcp_client.tools().items():
        if name in tools._TOOL_REGISTRY:
            continue                        # 与本地重名时本地优先（沿用旧口径）
        rows.append({"name": name, "server": entry.get("server", ""), "local": False,
                     "switch_key": "mcp_enabled", "switch_default": True})
    return sorted(rows, key=lambda r: (r["server"], r["name"]))


def matrix_snapshot(settings: dict | None = None) -> dict:
    """矩阵页数据源：行=工具、列=三层，每格给「出厂 / 实际 / 是否人工改过 / 是否红锁」。

    `allowed` 用 `ignore_switch=True` 算 —— 回答"**若整机开关打开**，这一层允许吗"，
    整机开关状态另用 `switch_on` 单列提示，免得管理员把"开关关着"误判成"矩阵没生效"。
    """
    st = settings if settings is not None else db.get_settings()
    grants = load_grants()
    rows = _registry_rows()
    known = {r["name"] for r in rows}
    for (role, tool), allowed in sorted(grants.items()):     # 已下线工具的遗留行
        if tool not in known:
            rows.append({"name": tool, "server": "(已下线)", "local": True,
                         "switch_key": "", "switch_default": True, "orphan": True})
            known.add(tool)
    tools_out = []
    for r in rows:
        orphan = bool(r.get("orphan"))
        factory, allowed, overridden, locked = {}, {}, {}, {}
        for role in ROLES:
            if orphan:
                factory[role] = allowed[role] = overridden[role] = False
                locked[role] = False
                continue
            factory[role] = factory_allows(role, r["name"], server=r["server"],
                                           local=r["local"])
            overridden[role] = (role, r["name"]) in grants
            locked[role] = is_locked(role, r["name"])
            allowed[role] = decide({"role": role}, r["name"], server=r["server"],
                                   local=r["local"], settings=st, grants=grants,
                                   ignore_switch=True)["allow"]
        switch_on = True if orphan else bool(
            st.get(r["switch_key"], r["switch_default"]) if r["switch_key"] else True)
        tools_out.append({"name": r["name"], "server": r["server"], "local": r["local"],
                          "orphan": orphan, "switch_on": switch_on,
                          "factory": factory, "allowed": allowed,
                          "overridden": overridden, "locked": locked})
    return {"roles": list(ROLES), "tools": tools_out,
            "grants": [{"role": gr, "tool": gt, "allowed": bool(ga)}
                       for (gr, gt), ga in sorted(grants.items())]}


def set_grants(changes: list[dict], actor: dict | None = None) -> dict:
    """逐格写入（**只有 admin 的路由才该调用**）。

    规则：角色非法 / 工具名为空 → 拒绝；命中 LOCKED → 拒绝（R3）；
    写回出厂值 → 删行（D2：只存差额），本来就是出厂值 → noop；否则 upsert。
    **逐格独立提交、不回滚已成功的格**；返回 `ok` = 所有格都成功。
    """
    actor = actor or {}
    by = str(actor.get("uid") or actor.get("slot") or "")
    existing = load_grants()
    results = []
    for ch in changes:
        role = str(ch.get("role") or "")
        tool = str(ch.get("tool") or "")
        want = bool(ch.get("allowed"))
        if role not in ROLES:
            results.append({"role": role, "tool": tool, "ok": False,
                            "action": "rejected", "reason": "unknown_role"})
            continue
        if not tool:
            results.append({"role": role, "tool": tool, "ok": False,
                            "action": "rejected", "reason": "empty_tool"})
            continue
        if is_locked(role, tool):
            audit.log("policy_deny", action="permissions_locked", role=role, tool=tool,
                      decision="deny", reason="r3_locked", by=by)
            results.append({"role": role, "tool": tool, "ok": False,
                            "action": "rejected", "reason": "locked"})
            continue
        before = existing.get((role, tool))
        base = factory_allows(role, tool)          # 与出厂一致 → 不留冗余行
        if want == base:
            removed = db.delete_role_grant(role, tool)
            action = "cleared" if removed else "noop"
        else:
            db.set_role_grant(role, tool, want, by=by)
            action = "set"
        if action == "set" or (action == "cleared" and before is not None):
            audit.log("policy_change", role=role, tool=tool, **{"from": before}, to=want,
                      action=action, by=by, slot=actor.get("slot", ""), via="api")
        existing[(role, tool)] = want
        results.append({"role": role, "tool": tool, "ok": True, "action": action,
                        "allowed": want})
    return {"ok": all(r["ok"] for r in results), "results": results}
