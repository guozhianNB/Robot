# 分层用户体系设计（管理层 / 集体层 / 老人层）

> 日期：2026-09-14（含当晚用户追加需求：集体层=病房用户、口令可改可关、MCP 暂缓）｜ 状态：待用户审查 ｜ 范围：后端会话与权限层 + 提示词分层 + 前端层级切换 + 集体层（病房用户）
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
| 集体层 | `ward` | **本期实现**（**同一个病房的老人打包成一个「病房用户」**，uid 形如 `ward_101`；小车进病房与"大家"打招呼、公布消息；未识别/未登录时的默认态也是它） |
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
| D16 | 本轮范围 | 用户 2026-09-14 明确「**先不管 mcp，先完成用户系统**」：本轮只做 §10 的 P0（角色地基 + 集体层 + 提示词分层 + 层级 UI）；依赖 car MCP 的**地点白名单 / `robot_goto` / 二次确认**（原 P1）**暂缓**，待 MCP 线重启再做 |

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
- 装配：`build_system(principal, settings, query)` = `base` + `role_fragment` + 现有动态块（档案/记忆 → 集体上下文 → 摘要 → 语录 → 当前时间）。

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
- MCP 工具：`MCP_SERVERS` 每项加 `roles` 字段（`car` → `{"elder","admin"}`；`fetch`/`tavily` → `{"elder","admin"}`；未声明视为 `admin` only——MCP 是不受控外部能力，默认从严）。
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

新表 `destinations`：`name`(PK) / `goal_json`（`{x, y, yaw, frame:"map"}`）/ `risk`（`low|high`）/ `elder_allowed`(0/1) / `note` / `learned_by` / `updated_at`。

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
  ⚠️ 已核实：该 srv 虽有 `place` 字段，但车端当前**明确拒绝** place（`robot_actions.py:179-184`「地点表后置…请改用 x/y/theta」）。故本期**不启用车端地点表**，地名→坐标唯一事实来源放在 LLM 侧 `destinations` 表（管理台可视化编辑）。将来若车端建了 place 表，只需改这一处转发。
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
  - 🏠 **集体层**：病房用户列表（`ward_101 101病房` …），当前项打勾。切换即把会话 uid 设为该病房用户 → role=ward。
  - 👴 **老人层**：仅显示**当前病房**的老人（默认按 `ward_id` 过滤，可切「全部病房」），显示昵称/床位；当前项打勾。
- 状态条（`VoiceStatusBar.vue`）：按角色换徽标——`🏠 101病房`（集体层）/ `👴 张奶奶 🔒`（老人层）/ `🛡 管理员 4:32`（管理层，倒计时来自 `ttl_remain`）。
- 管理员态额外显示：「退出管理员」「口令设置」（改口令 / 开关口令门，调 `/api/session/password` 与 `/api/session/admin-auth`）。
- 集体层态：对话区顶部提示「在跟 101 病房的大家说话」；不做个人档案展示。
- 老人层态：对话区顶部提示「私聊 · 张奶奶」，并可显示一行「病房里刚说过的事」入口（只读）。

### 8.3 admin

- **登录门**：`admin_auth_required=True` 且未登录时整站只显示登录卡（口令）；关闭口令门时直接进入，但顶部常驻红条警示「当前无口令保护」。
- Header：当前身份 + 剩余时间 + 「退出」。
- 新增页签「身份与权限」：策略矩阵只读展示（来自 `GET /api/policy/roles`）+ 口令设置（**改口令 / 开关口令门**，D13）+ 可调项（`admin_session_ttl_s`、`ward_context_window`）。
- 新增页签「病房管理」（集体层）：病房用户列表 + 新建病房 + 把老人归入病房（写 `profiles.ward_id`），并在现有「老人注册」页签的向导里加一个「病房」下拉（按床位号自动建议）。
- 「地点白名单」页签**本轮不做**（随 §7 暂缓）。

## 9. 数据与审计

- **`profiles` 表扩展**（集体层的全部数据基础）：新增 `kind`（`'elder'|'ward'`，默认 `'elder'`）与 `ward_id`（老人所属病房 uid，可空）；病房用户即 `kind='ward'` 的一条 profile（`uid=ward_101`、`bed` 留空、不录声纹）。旧数据迁移：现有 profiles 全部置 `kind='elder'`。
- **新表**：`destinations`（§7.1）——**随 §7 暂缓，本轮不建**。
- **不建 `accounts` 表**（D1 轻量口径）；口令哈希放 `settings` 表（`admin_password_hash` / `admin_password_salt` / `admin_auth_required`），将来扩多账号时再建表并迁移。
- **审计事件**（`log.py::log` 的 `event`）：`session_login`（含 `source=password|auth_disabled`）/ `session_login_fail` / `session_logout` / `session_expired` / `admin_password_generated` / `admin_password_changed` / `admin_auth_changed` / `ward_change` / `policy_deny` / `prompt_role_missing`；暂缓项对应的 `policy_confirm` / `policy_allow_dangerous` / `destination_change` 留待 §7 落地。
- 每条策略判定至少含：`role` / `uid` / `slot` / `tool` / `decision` / `reason`。
- **集体层对话的存储**：直接用现有 `db.add_history(uid=ward_101, ...)`，不新增表；`memory.note_turn()` 对 `role=ward` 不沉淀（§5.3）。

## 10. 分阶段实施

| 阶段 | 内容 | 交付物 | 预估 |
|---|---|---|---|
| **P0（本轮，用户指定优先）** 用户系统 | `profiles.kind/ward_id` 迁移 + `ward` 病房用户 CRUD + `session.py` 双槽主体与 TTL + 口令登录/登出/**改口令/开关口令门** + `derive_role` + 提示词分层（4 个 md + `_load_role_prompt`）+ `@tool(roles=)` 与 `effective_tools/run_tool` 校验 + 集体层上下文注入（含 R5 单向规则）+ 前端左侧层级栏/登录门/病房管理页签 + 审计 | 三层可切：管理层（口令可开可关）、集体层（病房用户）、老人层（声纹/手选）；越权请求被拒且有审计；老人能读到本病房集体上下文 | ~1.5 天 |
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

## 12. 测试与红线清单

**单测（pytest，放 `LLM/tests/`）**：
- `test_derive_role.py`：`admin`/`ward_*`/`elder_*`/未知 uid → 期望角色；伪造前缀（把老人 uid 起成 `ward_` 开头）仍按 `profiles.kind` 判定。
- `test_session.py`：TTL 到期降权、提权只升不降、双槽隔离、登录失败冷却、口令门关闭时登录放行且 `source=auth_disabled`。
- `test_prompt_layers.py`：四层装配正确（含 ward 的"不含个人档案"断言）、缺文件降级不崩。
- `test_ward_context.py`：集体上下文注入单向性（elder 看得到 ward、ward 看不到 elder 私聊）、跨病房不串。
- `test_policy_tools.py`：`effective_tools` 角色白名单 ∩ 全局开关；`run_tool` 二次校验（§11.4）。
- 暂缓项的单测（`test_destination_resolve.py` / `test_confirm_flow.py`）随 §7 一起做。

**端到端**：§11 的 12 条全部在**本机后端**可验（集体层/老人层用 `/api/chat` + `X-Surface` 模拟），**不需要动车、不需要板卡**。

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
| `LLM/session.py` | **新增**：双槽 Principal、`set_subject`/`derive_role`、登录/登出/改口令/开关口令门、TTL tick | P0 |
| `LLM/policy.py` | **新增**：`POLICY_DEFAULTS` 三角色策略、`allowed_tools`/`data_scope`/`ward_context`、`check_action` 骨架 | P0（`check_action` 动作分级随 P1 补全） |
| `LLM/prompt/ward.md` `elder.md` `admin.md` | **新增**：角色提示词片段 | P0 |
| `LLM/prompt.md` | 不动（共用 base） | — |
| `LLM/conf.py` | 新增设置项（`admin_session_ttl_s` / `admin_auth_required` / `ward_context_window`；P1 再加地点与确认项） | P0 |
| `LLM/db.py` | `profiles` 加 `kind`/`ward_id` + 迁移；病房用户 CRUD；口令哈希读写 | P0 |
| `LLM/chat.py` | `build_system/build_messages/chat_stream` 接 `principal`；`_load_role_prompt`；集体上下文注入 | P0 |
| `LLM/tools.py` | `@tool(roles=)`、`effective_tools(settings, principal)`、`run_tool(name, args, principal)` | P0 |
| `LLM/memory.py` | `note_turn()` 对 `role=ward` 不沉淀 | P0 |
| `LLM/voice_api.py` | 会话持有权移交 `session.py`（同名函数转发） | P0 |
| `LLM/voice/worker.py` | `_handle_speech` 按角色分支（§4.3） | P0 |
| `LLM/server.py` | 新增 `/api/session/{login,logout,password,admin-auth}`、`/api/wards`、`/api/policy/roles`；业务接口取 principal；广播 `session_expired`/`admin_auth_changed`/`ward_changed` | P0 |
| `frontend/packages/shared/src/{session.ts,events.ts,api/*}` | 新文件 + 事件扩展 + `X-Surface` 头 | P0 |
| `frontend/packages/kiosk/*` | **左侧层级栏**（管理层/集体层/老人层）+ 状态条角色徽标 + 管理员登录与口令设置 | P0 |
| `frontend/packages/admin/*` | 登录门 + Header 身份 + 「身份与权限」「病房管理」页签 + 注册向导加病房归属 | P0 |
| `LLM/car_mcp/car_server.py` / `car_controller.py` | 新增 `robot_goto` + 按地图点导航路径 | **P1（暂缓）** |
| `ros2_car/` | 不改代码（沿用 `robot/navigate_to` 的 x/y/theta 分支；`place` 仍拒绝） | P1（暂缓） |
| `docs/log.md` | 追加实现日志 | P0 |
| `AGENTS.md` | 「关键约定」补角色闸门与红线 R1–R5；`LLM/` 模块表补 `session.py`/`policy.py`/`prompt/` | P0 |
