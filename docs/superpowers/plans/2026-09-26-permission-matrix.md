# 权限矩阵（分配器）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。
> 规格：`docs/superpowers/specs/2026-09-26-permission-matrix-design.md`（D1–D11 与 §1.4 可编辑边界为权威）

**目标：** 把"某层能不能用某工具"从代码常量变成**可编辑矩阵**：新增 `permissions.decide()` 单一判定入口 + DB 覆盖层 + admin 可勾选的「身份与权限」页，勾选即生效。

**架构：** 出厂默认（`policy.py` 白名单 + `conf.MCP_SERVERS.roles` + `@tool(roles=)`）之上叠一张稀疏覆盖表 `role_tool_grants`（只存与出厂不同的格子）。判定一律走 `permissions.decide()`：`lock → switch → matrix → factory → tool_roles`。`tools.effective_tools()` 与 `tools.run_tool()` 两处重复实现改为调用它。

**技术栈：** Python 3 + SQLite（stdlib）+ FastAPI（既有）/ Vue3 + Vite + TS（既有 admin 端）。

---

## 执行环境注意（先读，避免误判失败）

本仓库的测试用 `tempfile.mkdtemp()` 建临时库（见 `LLM/tests/test_server_roles_routes.py:17`）。在受限沙箱里该目录可能不可写，症状是 `sqlite3.OperationalError: unable to open database file`（**不是代码 bug**）。若遇到，先在工作区建一个临时 pytest 插件把 `mkdtemp` 换成 `os.mkdir(path, 0o777)`，再用 `-p` 挂上：

```powershell
New-Item -ItemType Directory -Force .\_tmp_pytest | Out-Null
@'
import os, tempfile
def _mkdtemp(suffix="", prefix="tmp", dir=None):
    dir = dir or tempfile.gettempdir()
    for _ in range(100):
        p = os.path.join(dir, prefix + next(tempfile._RandomNameSequence()) + suffix)
        try:
            os.mkdir(p, 0o777); return p
        except FileExistsError:
            continue
    raise FileExistsError("no usable temp dir")
tempfile.mkdtemp = _mkdtemp
'@ | Set-Content -Encoding utf8 .\_tmp_pytest\mkdtemp_patch.py
$env:TEMP = "D:\_project\Robot\_tmp_pytest"; $env:PYTHONPATH = "D:\_project\Robot\_tmp_pytest"
# 之后所有 pytest 命令都带 -p mkdtemp_patch
```

**基线（改动前先跑一遍，确认起点是绿的）：**

```powershell
.venv\Scripts\python.exe -m pytest LLM/tests/test_policy_roles.py LLM/tests/test_policy_tools.py LLM/tests/test_server_roles_routes.py LLM/tests/test_settings_roles.py LLM/tests/test_worker_roles.py LLM/tests/test_session_roles.py -q -p mkdtemp_patch
# 预期：116 passed
```

---

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `LLM/store/db.py` | `role_tool_grants` 表 + 5 个存取函数（唯一持久化出口） | 修改 |
| `LLM/agent/permissions.py` | **唯一的权限判定入口**：`decide()` / `factory_allows()` / `matrix_snapshot()` / `set_grants()` / `LOCKED` | 创建 |
| `LLM/agent/tools.py` | `effective_tools()` / `run_tool()` 改为调 `permissions.decide()`；删掉重复实现的 `_mcp_tools_for()` | 修改 |
| `LLM/server.py` | 矩阵 GET / POST / reset 三条路由 | 修改 |
| `frontend/packages/admin/src/pages/RolesPage.vue` | 「身份与权限」页：只读矩阵表 **→ 可编辑矩阵**（规格 §6 说"新增 Commissions 页"，**实际按复用优先改造本页**，见任务 6 说明） | 修改 |
| `LLM/tests/test_permissions.py` | §8 十条测试 | 创建 |
| `AGENTS.md` / `docs/superpowers/specs/2026-09-14-*.md` / `docs/log.md` | 文档同步 | 修改 |

---

## 任务 1：DB 覆盖表与存取函数

**文件：**
- 修改：`LLM/store/db.py`（SCHEMA：`LLM/store/db.py:81` 之后插入建表；函数：`LLM/store/db.py:1500` 之后、`# ---- 地图标记索引缓存` 之前插入）
- 测试：`LLM/tests/test_permissions.py`（新建）

- [ ] **步骤 1：编写失败的测试**

创建 `LLM/tests/test_permissions.py`：

```python
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
```

- [ ] **步骤 2：运行测试验证失败**

```powershell
.venv\Scripts\python.exe -m pytest LLM/tests/test_permissions.py -q -p mkdtemp_patch
```
预期：FAIL / ERROR，报 `AttributeError: module 'LLM.store.db' has no attribute 'get_role_grants'`

- [ ] **步骤 3：写最少实现代码**

在 `LLM/store/db.py` 的 SCHEMA 里、`CREATE TABLE IF NOT EXISTS settings (...)` 那一行（`:81`）之后插入：

```sql
-- 权限矩阵覆盖层（规格 docs/superpowers/specs/2026-09-26-permission-matrix-design.md）：
-- **只存与出厂默认不同的格子**；删行 = 回到出厂默认（D2）。
CREATE TABLE IF NOT EXISTS role_tool_grants (
  role       TEXT NOT NULL,          -- ward | elder | admin
  tool       TEXT NOT NULL,          -- 工具名（本地与 MCP 同一命名空间）
  allowed    INTEGER NOT NULL,       -- 1=允许，0=显式拒绝
  updated_at TEXT NOT NULL,
  updated_by TEXT NOT NULL,
  PRIMARY KEY (role, tool)
);
```

在 `LLM/store/db.py` 的 `set_admin_auth_required()` 之后（约 `:1501`）插入：

```python
# ---------------------------------------------------------------- 权限矩阵（role_tool_grants）
def list_role_grants() -> list[dict]:
    """矩阵里的全部覆盖行（矩阵页与审计用）。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT role,tool,allowed,updated_at,updated_by FROM role_tool_grants "
            "ORDER BY role,tool").fetchall()
        return [{**dict(r), "allowed": bool(r["allowed"])} for r in rows]
    finally:
        conn.close()


def get_role_grants() -> dict[tuple[str, str], bool]:
    """判定热路径用：{(role, tool): allowed}。空表 = 全走出厂默认。"""
    conn = _conn()
    try:
        return {(r["role"], r["tool"]): bool(r["allowed"])
                for r in conn.execute("SELECT role,tool,allowed FROM role_tool_grants")}
    finally:
        conn.close()


def set_role_grant(role: str, tool: str, allowed: bool, by: str = "") -> None:
    """写一格（逐格 upsert，天然幂等，故不需要乐观锁——规格 D9）。"""
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                """INSERT INTO role_tool_grants(role,tool,allowed,updated_at,updated_by)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(role,tool) DO UPDATE SET
                     allowed=excluded.allowed, updated_at=excluded.updated_at,
                     updated_by=excluded.updated_by""",
                (role, tool, 1 if allowed else 0, now_iso(), by or ""))
            conn.commit()
        finally:
            conn.close()


def delete_role_grant(role: str, tool: str) -> int:
    """删一格 → 回到出厂默认。返回删除行数（0 = 本来就是默认）。"""
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute("DELETE FROM role_tool_grants WHERE role=? AND tool=?",
                               (role, tool))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def clear_role_grants(role: str = "") -> int:
    """恢复出厂：`role=""` 清全部，否则只清该列。返回删除行数。"""
    with _lock:
        conn = _conn()
        try:
            if role:
                cur = conn.execute("DELETE FROM role_tool_grants WHERE role=?", (role,))
            else:
                cur = conn.execute("DELETE FROM role_tool_grants")
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
```

- [ ] **步骤 4：运行测试验证通过**

```powershell
.venv\Scripts\python.exe -m pytest LLM/tests/test_permissions.py -q -p mkdtemp_patch
```
预期：`3 passed`

- [ ] **步骤 5：Commit**

```powershell
git add LLM/store/db.py LLM/tests/test_permissions.py
git commit -m "feat: 权限矩阵覆盖表 role_tool_grants 与存取函数"
```

---

## 任务 2：`permissions.py` 判定入口

**文件：**
- 创建：`LLM/agent/permissions.py`
- 测试：`LLM/tests/test_permissions.py`（追加）

**判定顺序（规格 §4.1，注意 4/5 的顺序在实现时定为「白名单先于工具 roles」）：** `lock → switch → matrix → factory(白名单) → tool_roles`。第 4/5 步之所以这样排，是为了让既有审计 reason **一字不变**：`tools.py` 旧实现的 reason 优先级就是"白名单先"（见 `LLM/agent/tools.py:269` 的 `out_of_role_whitelist if not allow_ok else tool_roles_mismatch`），否则 `test_policy_tools.py` 里按 reason 聚合的用例会红。

- [ ] **步骤 1：编写失败的测试**（追加到 `LLM/tests/test_permissions.py`）

```python
def test_decide_lock_allows_safety_tools(d):
    """R3：急停/呼救不受任何权限影响，即便出厂白名单里被删也放行。"""
    from LLM.agent import permissions
    for role in ("ward", "elder"):
        for tool in ("robot_stop", "notify_nurse"):
            out = permissions.decide({"role": role}, tool, settings={}, grants={})
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
    out = permissions.decide({"role": "elder"}, "robot_stop", server="srv", local=False,
                             settings={"mcp_enabled": True}, grants={})
    assert out["reason"] == "tool_roles_mismatch"      # 白名单内 → 拒因才轮到服务器 roles


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
```

- [ ] **步骤 2：运行测试验证失败**

```powershell
.venv\Scripts\python.exe -m pytest LLM/tests/test_permissions.py -q -p mkdtemp_patch
```
预期：FAIL / ERROR，报 `ModuleNotFoundError: No module named 'LLM.agent.permissions'`

- [ ] **步骤 3：写实现**

创建 `LLM/agent/permissions.py`：

```python
# -*- coding: utf-8 -*-
r"""权限矩阵：三层 × 每工具的**唯一判定入口**。

规格：docs/superpowers/specs/2026-09-26-permission-matrix-design.md
红线：R1 角色只从 principal 取（前端不可信）；R2 fail-closed（未知角色/未知工具/无记录 → 出厂最保守）；
      R3 急停与呼救**永不**被权限挡住（LOCKED，写入口硬拒，见 §7）。

判定顺序（规格 §4.1）：lock → switch → matrix → factory(白名单) → tool_roles。
`factory` 先于 `tool_roles` 是**故意的**：tools.py 旧实现的 reason 优先级就是"白名单先"，
换顺序会改动审计口径（test_policy_tools.py 按 reason 断言）。
"""
import time

from ..core import log as audit
from ..store import db
from .policy import POLICY_DEFAULTS, role_policy

ROLES = ("ward", "elder", "admin")

# R3：三类安全动作（急停 / 呼救）在任何层都不可取消。**不进 DB** ——
# 否则"能改这格的人"就等于"能绕过 R3 的人"。
LOCKED: dict[str, frozenset[str]] = {
    "ward": frozenset({"robot_stop", "notify_nurse"}),
    "elder": frozenset({"robot_stop", "notify_nurse"}),
}

_matrix_error_at = 0.0          # 读库异常的审计节流（每 30s 最多一条，别刷爆审计）


def normalize_role(role: str | None) -> str:
    """未知/None/空串 → "ward"（R2，与 role_policy 同口径）。"""
    return role if role in ROLES else "ward"


def is_locked(role: str | None, tool: str) -> bool:
    return tool in LOCKED.get(normalize_role(role), frozenset())


def load_grants() -> dict[tuple[str, str], bool]:
    """读覆盖表；异常回落空表（= 全走出厂默认）并审计。热路径**每轮只调一次**（规格 §4.2）。"""
    global _matrix_error_at
    try:
        return db.get_role_grants()
    except Exception as e:                      # noqa: BLE001
        now = time.monotonic()
        if now - _matrix_error_at > 30:
            _matrix_error_at = now
            audit.log("policy_deny", action="matrix_unavailable", error=str(e),
                      decision="fallback_factory")
        return {}


def tool_roles_ok(role: str, tool: str, *, server: str = "", local: bool = True) -> bool:
    """工具自身的角色声明（本地 `@tool(roles=…)` / MCP 服务器 `roles`，未声明 = {"admin"}）。"""
    from . import tools
    if local:
        reg = tools._TOOL_REGISTRY.get(tool)
        roles = (reg or {}).get("roles")
    else:
        roles = tools._mcp_roles(server)
    return roles is None or role in roles


def factory_allows(role: str, tool: str, *, server: str = "", local: bool = True) -> bool:
    """出厂默认 = policy 白名单（None=全放）∩ 工具自身 roles。只服务快照/对账。"""
    allow = role_policy(role)["allowed_tools"]
    if allow is not None and tool not in allow:
        return False
    return tool_roles_ok(role, tool, server=server, local=local)


def decide(principal: dict | None, tool: str, *, server: str = "", local: bool = True,
           settings: dict | None = None, grants: dict | None = None,
           ignore_switch: bool = False) -> dict:
    """唯一判定入口 → {"allow": bool, "reason": str, "source": str}。

    `ignore_switch=True` 供矩阵页快照使用（"若整机开关打开，这一层允许吗"）。
    """
    role = normalize_role((principal or {}).get("role"))

    if is_locked(role, tool):
        return {"allow": True, "reason": "", "source": "lock"}

    if not ignore_switch:
        st = settings if settings is not None else db.get_settings()
        if local:
            from . import tools
            reg = tools._TOOL_REGISTRY.get(tool) or {}
            if not st.get(f"{tool}_enabled", reg.get("enabled", True)):
                return {"allow": False, "reason": "tool_disabled", "source": "switch"}
        elif not st.get("mcp_enabled"):
            return {"allow": False, "reason": "mcp_disabled", "source": "switch"}

    g = grants if grants is not None else load_grants()
    if (role, tool) in g:
        allowed = bool(g[(role, tool)])
        return {"allow": allowed, "reason": "" if allowed else "matrix_deny", "source": "matrix"}

    allow = role_policy(role)["allowed_tools"]
    if allow is not None and tool not in allow:
        return {"allow": False, "reason": "out_of_role_whitelist", "source": "factory"}
    if not tool_roles_ok(role, tool, server=server, local=local):
        return {"allow": False, "reason": "tool_roles_mismatch", "source": "tool_roles"}
    return {"allow": True, "reason": "", "source": "factory"}
```

- [ ] **步骤 4：运行测试验证通过**

```powershell
.venv\Scripts\python.exe -m pytest LLM/tests/test_permissions.py -q -p mkdtemp_patch
```
预期：`10 passed`

- [ ] **步骤 5：Commit**

```powershell
git add LLM/agent/permissions.py LLM/tests/test_permissions.py
git commit -m "feat: 权限单一判定入口 permissions.decide（lock/switch/matrix/factory/tool_roles）"
```

---

## 任务 3：`tools.py` 接入单一入口（消除规则双重实现）

**文件：**
- 修改：`LLM/agent/tools.py:147-182`（删 `_mcp_tools_for`，重写 `effective_tools`）
- 修改：`LLM/agent/tools.py:231-270`（`run_tool` 的前半段判定）
- 测试：`LLM/tests/test_permissions.py`（追加）+ 既有 `test_policy_tools.py` 必须全绿

- [ ] **步骤 1：编写失败的测试**（追加）

```python
def test_effective_tools_matrix_grants_mcp_tool(d, monkeypatch):
    """矩阵放行后，老人层**看得见也调得到** tavily-search（此前只能 admin）。"""
    from LLM.agent import permissions, tools
    monkeypatch.setattr(tools.mcp_client, "tools", lambda: {
        "tavily-search": {"server": "tavily", "schema": {
            "type": "function",
            "function": {"name": "tavily-search", "description": "", "parameters": {}}}}})
    monkeypatch.setattr(tools.mcp_client, "call_tool", lambda n, a: {"ok": True})
    settings = {"mcp_enabled": True}
    p = {"uid": "elder_101_1", "role": "elder", "slot": "kiosk"}

    names = lambda: [t["function"]["name"] for t in tools.effective_tools(settings, p)]
    assert "tavily-search" not in names()                       # 出厂：只有 admin
    d.set_role_grant("elder", "tavily-search", True, by="admin")
    permissions._matrix_error_at = 0.0
    assert "tavily-search" in names()                           # 勾上即生效（无缓存）
    assert tools.run_tool("tavily-search", {}, p)["ok"] is True


def test_effective_tools_matrix_denies_admin_tool(d, monkeypatch):
    """反向：矩阵显式拒绝能盖掉 admin 的"全部"出厂默认。"""
    from LLM.agent import permissions, tools
    monkeypatch.setattr(tools.mcp_client, "tools", lambda: {
        "robot_move": {"server": "car", "schema": {
            "type": "function",
            "function": {"name": "robot_move", "description": "", "parameters": {}}}}})
    monkeypatch.setattr(tools, "_mcp_roles", lambda server: {"admin"})
    monkeypatch.setattr(tools.mcp_client, "call_tool", lambda n, a: {"ok": True})
    settings = {"mcp_enabled": True}
    admin = {"uid": "admin", "role": "admin", "slot": "admin"}
    names = lambda: [t["function"]["name"] for t in tools.effective_tools(settings, admin)]
    assert "robot_move" in names()
    d.set_role_grant("admin", "robot_move", False, by="admin")
    permissions._matrix_error_at = 0.0
    assert "robot_move" not in names()
    res = tools.run_tool("robot_move", {}, admin)
    assert res["ok"] is False and "不允许" in res["error"]


def test_run_tool_audits_matrix_source(d, monkeypatch):
    from LLM.agent import tools
    seen = []
    monkeypatch.setattr("LLM.core.log.log", lambda ev, **kw: seen.append((ev, kw)))
    d.set_role_grant("admin", "robot_move", False, by="admin")
    tools.run_tool("robot_move", {}, {"role": "admin", "slot": "admin"})
    assert seen[-1][1]["reason"] == "matrix_deny"
    assert seen[-1][1]["source"] == "matrix"
```

- [ ] **步骤 2：运行测试验证失败**

```powershell
.venv\Scripts\python.exe -m pytest LLM/tests/test_permissions.py -q -p mkdtemp_patch
```
预期：新增 3 条 FAIL（矩阵未接入 `effective_tools`）

- [ ] **步骤 3：写实现**

把 `LLM/agent/tools.py` 的 `_mcp_tools_for()`（`:147-162`）整段删除，`effective_tools()` 改为：

```python
def effective_tools(settings: dict, principal: dict | None = None) -> list[dict]:
    """闸门 2：整机开关 ∩ 角色白名单 ∩ 工具自身 roles，**统一经 permissions.decide()**。

    `grants` 在入口查一次（规格 §4.2：判定热路径禁止逐个工具查库）。
    """
    from . import permissions
    grants = permissions.load_grants()
    out = []
    for name, reg in _TOOL_REGISTRY.items():
        if permissions.decide(principal, name, local=True, settings=settings,
                              grants=grants)["allow"]:
            out.append(reg["schema"])
    if settings.get("mcp_enabled"):
        for name, entry in mcp_client.tools().items():
            if name in _TOOL_REGISTRY:
                continue                        # 与本地重名时本地优先（沿用旧口径）
            if permissions.decide(principal, name, server=entry.get("server", ""), local=False,
                                  settings=settings, grants=grants)["allow"]:
                out.append(entry["schema"])
    return out
```

`run_tool()` 的判定段（`：232-270`）改为：

```python
def run_tool(name: str, args: dict, principal: dict | None = None) -> dict:
    """统一分发。**执行前再校验一次**（闸门 2 第二道，与 effective_tools 同一入口）。"""
    from . import permissions
    from ..core import log as audit
    from .policy import POLICY_DEFAULTS
    p = principal or {}
    role = p.get("role")
    resolved = role if role in POLICY_DEFAULTS else "ward"
    reg = _TOOL_REGISTRY.get(name)
    is_local = reg is not None
    # MCP 侧取**一次快照**：两次查表之间工具可能消失（服务器断开/重连），裸下标会 KeyError。
    snapshot = mcp_client.tools() if not is_local else {}
    entry = snapshot.get(name)
    if not is_local and entry is None:
        # 未知工具（模型幻觉/拼错）：**不落越权审计**（别污染越权统计）
        return {"ok": False, "message": f"未知工具 {name}"}

    verdict = permissions.decide(p, name, server=(entry or {}).get("server", ""),
                                 local=is_local, grants=permissions.load_grants())
    if not verdict["allow"]:
        # 同时记 `role`（原始，可能缺省/未知）与 `resolved_role`（归一后真正参与判定的那个）
        audit.log("policy_deny", tool=name, role=role, resolved_role=resolved,
                  uid=p.get("uid"), slot=p.get("slot"),
                  decision="deny", args=_audit_args(name, args),
                  reason=verdict["reason"], source=verdict["source"])
        if verdict["reason"] == "mcp_disabled":
            return {"ok": False, "error": f"MCP 工具总开关已关闭，不允许调用工具 {name}"}
        if verdict["source"] == "switch":
            return {"ok": False, "error": f"工具已关闭，不允许调用工具 {name}"}
        return {"ok": False, "error": f"当前身份不允许调用工具 {name}"}
    if not is_local:
        # ……（以下与现状完全一致，不动：车控单槽仲裁 + call_tool）
        gate_token = None
        if entry.get("server") == "car" and name not in _CAR_GATE_BYPASS:
            ref = f"dialog:{threading.get_ident()}:{time.monotonic_ns()}"
            gate_token = action_gate.claim("dialog", ref)
            if gate_token is None:
                return {"ok": False, "status": "busy", "error": "车控正由另一个任务占用"}
        try:
            return mcp_client.call_tool(name, args or {})
        finally:
            if gate_token is not None:
                action_gate.release(gate_token)
    args_error = _local_args_error(reg, args)
    if args_error:
        return {"ok": False, "error": f"工具参数不符合 schema：{args_error}"}
    token = _CURRENT_PRINCIPAL.set(dict(p))
    try:
        return _run_fn(reg["fn"], args or {})
    except Exception as e:
        return {"ok": False, "message": f"工具执行失败: {e}"}
    finally:
        _CURRENT_PRINCIPAL.reset(token)
```

- [ ] **步骤 4：运行测试验证通过（含既有回归）**

```powershell
.venv\Scripts\python.exe -m pytest LLM/tests/test_permissions.py LLM/tests/test_policy_tools.py LLM/tests/test_policy_roles.py LLM/tests/test_notice_mcp.py LLM/tests/test_plan.py -q -p mkdtemp_patch
```
预期：全绿（`test_policy_tools.py` 的 60+ 条 reason 断言不变）

- [ ] **步骤 5：Commit**

```powershell
git add LLM/agent/tools.py LLM/tests/test_permissions.py
git commit -m "refactor: 工具可见性与调用校验统一走 permissions.decide（删 _mcp_tools_for 重复实现）"
```

---

## 任务 4：矩阵快照与写入口

**文件：**
- 修改：`LLM/agent/permissions.py`（追加 `matrix_snapshot()` / `set_grants()`）
- 测试：`LLM/tests/test_permissions.py`（追加）

- [ ] **步骤 1：编写失败的测试**（追加）

```python
def test_snapshot_matches_factory_when_table_empty(d):
    """对账：空表时 allowed 必须逐格等于 factory（防"改了默认忘了矩阵"）。"""
    from LLM.agent import permissions
    snap = permissions.matrix_snapshot(settings={"mcp_enabled": True})
    for t in snap["tools"]:
        for role in snap["roles"]:
            assert t["allowed"][role] == t["factory"][role]
            assert t["overridden"][role] is False


def test_snapshot_marks_locked_and_override(d):
    from LLM.agent import permissions
    d.set_role_grant("elder", "tavily-search", True, by="admin")
    snap = permissions.matrix_snapshot(settings={"mcp_enabled": True})
    by_name = {t["name"]: t for t in snap["tools"]}
    assert by_name["robot_stop"]["locked"]["ward"] is True
    assert by_name["robot_stop"]["locked"]["admin"] is False
    if "tavily-search" in by_name:                      # MCP 工具需后端在线才有 schema
        assert by_name["tavily-search"]["overridden"]["elder"] is True
        assert by_name["tavily-search"]["allowed"]["elder"] is True


def test_snapshot_marks_orphan_grant(d):
    """库里留着已下线工具的行 → 标 orphan，让人知道它不生效。"""
    from LLM.agent import permissions
    d.set_role_grant("elder", "早就没了的工具", True, by="admin")
    snap = permissions.matrix_snapshot(settings={})
    orph = [t for t in snap["tools"] if t["orphan"]]
    assert [t["name"] for t in orph] == ["早就没了的工具"]


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
    assert by_key[("nope", "robot_move")]["action"] == "rejected"
    assert by_key[("elder", "tavily-search")]["action"] == "set"
    assert d.get_role_grants() == {("elder", "tavily-search"): True}


def test_set_grants_clears_row_equal_to_factory(d):
    """写回出厂值 = 删行（D2：只存差额，别留冗余）。"""
    from LLM.agent import permissions
    d.set_role_grant("admin", "robot_move", False, by="admin")
    out = permissions.set_grants([{"role": "admin", "tool": "robot_move", "allowed": True}],
                                 actor={"uid": "admin", "slot": "admin"})
    assert out["results"][0]["action"] == "cleared"
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
```

- [ ] **步骤 2：运行测试验证失败**

```powershell
.venv\Scripts\python.exe -m pytest LLM/tests/test_permissions.py -q -p mkdtemp_patch
```
预期：新增用例 FAIL（`AttributeError: ... has no attribute 'matrix_snapshot'`）

- [ ] **步骤 3：写实现**（追加到 `LLM/agent/permissions.py`）

```python
def _registry_rows() -> list[dict]:
    """矩阵的行 = 注册表里**现在存在**的工具（本地 + 已在线的 MCP）。"""
    from . import mcp_client, tools
    rows = []
    for name, reg in tools._TOOL_REGISTRY.items():
        rows.append({"name": name, "server": "local", "local": True,
                     "switch_key": f"{name}_enabled",
                     "switch_default": bool(reg.get("enabled", True))})
    for name, entry in mcp_client.tools().items():
        if name in tools._TOOL_REGISTRY:
            continue
        rows.append({"name": name, "server": entry.get("server", ""), "local": False,
                     "switch_key": "mcp_enabled", "switch_default": True})
    return sorted(rows, key=lambda r: (r["server"], r["name"]))


def matrix_snapshot(settings: dict | None = None) -> dict:
    """矩阵页数据源：行=工具、列=三层，每格给「出厂 / 实际 / 是否人工改过 / 是否红锁」。"""
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
    """逐格写入（只 admin 的路由才该调用）。

    规则：角色非法 → 拒绝；命中 LOCKED → 拒绝（R3）；写回出厂值 → 删行；否则 upsert。
    **逐格独立提交、不回滚已成功的格**；`ok` = 所有格都成功。
    """
    actor = actor or {}
    by = str(actor.get("uid") or actor.get("slot") or "")
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
        before = load_grants().get((role, tool))
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
        results.append({"role": role, "tool": tool, "ok": True, "action": action,
                        "allowed": want})
    return {"ok": all(r["ok"] for r in results), "results": results}
```

- [ ] **步骤 4：运行测试验证通过**

```powershell
.venv\Scripts\python.exe -m pytest LLM/tests/test_permissions.py -q -p mkdtemp_patch
```
预期：`21 passed`

- [ ] **步骤 5：Commit**

```powershell
git add LLM/agent/permissions.py LLM/tests/test_permissions.py
git commit -m "feat: 权限矩阵快照与写入口（R3 红锁拒绝、写回出厂即删行、policy_change 审计）"
```

---

## 任务 5：三条 API 路由

**文件：**
- 修改：`LLM/server.py`（插在 `GET /api/policy/roles`（`:1224-1231`）之后、`Plan API / 护士台页面门` 段之前）
- 测试：`LLM/tests/test_permissions_api.py`（新建）

- [ ] **步骤 1：编写失败的测试**

创建 `LLM/tests/test_permissions_api.py`：

```python
# -*- coding: utf-8 -*-
"""权限矩阵路由测试（临时库 + TestClient，不进 lifespan）。"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

K = {"X-Surface": "kiosk"}
A = {"X-Surface": "admin"}


@pytest.fixture()
def c():
    from LLM.store import db
    from LLM.agent import session
    tmp = tempfile.mkdtemp()
    old = db.DB_PATH
    db.DB_PATH = os.path.join(tmp, "t.db")
    db.init_db()
    session.reset_for_test()
    db.set_admin_password("111111")
    import LLM.server as server
    server._login_fail.clear()
    client = TestClient(server.app)
    yield client
    server._login_fail.clear()
    db.DB_PATH = old


def _login(c, headers):
    return c.post("/api/session/login", json={"password": "111111"}, headers=headers).json()


def test_matrix_requires_admin_to_see_all(c):
    assert _login(c, A)["ok"] is True
    full = c.get("/api/permissions/matrix", headers=A).json()
    assert full["scope"] == "all" and set(full["roles"]) == {"ward", "elder", "admin"}
    self_only = c.get("/api/permissions/matrix", headers=K).json()
    assert self_only["scope"] == "self" and self_only["roles"] == ["ward"]
    assert "grants" not in self_only


def test_matrix_post_rejects_non_admin(c):
    r = c.post("/api/permissions/matrix",
               json={"changes": [{"role": "elder", "tool": "robot_move", "allowed": True}]},
               headers=K)
    assert r.status_code == 403


def test_matrix_post_applies_and_is_immediately_effective(c):
    _login(c, A)
    r = c.post("/api/permissions/matrix",
               json={"changes": [{"role": "elder", "tool": "robot_move", "allowed": False}]},
               headers=A)
    assert r.status_code == 200 and r.json()["ok"] is True
    from LLM.store import db
    assert db.get_role_grants() == {("elder", "robot_move"): False}
    row = [t for t in c.get("/api/permissions/matrix", headers=A).json()["tools"]
           if t["name"] == "robot_move"][0]
    assert row["overridden"]["elder"] is True and row["allowed"]["elder"] is False


def test_matrix_post_rejects_locked_but_keeps_others(c):
    _login(c, A)
    r = c.post("/api/permissions/matrix", headers=A, json={"changes": [
        {"role": "elder", "tool": "robot_stop", "allowed": False},
        {"role": "elder", "tool": "robot_move", "allowed": False}]})
    body = r.json()
    assert body["ok"] is False
    assert {(x["role"], x["tool"]): x["action"] for x in body["results"]} == {
        ("elder", "robot_stop"): "rejected", ("elder", "robot_move"): "set"}


def test_matrix_post_validates_payload(c):
    _login(c, A)
    assert c.post("/api/permissions/matrix", json={"changes": []}, headers=A).status_code == 400
    assert c.post("/api/permissions/matrix", json={"changes": [{"role": "x", "tool": "y",
                                                                 "allowed": True}]},
                  headers=A).status_code == 422


def test_permissions_reset(c):
    _login(c, A)
    c.post("/api/permissions/matrix",
           json={"changes": [{"role": "elder", "tool": "robot_move", "allowed": False}]},
           headers=A)
    assert c.post("/api/permissions/reset", json={"role": "elder"}, headers=A).json()["ok"] is True
    from LLM.store import db
    assert db.get_role_grants() == {}
    assert c.post("/api/permissions/reset", json={}, headers=K).status_code == 403
```

- [ ] **步骤 2：运行测试验证失败**

```powershell
.venv\Scripts\python.exe -m pytest LLM/tests/test_permissions_api.py -q -p mkdtemp_patch
```
预期：FAIL（404，路由不存在）

- [ ] **步骤 3：写实现**（插在 `LLM/server.py:1231` `policy_roles` 之后）

```python
# ---------------------------------------------------------------- 权限矩阵（规格 2026-09-26）
class PermissionChangeIn(BaseModel):
    role: str
    tool: str
    allowed: bool


class PermissionChangesIn(BaseModel):
    changes: list[PermissionChangeIn] = Field(min_length=1, max_length=200)


class PermissionResetIn(BaseModel):
    role: str = ""
    tool: str = ""


@app.get("/api/permissions/matrix")
async def permissions_matrix(x_surface: str = Header(default="kiosk")):
    """矩阵：admin 得三列全量；其他角色只拿自己那一列（写一律 403，见 POST）。"""
    from .agent import permissions
    role = session.get_principal(_surface(x_surface))["role"]
    snap = await asyncio.to_thread(permissions.matrix_snapshot)
    if role == "admin":
        return {"ok": True, "scope": "all", **snap}
    keep = [role] if role in snap["roles"] else ["ward"]
    return {"ok": True, "scope": "self", "roles": keep,
            "tools": [{**t,
                       "factory": {r: t["factory"][r] for r in keep},
                       "allowed": {r: t["allowed"][r] for r in keep},
                       "overridden": {r: t["overridden"][r] for r in keep},
                       "locked": {r: t["locked"][r] for r in keep}}
                      for t in snap["tools"]]}


@app.post("/api/permissions/matrix")
async def permissions_set(body: PermissionChangesIn, x_surface: str = Header(default="kiosk")):
    """改矩阵（**仅管理员**）。逐格独立提交，部分成功即部分生效；红锁格（R3）一律拒绝。"""
    from .agent import permissions
    slot = _surface(x_surface)
    principal = session.get_principal(slot)
    if principal["role"] != "admin":
        audit.log("policy_deny", action="permissions_matrix", slot=slot,
                  decision="deny", reason="admin_only")
        raise HTTPException(status_code=403, detail="仅管理员可修改权限矩阵")
    payload = [c.model_dump() for c in body.changes]
    for ch in payload:
        if ch["role"] not in permissions.ROLES:
            raise HTTPException(status_code=422, detail=f"未知角色：{ch['role']!r}")
    result = await asyncio.to_thread(permissions.set_grants, payload, principal)
    if not result["ok"]:
        # 与 /api/plans 的冲突口径一致：用 403 表达"有格被规则拒绝"，body 带逐格结果
        return JSONResponse(status_code=403, content=result)
    return result


@app.post("/api/permissions/reset")
async def permissions_reset(body: PermissionResetIn, x_surface: str = Header(default="kiosk")):
    """恢复出厂（**仅管理员**）：删覆盖行即回到 policy.py + conf 的默认。"""
    slot = _surface(x_surface)
    principal = session.get_principal(slot)
    if principal["role"] != "admin":
        audit.log("policy_deny", action="permissions_reset", slot=slot,
                  decision="deny", reason="admin_only")
        raise HTTPException(status_code=403, detail="仅管理员可恢复出厂权限")
    from .store import db
    removed = await asyncio.to_thread(db.clear_role_grants, body.role or "")
    audit.log("policy_change", action="reset", role=body.role or "*", tool=body.tool or "*",
              count=removed, by=principal["uid"], slot=slot, via="api")
    return {"ok": True, "removed": removed}
```

**实现注意：** `Body` 校验用既有 Pydantic 风格（本文件顶部已 `from pydantic import BaseModel, Field` 则直接可用；若 `Field` 未导入，改用 `changes: list[PermissionChangeIn]` 并在函数体内判空——两种都行，**别引入新依赖**）。

- [ ] **步骤 4：运行测试验证通过**

```powershell
.venv\Scripts\python.exe -m pytest LLM/tests/test_permissions_api.py LLM/tests/test_server_roles_routes.py -q -p mkdtemp_patch
```
预期：全绿

- [ ] **步骤 5：Commit**

```powershell
git add LLM/server.py LLM/tests/test_permissions_api.py
git commit -m "feat: 权限矩阵 API（GET/POST/reset，写仅管理员，红锁 403 + 审计）"
```

---

## 任务 6：admin「身份与权限」页改为可编辑矩阵

**文件：**
- 修改：`frontend/packages/admin/src/pages/RolesPage.vue`

**规格偏差（有意，复用优先）：** 规格 §6/§10 写的是"新增 `Permissions.vue`"，但仓库里已有 `RolesPage.vue`（页签 id `roles`、label「身份与权限」），它正是那张只读矩阵表，且页内文案写着"管理台只能看，不能改"。**改造它**：省一个页签、少一份重复代码，且口令设置/可调项继续留在同一页。

- [ ] **步骤 1：替换 `RolesPage.vue` 的矩阵段为可编辑表**

`<script setup>` 增加（保留原有口令设置与可调项逻辑不动）：

```ts
/** 矩阵一行的形状（后端 GET /api/permissions/matrix → tools[]）。 */
interface MatrixTool {
  name: string; server: string; local: boolean; orphan: boolean; switch_on: boolean;
  factory: Record<string, boolean>; allowed: Record<string, boolean>;
  overridden: Record<string, boolean>; locked: Record<string, boolean>;
}
const matrix = ref<MatrixTool[]>([]);
const matrixRoles = ref<string[]>([]);
const draft = ref<Record<string, boolean>>({});          // key = `${role}|${tool}`
const saving = ref(false);

function cellKey(role: string, tool: string) { return `${role}|${tool}`; }
function isDirty(role: string, tool: string) {
  return draft.value[cellKey(role, tool)] !== undefined;
}
function cellValue(role: string, tool: string, t: MatrixTool) {
  const k = cellKey(role, t.name);
  return draft.value[k] !== undefined ? draft.value[k] : t.allowed[role];
}
function toggle(role: string, tool: string, t: MatrixTool, next: boolean) {
  if (t.locked[role] || t.orphan) return;
  const k = cellKey(role, tool);
  const base = t.allowed[role];
  if (next === base) delete draft.value[k];              // 改回原值 = 不再是 diff
  else draft.value[k] = next;
  draft.value = { ...draft.value };                      // 触发响应式
}
async function loadMatrix() {
  const r = await req<{ ok: boolean; scope: string; roles: string[]; tools: MatrixTool[] }>(
    "/api/permissions/matrix");
  if (!r.ok || !r.data) { say(`❌ 读矩阵失败：${r.error}`, false); return; }
  matrix.value = r.data.tools;
  matrixRoles.value = r.data.roles;
  draft.value = {};
}
async function saveMatrix() {
  const changes = Object.entries(draft.value).map(([k, allowed]) => {
    const [role, tool] = k.split("|");
    return { role, tool, allowed };
  });
  if (!changes.length) { say("没有改动"); return; }
  saving.value = true;
  try {
    const r = await req<{ ok: boolean; results: any[] }>("/api/permissions/matrix", { changes });
    if (r.ok) say(`✅ 已保存 ${changes.length} 处改动（即时生效，无需重启）`);
    else {
      const bad = (r.data?.results ?? []).filter((x) => !x.ok)
        .map((x) => `${x.role}/${x.tool}(${x.reason})`).join("、");
      say(`❌ 部分被拒：${bad || r.error}`, false);
    }
    await loadMatrix();
  } finally { saving.value = false; }
}
async function resetMatrix() {
  if (!confirm("把全部人工改动恢复为出厂默认？（policy.py 的白名单与 conf 的 roles）")) return;
  const r = await req("/api/permissions/reset", {});
  if (r.ok) { say("✅ 已恢复出厂默认"); await loadMatrix(); }
  else say(`❌ ${r.error}`, false);
}
```

`load()` 里追加 `await loadMatrix();`。模板里把原来那张只读矩阵表换成：

```vue
<h4>权限矩阵（可编辑，即时生效）</h4>
<table>
  <thead>
    <tr><th>工具</th><th>来源</th>
      <th v-for="r in matrixRoles" :key="r">{{ r }}</th></tr>
  </thead>
  <tbody>
    <tr v-for="t in matrix" :key="t.name" :class="{ off: !t.switch_on, orphan: t.orphan }">
      <td class="mono">{{ t.name }}<span v-if="t.orphan" class="tag">已下线</span></td>
      <td class="mono">{{ t.server }}</td>
      <td v-for="r in matrixRoles" :key="r">
        <input type="checkbox" :checked="cellValue(r, t.name, t)"
               :disabled="t.locked[r] || t.orphan"
               :title="t.locked[r] ? 'R3：急停/呼救不受权限限制，不可取消'
                                   : (t.overridden[r] ? '已人工修改' : '出厂默认')"
               @change="toggle(r, t.name, t, ($event.target as HTMLInputElement).checked)" />
        <span v-if="t.locked[r]" class="lock">🔒</span>
        <span v-else-if="isDirty(r, t.name)" class="dirty">●</span>
        <span v-else-if="t.overridden[r]" class="over">已改</span>
      </td>
    </tr>
    <tr v-if="!matrix.length"><td :colspan="2 + matrixRoles.length">读不到矩阵</td></tr>
  </tbody>
</table>
<div class="row">
  <button :disabled="saving" @click="saveMatrix">保存改动</button>
  <button class="danger" @click="resetMatrix">全部恢复出厂</button>
</div>
<p class="hint">
  ● = 本次未保存的改动；「已改」= 已落库的覆盖；灰勾/灰空 = 出厂默认。
  整机开关关着的行（淡色）勾了也不生效，去「设置 → 工具」打开。
  急停/呼救（🔒）是红线 R3，任何层都不可取消。
</p>
```

样式段追加：

```css
tr.off { opacity: .55; }
tr.orphan td { color: #64748b; }
.tag, .over { font-size: 11px; color: #94a3b8; margin-left: 6px; }
.dirty { color: #fbbf24; margin-left: 6px; }
.lock { margin-left: 4px; }
input[type="checkbox"] { transform: scale(1.15); cursor: pointer; }
input[type="checkbox"]:disabled { cursor: not-allowed; }
```

并把页内那句"管理台只能看，不能改"的旧文案删掉，改为：
"三层能力有出厂默认（`LLM/agent/policy.py` + `LLM/conf.py` 的 MCP `roles`），下表是运行期覆盖层——勾选后**即时生效，无需重启**。"

- [ ] **步骤 2：构建验证**

```powershell
cd frontend; pnpm install; pnpm build:admin
```
预期：构建成功（`packages/admin/dist` 更新）。开发期也可 `pnpm dev:admin` 打开 http://127.0.0.1:5173 手工点两下：勾掉 `elder/robot_move` → 保存 → 刷新页面仍是勾掉状态。

- [ ] **步骤 3：Commit**

```powershell
git add frontend/packages/admin/src/pages/RolesPage.vue
git commit -m "feat: 身份与权限页改为可编辑权限矩阵（勾选即生效 + 恢复出厂）"
```

---

## 任务 7：文档同步

**文件：**
- 修改：`docs/superpowers/specs/2026-09-26-permission-matrix-design.md`（§4.1 顺序订正 + §6 页面偏差）
- 修改：`AGENTS.md`
- 修改：`docs/log.md`（按日期追加一条）

- [ ] **步骤 1：订正规格 §4.1 的 4/5 顺序**

把 §4.1 表格的 4/5 两行改成（理由：保持既有审计 reason 优先级不变，避免 `test_policy_tools.py` 的 reason 断言回归）：

```markdown
| 4 | **factory** | `policy.py` 白名单不含该工具 | `factory` | `out_of_role_whitelist` |
| 5 | **tool_roles** | 工具自身 roles 不含该角色（本地 `@tool(roles=…)` / MCP 服务器 `roles`） | `tool_roles` | `tool_roles_mismatch` |
```
并在表下补一句：`factory` 在 `tool_roles` 之前是**兼容性决定**（旧实现的 reason 优先级如此），不改变任何允许/拒绝结论。

- [ ] **步骤 2：订正规格 §6/§10 的页面偏差**

在 §6 开头补：`**实现偏差（2026-09-26）**：admin 端已有 RolesPage.vue（页签「身份与权限」）且正是这张只读矩阵，故改为**改造该页**，不新增 Permissions.vue；口令设置与可调项留在同页。`

- [ ] **步骤 3：`AGENTS.md` 同步（三处）**

1. `agent/policy.py` 那条后面补：`agent/permissions.py — **权限判定唯一入口**（lock→switch→matrix→factory→tool_roles）：出厂默认之上叠 role_tool_grants 覆盖层，管理端「身份与权限」页可勾选，改后即时生效；急停/呼救（LOCKED）任何层不可取消`；
2. 「关键约定」加一条：**权限判定只允许走 `permissions.decide()`**，`tools.py` 里不得再写第二份白名单/roles 判定；
3. admin 端页签列表里的「身份与权限」注明"含可编辑权限矩阵"。

- [ ] **步骤 4：`docs/log.md` 追加实现记录**（日期 `2026-09-26`，写清：动机、`role_tool_grants`、`decide()` 顺序与 reason 兼容性、R3 红锁、API、页面、测试数）

- [ ] **步骤 5：Commit**

```powershell
git add docs/superpowers/specs/2026-09-26-permission-matrix-design.md AGENTS.md docs/log.md
git commit -m "docs: 同步权限矩阵实现（判定顺序订正、页面复用偏差、AGENTS/log 更新）"
```

---

## 任务 8：收尾验证

- [ ] **步骤 1：跑全量相关测试**

```powershell
.venv\Scripts\python.exe -m pytest LLM/tests/test_permissions.py LLM/tests/test_permissions_api.py LLM/tests/test_policy_roles.py LLM/tests/test_policy_tools.py LLM/tests/test_server_roles_routes.py LLM/tests/test_settings_roles.py LLM/tests/test_worker_roles.py LLM/tests/test_session_roles.py LLM/tests/test_notice_mcp.py LLM/tests/test_plan.py LLM/tests/test_ward_autoswitch.py -q -p mkdtemp_patch
```
预期：全绿（基线 116 + 新增 ~27）

- [ ] **步骤 2：与真实运行态对账**

```powershell
.venv\Scripts\python.exe scripts/show_permissions.py --url http://127.0.0.1:8000 --audit 10
```
预期：`[2]` 段的每层生效工具与矩阵一致；`[7]` 段出现 `policy_change`（若本任务期间改过格子）。

- [ ] **步骤 3：端到端冒烟（人工）**

1. 打开 http://127.0.0.1:8000/admin → 登录 → 「身份与权限」；
2. 勾掉 `elder / robot_move` → 保存 → 页面显示「已改」；
3. 车前屏（`/kiosk`）选一位老人问"往前走一点" → 模型不应再调用 `robot_move`（工具不在 schema 里）；
4. 勾回 → 保存 → 再问一次 → 能调用（或至少模型知道有这工具）；
5. 点「全部恢复出厂」 → 矩阵回到灰勾/灰空。

- [ ] **步骤 4：最终 Commit**

```powershell
git status --short          # 确认只剩预期文件
git commit -am "chore: 权限矩阵 P1 收尾（验证记录）"   # 有残余改动才提交
```

---

## 自检记录（写计划时对照规格逐条核过）

| 规格条目 | 对应任务 |
|---|---|
| §3 数据模型（表 + 5 函数 + orphan 语义） | 任务 1、任务 4（orphan 展示） |
| §4 判定入口 + 顺序 + 热路径口径 | 任务 2、任务 3（`grants` 入口查一次） |
| §4.3 审计兼容（reason 不变 + 新增 source） | 任务 2（顺序）、任务 3（`run_tool`） |
| §4.4 快照 + §4.5 写入口 | 任务 4 |
| §5 三条 API + 鉴权 + 审计 | 任务 5 |
| §6 前端可编辑页（含锁/高危/三态） | 任务 6（偏差：改造 RolesPage.vue） |
| §7 红线 R1/R2/R3 + 审计 | 任务 2（LOCKED/回落）、任务 4（锁拒）、任务 5（403） |
| §8 十条测试 | 任务 1–5 累计 21 + 6 条 API 用例（覆盖 §8 全部十条） |
| §9 分期（P1 只工具维度） | 全计划范围内，未触碰 data_scope / Plan 白名单 |
| §10 改动清单 | 文件结构表逐项对应（页面一项按偏差改为 RolesPage.vue） |
| §11 边界（admin 全部语义 / orphan / 开关优先） | 任务 4（`cleared`/orphan）、任务 2（switch 先于 matrix）、任务 6（页面文案） |
