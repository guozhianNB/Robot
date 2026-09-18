# 交接文档：分层用户体系 P0（2026-09-14）

> **给接手的 AI/工程师**：这份文档是**唯一入口**。仓库 `D:\_project\Robot`（Windows）。
> 读完之后你应当能：① 知道这套东西是干什么的、边界在哪；② 知道已经落地了什么、验证到什么程度；③ 知道**工作区里有一份未验证的半成品**该怎么处理；④ 知道下一步做什么、怎么验证。
> 所有路径都是仓库相对路径。命令一律在**仓库根目录**执行，Python 用仓内虚拟环境 `.venv\Scripts\python.exe`。

---

## 0. 一页速览

| 项 | 状态 |
|---|---|
| 任务 | 实现《分层用户体系》规格的 **P0：管理层 / 集体层（病房）/ 老人层** 三层角色体系 |
| 规格 | `docs/superpowers/specs/2026-09-14-layered-user-roles-design.md`（**§14 = 二次复审结论 F1–F11；§15 = 实现落地说明与偏差**） |
| 计划 | `docs/superpowers/plans/2026-09-14-layered-user-roles.md`（15 任务 TDD + 文末「实现台账与偏差」） |
| 进度 | **15 个任务全部落地并逐个过审 + 整分支最终审查 + 修复浪潮已闭环** |
| 提交 | 计划 v2 基线 `6295df3` → 当前 HEAD `498488e`；**已推送到 `origin/main`**（`29fa314..498488e`） |
| 后端测试 | `LLM/tests` **231 passed**；根 `tests` **4 failed / 186 passed / 1 skipped**（那 4 个是**既有基线漂移**，与本批次无关，见 §5.3） |
| 前端验证 | **已真跑**：`pnpm test` → shared **15 passed**；`pnpm -r build` → admin / kiosk / mapeditor **三个包都构建成功** |
| ⚠️ 工作区 | **有 6 个文件的未提交改动（约 +135/−22）：是"遗留 Minor 收口"那一轮被中断留下的半成品，代码看着像写完了但【没有任何验证】** —— 见 §4，**先处理它** |
| 未完成 | ① 上面那份半成品的验证与提交；② P1（car MCP `robot_goto`/地点白名单/二次确认，**用户明确暂缓**）；③ 若干 Minor（§5.2）；④ 真机/用户侧验证（§5.4） |

---

## 1. 任务目标（要解决什么）

系统原本**只有"老人档案"一种主体**，没有账号/角色概念：当前是谁存在后端内存、全端共享；System Prompt 只有一份、所有人共用；工具开关是全局的；对外 REST 无鉴权（CORS 全开）。

本任务引入**三层角色 = 三套策略包**（提示词 + 工具白名单 + 数据可见范围 + 语音策略）：

| 层 | 角色 | 落地形态 |
|---|---|---|
| 管理层 | `admin` | 单管理员 + 口令（PBKDF2 入库），口令**可在 UI 改、可整体关闭**；不录声纹（避免误识别） |
| 集体层 | `ward` | **一个病房 = 一个"病房用户"**（`profiles.kind='ward'`，uid 形如 `ward_101`）；小车进病房与"大家"说话；**按小车位置自动切病房**；未识别/未登录的默认态就是它 |
| 老人层 | `elder` | 声纹识别或手动选人；可读**本病房**集体层的上下文（单向） |

用户原话（2026-09-14，规格 §1.2 有完整版）：
> 「我想创立一个管理员用户，与老人用户分开。前端可以切换。我的想法是分层级，比如管理员层，众人层（这个先放着不做），老人层，方便给每个层级的用户分配不同的权限和提示词。比如老人层可以使用声纹识别，发出简单的指令（比如让小车从病房出去），管理层可以发布所以指令」
> 「先不管mcp，先完成用户系统。还有，管理员的口令可以直接修改，甚至可以关闭。」
> 「前端换人按钮，按下后左侧显示当前层级（管理层 集体层 老人层）。**集体层**的设计是想让小车与"大家"对话……我想把同一个病房的老人打包成一个病房用户放在集体层，集体层的消息上下文也可以被老人用户读取到。」

**五条红线（改这块代码前必背，权威落点是 `AGENTS.md`「关键约定 7」）**

- **R1** 前端传上来的 `role` 一律不可信：uid→role 只由 `session.derive_role()` 按 `profiles.kind` 推导（不靠 uid 前缀）；拿 `admin` 只能走 `/api/session/login`（口令）。
- **R2** fail-closed：未知 uid/角色/会话过期 → 按**集体层**最小能力，绝不按 elder 放行。
- **R3** 急停/呼救永远放行（`POST /api/alarm` 任何角色都成功）；集体层白名单**必须**含安全工具。
- **R4** 医疗写入红线与角色无关（`memory.MEDICAL_KEYWORDS` 命中一律拒写）。
- **R5** 层级上下文单向：集体层对话对同病房老人可读；老人私聊**绝不**回灌集体层；跨病房不可读。

---

## 2. 已经做完的（架构 + 文件 + 契约）

### 2.1 后端新增模块

| 文件 | 职责 |
|---|---|
| `LLM/session.py` | 会话层：`derive_role()`（uid→role 唯一入口）、`get_principal(slot)`、`set_subject()`、`login_admin()/logout()`、`change_admin_password()/set_admin_auth()/ensure_admin_password()`、`current_ward()/manual_set_ward()/running_map_name()/autoswitch_state()`、`tick()`（每秒：admin TTL 降权 + 病房位置判定）。**双槽**：`kiosk`（车前/语音）与 `admin`（管理台）；`uid/locked/ward_uid` 全局共享，`role` 按槽隔离 |
| `LLM/policy.py` | 三角色策略包：`POLICY_DEFAULTS`（`prompt_file`/`allowed_tools`/`data_scope`/`ward_context`）+ `role_policy()`（未知角色 fail-closed 落 ward；**返回浅拷贝**防全局白名单被污染）。纯数据 + 纯函数、无 IO |
| `LLM/zonegeo.py` | 「点在不在区域内」：`point_in_polygon()`（射线法）+ `zone_hit()`（吃 `db.list_zones()` 的行；`shape='rect'` 用外接矩形；坏入参**绝不抛异常**）。口径与前端 `frontend/packages/mapeditor/src/lib/coords.ts` 一致 |
| `LLM/prompt/{ward,elder,admin}.md` | 三个角色提示词片段，由 `chat._load_role_prompt()` 按 `role_policy(role)["prompt_file"]` 装载并叠在 `prompt.md`（共用 base，**未改**）之上；缺文件 → 空串 + 审计 `prompt_role_missing`，不阻断对话 |

### 2.2 后端修改的模块

- `LLM/db.py`：`profiles` 加 `kind`/`ward_id`/`ward_map`/`ward_zone`（`kind` 默认 `'elder'`，旧行零迁移）；`list_profiles(kind=)`、`get_profile_kind`、`upsert_ward`、`set_ward_zone`、`set_profile_ward`、`list_wards`；管理员口令（PBKDF2-200k + 随机盐，存 `settings` 表 raw key `admin_password_*`，**且 `get_settings()` 会跳过这一族 key**，避免 `GET /api/settings` 泄露哈希）；`list_zones(..., kind=)`/`get_zone(uid, map_name="")` 向后兼容扩参。
  - **两条不可违反的约定**：① `upsert_profile` **不碰** `kind/ward_id/ward_map/ward_zone`（否则管理台编辑老人档案会静默清掉病房关联）；② `upsert_ward` 提权 `kind` 必须**直写** `SET kind='ward'`，**不许用 `COALESCE` 兜底**（旧行恒为 `'elder'`，兜底永不触发 → 静默失败）；三个字段统一"传空串 = 保持原值"（含 `name`）。
- `LLM/conf.py`：8 个新设置项 `admin_auth_required=True` / `admin_session_ttl_s=300` / `ward_context_window=10` / `ward_autoswitch_enabled=True` / `ward_switch_debounce=3` / `ward_zone_default_r=3.0` / `manual_override_sec=600` / `ward_map_source="auto"`。**注意 `current_map` 一期就有（不要重复加）；rosbridge 地址是 `conf.ROSBRIDGE_URL` 模块常量，不是设置项**。
- `LLM/chat.py`：`build_system/build_messages/chat_stream` 接**可选** `principal`（老签名仍可用）；`_load_role_prompt()`；`_ward_context()`（**只认真病房**：`db.get_profile_kind(ward_uid) == "ward"` 否则拒绝 + 审计）；数据注入口径 `data_uid = principal["uid"] or uid`；历史读写也认 `data_uid`（**别退回用请求体 uid**）。
- `LLM/tools.py`：`@tool(..., roles=None)`；`effective_tools(settings, principal=None)` = per-tool 开关 **∩** 角色白名单 **∩** 工具 `roles`（**白名单是天花板，只收窄不扩展**）；MCP 工具同样过滤（服务器 `roles` 未声明 = 仅 admin）；`run_tool(name, args, principal)` 入口二次校验 + `policy_deny` 审计（含 `decision`）。
- `LLM/memory.py`：`note_turn(..., role="elder")`，**非 elder/admin（含任何未知取值）一律不沉淀**。
- `LLM/voice_api.py` + `LLM/voice/worker.py`：会话持有权移交 `session`（`set_session_uid/get_session_uid` 同名保留）；`_apply_role_subject(recognized_uid)` 决定"这次说话算谁说的"——admin 早退（D8 不降权）/ 认出老人→切 elder / **没认出来→主体不动**；**解锁（`locked=False`）= 回当前病房集体层**；读库异常只落 `voice_error` 审计、不掐掉整句应答。
- `LLM/server.py`：新增 `/api/session/{login,logout,password,admin-auth,ward}`、`/api/wards`、`/api/wards/{uid}/zone`、`/api/profiles/{uid}/ward`、`/api/policy/roles`；`_surface()` 读 `X-Surface`（**非法值 400，不静默回落**）；`POST /api/session/user` 带 `role` 或 `uid="admin"` **一律 400**；`GET /api/session/user` 走 `asyncio.to_thread`；`GET /api/profiles` 默认只列 `kind='elder'`；`POST /api/settings` 的**特权键**（`admin_auth_required`/`admin_session_ttl_s`/`mcp_enabled`/`ward_autoswitch_enabled`）仅 admin 可改（403）；`lifespan` 补首启随机口令（打印 + 审计 `admin_password_generated`）、位置源不可用 WARN、**每秒 `session.tick()`**。

### 2.3 前端

- `frontend/packages/shared/`：`api/client.ts` 的 `apiGet/apiPost` 加**可选** `surface` 参数并带 `X-Surface`；`api/session.ts` 扩会话/登录/口令/病房 API；`events.ts` 加 `session_expired`/`admin_auth_changed`/`ward_changed` 三个事件并入 `KNOWN_TYPES`（**这是 SSE 事件的唯一事实来源**）。
- `frontend/packages/kiosk/`：`UserSwitcher.vue` 改成**左侧三组层级栏**（🛡 管理层走 `login()` 口令门；🏠 集体层病房列表；👴 老人层）；`VoiceStatusBar.vue` 按角色换徽标（含 admin 倒计时）；`App.vue` 统一 `loadSession()` + 监听三个事件。
- `frontend/packages/admin/`：`App.vue` 登录门（判据 `role !== 'admin'`，fail-closed）+ 无口令红条 + 两个新页签；新增 `pages/RolesPage.vue`（策略矩阵 + 改口令 + 开关口令门 + TTL/病房窗口）、`pages/WardsPage.vue`（病房 CRUD / 关联已画区域 / 记录当前房间为病房区域 / 老人归属）；`RegisterPage.vue` 加病房下拉（按床位号建议）。

### 2.4 整分支审查抓出并修掉的 3 条"任务级看不出的红线旁路"（`498488e`）

这三条是本次最值钱的产出，**回归它们就等于把安全边界拆了**：

1. `POST /api/settings` 无鉴权，而 `admin_auth_required` 是白名单键 → 一条未认证 POST 关掉口令门、再免口令进 admin → 整套角色保护归零（现已特权键 403 + 回归用例）。
2. `/api/chat` 的 `build_system` 已认 principal，但**滚动窗口历史读写**仍用请求体 `uid` → 传别人的 uid 就把别人私聊注入当前会话、还把回答写进别人历史（现已统一到 principal + 回归用例）。
3. `_post_chat_jobs` 的 `correct_instant` 无角色门 → 病房里的"不对/错了"能改写那位老人的**私有核心记忆**（现非 elder/admin 整条管线早退 + 回归用例）。

另修：worker 的 `locked_uid` 镜像会永久粘住（改为以会话层为唯一真相）；admin 对话页漏带 `X-Surface`。

---

## 3. 从任务级审查到整分支审查：本次验证到什么程度

| 验证层次 | 结果 |
|---|---|
| 每个任务的聚焦测试 | 每个任务都有 TDD 证据（RED→GREEN）与任务审查报告，见 `.superpowers/sdd/task-N-report.md` |
| 任务审查 | 15 个任务全部过审；任务 1/2/4/5/6/7/8/9/10 各有 1–2 轮修复 + 复审 |
| 端到端（本机，TestClient + 临时库 + 假位姿） | `.superpowers/sdd/task-15-acceptance.py`：**13/13 PASS**（三层可切 / 口令门与冷却 / 口令可改可关 / R1 400 / 集体层不含个人档案 / R5 单向 / 跨病房隔离 / 双槽隔离 / R3 报警放行 / 位置自动切病房 / 位置源不可用即降级 / 手动覆盖 / `import LLM.server` 成功） |
| 整分支最终审查 | 判定「修完再合」→ 修复浪潮 `498488e` 已闭环 |
| 后端全量 | `LLM/tests` **231 passed**；根 `tests` **4 failed / 186 passed / 1 skipped** |
| 前端 | `pnpm test` **15 passed**；`pnpm -r build` **三包全部成功**（admin 103.98 kB / kiosk 81.11 kB / mapeditor 118.80 kB） |
| 真机（rosbridge 真实位姿 / 真地图指纹 / 板卡） | **未做**，见 §5.4 |

> 完整审计轨迹（每个任务的分工、每轮审查的发现与处置、账目与 Minor 分诊）在 **`.superpowers/sdd/progress.md`**（约 53 KB，被 `.gitignore` 忽略但在本机可读）。**接手前建议先扫一眼**。

---

## 4. ⚠️ 工作区里的未提交半成品（**先处理这个**）

截至交接时 `git status` 有 6 个改动文件（`HEAD` 已是 `498488e`，**这些都还没提交**）：

```
 M LLM/db.py                                            +23/-..   (set_ward_zone/set_profile_ward 等返回 rowcount)
 M LLM/server.py                                        +60/-..   (历史读删认 principal + 新增 POST /api/session/ward)
 M LLM/tests/test_server_roles_routes.py                +35
 M LLM/tools.py                                         +17       (MCP 快照取一次 + mcp_enabled 总开关校验)
 M frontend/packages/kiosk/src/components/UserSwitcher.vue +16
 M frontend/packages/shared/src/api/session.ts           +6       (setWard)
?? .pytest-tmp/     ← 中断残留的测试临时目录，可直接删
?? LLM/vision_mcp/  ← 并行会话（另一路 vision 工作）的未跟踪目录，**不要碰**
```

**它是什么**：我派的"遗留 Minor 收口"实现子代理**在被中断前已把代码写进工作区**（所以文件是脏的），但**这一轮没有任何验证、也没有报告、更没有提交**。已确认写入的关键点（用 grep 核过）：

- `LLM/server.py:459` `raise HTTPException(400, "只能读取自己的会话历史")`、`:462` `audit.log("chat", action="history_read", ...)`、`:968` `@app.post("/api/session/ward")`。
- `LLM/tools.py:173` `snapshot = mcp_client.tools() ...`、`:185` `reason="mcp_disabled"`。
- `LLM/db.py:327/340/512/727` 若干 `return cur.rowcount`。
- `frontend/packages/shared/src/api/session.ts:51` `export function setWard(...)`。

**它打算做什么（目标，逐条）**：

1. **（最重要）堵 `GET|DELETE /api/chat/history` 的任意 uid**：现在非管理员可以 `?uid=<别人的 uid>` 读**或删**任意老人的私聊历史（CORS 全开、无主体校验）—— 与 `498488e` 修掉的两条是同一根因。目标：不带 uid → 读自己的；非管理员带别人的 uid → **400**；admin 带 uid → 允许并落审计。
2. **给"手动切病房"补 HTTP 入口**（`POST /api/session/ward`）：规格 D18 要求"手动切病房后 10 分钟内位置判定不覆盖"，但 `session.manual_set_ward()` 之前**没有路由**，kiosk 层级栏点病房走的是 `setSessionUser(uid, locked=true)`（= 锁定主体，语义不对）。配套：`shared` 加 `setWard()`，kiosk 的**病房行**改走它（**老人行仍用锁定切换**）。
3. **MCP 分支两处不对称**：`run_tool` 里 `mcp_client.tools()[name]` 裸下标 + 两次查表（竞态下 `KeyError` 可穿透对话链路）→ 取一次快照用 `.get()`；`run_tool` 不看 `mcp_enabled` → 总开关关掉后仍能被直调 → 补总开关校验。
4. **写关联时校验 uid 存在**：`db.set_ward_zone`/`set_profile_ward` 对不存在的 uid 是静默 no-op（REST 却报 ok）→ 返回 `rowcount` 并让路由在 0 行时给 `{"ok": false, "error": "uid 不存在：…"}`。

**接手后建议的动作（按顺序）**：

```powershell
# 1) 先看清这份半成品到底改了什么
git diff -- LLM/db.py LLM/server.py LLM/tools.py LLM/tests/test_server_roles_routes.py
git diff -- frontend/packages/shared frontend/packages/kiosk
# 2) 跑起来验证（未验证就是未验证，别假设它是对的）
.venv\Scripts\python.exe -m pytest LLM/tests -q
.venv\Scripts\python.exe -m pytest tests -q
cd frontend; pnpm test; pnpm -r build; cd ..
# 3) 通过 → 带 pathspec 提交；不通过 → 修好再提交，或者 git checkout -- <files> 丢掉重做
```
若决定**丢掉重做**，这 4 条要点就是我原本派单的完整内容，照 §4 的"它打算做什么"重写一遍即可（我上一轮就是按这个派的）。**无论哪条路，都要补回归用例**（历史读删的 400/200/200 三种情形、`/api/session/ward` 的覆盖语义）。

---

## 5. 还没做的

### 5.1 P1（**用户明确暂缓**，等 MCP 线重启）
`LLM/car_mcp/car_server.py` 新增 `robot_goto(destination)`（地名→坐标的换算放 LLM 侧，车端只收 x/y/theta；车端 `place` 字段当前明确拒绝）+ 地点白名单（真相在**地图文件夹的 `<图名>.tags.json`**，不是 `brain.db`）+ 风险分级 + 二次确认状态机 + admin「地点白名单」页签。规格 §6.3/§7 + 计划文末"P1 暂缓"一节。

### 5.2 其余 Minor（整分支审查已逐条分诊，详见 `.superpowers/sdd/progress.md` 与最终审查报告）
- `session._map_cache` 只按时间失效（窗口内改 `ward_map_source`/换图最长 30s 不生效）。
- `_ward_tick` 每 tick 每病房一次 `db.get_zone`（各自开 SQLite 连接）。
- `zonegeo` 对**非数值 x/y** 仍抛 `TypeError`（现由 `tick()` 的外层 try 兜住）；`test_zonegeo.py` 大整数断言形式偏弱。
- `db.set_admin_password` 的盐与哈希是两次独立事务（半写坏状态现已能自愈，但可收成一次事务）。
- `policy_deny` 审计的 `role` 记的是原始值（已补 `resolved_role`，可再用）。
- `test_policy_tools.py` 缺"白名单内 + 服务器 roles 排除"形状的 `run_tool` 用例。
- 前端 `shared` 的 `apiPost` 在非 2xx 时**丢错误体**（kiosk 的"记录位置"用同 URL 重发取 error 的绕法；根治是让 `request()` 读 `error/detail` 再抛）。
- kiosk 状态条老人显示 uid 而非昵称。
- 病房"解除区域关联"没有公开接口（只能 `db.set_ward_zone(uid,"","")`）。
- 规格 §12/§13 写"10 个新测试文件"，实际 **11** 个。

### 5.3 那两个"既有红态"**不是**本批次的问题，**不要修**
```
tests/test_modules_status.py::test_modules_status_shape                    # 模块集合没把 mcp 算进去
tests/test_unlock_switch.py::test_locked_ignores_voiceprint_switch         # VoiceWorker 旧签名 chat_fn（现签名是 stream_fn）
tests/test_unlock_switch.py::test_unlocked_switches_uid_and_broadcasts
tests/test_unlock_switch.py::test_unlocked_low_confidence_keeps_current
```
接手时的判据是"**失败集合仍是这 4 个**"，而不是"通过数等于某个数字"（并行会话会改动通过数）。

### 5.4 需要真机/用户侧做的验证
- **真机联调**：`rosbridge` 真实位姿 + 真地图指纹下的病房自动切换（P0 全部用 `locator.set_pose_for_test()` 假位姿验证过）。前置：板卡雷达/导航在跑（`~/tools/nav_screen.sh lat` 起 rosbridge :9090；`nav <图名>` 起导航），AMCL 初始位姿要对齐（此前 `/amcl_pose` 恒在 (0,0,0)）。
- **板卡不可达的前置阻塞**（历史遗留，与本批次无关但影响验收）：`ssh sunrise@100.65.82.93:22` 曾实测超时；`MAPS_IO=ssh` 的真实地图读写与地图编辑器真机验收都没做过。
- 浏览器级人工确认：admin 登录门、kiosk 三层级栏、病房管理页（本批次只做了接口级 + 构建级验证）。

---

## 6. 怎么验证（照抄即可）

### 6.1 后端
```powershell
.venv\Scripts\python.exe -m pytest LLM/tests -q
#   期望：全绿（基线 231 passed；若 §4 的半成品已提交，会更多）
#   注意：LLM/tests 里有一个既有偶发串扰（并发跑时偶尔 +1 失败），单跑可复现性更好
.venv\Scripts\python.exe -m pytest tests -q
#   期望：4 failed / 186 passed / 1 skipped，失败就是 §5.3 那 4 个（与本批次无关）
.venv\Scripts\python.exe -c "import LLM.server; print('import ok')"
#   期望：import ok（红线：可选依赖缺失也不许炸；新代码不得引入顶层硬 import 第三方）
```
起后端（不接 ROS/板卡也能起）：
```powershell
.venv\Scripts\python.exe -m uvicorn LLM.server:app --host 0.0.0.0 --port 8000
#   期望：启动日志里 [INFO] 已生成管理员初始口令：xxxxxx（首启）/ [WARN] 位置源不可用（…）
#   然后：GET /api/health → 200；GET /api/session/user -H "X-Surface: kiosk" → ok:true + autoswitch{enabled:false,reason:"no_pose"}
```

### 6.2 前端
```powershell
cd frontend
pnpm test            # 期望：shared 15 passed
pnpm -r build        # 期望：admin / kiosk / mapeditor 三包都 ✓ built
```
（这三个包此前**只在沙箱里做过 SFC 编译与 tsc 探针**；现在已在真实工具链跑通一次。以后每次改前端都请真跑这条。）

### 6.3 端到端（本机，不需要动车/板卡）
```powershell
.venv\Scripts\python.exe .superpowers\sdd\task-15-acceptance.py
#   期望：13/13 PASS（TestClient 不进 lifespan + 临时库 + 假位姿）
```
手写探针时的两个关键技巧：① `TestClient(LLM.server.app)` **不进 lifespan**（不拉起语音/rosbridge），所以要用 `db.DB_PATH` 指向临时库 + `db.init_db()` + `session.reset_for_test()`；② 病房位置判定用 `locator.set_pose_for_test(x, y, yaw)` 注入假位姿，并把设置改成 `ward_map_source="setting"` + `current_map="<图名>"`（这样不必依赖 `/map` 指纹与 SSH 地图）。

### 6.4 真机（需要板卡/车，动前确认现场安全）
见 §5.4。`POST /api/mapeditor/pose/inject` 可注入假位姿/假地图元数据；真地图上给病房建区域走 `/api/wards/{uid}/zone`（以当前位姿为圆心采样 16 边形）或到 `/mapeditor` 手绘。

---

## 7. 仓库与流程铁律（**踩过的坑，别再踩**）

1. **提交一律带 pathspec**：`git commit -m "..." -- <exact files>`。本仓同一工作区**有并行会话在改代码并提交**（vision 摄像头 / mapsources / 地图编辑器启动优化），索引里可能已有别人暂存的内容；裸 `git add <files>` + `git commit` 会把它们一起提交（本批次踩过一次：误含 49 个文件，只能 `git reset --soft HEAD~1` 重提）。
2. **审查包/对比区间要按路径限定**：`git diff -U10 <BASE>..<HEAD> -- <本任务文件>`。外部提交会插在你自己的提交之间，裸区间会夹带无关 diff。
3. **共享工作区跑全量出现"基线外失败"时不要去修**：先用临时 `git worktree` 复跑自己的基线，或在报告里同时给两个数字（本次并行会话一度让 `tests/test_vision.py`、`test_mapeditor.py` 抖动）。
4. **别修 §5.3 那 4 个既有红态**；判据是"失败集合没变"。
5. **可选依赖必须降级**：`numpy/sherpa-onnx/sounddevice/modelscope/torch` 等**不许**出现在 `server.py` 及后端导入链的顶层硬 import 里；`LLM.server` 必须能无条件 import、`lifespan` 必须能无条件启动（范例 `LLM/voice_api.py`）。
6. **SSE 事件两端强耦合**：后端 `bus.publish` 的事件名必须与 `frontend/packages/shared/src/events.ts` 的 `KNOWN_TYPES` 同步（**后者是唯一事实来源**）。本批次新增了 `session_expired` / `admin_auth_changed` / `ward_changed`（`user_changed` 只发 `uid/locked/source`，前端靠 `loadSession()` 补齐其它字段）。
7. **地图标记的唯一真相是地图文件夹的** `<图名>.tags.json`（`LLM/maptags.py` 是唯一读写入口）；`brain.db` 的 `zones`/`destinations` **只是只读索引缓存**，唯一写入口是 `maptags.sync_map()`。**病房↔区域关联记在 `profiles.ward_map` + `profiles.ward_zone`（区域 uid 形如 `z1`），不是 `zone_id`。**
8. **别把"候选路径"当事实**：`running_map_name()` 用 `/map` 四项指纹反查（`locator.current_map()`）；认不出就**不判命中**（fail-safe），`settings.current_map` 只在 `ward_map_source="setting"` 时当依据。tick 里**只读本地缓存**，不许调 `maptags.get_zones()`（它可能触发 SSH 同步）。
9. **工作纪律**：长任务要**中途给人话进度播报**（用户明确要求过两次）；有疑问先问一句带推荐项的选择题，不要硬猜；文档/记忆里的"经验规则"要用**源码 + 只读探针**复核再照做。

---

## 8. 上下文从哪来（接手必读顺序）

1. `AGENTS.md` 的「架构与模块（LLM/ 后端）」新条目 + **「关键约定 7」**（角色闸门与红线的权威落点）。
2. 规格 `docs/superpowers/specs/2026-09-14-layered-user-roles-design.md`：**§1 目标 / §2 决策 D1–D18 / §3 策略矩阵 / §4 会话与认证 / §5 提示词分层 / §9 数据与审计 / §11 验收 16 条 / §14 复审结论 F1–F11 / §15 实现落地说明**。
3. 计划 `docs/superpowers/plans/2026-09-14-layered-user-roles.md`：15 个任务的**完整代码与测试**（可作为"实现应该长什么样"的对照）+ 文末「**实现台账与偏差**」（实现期相对计划文本改了什么、为什么）。
4. `.superpowers/sdd/progress.md`：全程账本（每个任务的分工、每轮审查的发现与处置、Minor 分诊、批次收尾清单）。
5. `.superpowers/sdd/task-{1..15}-report.md`：各任务的实现报告（含 TDD 证据）；`.superpowers/sdd/final-fix-report.md`：最终修复浪潮。
6. `docs/log.md` 2026-09-14 条目：本批次的实现日志 + 13 条验收结果。

---

## 9. 已知风险清单（接手时的"雷"）

1. **§4 那 6 个文件的未提交改动是"未验证的代码"** —— 最大的雷。先看 diff、跑测试，再决定提交还是丢弃。
2. **并行会话**：同一工作区可能有别的 agent 在改 `vision/`、`mapstore.py`、`mapeditor`。动 `LLM/server.py`、`LLM/conf.py`、`AGENTS.md`、`docs/log.md` 前先 `git status`，提交一律带 pathspec。
3. **`LLM/session.py` 与 `LLM/voice/session.py` 同名不同物**：前者是角色会话层（本批次），后者是语音状态机（IDLE/LISTENING/SPEAKING，worker 里 import 为 `session_mod`）。新代码引用前者请用 `role_session` 别名。
4. **`db.get_zone(uid, map_name)` 与 `maptags.get_zone(map_name, uid)` 参数顺序相反**，别混用（取病房几何走 `db.get_zone`，那读的是缓存）。
5. **`_ensure_schema()` 每次命中缓存会做一次探活查询**（`db.get_profile_kind("__schema_probe__")`）—— 这是为了"库文件被删/换时不永久 500"，成本可忽略但要知道它在。
6. **`admin` 的 uid 是字面量 `"admin"`，不写进 `profiles`**：所以它不会出现在换人/层级列表里，也不会被声纹匹配到；`POST /api/session/user {"uid":"admin"}` 会 400（这是有意为之）。
7. **口令有两处"反直觉"的正确行为**：① 只要**已设过**口令，改口令就必须验旧口令——**与口令门开关无关**（堵"关门→改口令→开门永久占住"的链）；② 重开口令门会把两个槽的 admin 会话立即作废。

---

## 10. 一句话总结给接手者

**P0 的后端 + 前端 + 文档都已完成、已推送、已过整分支审查；你现在有三件事：① 处理工作区里那份"未验证的半成品"（历史读删认 principal / 手动切病房路由 / MCP 分支两处 / 写关联校验 uid）；② 按需推进 P1 或 §5.2 的 Minor；③ 补真机与浏览器级验证。改任何权限相关代码前，先把 `AGENTS.md`「关键约定 7」和 §1 那五条红线过一遍。**
