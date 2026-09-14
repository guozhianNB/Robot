# 分层用户体系设计（管理层 / 集体层 / 老人层）

> 日期：2026-09-14（含当晚用户追加需求：集体层=病房用户、口令可改可关、MCP 暂缓）｜ **2026-09-14 二次复审修订**（结论与逐条处置见 **§14**：修正与代码现状的 7 处冲突——病房区域几何载体、位置源配置项、`locator.py` 已存在、`profiles` 关联列、地点真相载体、`db` 既有函数签名、测试基线）｜ 状态：已复审，P0 待实施 ｜ 范围：后端会话与权限层 + 提示词分层 + 前端层级切换 + 集体层（病房用户）
> P0 实现计划（可直接照着写代码的那一份）：`docs/superpowers/plans/2026-09-14-layered-user-roles.md`（2026-09-14 二次修订版，15 任务 TDD）。
> 关联：`docs/superpowers/specs/2026-08-27-frontend-multi-end-design.md`（D11 手动选择+锁定模式，本设计在其上扩展「角色」维度）、
> `docs/superpowers/specs/2026-08-22-voice-pipeline-design.md`（声纹识别产出 uid，本设计消费它）、
> `docs/superpowers/specs/2026-08-24-elder-registration-flow-design.md`（老人档案与声纹注册，本设计在同一张 `profiles` 表上扩展 `kind='ward'`）、
> `docs/目标文档及说明/大模型端开发目标.md` 模块 11（报警，本设计明确「急停不受权限限制」）

## 1. 目标与背景

### 1.1 要解决的问题

现状：系统里**只有「老人档案」一种主体**，没有任何账号或角色概念。

- 「当前是谁」存在后端内存（`voice_api._session_uid` + `_session_locked`），全端共享；前端只负责显示，后端**不校验任何身份**。
- System Prompt 只有一份 `LLM/prompt.md`（人设+红线），老人、护士、访客拿到的是同一套人设。
- 工具开关（`tools.py` 的 `<工具名>_enabled`）是**全局**的，不区分谁在说话——「谁能指挥小车」目前只能整机开或整机关。
- 对外的 REST 无鉴权（CORS 全开），任何人打开 `/admin` 就是管理员。

结果：老人和护士的操作能力无法区分，也没法为不同角色写不同的说话方式。

### 1.2 目标

引入**角色分层**，每一层 = 一套策略包（提示词 + 可用工具 + 动作约束 + 数据可见范围 + 语音策略）：

| 层 | 角色 | 本期状态 |
|---|---|---|
| 管理层 | `admin` | **本期实现**（口令登录；口令可在 UI 直接改、也可整体关闭；护士站/家属用） |
| 集体层 | `ward` | **本期实现**（**同一个病房的老人打包成一个「病房用户」**，uid 形如 `ward_101`；小车进病房与"大家"打招呼、公布消息；**按小车位置自动切到所在病房**，见 §4.5；未识别/未登录时的默认态也是它） |
| 老人层 | `elder` | **本期实现**（声纹识别 + 手动选人；可读取**本病房**集体层的消息上下文） |

用户原话（2026-09-14）：
> 「我想创立一个管理员用户，与老人用户分开。前端可以切换。我的想法是分层级，比如管理员层，众人层（这个先放着不做），老人层，方便给每个层级的用户分配不同的权限和提示词。比如老人层可以使用声纹识别，发出简单的指令（比如让小车从病房出去），管理层可以发布所以指令」
> 「挂人身份上。但不要声纹，管理员是特殊账号，以免误识别。但语音链路要保留，以实现管理员也可以语音控制」
> 「先不管mcp，先完成用户系统。还有，管理员的口令可以直接修改，甚至可以关闭。」
> 「前端换人按钮，按下后左侧显示当前层级（管理层 集体层 老人层）。**集体层**的设计是想让小车与"大家"对话。比如小车进入某个病房，与大家打招呼，公布消息之类的。然后就可以根据声纹切换到特定老人与他对话。我想把同一个病房的老人打包成一个病房用户放在集体层，集体层的消息上下文也可以被老人用户读取到。」

### 1.3 关键架构原则（继承且强化）

交互闭环在后端（见前端多端设计 D4）：**权限判定必须全部发生在后端**。前端只做两件事——显示当前身份、发起「以某身份登录」的请求。

> **红线 R1：前端传上来的 role 一律不可信。** `/api/chat`、`/api/voice/*`、`/api/tools` 等业务接口**不接受** role 参数。前端切主体只传 **uid**，role 由后端按主体类型推导（`admin`/`ward_*`/`elder_*` → `admin`/`ward`/`elder`，见 §4.4）；要拿 `admin` 只能走 `/api/session/login`（口令）或 `/api/session/logout`。

> **红线 R2：fail-closed。** 角色未知、会话过期、字段缺失时，按**最保守**处理（回落到**集体层**的最小能力集：可闲聊、可播报状态，但**不注入任何老人的个人档案/私人记忆**、无车控工具），绝不按 `elder` 放行。

> **红线 R5：层级之间的上下文单向流动。** 集体层（病房）的对话**对同病房老人可读**；老人与小车的一对一私聊**绝不回灌**集体层（隐私）。跨病房一律不可读。

> **红线 R3：急停与呼救不受权限限制。** `POST /api/alarm`、`/robot/cmd_stop` 对任何角色永远放行——安全动作不能因为没登录而被挡住。

> **红线 R4：医疗信息写入红线不变。** `memory.MEDICAL_KEYWORDS` 命中一律拒绝写入，与角色无关（管理员也不能通过对话写入病历）。

## 2. 已确认决策记录

| # | 决策点 | 结论 |
|---|--------|------|
| D1 | 管理员认证强度 | **轻量口令门**：单管理员账号 + 口令（PBKDF2 哈希入库），不做用户名/注册/多账号。将来要扩多账号时平滑加 `accounts` 表 |
| D2 | 管理员是否录声纹 | **不录**。管理员是特殊账号，避免被别的老人声音误识别；身份由口令登录产生 |
| D3 | 语音链路 | **保留不变**。管理员登录后，车前语音输入按当前会话角色处理（角色是会话态，不靠声音判定） |
| D4 | 权限挂载点 | **挂人（身份）**，不挂页面、不挂 IP。页面只是显示层 |
| D5 | 老人层动作力度 | **地点白名单 + 分级**：老人只能说预设地点名；低风险直接执行，高风险（离房/跨区）加一句口头二次确认；任意坐标/改参数只有管理员能下 |
| D6 | 集体层 | **本期实现**：一个病房 = 一个「病房用户」（`profiles` 表里 `kind='ward'`，uid=`ward_101`）。**复用现有 uid 机制**——对话历史 `db.load_history(uid)` 与记忆天然按 uid 隔离，集体层上下文不需要新建一套存储；未识别/未登录的默认态就是集体层 |
| D7 | 会话粒度 | **双槽会话**：`kiosk` 槽（车前设备，承载语音）与 `admin` 槽（管理台）各自持有 role；`uid`/`locked` 仍全局共享（沿用现状，切人两端同步） |
| D8 | 管理员会话时效 | 默认 **5 分钟**（`admin_session_ttl_s` 可配），无操作自动降权回**集体层**并广播 `session_expired`；提权**只升不降** |
| D9 | 提示词分层方式 | 保留 `LLM/prompt.md` 作**共用 base**（不搬文件，零破坏），新增 `LLM/prompt/<role>.md` 角色片段，运行时拼接 |
| D10 | 地点坐标来源 | 复用现有 car MCP `robot_status` 读位姿：管理员在管理台点「记录当前位置为『护士站』」，后端落 `destinations` 表；**地名→坐标的唯一事实来源在 LLM 侧**，车端只收 x/y/theta（车端 `place` 字段当前明确拒绝，见 §7.2） |
| D11 | 二次确认实现位置 | **后端状态机**（会话内挂 `pending_action`，TTL 30s），不依赖前端——无屏也能用 |
| D12 | 口令初始值 | 首次启动若无 `ADMIN_PASSWORD` 且库内无哈希 → 生成 6 位随机口令，打印到后端终端 + 落审计（`admin_password_generated`），并要求首次登录后修改 |
| D13 | 口令可维护性 | 口令**可在 UI 直接修改**（`POST /api/session/password`，需旧口令），也可**整体关闭**（`admin_auth_required=False`）：关闭后从层级栏选「管理层」即直接进入 admin，UI 显式警示「当前无口令保护」+ 审计 `session_login source=auth_disabled`。默认 `True`（有保护） |
| D14 | 集体层 ↔ 老人层上下文 | **单向可读**（红线 R5）：老人可读**本病房**集体层最近 N 条；私聊不回灌集体层；跨病房不可读。老人→病房归属由 `profiles.ward_id` 决定 |
| D15 | 前端层级切换 | 「换人」按钮 → **左侧层级栏**列出 管理层 / 集体层 / 老人层 及其主体；前端只传主体 uid，**role 由后端推导**（守住 R1） |
| D16 | 本轮范围 | 用户 2026-09-14 明确「**先不管 mcp，先完成用户系统**」：本轮只做 §10 的 P0（角色地基 + 集体层 + 提示词分层 + 层级 UI + 病房自动切换）；依赖 car MCP 的**地点白名单 / `robot_goto` / 二次确认**（原 P1）**暂缓**，待 MCP 线重启再做 |
| D17 | 「当前在哪个病房」 | **按小车位置自动切**（用户 2026-09-14 选定 B）：取小车在当前地图中的位姿 → 命中病房区域（`<图名>.tags.json` 里 `kind='ward'` 的多边形，米坐标，**点在多边形内**判定；`brain.db.zones` 只是只读索引缓存）即为当前病房 → 跨病房自动切集体层。位置源走 **rosbridge（websocket，非 MCP）**，与 D16 不冲突。**复审补注（2026-09-14）**：① 位置源**复用一期已落地的** `LLM/locator.py` + `LLM/roslink.py`（`get_pose()`/`available()`），**不是**本设计新增的模块；② rosbridge 地址是 `conf.ROSBRIDGE_URL`（模块级常量，环境变量 `ROSBRIDGE_URL` 可改），**不进 settings 表**（`set_settings` 白名单不含它）——「一键停用自动切病房」改用新增设置项 `ward_autoswitch_enabled`（默认 `True`）；③ **「车此刻在跑哪张图」以 `/map` 四项指纹反查（`locator.current_map()`）为准**，认不出（`source != "map_topic"`）时**一律不判命中**（fail-safe：宁可不切，也不误切）；`settings.current_map` 是"下次启导航用哪张图"的目标值，仅当把 `ward_map_source` 显式设为 `"setting"` 时才拿它当判定依据；④ 拿不到位姿 / 认不出当前图 / 本图没有 `kind='ward'` 区域 / 该病房未关联区域 → 自动切换停用、退回手动，绝不阻塞对话、绝不误判 |
| D18 | 自动切换的克制规则 | 位置只驱动**「当前病房」这个背景变量**；仅当 kiosk 槽为 `ward` 且未锁定才真正改会话主体——**正在老人私聊时绝不抢会话**；声纹认出老人时把当前病房**跟随**到该老人的病房（用户 2026-09-14 选定 A）；手动切病房后 10 分钟内位置判定不覆盖 |

## 3. 核心模型

### 3.1 Principal（主体）

```
Principal = {
  role:  "admin" | "ward" | "elder",
  uid:   str,               # elder→"elder_001"；ward→"ward_101"（病房用户）；admin→"admin"
  source: "manual" | "voiceprint" | "password" | "default" | "auth_disabled",
  slot:  "kiosk" | "admin", # 该角色挂在哪个端槽位
  until: float | None,      # 管理员提权到期时间戳（monotonic）；None=不过期
}
```

`uid` 全局唯一一份（两端共享，沿用现状）；`role` 按槽位（D7）。**病房用户的 uid 就是它自己的 uid**——对话历史、记忆、摘要在现有 `db` 里全部按 uid 存放，所以集体层不需要新的存储层（D6）。

### 3.1.1 病房用户（集体层的落地形态）

- 一个病房 = `profiles` 表里一条 `kind='ward'` 的记录：`uid=ward_101`、`name="101 病房"`、`bed` 留空、不录声纹。
- 老人通过 `profiles.ward_id`（新增字段，指向病房 uid）归属病房；**一个老人只属于一个病房**。
- 归属建议：注册向导里按 `bed`（床位号）自动建议病房号（如 `bed="101-2"` → 建议 `ward_101`，可改），也可手动指定。
- 声纹识别到某位老人 → 从集体层切到该老人的 `elder` 会话（现有声纹链路，§4.3）。
- 每个病房用户还有一块**地图区域**，用于"小车在哪个病房"的位置判定（§4.5）。**几何的唯一真相是地图文件夹里的 `<图名>.tags.json`**（`2026-09-14-map-editor-design.md` §4.1 与 §〇 第 7 条：多边形/矩形、米坐标、层级用 `parent` 的 uid；`brain.db.zones` 只是**只读索引缓存**，严格单向、可丢弃可重建）。`profiles` 只记 **`ward_map`（地图名）+ `ward_zone`（区域 uid，形如 `z1`）** 两列，**不复制几何**——两处几何并存必然打架。解析几何一律走 `maptags.get_zone(map_name, uid)`，**不要**用 `db.get_zone(uid)`（只按 uid 查，三张图会串）。
  > **口径变更史（勿用中间版本）**：① `profiles.zone_json`（存圆）→ ② 独立 `zones` 表自增 `id`（`profiles.zone_id`）→ ③ **最终：`<图名>.tags.json` + `(地图名, 区域 uid)`**。① ② 均已作废；`zone_id` / `zone_json` 这两个名字在代码里**从未落地过一行**（grep 确认），所以**没有迁移负担**。

### 3.2 角色策略包（Policy）

`LLM/policy.py` 中的 `POLICY_DEFAULTS`，key 为 role：

| 字段 | 含义 |
|---|---|
| `prompt_file` | 角色提示词片段路径（`LLM/prompt/ward.md` …） |
| `allowed_tools` | 工具白名单（`None`=全部；`[]`=无工具）；与全局 per-tool 开关取**交集** |
| `data_scope` | `self`（仅本人档案/RAG）/ `ward`（本病房集体上下文 + 无个人档案）/ `all`（全部）/ `none`（不注入） |
| `ward_context` | 是否注入**所属病房**集体层的最近 N 条对话（`elder` 为 true，见 D14/R5） |
| `destinations` | `none` / `whitelist`（按表）/ `any`（可下任意坐标）——**本轮暂缓（D16）** |
| `risk_confirm` | 中风险动作是否需要口头二次确认——**本轮暂缓（D16）** |
| `voice` | 是否允许语音输入、是否需要唤醒词 |

### 3.3 能力矩阵（本设计的事实来源）

| 能力 | `ward`（集体层：病房用户 / 未识别默认态） | `elder` | `admin` |
|---|---|---|---|
| 闲聊问答 | ✅（**只谈大家的事**：打招呼、公布消息、病房级话题；不注入任何老人个人档案） | ✅（注入本人档案/RAG/语录 + 本病房集体上下文） | ✅（专业向人设，默认不注入老人记忆） |
| 读本病房集体层消息 | ✅ 本病房 | ✅ 本病房（只读） | ✅ 全部病房 |
| 读老人个人档案/记忆 | ❌ | ✅ 仅本人 | ✅ 全部 |
| 写老人私聊内容回集体层 | ❌（也不可能，单向 R5） | ❌ | ❌ |
| 读审计/工具日志/系统状态 | ❌ | ❌ | ✅ |
| 车 · 状态查询（位姿/电量） | ✅ 只读播报 | ✅ | ✅ |
| 车 · 急停 / 呼救 | ✅ 永远允许（R3） | ✅ | ✅ |
| 车 · 去**白名单内**地点 | ❌ | ✅（§7，**本轮暂缓**） | ✅ |
| 车 · 任意坐标 / 跨区域 | ❌ | ❌ | ✅（暂缓） |
| 车 · 解除急停 / 改底盘参数 | ❌ | ❌ | ✅ |
| 系统 · 关机 / 改设置 / 改口令 / 开关口令门 | ❌ | ❌ | ✅ |
| 记忆写入/纠正 | ❌（集体层对话只作集体上下文，不沉淀成某位老人的记忆） | ✅（仅自己对话） | ✅ |
| 地点白名单管理 | ❌ | ❌ | ✅（暂缓，D16） |
| 层级切换 / 登录登出 | ❌ | ❌ | ✅ |

## 4. 会话与认证

### 4.1 新模块 `LLM/session.py`

取代 `voice_api._session_uid/_session_locked` 的持有权（`voice_api` 保留同名函数作薄封装转发，**现有调用点不用改**）。

```
get_principal(slot) -> Principal      # 读；过期即降权（懒 tick）
set_subject(uid, locked, source)      # 语音/手动切主体：写 uid，role 由主体类型推导（§4.4）
login_admin(password, slot)           # 口令开：校验通过 → 该槽 role=admin + until=now+TTL
                                      # 口令关（admin_auth_required=False）→ 直接放行，审计 source=auth_disabled
logout(slot)                          # 该槽回落集体层（role=ward，uid=当前病房用户）
tick()                                # 定时（沿用 bus/reminder 线程或 lifespan 后台任务）
```

- **单进程全局**：沿用现状（一辆车、一块屏），不做 per-connection 会话。
- **双槽**：`kiosk` 槽承载语音链路与车前屏；`admin` 槽承载管理台。**管理台登录不会把车前屏提权**（这是双槽存在的唯一理由）。
- **槽位判定**：请求头 `X-Surface: kiosk|admin`（shared 的 API client 统一带上）；缺省时按路径判定（静态 `/admin/*` → admin，其余 → kiosk）。
- **提权只升不降（D8）**：`kiosk` 槽为 `admin` 期间，声纹识别到某个老人**不改变 role、不改 uid**，只落审计 `voice_spk action=ignored_in_admin`。降权只有三条路：TTL 到期、显式登出、显式切换。
- **TTL 到期**：`tick()` 每分钟检查，到期 → 该槽 role 回落**集体层**（uid 保持当前病房用户）并广播 `session_expired`（bus），落审计。

### 4.2 接口变更

| 接口 | 变更 |
|---|---|
| `GET /api/session/user` | 响应扩展：`{uid, name, role, locked, source, slot, ttl_remain, ward_uid, auth_required}`（`role` 按请求槽位返回） |
| `POST /api/session/user` | **保持现签名**（`{uid, locked}`），只允许切主体/锁定；**拒绝 `role` 字段**（有则 400）。可传病房用户 uid（如 `ward_101`）切到集体层——role 由后端按 uid 推导（§4.4） |
| `POST /api/session/login`（新增） | `{password}` + `X-Surface` → 成功 `{ok:true, role:"admin", uid:"admin", ttl_remain}`；口令门关闭时**无需 password 直接放行**（`source=auth_disabled`）；失败 `{ok:false, error}`，失败计数 + 审计 |
| `POST /api/session/logout`（新增） | 该槽回落集体层 |
| `POST /api/session/password`（新增） | 管理员改口令（需旧口令）；口令门关闭时可直接设新口令并重新开启 |
| `GET|POST /api/session/admin-auth`（新增） | 读/写 `admin_auth_required`（**口令门的开关**，D13）；关闭需 admin 身份，落审计 |
| `GET|POST /api/wards`（新增） | 病房用户列表/新建（`kind='ward'` 的 profile）；`POST` 可带 `{name, ward_uid}` |
| `GET /api/policy/roles`（新增） | 返回策略矩阵（`admin` 可见全量，其它角色只拿到自己那份摘要） |
| `GET /api/profiles` | **默认只列 `kind='elder'`**（新增 `?kind=` 查询参数，传 `all` 才全量）。理由：病房用户不能混进前端的"老人列表"（记忆页/对话页/注册页下拉、车前屏换人弹层都吃这个接口）；要看病房用 `/api/wards` |

口令校验：`hashlib.pbkdf2_hmac("sha256", pw, salt, 200_000)`，盐与哈希存 `settings` 表（key `admin_password_hash` / `admin_password_salt`，**不落明文**）；初始口令见 D12。防暴力：连续 3 次失败 → 该槽冷却 10s。

### 4.3 语音链路改动（最小）

`voice/worker.py::_handle_speech`：把 `uid = id_mod.effective_uid(...)` 之后的流程改为

```
principal = session.get_principal("kiosk")
if principal.role == "admin":          # D8：不提权也不降权
    uid = principal.uid                # "admin"，语音按 admin 角色走
else:
    recognized = id_mod.effective_uid(...)   # 现有声纹逻辑
    uid = recognized or principal.uid        # 认到老人→elder；没认出来→留在集体层(ward_101)
session.set_subject(uid, locked, source="voiceprint")   # role 由 uid 推导（§4.4）
```

`_stream_fn(uid, text)` 签名不变，但内部通过 `session.get_principal("kiosk")` 取角色——**不新增参数、不信任调用方**。

### 4.4 role 推导规则（唯一入口，守住 R1）

```
def derive_role(uid, *, slot, admin_logged_in=False) -> str:
    if admin_logged_in:                     return "admin"
    if uid and db.get_profile_kind(uid) == "ward":  return "ward"
    if uid and db.get_profile_kind(uid) == "elder": return "elder"
    return "ward"                           # 兜底：未知 uid 一律按集体层最小能力（R2 fail-closed）
```

- **权威判定查 `profiles.kind`**，不靠 uid 前缀——否则有人把老人 uid 起成 `ward_` 开头就能骗过判定。
- `admin` 的 uid 是字面量 `"admin"`，**不写进 `profiles`**，因此永远不会出现在换人/层级列表里被误选（也不会被声纹匹配到）。
- 前端只能提交 uid；任何带 `role` 的请求按 400 拒绝（R1）。

### 4.5 「当前病房」的确定与自动切换（D17/D18）

**一句话**：小车在哪个病房，集体层就是哪个病房；但**绝不打断正在进行的私聊**。

「当前病房」有三个来源，优先级从高到低：

1. **跟随老人**（Q2 选 A）：声纹认出 `elder_102_1` → 当前病房 := 该老人的 `ward_id`（`ward_102`）——他属于哪个病房，集体层就跟到哪个，避免"在 102 认出了 102 的老人，切回集体层还停在 101"。
2. **位置自动**（Q1 选 B）：`LLM/locator.py` 取小车位姿 → 命中一期 `zones` 表里 `kind='ward'` 的多边形即为该病房（判定函数由一期的区域数据层提供；P0 先开工时 P0 自带最小实现，见实现计划）。
3. **手动覆盖**：层级栏里手动点某个病房 → 当前病房 := 该病房，并带 `manual_override_until`（默认 10 分钟），期间位置判定不覆盖，避免"刚设好就被抢回去"。

切换规则（防误切、防打断）：

- **防抖**：连续 3 次 tick（≈3s）落在同一病房才认；离开病房区（进走廊/未知区域）**不切**，保持上一病房。
- **不抢私聊**：仅当 kiosk 槽 `role == "ward"` 且 `locked == False` 时，自动切换才真正改会话主体（切到新病房的集体层）。若正处在 `elder` 私聊中或已锁定 → **只更新"当前病房"背景变量**并广播 `ward_changed`，会话主体不动；退出私聊/解锁回集体层时自然用上新病房。
- 任何来源的切换都广播 `ward_changed`（载荷 `{uid, action}`，`action = location|manual|upsert|zone|assign`）并落审计 `ward_change`。**"跟随老人"不单独广播 `ward_changed`**：它随 `user_changed` 一起体现（`set_subject` 里同步当前病房），避免同一件事广播两条。

位姿来源与降级（**复用一期已落地的 `LLM/locator.py` + `LLM/roslink.py`，P0 不新写位置源**）：

- 位姿来自 rosbridge（`ws://<板卡>:9090`，即板卡上 `~/tools/nav_screen.sh lat` 起的那个会话）订阅 `/amcl_pose`（map 系，米 + 弧度；AMCL 只在位移 ≥0.25m 或转角 ≥0.2rad 时发布，判"进没进病房"足够）。**这是 websocket，不是 MCP**，与 D16「先不管 MCP」不冲突。地址取 `conf.ROSBRIDGE_URL`（默认 `ws://100.65.82.93:9090`，环境变量 `ROSBRIDGE_URL` 可覆盖）——**不是 settings 表项**。
- 现成接口口径（已实现，直接调用，不要重写）：`locator.get_pose() -> dict|None`（`{x, y, yaw(弧度), source, at, suspect}`，拿不到 = `None`）、`locator.available() -> (bool, reason)`、`locator.current_map() -> {ok, source, name, detail}`、`locator.set_pose_for_test(x, y, yaw)`（已有，供无 ROS 环境注入假位姿）。
- **停用阀**：新增设置项 `ward_autoswitch_enabled`（默认 `True`，设置页可关）。位姿不可用 / 当前图认不出 / 缓存里没有病房区域 → 自动切换**自动停用**，UI 显示「位置未知 · 手动切病房」，会话保持当前病房不变；**不报错、不阻断对话**（沿用降级原则）。
- 备选（可选加固，非 P0 必须）：订阅 `/tf` 自己串 `map→base_link` 拿连续位姿。

病房的区域录入走两条路，**都落一期的 `<图名>.tags.json`**（`LLM/maptags.py`：`kind='ward'`、`shape='polygon'`、`polygon` 为 `[[x米, y米], ...]`；`brain.db.zones` 由 `maptags.sync_map()` 单向刷成只读缓存）：① **便捷入口**——管理员在「病房管理」页点「记录当前房间为病房区域」→ 取 `locator.get_pose()` 当前位姿作圆心 + 半径 `ward_zone_default_r`（3m，可调），调**现成的** `maptags.record_room_polygon(map_name, name, x, y, radius_m, kind="ward", segments=16)`（16 边形近似圆，**已有实现、目前没有 HTTP 入口**，P0 补一条 `POST /api/wards/{uid}/zone`）→ 写文件 + 刷缓存 + 把 `(地图名, 区域 uid)` 回填进 `profiles.ward_map / ward_zone`；拿不到位姿 → 返回 `ok:false` 并提示"先起定位/rosbridge，或到地图编辑器手绘"；② **精确形状**——到地图编辑器（`/mapeditor`）画多边形/矩形（`kind` 选「ward 病区」），病房管理页只负责把病房**关联**到该区域的 `uid`。

**判定读哪份数据（性能约束）**：位置自动切换的 tick 约 1s 一次，**只读本地 `brain.db.zones` 只读缓存**（`db.list_zones(map_name=…, kind="ward")`）；**绝不**在 tick 里走 `maptags.get_zones()`——那条路径含 `_ensure_fresh → sync_map`，在 `MAPS_IO=ssh` 下可能秒级阻塞。缓存新鲜度由「编辑器/接口读过或写过就会刷」保证。同理，`locator.current_map()` 内部要列地图 + 逐图读元数据（可能走 SSH），**必须缓存**（TTL 10s）后再用于 tick。

## 5. 提示词分层（D9）

### 5.1 文件组织

```
LLM/prompt.md            # 共用 base：人设（小护）+ 安全红线（现状文件保留不动）
LLM/prompt/ward.md       # 集体层片段（本期新增）
LLM/prompt/elder.md      # 老人层片段（本期新增）
LLM/prompt/admin.md      # 管理层片段（本期新增）
```

- `chat.py::_load_prompt_base()` **逻辑与路径不变**（继续读 `prompt.md` 的 `<!-- PROMPT -->` 之下正文、缺文件退回 `_DEFAULT_PROMPT_BASE`）。
- 新增 `_load_role_prompt(role)`：读 `LLM/prompt/<role>.md` 全文，缺失 → 返回空串 + 告警一次（`prompt_role_missing` 审计），**不阻断对话**。
- 装配：`build_system(uid, settings, query, principal=None)` = `base` + `role_fragment` + 现有动态块（档案/记忆 → 集体上下文 → 摘要 → 语录 → 当前时间）。**`uid` 老签名保留、`principal` 新增且可选**（缺省取 kiosk 槽），这样既有调用点与测试不用改；`build_messages`/`chat_stream` 同样加可选 `principal` 透传。

### 5.2 各层片段要点（内容要点，措辞在实现时按现有 prompt.md 的笔法写）

- **ward.md（集体层）**：面对的是"一屋子人"，不是某一位老人——用**打招呼/广播**的口吻，一句话说清一件事，不追问个人隐私、不点名谁；被问"我是谁/我的药"这类个人问题 → 回「这个我得单独跟本人说」；未识别出说话人时保持集体层姿态。样例句：
  > 你现在是跟一个病房里的大家说话，不是在跟某一个人私聊。有事说事，一句话讲完；别问谁是谁，也别替谁说他的私事。
  > 等认出了具体是哪位老人，再跟他单独聊他的事（那时会切到他的私聊里）。
- **elder.md（老人层）**：熟人腔（沿用现有 prompt.md 的示范）+ 一条**病房上下文**说明——「你刚才在病房里跟大家说过的话，这位老人也知道，不用重复介绍」；个人助手身份；安全红线口语化复述。样例句：
  > 病房里集体聊过的内容，这位老人也在场听过，你可以接着聊，不用从头再说一遍；他私下的心事只留在你俩之间，别再拿到病房里说。
- **admin.md（管理层）**：专业、简洁、可带术语与数字（状态、参数）；明确「你不是在陪聊，你在向管理员报告」；不做老人腔、不卖萌。样例句：
  > 你在跟管理员说话，不是跟老人。直接给结论和数字，一句话报完，不用寒暄、不用哄。
- **口令门关闭时**：admin.md 追加一句风险提示（「当前没有口令保护，任何人都能进管理层」），提醒管理员尽快开回来。

### 5.3 上下文注入按 `data_scope` 开关（含集体层规则）

- `ward` → 注入**本病房**集体层最近 N 条（`WARD_CONTEXT_WINDOW=10`），**不注入**任何老人档案/私人记忆/语录。
- `elder` → 注入本人档案/RAG/语录（现状行为）+ **本病房集体层**最近 N 条（D14/R5，单向：只读不回灌），以「【病房里刚说过的事（这位老人也在场）】」小节呈现。
- `admin` → 默认不注入；管理台「以某老人视角看」时显式传 `as_uid`（仅该调试入口使用，落审计）。
- 隐私红线（R5）：`elder` 私聊内容**不得**进入集体层注入；跨病房一律不注入。实现上双侧都只按 uid 精确取用，不做模糊匹配。
- 集体层对话**不参与** `memory.note_turn()` 的沉淀（不写成任何老人的记忆，见 §3.3 矩阵）。

## 6. 权限闸门（三道，纵深防御）

### 6.1 闸门 1：路由层（`LLM/server.py`）

业务接口在进入业务逻辑前取 `principal = session.get_principal(surface)`，塞进请求上下文（`chat_stream(..., principal=...)`）。业务接口**永不读前端传的 role**（R1）。

### 6.2 闸门 2：对话/工具层（`LLM/chat.py` + `LLM/tools.py`）

- `tools.py::effective_tools(settings, principal)`：全局 per-tool 开关 **∩** 角色白名单。
- 注册装饰器扩展：`@tool(name, desc, schema, enabled=True, roles=None)`（`None`=不限角色；现有工具不改则默认不限，行为兼容）。
- MCP 工具：`conf.MCP_SERVERS` 每项加 `roles` 字段（未声明视为 `{"admin"}`——MCP 是不受控外部能力，默认从严）。运行时经 `mcp_client.tools()[工具名]["server"]` 反查所属服务器名，再取 `MCP_SERVERS[server].get("roles")` 判定。现状：`MCP_SERVERS` 只有 `fetch` / `tavily` 两台（car 那台属 P1），故 P0 的 net 效果是「MCP 工具只给 admin」。
- `run_tool(name, args, principal)`：执行前**再校验一次**（防绕过闸门 2 的直调点），并调用 `policy.check_action()`。

### 6.3 闸门 3：动作层（`LLM/policy.py::check_action`）

```
check_action(principal, tool, args) -> {"decision": "allow"|"confirm"|"deny", "reason": str}
```

风险分级：

| 风险 | 例子（现有 car MCP 工具名） | ward | elder | admin |
|---|---|---|---|---|
| low | `robot_status`、`robot_stop`、地点表内 `risk=low` 的 `robot_goto` | allow（`robot_stop`）/ deny（其余） | allow | allow |
| mid | 地点表内 `risk=high` 的 `robot_goto`（离房/出走廊）、`robot_move`/`robot_turn` | deny | **confirm**（D11） | allow |
| high | 显式坐标的 `robot_goto`、解除急停、改底盘参数、关机 | deny | deny | allow |

> ⚠️ 本节（闸门 3 的动作分级 + §7 地点白名单 + `robot_goto`）依赖 car MCP，**按 D16 本轮暂缓**。本轮只落地闸门 1/2 与角色策略本身；分级表先按此定稿，MCP 线重启后直接照做。

判定结果**全部落审计**：`policy_confirm` / `policy_deny` / `policy_allow_dangerous`。
被 deny 的动作必须**给模型一句可复述的拒绝理由**（如「这个我去不了，我只认得护士站和活动室」），不能静默失败。

## 7. 地点白名单（D5 / D10）——**本轮暂缓（D16）**

### 7.1 数据

**地点数据的唯一真相 = 地图文件夹里的 `<图名>.tags.json`**（`LLM/maptags.py`，一期已落地；字段 `uid`/`name`/`aliases`/`x`/`y`/`yaw_deg`/`risk`/`elder_allowed`/`note`/`learned_by`）。`brain.db.destinations` 只是**只读索引缓存**（复合主键 `(map_name, uid)`，`UNIQUE(map_name, name)`），唯一写入口是 `maptags.sync_map()`；**任何接口都不允许只改 SQLite 表**。

> 原「新表 `destinations`：`name`(PK) / `goal_json`（`{x, y, yaw, frame:"map"}`）…」的写法**已作废**（2026-09-14，随地图标记迁到地图文件夹）。`name` 也不再是主键——稳定身份是 `uid`（`d<N>`），改名不改 uid。

新设置项（`conf.DEFAULT_SETTINGS`）：`elder_destinations_enabled: True`、`elder_confirm_required: True`、`admin_session_ttl_s: 300`、`pending_action_ttl_s: 30`、`dest_point_tolerance_m: 0.3`。

### 7.2 新增车端工具 `robot_goto`（**本轮暂缓**，MCP 线重启后做）

现有 car MCP（`LLM/car_mcp/car_server.py`）只有 `robot_move(direction, distance_m)` / `robot_turn(angle_deg)` / `robot_stop()` / `robot_status()`，**没有"去某个地图点"的能力**，所以本设计需要新增：

```
robot_goto(destination: str = "", x: float = 0, y: float = 0, yaw: float = 0) -> str
```

- **模型只能填 `destination`（地名）**；`x/y/yaw` 是给管理员/内部用的显式坐标，`elder`/`ward` 传入一律被清空（见下）。
- **坐标由后端闸门 3 解析后注入，不由模型产生**：`run_tool()` 在执行前调 `policy.resolve_destination(principal, args)`，
  - `elder`：`destination` 必须在 `destinations` 表内且 `elder_allowed=1` → 注入表里的 `x/y/yaw`；否则 `deny`。
  - `admin`：可给表内地名，也可给显式 `x/y/yaw`（`explicit` 语义由"是否带坐标"表达）。
  - 解析后 MCP 侧拿到的**永远是坐标**，`destination` 只作日志/回话用。
- **落地实现**：car_server 经既有 ROS2 服务 `robot/navigate_to`（`robot_interfaces/srv/NavigateTo`，见 `ros2_car/src/robot_navigation/robot_navigation/robot_actions.py:75`）下发 **x/y/theta**。
  ⚠️ 已核实：该 srv 虽有 `place` 字段，但车端当前**明确拒绝** place（`robot_actions.py:179-184`「地点表后置…请改用 x/y/theta」）。故本期**不启用车端地点表**；地名→坐标的唯一事实来源是**地图文件夹里的 `<图名>.tags.json`**（`LLM/maptags.py`，地图编辑器/管理台可视化编辑），`brain.db.destinations` 只是只读索引缓存。将来若车端建了 place 表，只需改这一处转发。
- **前置条件与降级**：需导航栈在跑（`~/tools/nav_screen.sh nav <地图>`；见 `ros2_car/《建图与导航操作手册.md》`）。导航栈没起 / rclpy 不可用 / 服务超时 / 目标越界 → 返回 `{"ok": false, "error": "..."}`，模型据此回话「我现在走不了」，**绝不假装成功**（沿用 car_server 顶部注释里的 `ok/error` JSON 风格）。

### 7.3 管理接口

| 接口 | 说明 |
|---|---|
| `GET /api/destinations` | 列表（`admin` 专属） |
| `POST /api/destinations/learn` | `{name, risk, elder_allowed, note}` → 经 car MCP `robot_status` 取**当前位姿**落库（D10） |
| `POST /api/destinations/{name}` | 改名/改 risk/改 elder_allowed/改坐标 |
| `DELETE /api/destinations/{name}` | 删除 |

### 7.4 解析路径（关键：坐标不由模型产生）

1. 老人说「带我出去转转 / 去护士站」。
2. `elder.md` 提示词里动态注入当前**可用地点清单**（从 `destinations` 取 `elder_allowed=1` 的名字）。
3. 模型只能调 `robot_goto(target="护士站")`——**参数是名字，不是坐标**（§7.2）。
4. 后端 `policy.resolve_destination(principal, args)`（§7.2）：查表得坐标并**改写参数**；`elder` 只能给表内 `elder_allowed=1` 的地名，否则 `deny`；`admin` 可用地名或显式坐标。
5. 出车前校验：地图边界余量 ≥ `dest_point_tolerance_m`（复用 `ros2_car/tools/where_am_i.py` 的判据口径）、底盘限速红线不变（vx≤0.5 / vy≤0.3 / wz≤0.8 + 看门狗）。

### 7.5 二次确认状态机（D11，后端闭环）

- mid 风险且 `elder_confirm_required=True`、且说话人是 `elder` → 不立即执行：
  1. 会话挂 `pending_action = {tool, args, uid, expire_at}`（TTL `pending_action_ttl_s`）。
  2. 机器人回话：「要去护士站吗？说一声我就走。」
  3. 下一句若命中确认词（好/走吧/对/嗯/可以）→ 执行；否则丢弃动作 + 审计 `policy_confirm` 结果。
- 确认词解析放 `policy.py::is_affirmative(text)`（纯规则，不调 LLM——省延迟且不受模型幻觉影响）。
- `admin` 一律跳过确认。

## 8. 前端改动

### 8.1 shared（`frontend/packages/shared`）

- 新增 `src/session.ts`：`login(password)` / `logout()` / `getSession()` / `setAdminAuth(required)` / `ttlRemain` 心跳。
- API client 统一带 `X-Surface` 头（kiosk 包注入 `kiosk`，admin 包注入 `admin`）。
- `src/events.ts`（**唯一事实来源，改后端 publish 点必须同步这里**）：
  - `user_changed`：payload 增 `role`、`slot`、`ward_uid`；
  - 新增 `session_expired`（`{slot}`）、`admin_auth_changed`（`{required}`）、`ward_changed`（`{uid, action}`）。

### 8.2 kiosk（层级切换栏 + 集体层展示）

- **「换人/切换」按钮 → 左侧层级栏**（D15，用户明确要求）：抽屉从左侧滑出，三组分区自上而下——
  - 🛡 **管理层**：单条「管理员」。已登录显示「当前（剩 4:32）」+「退出」；未登录显示「输入口令进入」；口令门关闭时显示「直接进入（无口令保护⚠️）」。
  - 🏠 **集体层**：病房用户列表（`ward_101 101病房` …），当前项打勾并标注来源——`📍自动`（按位置）/ `✋手动`（覆盖中，显示剩余时间）/ `跟随老人`；位置源不可用时顶部提示「位置未知 · 手动切病房」。切换即把会话 uid 设为该病房用户 → role=ward。
  - 👴 **老人层**：仅显示**当前病房**的老人（默认按 `ward_id` 过滤，可切「全部病房」），显示昵称/床位；当前项打勾。
- 状态条（`VoiceStatusBar.vue`）：按角色换徽标——`🏠 101病房`（集体层）/ `👴 张奶奶 🔒`（老人层）/ `🛡 管理员 4:32`（管理层，倒计时来自 `ttl_remain`）。
- 管理员态额外显示：「退出管理员」「口令设置」（改口令 / 开关口令门，调 `/api/session/password` 与 `/api/session/admin-auth`）。
- 集体层态：对话区顶部提示「在跟 101 病房的大家说话」；不做个人档案展示。
- 老人层态：对话区顶部提示「私聊 · 张奶奶」，并可显示一行「病房里刚说过的事」入口（只读）。

### 8.3 admin

- **登录门**：`admin_auth_required=True` 且未登录时整站只显示登录卡（口令）；关闭口令门时直接进入，但顶部常驻红条警示「当前无口令保护」。
- Header：当前身份 + 剩余时间 + 「退出」。
- 新增页签「身份与权限」：策略矩阵只读展示（来自 `GET /api/policy/roles`）+ 口令设置（**改口令 / 开关口令门**，D13）+ 可调项（`admin_session_ttl_s`、`ward_context_window`）。
- 新增页签「病房管理」（集体层）：病房用户列表 + 新建病房 + **「记录当前房间为病房区域」**（取 locator 当前位姿作圆心 + 半径，采样成 16 边形落 `zones` 表，§4.5；页面注明"精确形状请到地图编辑器画多边形"）+ 把老人归入病房（写 `profiles.ward_id`），并在现有「老人注册」页签的向导里加一个「病房」下拉（按床位号自动建议）。
- 「地点白名单」页签**本轮不做**（随 §7 暂缓）。

## 9. 数据与审计

- **`profiles` 表扩展**（集体层的全部数据基础）：新增 `kind`（`'elder'|'ward'`，默认 `'elder'`）、`ward_id`（老人所属病房 uid，可空）、`ward_map` + `ward_zone`（仅 `kind='ward'` 用：**「地图名 + 区域 uid」**，指向 `<地图名>.tags.json` 里的 `z<N>`，空=未关联，**不存几何**，§4.5）；病房用户即 `kind='ward'` 的一条 profile（`uid=ward_101`、`bed` 留空、不录声纹）。旧数据迁移：`kind` 列默认值就是 `'elder'`，`ALTER TABLE ADD COLUMN` 后旧行自动满足，**不需要写迁移代码**。
  > **复审补注**：现状 `profiles` 连 `kind`/`ward_id` 都没有（P0 任务 1 曾实现又 revert，`zone_id` 从未落地），所以是"纯加列"。两条来自实操的教训必须守住：① **病房↔区域的关联只允许由专用函数（`upsert_ward`/`set_ward_zone`）写，`upsert_profile` 不得带上/覆盖这两个字段**——否则管理台编辑老人档案时会静默清掉病房关联；② `upsert_ward` 提权 `kind` 必须**直写 `SET kind='ward'`**，**不要**用 `COALESCE(profiles.kind, excluded.kind)` 兜底——旧行恒为 `'elder'`、兜底永不触发，会把"把已有 uid 提升成病房"变成静默失败（半状态：kind 还是 elder，区域关联却写进去了）。
- **新增设置项**（都进 `conf.DEFAULT_SETTINGS`，否则 `set_settings` 的白名单会拒收）：`admin_auth_required`（口令门，默认 True）、`admin_session_ttl_s`（默认 300）、`ward_context_window`（默认 10）、`ward_autoswitch_enabled`（默认 True；**这是"一键停用病房自动切换"的开关**）、`ward_switch_debounce`（默认 3 次 tick）、`ward_zone_default_r`（默认 3.0m；**仅用于便捷入口采样 16 边形的半径**）、`manual_override_sec`（默认 600s）、`ward_map_source`（默认 `"auto"`：以 `/map` 指纹反查为准；`"setting"`=改用 `settings.current_map` 当判定地图）。
  > **复审修正**：原文把 `rosbridge_url` 列为设置项——**不行**。rosbridge 地址是 `conf.ROSBRIDGE_URL` 模块级常量（环境变量 `ROSBRIDGE_URL` 可改），不在 `DEFAULT_SETTINGS` 里，`set_settings` 也拒收；"空=停用自动切病房"的功能改由 `ward_autoswitch_enabled` 承担。`current_map`（一期 §4.3 同名项，默认 `"my_map"`）**已经存在**（`conf.py:42`），P0 **不要重复添加**。
- **新表**：**不建任何新表**。`zones` / `destinations` / `map_tags_manifest` 三张**只读索引缓存表一期已建**（`db.py:91-129`，复合主键 `(map_name, uid)`）；`destinations` 相关业务接口随 §7 暂缓。
- **不建 `accounts` 表**（D1 轻量口径）；口令哈希放 `settings` 表（`admin_password_hash` / `admin_password_salt` / `admin_auth_required`），将来扩多账号时再建表并迁移。
- **审计事件**（`log.py::log` 的 `event`）：`session_login`（含 `source=password|auth_disabled`）/ `session_login_fail` / `session_logout` / `session_expired` / `admin_password_generated` / `admin_password_changed` / `admin_auth_changed` / `ward_change` / `policy_deny` / `prompt_role_missing`；暂缓项对应的 `policy_confirm` / `policy_allow_dangerous` / `destination_change` 留待 §7 落地。
- 每条策略判定至少含：`role` / `uid` / `slot` / `tool` / `decision` / `reason`。
- **集体层对话的存储**：直接用现有 `db.add_history(uid=ward_101, ...)`，不新增表；`memory.note_turn()` 对 `role=ward` 不沉淀（§5.3）。

## 10. 分阶段实施

| 阶段 | 内容 | 交付物 | 预估 |
|---|---|---|---|
| **P0（本轮，用户指定优先）** 用户系统 | `profiles` 加 `kind`/`ward_id`/`ward_map`/`ward_zone`（纯加列，无迁移）+ `ward` 病房用户 CRUD + 管理员口令数据层（PBKDF2 哈希存 `settings` raw key）+ 新增 `zonegeo.py`（"点在区域内"，纯 stdlib，约 30 行）+ `session.py`（双槽主体 / TTL / `derive_role` / 口令登录·登出·改·关·开 / **当前病房与位置自动切换**）+ `policy.py`（角色策略包）+ 提示词分层（`LLM/prompt/{ward,elder,admin}.md` + `_load_role_prompt`）+ `@tool(roles=)` 与 `effective_tools/run_tool` 白名单校验 + 集体层上下文注入（R5 单向）+ 前端 shared（`X-Surface` + 事件）、kiosk 左侧层级栏、admin 登录门 + 「身份与权限」「病房管理」页签 + 审计 | 三层可切：管理层（口令可开可关）、集体层（病房用户，**按位置自动切**）、老人层（声纹/手选）；越权请求被拒且有审计；老人能读到本病房集体上下文 | ~2 天 |
| **P1（暂缓，D16）** 动作约束 | car MCP 新增 `robot_goto`（§7.2）+ `destinations` 表与接口 + 地点白名单解析 + 风险分级 + 二次确认状态机 + admin「地点白名单」页签 | 老人说「去护士站」能走通，说「去停车场」被拒 | ~1.5 天（MCP 线重启后） |
| **P2** 预留 | 集体层能力扩展（进病房自动打招呼/广播）、`accounts` 表多账号、老人动作审批流、权限矩阵细化到「谁能看哪段记忆」 | 另立 spec | — |

**明确不做（YAGNI）**：用户名/密码注册流程、RBAC 权限编辑器、per-user 工具开关、OAuth/JWT、per-connection 多会话、审计日志前端查询界面（沿用现有「工具日志」页签）、集体层独立存储（复用 uid 历史即可）。

## 11. 验收标准（P0，本轮）

1. **三层可切**：kiosk 点「切换」→ 左侧层级栏出现 管理层/集体层/老人层 三组；选 `ward_101` → 状态条显示 `🏠 101病房`，对话历史切到病房 uid。
2. **口令门**：默认需口令；错误口令连续 3 次 → 冷却 + 审计 `session_login_fail`；正确口令 → 进入管理层（Header/状态条显示倒计时）。
3. **口令可改可关**：在管理层「口令设置」里改口令 → 旧口令失效、新口令可用（审计 `admin_password_changed`）；关掉口令门后再点「管理层」→ 无需口令直接进入，UI 显示「无口令保护⚠️」（审计 `source=auth_disabled`）；重新开启后需口令。
4. **R1 前端 role 不可信**：`POST /api/session/user` 带 `role:"admin"` → 400；集体层时让模型「去护士站」→ 工具不可见/被拒，且审计 `policy_deny`。
5. **集体层读不到个人数据**：集体层会话的 System Prompt 内**不含**任何老人档案/摘要/语录（抓 `GET /api/context` 或审计比对），但含本病房集体上下文。
6. **老人层可读集体上下文（R5 单向）**：小车先在 `ward_101` 里说「明天九点体检」→ 声纹切到 `elder_101_1` 后，其 System Prompt 里出现这条病房上下文；反向不成立——老人私聊「我昨晚没睡好」**不出现**在 `ward_101` 的后续上下文里。
7. **跨病房隔离**：`elder_102_1`（102 病房）的上下文里**不含** `ward_101` 的任何消息。
8. **管理员 TTL**：登录 5 分钟无操作 → 提示过期、权限回落集体层（审计 `session_expired`），此时再发管理类请求被拒。
9. **双槽隔离**：管理台登录**不影响**车前屏角色（kiosk 槽仍为集体层/老人层）。
10. **管理员语音**：管理员在车前（kiosk 槽）登录后说话 → 按 `admin` 角色的提示词与权限处理；期间声纹识别到老人**不降权**（审计 `ignored_in_admin`）。
11. **未识别默认态**：完全没有声纹匹配时说话 → 走集体层（`🏠 当前病房`），不注入任何老人档案。
12. **不破坏现有链路**：SOS（`/api/alarm`）在任何角色下都成功（R3）；缺依赖时后端仍能启动（降级运行）。
13. **按位置自动切病房**：先在某图的 `<图名>.tags.json` 里建一条 `kind='ward'` 的多边形区域，把**地图名 + 区域 uid** 写进 `ward_102` 的 `ward_map`/`ward_zone`，再给 locator 注入落在该区域内的位姿（判定的地图按 `ward_map_source`：默认用 `/map` 指纹；本机验收时注入 `/map` 元数据，或临时置 `ward_map_source="setting"` + `current_map`）→ kiosk 槽（当前为集体层、未锁定）在防抖窗口后自动切到 `ward_102`，广播 `ward_changed source=location`、审计 `ward_change`；位姿回到病房外（走廊）→ 不切、保持原病房。
14. **不打断私聊（D18）**：老人私聊中（kiosk 槽 role=elder）注入"换到 102 病房"的位姿 → 会话主体不变，仅"当前病房"更新；退出私聊回集体层时已是 `ward_102`。
15. **位置源不可用即降级**：停掉 rosbridge（或把 `ward_autoswitch_enabled` 置假、或让指纹认不出当前图）→ 自动切换停用，UI 显示「位置未知 · 手动切病房」，对话链路照常、无报错、无卡顿。
16. **手动覆盖生效**：手动把当前病房设为 `ward_101` 后，注入指向 `ward_102` 的位姿 → `manual_override_sec` 内不抢（仍为 `ward_101`），超时后恢复位置判定。

## 12. 测试与红线清单

**单测（pytest，放 `LLM/tests/`；文件划分与 P0 计划的任务一一对应）**：
- `test_zonegeo.py`：射线法点在多边形内/外/边界、`rect` 用外接矩形、点数 <3 一律 `False`（不抛异常）。
- `test_ward_db.py`：`profiles` 扩列（旧行 `kind` 默认 `elder`）、`get_profile_kind`、`upsert_ward`/`set_ward_zone`/`list_wards` 往返、`upsert_profile` **不覆盖** `ward_id`/`ward_map`/`ward_zone`、`list_zones(kind=)` 过滤、口令哈希往返。
- `test_session_roles.py`：`derive_role`（含伪造前缀仍按 `profiles.kind`）、双槽隔离、提权只升不降、TTL 到期降权、登录失败冷却、口令门关闭时放行且 `source=auth_disabled`、改口令/开关口令门/首启生成随机口令。
- `test_policy_roles.py` + `test_policy_tools.py`：三层策略包取值与 fail-closed 兜底；`effective_tools` 角色白名单 ∩ 全局开关；`run_tool` 二次校验（§11.4）。
- `test_prompt_layers.py`：四层装配正确（含 ward 的"不含个人档案"断言）、缺文件降级不崩、集体上下文单向（elder 看得到 ward、ward 看不到 elder 私聊）。
- `test_ward_autoswitch.py`（**全部用注入假位姿，不需要 ROS**）：跨区域切/不切、防抖窗口、私聊期间不抢、手动覆盖期内不抢、跟随老人、认不出当前图或拿不到位姿时整体停用且不改会话。
- `test_ward_memory.py` + `test_worker_roles.py`：集体层不沉淀记忆；`voice_api` 会话函数转发到 `session`。
- `test_server_roles_routes.py`：会话/病房/策略路由；`POST /api/session/user` 带 `role` → 400；非管理员调管理接口 → 403。
- 暂缓项的单测（`test_destination_resolve.py` / `test_confirm_flow.py`）随 §7 一起做。

**测试基线（2026-09-14 实测，改代码前先认下这个数）**：
```
.venv\Scripts\python.exe -m pytest LLM/tests tests -q
→ 4 failed, 191 passed in ~78s
```
那 4 个红态是**既有基线漂移，与本次无关**：`tests/test_modules_status.py::test_modules_status_shape`（模块集合没算 `mcp`）与 `tests/test_unlock_switch.py` 3 例（`VoiceWorker` 旧签名 `chat_fn`，现签名是 `stream_fn`）。**不要去修、也不要当成自己打坏的**；本设计的预期是"红态仍是这 4 个、通过数只增不减"。

**端到端**：§11 的 16 条全部在**本机后端**可验（集体层/老人层用 `/api/chat` + `X-Surface` 模拟），**不需要动车、不需要板卡**。

**红线自检（改代码时逐条对照）**：
- R1 前端 role 不可信 → 所有业务接口无 role 入参，uid→role 由 `derive_role` 推导。
- R2 fail-closed → 未知 uid/会话过期 = 集体层最小能力（无个人档案、无车控工具）。
- R3 急停/呼救永远放行。
- R4 医疗写入红线不受角色影响。
- R5 层级上下文单向（集体→老人可读，反向不可读，跨病房不可读）。
- 后端必须容忍可选依赖缺失、降级启动（新代码不得引入顶层硬 import，见根 `AGENTS.md`「系统稳健性」）。
- SSE 事件改动必须同步 `frontend/packages/shared/src/events.ts`（唯一事实来源）。

## 13. 影响面清单（实现时逐项核对）

| 文件 | 改动 | 轮次 |
|---|---|---|
| `LLM/locator.py` | **不改代码**（一期已落地）：直接复用 `get_pose()` / `available()` / `current_map()` / `set_pose_for_test()`；P0 只补单测锁定其降级语义 | P0（复用） |
| `LLM/session.py` | **新增**：双槽 Principal、`set_subject`/`derive_role`、登录/登出/改口令/开关口令门、TTL tick、**当前病房与自动切换判定（含防抖/手动覆盖）** | P0 |
| `LLM/policy.py` | **新增**：`POLICY_DEFAULTS` 三角色策略、`role_policy()`、`allowed_tools`/`data_scope`/`ward_context` | P0（`check_action` 动作分级**不写空壳**，随 P1 与 §7 一起做） |
| `LLM/prompt/ward.md` `elder.md` `admin.md` | **新增**：角色提示词片段 | P0 |
| `LLM/prompt.md` | 不动（共用 base） | — |
| `LLM/conf.py` | 新增设置项（`admin_auth_required` / `admin_session_ttl_s` / `ward_context_window` / `ward_autoswitch_enabled` / `ward_switch_debounce` / `ward_zone_default_r` / `manual_override_sec` / `ward_map_source`）；**不加 `rosbridge_url`**（那是 `conf.ROSBRIDGE_URL`，环境变量可控）；`current_map` 一期已有、**不重复加** | P0 |
| `LLM/db.py` | `profiles` 加 `kind`/`ward_id`/`ward_map`/`ward_zone`（纯加列）；`list_profiles(kind=)`、`get_profile_kind`、`upsert_ward`/`set_ward_zone`/`set_profile_ward`/`list_wards`；口令哈希 raw-key 读写；`list_zones(map_name, uid, kind=)` 补 `kind` 过滤、`get_zone(uid, map_name="")` 补地图名（顺手修掉既有跨图歧义） | P0 |
| `LLM/zonegeo.py` | **新增**（约 30 行，纯 stdlib）：`point_in_polygon(x, y, poly)` + `zone_hit(zone, x, y)`（多边形射线法；`shape='rect'` 用外接矩形；点数 <3 返回 `False`）——口径照抄前端 `packages/mapeditor/src/lib/coords.ts:44` | P0 |
| `LLM/chat.py` | `build_system/build_messages/chat_stream` 接 `principal`；`_load_role_prompt`；集体上下文注入 | P0 |
| `LLM/tools.py` | `@tool(roles=)`、`effective_tools(settings, principal)`、`run_tool(name, args, principal)` | P0 |
| `LLM/memory.py` | `note_turn()` 对 `role=ward` 不沉淀 | P0 |
| `LLM/voice_api.py` | 会话持有权移交 `session.py`（同名函数转发） | P0 |
| `LLM/voice/worker.py` | `_handle_speech` 按角色分支（§4.3） | P0 |
| `LLM/server.py` | 新增 `/api/session/{login,logout,password,admin-auth}`、`/api/wards`、`/api/wards/{uid}/zone`（调 `maptags.record_room_polygon`）、`/api/policy/roles`；业务接口按 `X-Surface` 取 principal；`lifespan` 补首启口令 + 每秒 `session.tick()`；广播 `session_expired`/`admin_auth_changed`/`ward_changed` | P0 |
| `frontend/packages/shared/src/{api/client.ts,api/session.ts,events.ts}` + `tests/` | **扩展现有文件**（**不要**新建 `src/session.ts`——session API 在 `src/api/session.ts`）：client 加 `X-Surface`；session 加登录/登出/口令/口令门/病房 API；events 事件 +3 与 `user_changed` 载荷扩展；vitest 补用例 | P0 |
| `frontend/packages/kiosk/src/{components/UserSwitcher.vue,components/VoiceStatusBar.vue,App.vue}` | **左侧层级栏**（管理层/集体层/老人层）+ 状态条角色徽标/倒计时 + 管理员登录与口令设置 + 监听 `ward_changed`/`session_expired` | P0 |
| `frontend/packages/admin/src/{App.vue,pages/RolesPage.vue,pages/WardsPage.vue,pages/RegisterPage.vue}` | 登录门 + Header 身份/退出 + 页签数组加「身份与权限」「病房管理」+ 注册向导加病房归属下拉 | P0 |
| `LLM/tests/*` + `tests/`（既有） | 10 个新测试文件（§12）；既有基线 191 passed / 4 failed 的红态**不动** | P0 |
| `LLM/car_mcp/car_server.py` / `car_controller.py` | 新增 `robot_goto` + 按地图点导航路径 | **P1（暂缓）** |
| `ros2_car/` | 不改代码（沿用 `robot/navigate_to` 的 x/y/theta 分支；`place` 仍拒绝） | P1（暂缓） |
| `docs/log.md` | 追加实现日志 | P0 |
| `AGENTS.md` | 「关键约定」补角色闸门与红线 R1–R5；`LLM/` 模块表补 `session.py`/`policy.py`/`zonegeo.py`/`prompt/` | P0 |

---

## 14. 复审结论（2026-09-14 二次修订）

**复审方法**：把规格里的每一处接口/字段/配置项逐条对照**当前工作区代码**（`LLM/*.py`、`frontend/packages/*`）核实，而不是靠文档互引。README 级结论：**设计意图与决策（三层结构、双槽、口令可改可关、按位置自动切、R1–R5 红线、MCP 暂缓）全部成立，不需要推翻**；出问题的是"落地口径"——有几处描述的是**已经变过的中间版本**，照它写代码会撞车或建出第二份真相。

| # | 问题（复审发现） | 证据 | 处置（已写进正文章节） |
|---|---|---|---|
| F1 | **病房区域几何载体过期**：正文与 P0 计划都按"`zones` 表 = 唯一真相 + 自增 `id` + `profiles.zone_id`"写；中途还出现过 `profiles.zone_json` | 地图编辑器定案：真相 = `<图名>.tags.json`（`maptags.py`），`brain.db.zones` 降为只读缓存；`map-editor-design.md` §〇 第 7 条、§十 回填项明确要求本规格二次回填 | §3.1.1 / §4.5 / §9：几何真相 = tags.json；`profiles` 记 **`ward_map` + `ward_zone`（`z<N>`）**；解析走 `maptags.get_zone(map_name, uid)`；顺带记录三次口径变更史，避免再用 `zone_id`/`zone_json` |
| F2 | **`rosbridge_url` 不是设置项**：它不在 `DEFAULT_SETTINGS`，而 `set_settings()` 只接受白名单里的 key → 写了也存不进去、改不了 | `conf.py:80-83` 是模块级 `ROSBRIDGE_URL`（环境变量可改）；`db.set_settings()` 白名单逻辑 | §9：改为新增 `ward_autoswitch_enabled`（默认 True）承担"一键停用"；`rosbridge_url` 从设置项清单删除 |
| F3 | **`locator.py` 不是本设计新增**：一期地图编辑器已落地，且既有签名与计划里"新写一份"不同（`available()` 返回 `(bool, reason)`；`get_pose()` 拿不到返回 `None`；`current_map()`/`set_pose_for_test()` 都已存在） | `LLM/locator.py`（240 行）、`LLM/roslink.py`（255 行）已在工作区 | §4.5 / §13：**复用，不重写**；P0 只补单测锁定降级语义 |
| F4 | **`db` 里已有 `zones`/`destinations` 访问函数，且签名与计划冲突**：`db.get_zone(uid)` 只按 uid（多图歧义）、`db.list_zones(map_name, uid)` 无 `kind` | `db.py:1231-1260`；被 revert 的那版计划写的是 `get_zone(int)` + `add_zone()` | §13：改为**向后兼容扩参**（`list_zones(..., kind="")`、`get_zone(uid, map_name="")`），**不新建表、不加 `add_zone`**；`zones` 只读 |
| F5 | **地点真相也过期**：§7.1 写"新表 `destinations`，`name` 为主键"，§7.2 写"地名→坐标唯一事实来源在 LLM 侧 destinations 表" | 同 F1：地点已随地图迁到 tags.json，稳定身份是 `d<N>` uid | §7.1 / §7.2 / §9：改为 tags.json 为真相、`brain.db.destinations` 为只读缓存、主键 `(map_name, uid)`、**本轮不建任何新表** |
| F6 | **前端落点写错**：计划让"新建 `shared/src/session.ts`"，实际 session API 在 `src/api/session.ts`；`X-Surface` 前端零实现；kiosk 换人入口在 `VoiceStatusBar.vue:34`，弹层是 `UserSwitcher.vue`；admin 页签注册唯一位置是 `App.vue` 的 `tabs` 数组 | `shared/src/`（4 个文件）、`kiosk/src/`（8 个文件）、`admin/src/App.vue:14-23`；grep `X-Surface` 21 处全在文档里 | §8 / §13：改成**扩展现有文件**并写清真实落点 |
| F7 | **测试基线与既有 API 名过期**：计划写"预期全 passed / 既有 83 项不回归"，实际全量是 **191 passed / 4 failed**（红态在 `tests/test_modules_status.py` 与 `tests/test_unlock_switch.py`，与本次无关）；计划里的 `db.add_history` 实际叫 **`append_history`** | 本次实测 `pytest LLM/tests tests -q` → `4 failed, 191 passed in 78.34s`；`db.py:921` | §12：写死真实基线与"4 个红态不许修"；计划里所有 `add_history` 改为 `append_history` |
| F8 | **「当前在跑哪张图」取值不可靠**：按 `settings.current_map` 判会"换图后位姿恰好落进旧图病房"→ 静默切错病房（D17 明令禁止） | 一期已落地 `/map` 指纹反查 `locator.current_map()`；`conf.py:42` 注释明说 `current_map` ≠ 车此刻在跑的图 | §2 D17 补注 / §4.5：默认以指纹反查为准，认不出**不判命中**（fail-safe）；`settings.current_map` 仅在 `ward_map_source="setting"` 时当判定依据 |
| F9 | **热路径会打网络**：每秒 tick 里调 `maptags.get_zones()` 会走 `_ensure_fresh → sync_map`（`MAPS_IO=ssh` 下可能秒级阻塞），`locator.current_map()` 也会列图 + 逐图读元数据 | `maptags.py:370/390`、`locator.py:200-218` | §4.5：tick **只读本地 `db.zones` 缓存**；`current_map()` 结果做 10s TTL 缓存 |
| F10 | **档案编辑会清掉病房关联**（上一版实现踩过的坑：`f6f6e54` 专门修过） | git 历史 `f6f6e54 fix(llm): 病房 zone_id 只由 upsert_ward 写，避免档案编辑静默清掉区域关联` | §9 补注：`ward_id`/`ward_map`/`ward_zone` **只允许 `upsert_ward`/`set_ward_zone`/`set_profile_ward` 写**，`upsert_profile` 不碰 |
| F11 | **两条只有写代码才会暴露的隐患**（来自被 revert 的那版实现）：① `kind=COALESCE(profiles.kind, excluded.kind)` 因旧行已被回填成 `'elder'`、永不为空 → 兜底**永不触发** → 把已有 uid 提升成病房**静默失败**却照写区域关联（半状态）；② `list_profiles()` 不过滤 `kind` → 病房行混进 `GET /api/profiles` 的"老人列表"（记忆页/对话页/注册页下拉、车前屏换人弹层全吃它） | 被 revert 提交 `478504d`/`f6f6e54` 的复盘 | §9 加"不许用 COALESCE 兜底、直接 `SET kind='ward'`"；§4.2 表格新增一行：`GET /api/profiles` 默认只列 `kind='elder'`（`?kind=all` 才全量）；计划任务 2/11 各带一条回归用例 |

**未改动（复审确认成立）**：三层结构与能力矩阵（§3.3）、双槽会话与提权只升不降（§4）、口令可改可关与首启随机口令（D12/D13）、提示词分层方式（§5，`prompt.md` 继续作共用 base）、R1–R5 红线（§1.3）、P1 暂缓范围（§6.3/§7 的动作分级与 `robot_goto`）。

**给实施者的三条提醒**：① 动手前先跑一次 §12 的基线命令，把"4 个既有红态"认下来；② 任何"改标记"的接口都必须是「改 `<图名>.tags.json` → `maptags.sync_map()` 刷缓存 → 审计」三步，**禁止只改 SQLite**；③ 病房自动切换的一切路径都要能"拿不到就不切"。
