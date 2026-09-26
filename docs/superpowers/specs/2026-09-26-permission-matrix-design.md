# 权限矩阵设计（分配器 = 可编辑能力矩阵 + 单一判定入口）

> 日期：2026-09-26 ｜ **状态：设计定稿（待实现，P1 未开工）** ｜ 范围：后端权限判定收敛 + admin 可写的矩阵 API + 管理端「权限」页
> 关联：`docs/superpowers/specs/2026-09-14-layered-user-roles-design.md`（三层体系与红线 R1–R5，本设计是它的**运行期可编辑化**，不改变三层语义）
> ｜ `docs/superpowers/specs/2026-09-18-llm-notify-nurse-mcp-design.md`（`notify_nurse` = 呼救族，本设计把它列入不可取消锁）
> ｜ `docs/参考资料/plan表设计-初版.md`（Plan 执行器白名单不在本设计范围内，见 §9）
> 用户原话（2026-09-26）：
> > 「我想制作一个分配器，所有的功能都向它报道，通过分配器统一调度权限。这样也方便直接人工勾选改权限层可以分配哪些权限」

## 1. 目标与背景

### 1.1 现状（本设计要取代的东西）

"某一层能不能用某个工具"目前是**三个代码常量的交集**，运行期只读、不可编辑：

| 判定要素 | 位置 | 语义 |
|---|---|---|
| 角色白名单（天花板） | `LLM/agent/policy.py::POLICY_DEFAULTS[role]["allowed_tools"]` | `None` = 不裁剪；`[]` = 一个都不给 |
| MCP 服务器角色门 | `LLM/conf.py::MCP_SERVERS[server]["roles"]` | **未声明 = `{"admin"}`**（`tools._mcp_roles()`，不受控外部能力默认从严） |
| 全局开关 | `settings["<工具名>_enabled"]`（本地）/ `settings["mcp_enabled"]`（MCP） | 与角色无关的整机开关 |
| 数据可见范围 | `policy.py` 的 `data_scope` / `ward_context` | 决定 RAG/档案注入，与工具无关 |

判定发生在两处，**规则重复实现**：

- `tools.effective_tools(settings, principal)`（`LLM/agent/tools.py:165`）—— 每轮对话算一次"模型看得见哪些工具"，`chat.py:583` 把它当 `tools=` 传给 LLM；
- `tools.run_tool(name, args, principal)`（`tools.py:231`）—— 调用时再校验一遍（模型凭缓存点名也拦得住），拒绝落 `policy_deny` 审计。

### 1.2 痛点

1. **权限是常量不是数据**：改权限 = 改代码 + 重启，没有 UI 可勾，也没有"谁改的"记录。
2. **真相分散在三处**：想给老人层开某个 MCP 工具，必须同时改 `policy.py`（白名单）与 `conf.py`（服务器 `roles`）——只改一处是静默无效（白名单是天花板；`roles` 只管收窄）。
3. **规则双重实现**：`effective_tools` 与 `run_tool` 各写一遍同样的交集，改一处漏一处就是"看得见调不动"或"看不见能调"。
4. **没有对外快照**：`GET /api/tools` 只报开关，`GET /api/policy/roles` 只报出厂白名单，**看不到"某层当前实际生效什么"**。

### 1.3 目标

把三道闸门收敛成**一个判定入口 + 一张可编辑矩阵**：

- **单一真相**：所有"某层能不能用某工具"的判断只经 `permissions.decide()`；
- **可编辑**：出厂默认（`policy.py` + `conf.py`）之上叠一层 DB 覆盖，管理员在管理端勾选、即时生效（不需重启）；
- **可审计**：每一格改动都落 `policy_change` 审计；
- **可解释**：每一格的"允许/拒绝 + 为什么"可查（`source` 字段）；
- **不动三层语义**：R1–R5 红线原样保留，急停/呼救仍是不可取消。

**非目标**：不重写工具注册与 MCP 桥（`@tool` 与 `MCP_SERVERS` 已经就是"功能向它报道"的机制）；不改 Plan 执行器白名单；不做多账号/多租户。

### 1.4 可编辑边界（防误读，先说清楚"表不是只读的"）

**本设计的矩阵是数据、是写入口、是管理端可手动编辑的表**——不是只读视图。`§1.1` 里那句"运行期只读、不可编辑"描述的是**今天**（权限写死在代码常量里），不是本设计的结论。

| 位置 | 规格依据 | 能否手动改 |
|---|---|---|
| admin 端「权限」页：每个工具 × 三个角色层 | §5.2 + §6 | ✅ **勾选即改**，提交后即时生效（无需重启） |
| 整机开关（`mcp_enabled` / `<工具名>_enabled`） | 既有设置/工具页 | ✅ 已有 |
| `robot_stop`、`notify_nurse` 在 `ward`/`elder` 两列 | D8 + §7 | 🔒 **只读** —— 全表唯一不可勾的格子（红线 R3：急停/呼救永不因权限被挡） |
| 非 admin 读矩阵接口 | §5.1 | 只回自己那一列（只读；写一律 403） |
| `GET /api/policy/roles` | §5.4 | 只读 —— "出厂默认是什么"的参照系，不是编辑入口 |
| Plan 执行器白名单（`tools._PLAN_ACTIONS`） | §9 | 不在范围（它不是"层"） |

除红锁与后三行外，**所有格子（含 `fetch_*` / `tavily-*` / 车控 goto / `see_what` / `create_plan`）都可由管理员在页面上勾选或取消**。用户在 2026-09-26 明确确认：矩阵全可编辑，R3 红锁保留。

## 2. 已确认决策记录

| # | 决策点 | 结论 | 理由 |
|---|--------|------|------|
| D1 | 架构形态 | **矩阵覆盖层**（出厂默认 ⊕ DB 差额），不重写 `tools.py` 主链路 | 改动集中在判定函数，回归面可控；默认值仍 fail-closed |
| D2 | 覆盖粒度 | **只存与出厂默认不同的格子**；删行 = 恢复默认 | 天然三态（默认允许/默认拒绝/人工改过），不会把"忘了写"变成"拒绝" |
| D3 | 存储 | SQLite 新表 `role_tool_grants`（`store/db.py`），不用 settings JSON 键 | 逐格 upsert 幂等；JSON 整份覆盖在并发下会互相吞 |
| D4 | 判定入口 | 新增 `LLM/agent/permissions.py::decide()`；`effective_tools`/`run_tool` 改调它 | 消除规则双重实现；留出 P2/P3 扩展缝 |
| D5 | `conf.MCP_SERVERS.roles` 的地位 | **降级为"出厂默认"**，可被矩阵覆盖（即管理员能把 `fetch`/`tavily` 勾给别的层） | 用户诉求就是"人工勾选改权限"；保留未声明=`admin` 作为出厂值 |
| D6 | 生效时机 | **不缓存**：每次判定读一次库（整表一次查询） | 勾选即生效、零一致性逻辑；SQLite/WAL 本机查询微秒级 |
| D7 | 读库异常 | **回落出厂政策** + 审计 `policy_deny(reason=matrix_unavailable)` | 绝不因读库失败而放行（fail-closed） |
| D8 | R3 锁 | `robot_stop`（急停）与 `notify_nurse`（呼救）在 `ward`/`elder` 两列**不可写、UI 只读**；红锁**不越过整机开关**（见 §4.1） | 规格 R3「安全动作永远放行」不能被一次误勾破坏；但运维把能力拔掉（`mcp_enabled=false`）是另一回事，不是权限分歧 |
| D9 | 并发控制 | **不做乐观锁**（`version`） | 逐格 upsert 天然幂等；冲突语义只是"最后一次勾选生效" |
| D10 | 页面归属 | 只做在 `frontend/packages/admin`；**改造既有的「身份与权限」页（`RolesPage.vue`）**，不新增页签 | 权限 = 管理动作，与既有「设置/工具」页同端；该页原本就是这张只读矩阵，复用优先（见 §12 实现台账偏差 1） |
| D11 | 可编辑性 | **矩阵全可编辑**（admin 端手动勾选并保存），唯一例外是 D8 的 R3 红锁 | 用户 2026-09-26 明确要求"admin 页面可以手动编辑"，并确认保留急停/呼救红锁（§1.4） |

## 3. 数据模型（`LLM/store/db.py`）

```sql
CREATE TABLE IF NOT EXISTS role_tool_grants (
  role       TEXT NOT NULL,          -- ward | elder | admin
  tool       TEXT NOT NULL,          -- 工具名（本地与 MCP 同一命名空间；重名本地优先，与既有注册表一致）
  allowed    INTEGER NOT NULL,       -- 1 = 允许，0 = 显式拒绝（显式拒绝必须能盖掉"出厂允许"）
  updated_at TEXT NOT NULL,          -- 与既有表一致的本地时间字符串
  updated_by TEXT NOT NULL,          -- 写入者（slot/uid），审计追溯
  PRIMARY KEY (role, tool)
);
```

建表沿用 `db.init_db()` 里现有的 `CREATE TABLE IF NOT EXISTS` 段落（`_migrate()` 风格），不新增迁移脚本。

db 函数（命名照既有规矩）：

```python
def list_role_grants() -> list[dict]                      # 全表（矩阵页用）
def get_role_grants() -> dict[tuple[str, str], bool]      # {(role, tool): allowed}，判定热路径用
def set_role_grant(role, tool, allowed: bool, by: str) -> None
def delete_role_grant(role, tool) -> int                  # 返回删除行数（0 = 本来就是默认）
def clear_role_grants(role: str = "") -> int              # 恢复出厂（role="" = 全部）
```

**遗留行（orphan）语义**：工具下线/改名后，矩阵里那一行仍存在，但 `decide()` 永远不会查到它。`matrix_snapshot()` 必须把这些行标成 `orphan: true` 显示出来，**并如实显示库里存了什么**（`allowed`/`overridden` 照实取覆盖表），以便一键清理 —— 否则矩阵页会长期展示一个不存在的工具、且管理员看不到自己留下的那条覆盖。

## 4. 判定入口（新增 `LLM/agent/permissions.py`）

```python
ROLES = ("ward", "elder", "admin")

# D8：R3 保护对象，写入口硬拒（UI 同步显示为只读）
LOCKED: dict[str, frozenset[str]] = {
    "ward":  frozenset({"robot_stop", "notify_nurse"}),
    "elder": frozenset({"robot_stop", "notify_nurse"}),
}

def normalize_role(role: str | None) -> str:
    """未知/None/空 → "ward"（R2 fail-closed），与 role_policy 同口径。"""

def factory_allows(role: str, tool: str, *, server: str = "", local: bool = True) -> bool:
    """出厂默认 = policy.py 白名单（None=全放）∩ 工具自身 roles。
    工具自身 roles：本地看 `@tool(roles=...)`（`_TOOL_REGISTRY[name]["roles"]`，None=不限），
    MCP 看服务器声明（`tools._mcp_roles(server)`，未声明 = {"admin"}）。
    本函数只服务快照/对账；实际判定按 §4.1 分步走，以便如实报告第一因。"""

def decide(principal: dict | None, tool: str, *, server: str = "", local: bool = True,
           settings: dict | None = None, grants: dict | None = None,
           ignore_switch: bool = False) -> dict:
    """唯一判定入口 → {"allow": bool, "reason": str, "source": str}

    `ignore_switch=True` 供矩阵页快照使用：跳过整机开关那一步，回答"若开关打开，这一层允许吗"。"""
```

### 4.1 判定顺序（任一环节拒绝即拒绝，`source` 如实上报第一因）

| 序 | 环节 | 条件 | `source` | `reason` |
|---|---|---|---|---|
| 1 | **switch** | 本地 `settings["<名>_enabled"]` 为假 / MCP `settings["mcp_enabled"]` 为假 | `switch` | `tool_disabled` / `mcp_disabled` |
| 2 | **lock** | `(role, tool)` 命中 `LOCKED` | `lock` | —（直接 allow） |
| 3 | **matrix** | 表里有 `(role, tool)` 行 | `matrix` | `matrix_deny`（`allowed=0`） |
| 4 | **factory** | `policy.py` 白名单不含该工具 | `factory` | `out_of_role_whitelist` |
| 5 | **tool_roles** | 工具自身 roles 不含该角色（本地 `@tool(roles=…)` / MCP 服务器 `roles`） | `tool_roles` | `tool_roles_mismatch` |

**三条顺序都是刻意的**（2026-09-26 实现期订正，见 §12 实现台账）：

1. **矩阵（3）先于出厂闸门（4/5）** —— 这正是"可覆盖"的含义；
2. **`switch`（1）先于 `lock`（2）与矩阵** —— 整机开关（`mcp_enabled` / `<工具>_enabled`）是**运维把能力拔掉**的开关，不是权限。R3 保护的是"身份/权限不能挡住急停与呼救"，不是"绕过全局 kill switch"；把开关放在最前，也让"开关关着 → 勾了不生效"有唯一解释（否则管理员会把开关问题误判成矩阵 bug）；
3. **`factory`（4）先于 `tool_roles`（5）** —— 兼容性决定：`tools.py` 旧实现的审计 reason 优先级就是"白名单先"（`out_of_role_whitelist if not allow_ok else tool_roles_mismatch`），换顺序会改动审计口径并让按 reason 聚合的既有用例回归；**允许/拒绝的结论完全相同**。

### 4.2 热路径性能口径（必须遵守）

`effective_tools()` 一轮对话要对 N 个工具判定：**在入口查一次 `db.get_role_grants()`，把 `grants` 传给每个 `decide()`**，禁止在 `decide()` 内部查库。`settings` 同样由调用方传入（`chat.py` 本来就有）。

### 4.3 与既有审计的兼容

`run_tool()` 现有的审计事件名、字段（`policy_deny` / `tool=… / role=… / resolved_role=… / uid=… / slot=… / decision=deny / args=… / reason=…`）**保持不变**，新增 `source` 字段。既有测试对 `reason` 的断言继续成立（`out_of_role_whitelist` / `tool_roles_mismatch` / `mcp_disabled` 三个字符串原样保留）。

### 4.4 快照（矩阵页与诊断用）

```python
def matrix_snapshot(settings=None, registry=None) -> dict:
    """→ {
      "roles": ["ward","elder","admin"],
      "tools": [{"name","server","local","switch_on","locked": {role: bool},
                 "factory": {role: bool}, "allowed": {role: bool},
                 "overridden": {role: bool}, "orphan": bool}],
      "grants": [{"role","tool","allowed","updated_at","updated_by"}],
    }"""
```

- `factory` = 出厂默认（不含矩阵，§4.1 的 4/5 两步）；
- `allowed` = `decide(..., ignore_switch=True)` 的结论，即**只看矩阵与出厂、不看整机开关**（"若开关打开会怎样"）；整机开关状态单列 `switch_on` 提示，避免管理员把"开关关着"误判成"矩阵没生效"；
- `overridden` = 该格有矩阵行。

### 4.5 写入口

```python
def set_grants(changes: list[dict], actor: dict) -> dict:
    """changes = [{"role","tool","allowed"}]; actor = principal（uid/slot）。
    逐格：角色非法 → 该格 ok:false；命中 LOCKED → 该格 ok:false（reason=locked）；
    与出厂一致 → 删行（回到默认，不留冗余）；否则 upsert。
    → {"ok": bool, "results": [{"role","tool","ok","action":"set|cleared|rejected","reason"}]}
    审计：每格成功改动的写一条 policy_change。"""
```

**调用方（路由）负责 admin 鉴权**，`set_grants` 只做数据合法性 + 锁判定（`decide()` 与 LOCKED 的真相在这一层，避免两处判断）。

## 5. API 契约（`LLM/server.py`）

### 5.1 `GET /api/permissions/matrix`

- 鉴权：`_surface(x_surface)` 取槽位；`get_principal(slot)["role"]`。
- admin → 返回完整快照；非 admin → **只返回自己那一列**（`factory`/`allowed`/`locked` 仅该列），`roles` 字段只含自己，另带 `"scope": "self"`。与既有 `GET /api/policy/roles` 的分级行为对齐。
- 响应：`{"ok": true, "scope": "all|self", "roles": [...], "tools": [...], "grants": [...]}`（`grants` 仅 admin 返回）。

### 5.2 `POST /api/permissions/matrix`

- 鉴权：非 admin → `403 仅管理员可修改权限矩阵` + 审计 `policy_deny(action=permissions_matrix, decision=deny)`。
- body：`{"changes": [{"role": "elder", "tool": "tavily-search", "allowed": true}]}`（1–200 格，超限 400）。
- 响应：`200 {"ok": true, "results": [...]}`；**逐格独立提交、不回滚已成功的格**（部分成功 = 部分生效），响应 `ok` 为"所有格都成功"的与运算，前端按 `results` 逐格标红。
- 审计：成功的每一格 `policy_change`（`actor_uid` / `actor_slot` / `role` / `tool` / `from` / `to` / `via="api"`）。

### 5.3 `POST /api/permissions/reset`

- 仅 admin；body `{"role": "elder"}` 或 `{"role": "elder", "tool": "tavily-search"}`（都不给 = 全部恢复）。
- 审计 `policy_change(from="matrix", to="factory")` 逐格或一条汇总（实现取汇总一条，字段含 `role`/`tool`/`count`）。

### 5.4 不改动的东西

`GET /api/policy/roles` 语义不变（出厂政策视图，用于对照"默认是什么"）；`GET /api/tools` 不变（整机开关视图）。三个视图分工写进管理端页面文案。

## 6. 前端（`frontend/packages/admin`）

**「身份与权限」页里的矩阵是一张可编辑表**（不是只读报表）：勾选 → 只提交 diff → 后端逐格落库 → 即时生效；刷新后能看到自己改过的格子（「已改」）与出厂默认（灰）的差别。

> **实现偏差（2026-09-26）**：仓库里已有 `packages/admin/src/pages/RolesPage.vue`（页签「身份与权限」），它原本就是这张只读矩阵（且页内文案写着"管理台只能看，不能改"）。故**改造该页**，不新增 `Permissions.vue`、也不新增 `shared/src/api/permissions.ts` —— 该页已经有一套裸 `fetch`（`req()`）来处理"非 2xx 时保留响应体 error/detail"的需求，矩阵的 403（红锁逐格结果）正好吃这套，复用即可。

- 布局：表格，**行 = 工具**（按 `server` 分组：本地 / 车 / 通知 / 视觉 / 联网 / 抓取），**列 = ward / elder / admin** 三个勾选框。
- 三态渲染：
  - 灰勾 = 出厂允许；灰空 = 出厂拒绝；**实心 + 「已改」** = 人工改过；**●** = 本次未保存的 diff；
  - 开关关闭（`switch_on: false`）的整行淡色；已下线（`orphan`）的行灰显且不可勾。
- 🔒 锁：`locked` 为真的格子 `disabled` + tooltip「R3：急停/呼救不受权限限制，不可取消」。
- ⚠️ 高危二次确认：勾**开启**出厂对本层拒绝的格子时 `confirm()` 提示（典型场景：把 `fetch_*` / `tavily-*` 这类联网/抓取工具放开给老人层）。
- 保存：只提交 diff（改动的格子）；成功后重拉矩阵并提示；失败时按 `results` 把被拒的格子写进提示（含"R3 红锁"文案）。
- 「全部恢复出厂」= `POST /api/permissions/reset`。
- `AGENTS.md` 的管理端页签列表注明该页含可编辑矩阵。

## 7. 红线与安全

| 红线 | 本设计的落实 |
|---|---|
| R1 前端 role 不可信 | 矩阵读写都只从 `X-Surface` → `get_principal()` 取角色；请求体里的 role 只作"要改哪一列"的数据，**改哪一列由 admin 身份决定**（非 admin 只能读自己那列、一律不能写） |
| R2 fail-closed | 未知角色/未知工具/表里没记录 → 落出厂政策（最保守）；读库异常 → 出厂政策 + 审计 |
| R3 急停/呼救 | `LOCKED` 硬编码 + 写入口硬拒 + UI 只读；不因矩阵/白名单/服务器 roles 而失效；**但服从整机开关**（`mcp_enabled=false` 时车控/通知子进程根本没起，那是运维开关不是权限，见 §4.1） |
| R4 医疗红线 | 不在本设计范围（`memory.MEDICAL_KEYWORDS` 不变） |
| R5 上下文单向 | 不在本设计范围（`data_scope` 属 P2） |
| 审计 | 每一格改动 = 一条 `policy_change`；每一次越权 = 既有 `policy_deny`（新增 `source`） |

**明确不做**：`LOCKED` 不放进 DB（否则"能改锁的人"= 能绕过 R3）；不放宽 admin 之外的角色写矩阵。

## 8. 测试清单（新增 `LLM/tests/test_permissions.py`）

1. **对账**：空表时 `matrix_snapshot()["allowed"]` 逐格等于 `decide()` 等于今天的 `effective_tools` 行为（防"改了默认忘了矩阵"）。
2. **覆盖放行**：给 `elder` 写 `tavily-search=1`（同时 `conf` 不动）→ `effective_tools` 可见 + `run_tool` 放行。
3. **显式拒绝盖掉出厂允许**：给 `admin` 写 `robot_move=0` → `effective_tools` 不含它、`run_tool` 拒绝（reason 落 `matrix_deny`）。
4. **R3 锁**：`set_grants` 对 `ward/elder × robot_stop|notify_nurse` 逐格 `ok:false` 且 `action=rejected`；`decide()` 在整机开关打开时仍 allow（开关关闭时由 `switch` 拦下，这是 D8 的边界）。
5. **fail-closed**：未知角色（`"??"`）、未知工具 → 拒；`db.get_role_grants` 抛异常 → 回落出厂 + 审计。
6. **鉴权**：非 admin `POST /api/permissions/matrix` → 403 + `policy_deny`；admin → 200 + `policy_change`（含 from/to）。
7. **即时生效**：写完立刻 `effective_tools` 生效（无缓存断言）。
8. **幂等与清理**：写 `allowed == 出厂值` → 行被删除（`action=cleared`）；`reset` 后回到对账态。
9. **orphan**：库里塞一个注册表不存在的工具名 → `matrix_snapshot` 标 `orphan: true`。
10. **回归**：`test_policy_roles.py` / `test_policy_tools.py` 全绿（出厂默认仍是唯一真相；未 monkeypatch 矩阵时行为与今天一致）。**一处有意的语义调整**：`test_policy_tools.py::test_run_tool_denies_whitelisted_tool_excluded_by_server_roles` 与 `test_notice_mcp.py` 里两条以 `robot_stop` / `notify_nurse` 当"可被服务器 roles 拒掉"样本的用例，因两者进入 `LOCKED` 而改为用 `see_what` 做探针、或断言"红锁不可被 roles 收窄"（见 §12 实现台账偏差 2）。

## 9. 分期

- **P1（本设计范围）**：`role_tool_grants` 表 + `permissions.py` + 两处判定改调 `decide()` + 三条 API + 管理端「权限」页 + 审计 + 锁 + 上述测试。
- **P2**：把 `data_scope` / `ward_context` 纳入同一矩阵（行从"工具"推广为"能力"，`decide()` 增加 `capability` 维度）。
- **P3**：规格 §7 的动作分级（`policy.check_action()` 二次确认，如"老人层去某地点需确认"）挂到同一个 `decide()` 上，与工具可见性共用一张表。
- **不在范围**：Plan 执行器白名单（`tools._PLAN_ACTIONS` / `run_plan_tool`）——它不是"层"，继续独立；`/api/plans*` 与 `/api/notifications` 的免鉴权边界由 `docs/参考资料/plan表设计-初版.md` 决定，本设计不动。

## 10. 改动清单（实现时逐文件）

| 文件 | 改动 |
|---|---|
| `LLM/store/db.py` | 新表 + 5 个 `*_role_grant*` 函数（§3） |
| `LLM/agent/permissions.py` | **新增**：`ROLES` / `LOCKED` / `decide()` / `matrix_snapshot()` / `set_grants()` / `normalize_role()` / `factory_allows()`（§4） |
| `LLM/agent/tools.py` | `effective_tools()` / `run_tool()` 改调 `decide()`（入口查一次 `grants`）；审计加 `source`；**签名与既有 reason 字符串不变** |
| `LLM/server.py` | 新增 §5 三条路由（矩阵 GET/POST、reset），插在 `GET /api/policy/roles` 之后 |
| `frontend/packages/admin/src/pages/RolesPage.vue` | **改造**：只读矩阵表 → 可编辑矩阵（不新增页面、不新增 shared 契约，见 §6 偏差说明） |
| `LLM/tests/test_permissions.py` | **新增**（§8 十条，19 条用例） |
| `LLM/tests/test_permissions_api.py` | **新增**（路由：鉴权 / 逐格结果 / 重置，6 条用例） |
| `AGENTS.md` | `permissions.py` 条目 + 管理端页签列表 + 「关键约定」补一条"权限判定只有 `permissions.decide()` 一个入口" |
| `docs/superpowers/specs/2026-09-14-layered-user-roles-design.md` §3.3 | 加一句：能力矩阵的**出厂默认**见 `policy.py`，运行期可由 admin 通过权限矩阵覆盖（本设计） |
| `docs/log.md` | 追加 2026-09-26 实现记录 |

## 11. 边界与未决

1. **`admin` 列的"全部"语义**：出厂 `allowed_tools=None`（不裁剪）。矩阵对 admin 只可能"显式拒绝"才有意义；写 `allowed=1` 等价默认，`set_grants` 按 D2 删行处理（`action=cleared`）。
2. **`robot_status`（只读播报）不在 `LOCKED`**：R3 只保护急停/呼救，管理员若把状态播报从某层拿掉是合法配置（默认仍全层允许）。
3. **工具新增**：新工具无需任何矩阵操作，自动按出厂默认出现在矩阵页（`matrix_snapshot` 从注册表拉行）。
4. **MCP 服务器整台下线**：其工具从注册表消失 → 矩阵行变 `orphan`，页面提示可清理，不自动删除（保留管理员意图，重新上线即恢复）。
5. **矩阵与"整机开关"的优先级**：开关关 > 矩阵勾（§4.1 顺序），页面必须有文案说明，否则会被当成 bug。

## 12. 实现台账与偏差（2026-09-26 P1 落地）

实现计划：`docs/superpowers/plans/2026-09-26-permission-matrix.md`（8 任务 TDD）。分支 `feature/permission-matrix`，逐任务 commit。

| # | 偏差 / 决定 | 说明 |
|---|---|---|
| 1 | **前端改造 `RolesPage.vue`，不新建页** | 规格 §6/§10 原写"新增 `Permissions.vue` + `shared/src/api/permissions.ts`"；实际仓库里已有同样内容的只读页。复用它的裸 `fetch`（`req()`）恰好满足"403 要拿到逐格 results"的需求，省一个页签、少一份重复代码。 |
| 2 | **判定顺序订正为 `switch → lock → matrix → factory → tool_roles`** | 原设计把 `lock` 放第一位。实现期发现两条后果：① 红锁会让"急停"穿过 `mcp_enabled=false`（全局 kill switch），而 `mcp_enabled=false` 时车控/通知子进程根本没起，语义上说不通；② `factory` 与 `tool_roles` 的顺序会改变 `policy_deny` 的 `reason`（旧实现是"白名单先"），影响按 reason 聚合的既有用例。故按 §4.1 现在的顺序定稿。 |
| 3 | **`notify_nurse` 纳入 R3 红锁的连带影响** | 它原本可被"服务器 roles 收窄"拒掉。进红锁后，`test_notice_mcp.py` 里那条用例改为断言"红锁不可被 roles 收窄"；`test_policy_tools.py` 里用 `robot_stop` 当"可被服务器 roles 拒"的探针改为 `see_what`（它同样在 elder 白名单内且不在红锁里）。**生产配置不变**（`notice` 声明了三层 roles）。 |
| 4 | **orphan 行如实显示覆盖** | 已下线工具的遗留行不仅标 `orphan`，还照实回显 `allowed`/`overridden`，否则管理员看不到自己留下的那条覆盖、也无从清理。 |
| 5 | **顺手修掉一处既有的测试顺序依赖** | `test_chat_text_tts.py` 会因 `test_ward_autoswitch.py` / `test_worker_roles.py` 遗留的 kiosk 会话状态而失败；已在基线 worktree（`f0dcb17`）复现，与本设计无关。补一个 `session.reset_for_test()` 的 autouse 隔离夹具。 |
| 6 | **`switches` 未纳入矩阵** | 整机开关仍在设置页（`<工具名>_enabled` / `mcp_enabled`），矩阵只管"层 × 工具"。两者优先级在 §4.1 与页面文案里写明。 |

