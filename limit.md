# 三层权限对照表（admin / ward / elder）

> 事实来源：`LLM/agent/policy.py`（策略包）、`LLM/agent/session.py`（角色推导与双槽会话）、`LLM/agent/tools.py`（工具闸门）、`LLM/agent/chat.py`（上下文注入）、`LLM/server.py`（接口闸门）。
> 设计依据：`docs/superpowers/specs/2026-09-14-layered-user-roles-design.md` §3.2/§3.3（能力矩阵）、§1.3（红线 R1–R5）。
> 口径：`ward` = 集体层（一屋子人 / 未识别默认态），`elder` = 老人层（= 你说的 "older"），`admin` = 管理层。

## 1. 主表：三层权限情况

| # | 维度 | `ward`（集体层） | `elder`（老人层） | `admin`（管理层） |
|---|---|---|---|---|
| 1 | **怎么进入这一层** | 会话主体是 `profiles.kind='ward'` 的 uid（如 `ward_101`）；**未知 uid / 空 uid 一律 fail-closed 落到本层**（R2） | `profiles.kind='elder'`（如 `elder_001`），声纹认出或手动切人 | 只能走 `POST /api/session/login`（口令）；uid 是字面量 `"admin"`，**不写进 `profiles`**。`POST /api/session/user` 带 `role` 或 `uid="admin"` 一律 400（R1） |
| 2 | **提示词片段** | `LLM/agent/prompt/ward.md` | `LLM/agent/prompt/elder.md` | `LLM/agent/prompt/admin.md` |
| 3 | **`allowed_tools`（工具白名单）** | `["robot_status", "robot_stop"]` | `["robot_status", "robot_stop"]` | `None` = 不按角色裁剪（仍受全局 per-tool 开关 ∩ MCP 服务器 `roles` 约束） |
| 4 | **`data_scope`** | `none`（**不注入任何老人档案**） | `self`（仅本人档案/RAG/语录/历史摘要） | `all`（全量接口；**不向模型注入老人记忆**） |
| 5 | **`ward_context`（本病房集体层上下文注入）** | ✅ 是（窗口 `ward_context_window`=10 条） | ✅ 是（只读，与本人档案叠加） | ❌ 否 |
| 6 | **读本病房集体层消息** | ✅ 本病房 | ✅ 本病房（只读、单向 R5） | ✅ 全部病房 |
| 7 | **读老人个人档案 / 记忆 / 画像 / 语录** | ❌ | ✅ 仅本人 | ✅ 全部（走接口，不进对话上下文） |
| 8 | **私聊回灌集体层** | —（不可能，单向 R5） | ❌ | ❌ |
| 9 | **对话沉淀成记忆**（`note_turn`/`consolidate`） | ❌ **整条管线早退**，不写成任何人的记忆 | ✅ 仅本人 | ✅ 本人（admin 槽） |
| 10 | **读审计 / 工具日志 / 系统状态** | ❌（模型侧无工具；接口侧见 §3 未收口） | ❌ | ✅ |
| 11 | **车 · 状态查询（位姿/电量）`robot_status`** | ✅ 只读播报 | ✅ | ✅ |
| 12 | **车 · 急停 / 呼救**（`robot_stop`、`POST /api/alarm`） | ✅ **永远放行（R3）** | ✅ 永远放行 | ✅ 永远放行 |
| 13 | **车 · 去白名单内地点** | ❌ | ✅ 设计允许（**暂缓 D16，未实现**） | ✅（**暂缓 D16**） |
| 14 | **车 · 任意坐标 / 跨区域** | ❌ | ❌ | ✅（暂缓） |
| 15 | **车 · 解除急停 / 改底盘参数** | ❌ | ❌ | ✅（暂缓） |
| 16 | **地点白名单管理** | ❌ | ❌ | ✅（暂缓 D16） |
| 17 | **层级切换 / 登录 / 登出** | ❌ | ❌ | ✅ |
| 18 | **改设置 / 改口令 / 开关口令门 / 关机** | ❌ | ❌ | ✅（`POST /api/settings` 的**特权键**：`admin_auth_required`、`admin_session_ttl_s`、`mcp_enabled`、`ward_autoswitch_enabled` 只有 admin 能写） |
| 19 | **读对话历史 `GET /api/chat/history`** | 只能读自己 uid（传别人的 uid 被忽略并回落本人） | 只能读自己 uid | ✅ 可读任意 uid（落审计 `by="admin"`） |
| 20 | **医疗信息写入（R4）** | ❌ `MEDICAL_KEYWORDS` 命中一律拒绝 | ❌ 同上，**与角色无关** | ❌ 管理员也不能通过对话写病历 |

## 2. 接口层闸门（P0 已收口的部分）

| 接口 | 闸门 |
|---|---|
| `POST /api/chat`、`GET\|DELETE /api/chat/history` | 角色只从 `session.get_principal(X-Surface)` 取；数据 uid **与权限同源**（不认请求体里的 uid） |
| `POST /api/settings` | 特权键需 admin，其余键任意端可写（现状） |
| `POST /api/session/password`、`/password/restore-factory` | 需 admin |
| `GET\|POST /api/session/admin-auth` | `POST` 需 admin（`GET` 任何角色可读） |
| `POST /api/wards`、`DELETE /api/wards/{uid}`、`POST /api/wards/{uid}/zone`、`POST /api/profiles/{uid}/ward` | 需 admin |
| `GET /api/policy/roles` | admin 拿全量矩阵；其它角色只拿到自己那一份摘要 |
| `GET /api/profiles` | 默认只列 `kind='elder'`（`?kind=all` 才全量，病房用户不混进老人列表） |
| `POST /api/alarm` | **不设闸门**，任何角色永远放行（R3） |
| `X-Surface` 请求头 | 只接受 `kiosk` / `admin`；缺头按 `kiosk`，**非法值显式 400**（不静默回落） |

## 3. 尚未按角色收口的接口（现状 = 不设闸门，勿当成"已授权"）

`/api/memories/*`、`/api/reminders/*`、`/api/tools`、`/api/tools/log`、`/api/voice/*`、`/api/face/status`、`/api/vision/*`、`/api/logs/warnings`、`/api/modules/status`、`/api/context`、`POST /api/system/shutdown` —— P0 只收了上表那几族，这些目前仅靠管理端 UI 与 `X-Surface` 语义区分，**接口本身不校验角色**。需要真正的权限边界时先补闸门。

## 4. 五条红线（三层之上，永远优先）

| 红线 | 内容 |
|---|---|
| **R1** | 前端传的 `role` 一律不可信；uid→role 只由 `session.derive_role()` 按 `profiles.kind` 推导（**不靠 uid 前缀**） |
| **R2** | fail-closed：角色未知 / uid 未知 / 会话过期 → 按 `ward` 最小能力，绝不按 `elder` 放行 |
| **R3** | 急停与呼救不受权限限制，三层永远放行 |
| **R4** | 医疗信息写入红线与角色无关，命中关键词一律拒绝 |
| **R5** | 层级上下文单向：集体层对同病房老人可读；老人私聊**绝不**回灌集体层；跨病房一律不可读 |

> 附注：`admin` 提权只走口令（TTL 默认 300s，到期自动降权到集体层）；`kiosk` 槽处于 admin 期间声纹认出老人**不降权**，只落审计 `voice_spk action=ignored_in_admin`（D8 提权只升不降）。
