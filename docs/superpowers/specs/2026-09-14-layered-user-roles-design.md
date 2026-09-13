# 分层用户体系设计（管理员层 / 众人层 / 老人层）

> 日期：2026-09-14 ｜ 状态：待用户审查 ｜ 范围：后端会话与权限层 + 提示词分层 + 前端身份切换 + 地点白名单
> 关联：`docs/superpowers/specs/2026-08-27-frontend-multi-end-design.md`（D11 手动选择+锁定模式，本设计在其上扩展「角色」维度）、
> `docs/superpowers/specs/2026-08-22-voice-pipeline-design.md`（声纹识别产出 uid，本设计消费它）、
> `docs/superpowers/specs/2026-08-24-elder-registration-flow-design.md`（老人档案与声纹注册）、
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
| 管理员层 | `admin` | **本期实现**（口令登录；护士站/家属用） |
| 老人层 | `elder` | **本期实现**（声纹识别 + 手动选人；面向被陪护老人） |
| 众人层 | `public` | **本期只落地最小能力集**（= 未登录访客的默认态：无工具、无个人数据），能力扩展留待后续单独 spec |

用户原话（2026-09-14）：
> 「我想创立一个管理员用户，与老人用户分开。前端可以切换。我的想法是分层级，比如管理员层，众人层（这个先放着不做），老人层，方便给每个层级的用户分配不同的权限和提示词。比如老人层可以使用声纹识别，发出简单的指令（比如让小车从病房出去），管理层可以发布所以指令」
> 「挂人身份上。但不要声纹，管理员是特殊账号，以免误识别。但语音链路要保留，以实现管理员也可以语音控制」

### 1.3 关键架构原则（继承且强化）

交互闭环在后端（见前端多端设计 D4）：**权限判定必须全部发生在后端**。前端只做两件事——显示当前身份、发起「以某身份登录」的请求。

> **红线 R1：前端传上来的 role 一律不可信。** `/api/chat`、`/api/voice/*`、`/api/tools` 等业务接口**不接受** role 参数；role 只能由后端会话槽位（`LLM/session.py`）推导。前端若要改角色，只能走 `/api/session/login`（口令）或 `/api/session/logout`。

> **红线 R2：fail-closed。** 角色未知、会话过期、字段缺失时，按**最保守**处理（等同 `public`：无工具、无个人数据），绝不按 `elder` 放行。

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
| D6 | 众人层 | 本期不做独立功能，只作为 `public` 策略落地（未登录默认态 = 最小能力集），前端不暴露入口 |
| D7 | 会话粒度 | **双槽会话**：`kiosk` 槽（车前设备，承载语音）与 `admin` 槽（管理台）各自持有 role；`uid`/`locked` 仍全局共享（沿用现状，切人两端同步） |
| D8 | 管理员会话时效 | 默认 **5 分钟**（`admin_session_ttl_s` 可配），无操作自动降权回 `public` 并广播 `session_expired`；提权**只升不降** |
| D9 | 提示词分层方式 | 保留 `LLM/prompt.md` 作**共用 base**（不搬文件，零破坏），新增 `LLM/prompt/<role>.md` 角色片段，运行时拼接 |
| D10 | 地点坐标来源 | 复用现有 car MCP `robot_status` 读位姿：管理员在管理台点「记录当前位置为『护士站』」，后端落 `destinations` 表；**地名→坐标的唯一事实来源在 LLM 侧**，车端只收 x/y/theta（车端 `place` 字段当前明确拒绝，见 §7.2） |
| D11 | 二次确认实现位置 | **后端状态机**（会话内挂 `pending_action`，TTL 30s），不依赖前端——无屏也能用 |
| D12 | 口令初始值 | 首次启动若无 `ADMIN_PASSWORD` 且库内无哈希 → 生成 6 位随机口令，打印到后端终端 + 落审计（`admin_password_generated`），并要求首次登录后修改 |

## 3. 核心模型

### 3.1 Principal（主体）

```
Principal = {
  role:  "admin" | "elder" | "public",
  uid:   str | None,        # elder→档案 uid（elder_001）；admin→"admin"；public→None
  source: "manual" | "voiceprint" | "password" | "default",
  slot:  "kiosk" | "admin", # 该角色挂在哪个端槽位
  until: float | None,      # 管理员提权到期时间戳（monotonic）；None=不过期
}
```

`uid` 全局唯一一份（两端共享，沿用现状）；`role` 按槽位（D7）。

### 3.2 角色策略包（Policy）

`LLM/policy.py` 中的 `POLICY_DEFAULTS`，key 为 role：

| 字段 | 含义 |
|---|---|
| `prompt_file` | 角色提示词片段路径（`LLM/prompt/elder.md` …） |
| `allowed_tools` | 工具白名单（`None`=全部；`[]`=无工具）；与全局 per-tool 开关取**交集** |
| `data_scope` | `self`（仅本人档案/RAG）/ `all`（全部）/ `none`（不注入） |
| `destinations` | `none` / `whitelist`（按表）/ `any`（可下任意坐标） |
| `risk_confirm` | 中风险动作是否需要口头二次确认 |
| `voice` | 是否允许语音输入、是否需要唤醒词 |

### 3.3 能力矩阵（本设计的事实来源）

| 能力 | `public`（未登录默认态） | `elder` | `admin` |
|---|---|---|---|
| 闲聊问答 | ✅（通用人设，**不注入**任何档案） | ✅（注入本人档案/RAG/语录） | ✅（专业向人设，默认不注入老人记忆） |
| 读本人档案/记忆 | ❌ | ✅ 仅本人 | ✅ 全部 |
| 读审计/工具日志/系统状态 | ❌ | ❌ | ✅ |
| 车 · 状态查询（位姿/电量） | ✅ 只读播报 | ✅ | ✅ |
| 车 · 急停 / 呼救 | ✅ 永远允许（R3） | ✅ | ✅ |
| 车 · 去**白名单内**地点 | ❌ | ✅ 高风险需一句确认 | ✅ 直接执行 |
| 车 · 任意坐标 / 跨区域 | ❌ | ❌ | ✅ |
| 车 · 解除急停 / 改底盘参数 | ❌ | ❌ | ✅ |
| 系统 · 关机 / 改设置 / 改口令 | ❌ | ❌ | ✅ |
| 记忆写入/纠正 | ❌ | ✅（仅自己对话沉淀） | ✅ |
| 地点白名单管理 | ❌ | ❌ | ✅ |
| 身份切换 / 登录登出 | ❌ | ❌ | ✅ |

## 4. 会话与认证

### 4.1 新模块 `LLM/session.py`

取代 `voice_api._session_uid/_session_locked` 的持有权（`voice_api` 保留同名函数作薄封装转发，**现有调用点不用改**）。

```
get_principal(slot) -> Principal      # 读；过期即降权（懒 tick）
set_elder(uid, locked, source)        # 语音/手动认人：写 uid + 设 kiosk 槽 role=elder
login_admin(password, slot)           # 校验口令 → 该槽 role=admin + until=now+TTL
logout(slot)                          # 该槽 role=public
tick()                                # 定时（沿用 bus/reminder 线程或 lifespan 后台任务）
```

- **单进程全局**：沿用现状（一辆车、一块屏），不做 per-connection 会话。
- **双槽**：`kiosk` 槽承载语音链路与车前屏；`admin` 槽承载管理台。**管理台登录不会把车前屏提权**（这是双槽存在的唯一理由）。
- **槽位判定**：请求头 `X-Surface: kiosk|admin`（shared 的 API client 统一带上）；缺省时按路径判定（静态 `/admin/*` → admin，其余 → kiosk）。
- **提权只升不降（D8）**：`kiosk` 槽为 `admin` 期间，声纹识别到某个老人**不改变 role、不改 uid**，只落审计 `voice_spk action=ignored_in_admin`。降权只有三条路：TTL 到期、显式登出、显式切换。
- **TTL 到期**：`tick()` 每分钟检查，到期 → 该槽 role 回 `public`，广播 `session_expired`（bus），落审计。

### 4.2 接口变更

| 接口 | 变更 |
|---|---|
| `GET /api/session/user` | 响应扩展：`{uid, name, role, locked, source, slot, ttl_remain}`（`role` 按请求槽位返回） |
| `POST /api/session/user` | **保持现签名**（`{uid, locked}`），只允许切人/锁定；**拒绝 `role` 字段**（有则 400） |
| `POST /api/session/login`（新增） | `{password}` + `X-Surface` → 成功 `{ok:true, role:"admin", uid:"admin", ttl_remain}`；失败 `{ok:false, error}`，失败计数 + 审计 |
| `POST /api/session/logout`（新增） | 该槽回落 `public` |
| `POST /api/session/password`（新增） | 管理员改口令（需旧口令） |
| `GET /api/policy/roles`（新增） | 返回策略矩阵（`admin` 可见全量，其它角色只拿到自己那份摘要） |

口令校验：`hashlib.pbkdf2_hmac("sha256", pw, salt, 200_000)`，盐与哈希存 `settings` 表（key `admin_password_hash` / `admin_password_salt`，**不落明文**）；初始口令见 D12。防暴力：连续 3 次失败 → 该槽冷却 10s。

### 4.3 语音链路改动（最小）

`voice/worker.py::_handle_speech`：把 `uid = id_mod.effective_uid(...)` 之后的流程改为

```
principal = session.get_principal("kiosk")
if principal.role == "admin":        # D8：不提权也不降权
    uid = principal.uid or "admin"   # 语音按 admin 角色走
else:
    uid = id_mod.effective_uid(...)  # 现有声纹逻辑，role=elder
session.set_elder(uid, locked, source="voiceprint")   # 仅 elder 分支
```

`_stream_fn(uid, text)` 签名不变，但内部通过 `session.get_principal("kiosk")` 取角色——**不新增参数、不信任调用方**。

## 5. 提示词分层（D9）

### 5.1 文件组织

```
LLM/prompt.md            # 共用 base：人设（小护）+ 安全红线（现状文件保留不动）
LLM/prompt/elder.md      # 老人层片段（本期新增）
LLM/prompt/admin.md      # 管理层片段（本期新增）
LLM/prompt/public.md     # 众人层片段（本期新增，最小版）
```

- `chat.py::_load_prompt_base()` **逻辑与路径不变**（继续读 `prompt.md` 的 `<!-- PROMPT -->` 之下正文、缺文件退回 `_DEFAULT_PROMPT_BASE`）。
- 新增 `_load_role_prompt(role)`：读 `LLM/prompt/<role>.md` 全文，缺失 → 返回空串 + 告警一次（`prompt_role_missing` 审计），**不阻断对话**。
- 装配：`build_system(principal, settings, query)` = `base` + `role_fragment` + 现有动态块（档案/记忆 → 摘要 → 语录 → 当前时间）。

### 5.2 各层片段要点（内容要点，措辞在实现时按现有 prompt.md 的笔法写）

- **elder.md**：能力边界说清楚——「我只认得去 XX、XX 这几个地方；你换个说法我可能听不懂」。确认仪式（D11）的一句固定话术模板。安全红线的口语化复述。样例句：
  > 你能去的只有这几处：{可用地点列表}。别的地方你去不了，就直说「这个我去不了，我只认得……」，别硬答应。
  > 他要走远一点的地方，你先问一句「要去{地点}吗？说一声我就走」，他答应了再动。
- **admin.md**：专业、简洁、可带术语与数字（坐标、状态、参数）；明确「你不是在陪聊，你在向管理员报告」；不做老人腔、不卖萌；高风险动作执行前回一句「已执行：去护士站」。样例句：
  > 你在跟管理员说话，不是跟老人。直接给结论和数字（位姿/状态/参数），一句话报完，不用寒暄、不用哄。
- **public.md**：通用礼貌助手；**不主动谈任何老人信息**；能力边界（能聊天、能报小车状态，不能命令小车移动）。样例句：
  > 你不知道在这台车上看护的是哪位老人，别人问起就说「这个我不清楚」。小车要去哪儿你说了不算。

### 5.3 记忆注入按 `data_scope` 开关

- `public` → 不调 `_recall_cached`、不注入摘要/语录。
- `elder` → 用 `principal.uid`（现状行为）。
- `admin` → 默认不注入；管理台「以某老人视角看」时显式传 `as_uid`（仅该调试入口使用，落审计）。

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

| 风险 | 例子（现有 car MCP 工具名） | public | elder | admin |
|---|---|---|---|---|
| low | `robot_status`、`robot_stop`、地点表内 `risk=low` 的 `robot_goto` | allow（`robot_stop`）/ deny（其余） | allow | allow |
| mid | 地点表内 `risk=high` 的 `robot_goto`（离房/出走廊）、`robot_move`/`robot_turn` | deny | **confirm**（D11） | allow |
| high | 显式坐标的 `robot_goto`、解除急停、改底盘参数、关机 | deny | deny | allow |

判定结果**全部落审计**：`policy_confirm` / `policy_deny` / `policy_allow_dangerous`。
被 deny 的动作必须**给模型一句可复述的拒绝理由**（如「这个我去不了，我只认得护士站和活动室」），不能静默失败。

## 7. 地点白名单（D5 / D10）

### 7.1 数据

新表 `destinations`：`name`(PK) / `goal_json`（`{x, y, yaw, frame:"map"}`）/ `risk`（`low|high`）/ `elder_allowed`(0/1) / `note` / `learned_by` / `updated_at`。

新设置项（`conf.DEFAULT_SETTINGS`）：`elder_destinations_enabled: True`、`elder_confirm_required: True`、`admin_session_ttl_s: 300`、`pending_action_ttl_s: 30`、`dest_point_tolerance_m: 0.3`。

### 7.2 新增车端工具 `robot_goto`（P1 前置项）

现有 car MCP（`LLM/car_mcp/car_server.py`）只有 `robot_move(direction, distance_m)` / `robot_turn(angle_deg)` / `robot_stop()` / `robot_status()`，**没有"去某个地图点"的能力**，所以本设计需要新增：

```
robot_goto(destination: str = "", x: float = 0, y: float = 0, yaw: float = 0) -> str
```

- **模型只能填 `destination`（地名）**；`x/y/yaw` 是给管理员/内部用的显式坐标，`elder`/`public` 传入一律被清空（见下）。
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

- 新增 `src/session.ts`：`login(password)` / `logout()` / `getSession()` / `ttlRemain` 心跳。
- API client 统一带 `X-Surface` 头（kiosk 包注入 `kiosk`，admin 包注入 `admin`）。
- `src/events.ts`（**唯一事实来源，改后端 publish 点必须同步这里**）：
  - `user_changed`：payload 增 `role`、`slot`；
  - 新增 `session_expired`（`{slot}`）、`destination_changed`（`{action,name}`）。

### 8.2 kiosk

- 状态条（`VoiceStatusBar.vue`）：`👴 elder_001 🔒` → 管理员态显示 `🛡 管理员 4:32`（倒计时，来自 `ttl_remain`）。
- 切换弹层（`UserSwitcher.vue`）加「管理员登录」入口：口令输入（不回显、可清空），登录后弹层关闭、状态条变红/金以示区分。
- 管理员态额外显示：可用地点列表、「记录当前位置为…」按钮（调 `/api/destinations/learn`）、「退出管理员」。
- 老人态**不显示任何管理员入口的细节**（只显示一个「管理员登录」小按钮，防止老人误触后卡住——连错 3 次自动冷却并提示）。

### 8.3 admin

- **登录门**：未登录时整站只显示登录卡（口令）；登录成功后进入现有页签。
- Header：当前身份 + 剩余时间 + 「退出」。
- 新增页签「身份与权限」：策略矩阵只读展示（来自 `GET /api/policy/roles`）+ 可调项（`admin_session_ttl_s`、`elder_confirm_required`、`elder_destinations_enabled`）。
- 新增页签「地点白名单」：列表（名字/risk/老人是否可去/坐标/更新时间）、`记录当前位置为…`、启停、删除。

## 9. 数据与审计

- **新表**：`destinations`（§7.1）。**不建 `accounts` 表**（D1 轻量口径）；口令哈希放 `settings` 表，将来扩多账号时再建表并迁移。
- **审计事件**（`log.py::log` 的 `event`）：`session_login` / `session_login_fail` / `session_logout` / `session_expired` / `admin_password_generated` / `admin_password_changed` / `policy_deny` / `policy_confirm` / `policy_allow_dangerous` / `destination_change` / `prompt_role_missing`。
- 每条策略判定至少含：`role` / `uid` / `slot` / `tool` / `decision` / `reason`。

## 10. 分阶段实施

| 阶段 | 内容 | 交付物 | 预估 |
|---|---|---|---|
| **P0** 权限地基 | `session.py` 双槽主体 + 口令登录/登出/改口令 + 提示词分层（4 个 md + `_load_role_prompt`）+ `@tool(roles=)` + `effective_tools/run_tool` 校验 + 前端身份显示与登录门 + 审计 | 老人层/管理层可切，越权请求被拒且有审计 | ~1 天 |
| **P1** 动作约束 | car MCP 新增 `robot_goto`（§7.2）+ `destinations` 表与接口 + 地点白名单解析 + 风险分级 + 二次确认状态机 + admin「地点白名单」页签 + TTL 自动降权 + `public` 最小能力集收口 | 老人说「去护士站」能走通，说「去停车场」被拒 | ~1.5 天 |
| **P2** 预留 | 众人层能力扩展（独立 spec）、`accounts` 表多账号、老人动作审批流（待办→管理台确认）、权限矩阵细化到「谁能看哪段记忆」 | 另立 spec | — |

**明确不做（YAGNI）**：用户名/密码注册流程、RBAC 权限编辑器、per-user 工具开关、OAuth/JWT、per-connection 多会话、审计日志前端查询界面（沿用现有「工具日志」页签）。

## 11. 验收标准（P0 + P1）

1. 未登录时对 `/api/chat` 问「让小车去护士站」→ 模型**看不到** `robot_goto` 工具，回答为「这个我做不到」；审计出现 `policy_deny` 或工具不可见。
2. 管理台登录后同一句话 → 真正下发导航目标（MCP `robot_goto` 调用成功 / 或 high 风险走 `robot_move`+`robot_turn`），审计 `policy_allow_dangerous`（high 风险）或 allow。
3. 老人态（声纹识别为 `elder_001`）说「去护士站」（表内 `risk=high`）→ 机器人问确认 → 回「好」→ 执行；回「不用了」→ 不执行且有审计。
4. 老人态说「去停车场」（不在表）→ 拒绝并有可复述理由，**无任何底盘命令发出**（用 `ros2_car/tools/probe_chassis_frames.py` 只读统计命令号验证）。
5. 管理台登录 5 分钟后无操作 → 状态条/Header 提示过期，权限回落为 `public`，再次下指令被拒（审计 `session_expired`）。
6. 管理台登录**不影响**车前屏角色：kiosk 槽仍为 `elder`（双槽隔离，D7）。
7. 管理员在车前语音下指令 → 按 `admin` 角色执行（D3），且期间声纹识别到老人**不降权**（审计 `ignored_in_admin`）。
8. 前端伪造 `role:"admin"` 字段调用 `/api/session/user` → 400；未登录时以 `elder` 态在对话里要求「去停车场坐标 3,4」→ 工具不可见或 `policy_deny`，无命令下发。
9. 未登录状态按 SOS → 仍然成功（R3）。
10. 访客（`public`）对话 → System Prompt 内**不含**任何老人档案/摘要/语录（抓 `GET /api/context` 或审计比对）。

## 12. 测试与红线清单

**单测（pytest，放 `LLM/tests/`）**：
- `test_policy_matrix.py`：角色 × 工具 × 参数 → 期望 decision（§3.3/§6.3 矩阵逐格）。
- `test_session.py`：TTL 到期降权、提权只升不降、双槽隔离、登录失败冷却。
- `test_destination_resolve.py`：表内/表外/坐标注入/边界余量。
- `test_confirm_flow.py`：`is_affirmative` 正负样本 + `pending_action` TTL。
- `test_prompt_layers.py`：四层装配正确、缺文件降级不崩。

**端到端（后端本机）**：§11 的 10 条，其中第 4、7 条需在板卡上跑（需用户确认现场安全后由助手远程执行，固件烧录仍由用户按 F5）。

**红线自检（改代码时逐条对照）**：
- R1 前端 role 不可信 → 所有业务接口无 role 入参。
- R2 fail-closed → 未知角色 = `public` 能力集。
- R3 急停/呼救永远放行。
- R4 医疗写入红线不受角色影响。
- 后端必须容忍可选依赖缺失、降级启动（新代码不得引入顶层硬 import，见根 `AGENTS.md`「系统稳健性」）。
- SSE 事件改动必须同步 `frontend/packages/shared/src/events.ts`（唯一事实来源）。

## 13. 影响面清单（实现时逐项核对）

| 文件 | 改动 |
|---|---|
| `LLM/session.py` | **新增**：双槽 Principal、登录/登出、TTL tick |
| `LLM/policy.py` | **新增**：策略表、`check_action`、`resolve_destination`、`is_affirmative` |
| `LLM/prompt/elder.md` `admin.md` `public.md` | **新增**：角色提示词片段 |
| `LLM/prompt.md` | 不动（共用 base） |
| `LLM/conf.py` | 新增 5 个设置项；`MCP_SERVERS` 增 `roles` |
| `LLM/db.py` | `destinations` 表 + CRUD；口令哈希读写 |
| `LLM/chat.py` | `build_system/build_messages/chat_stream` 接 `principal`；`_load_role_prompt` |
| `LLM/tools.py` | `@tool(roles=)`、`effective_tools(settings, principal)`、`run_tool(name, args, principal)` |
| `LLM/voice_api.py` | 会话持有权移交 `session.py`（同名函数转发） |
| `LLM/voice/worker.py` | `_handle_speech` 按角色分支（§4.3） |
| `LLM/car_mcp/car_server.py` | **新增** `robot_goto(target, explicit)` 工具（§7.2）；既有 4 个工具名不变 |
| `LLM/car_mcp/car_controller.py` | 新增按地图点导航的调用路径（`robot_navigation`/Nav2），不可用时 `ok:false` 降级 |
| `ros2_car/` | **不改代码**：沿用既有服务 `robot/navigate_to`（x/y/theta 分支）；`place` 分支仍保持「地点表未配置」的拒绝，本期不启用 |
| `LLM/server.py` | 新增 6 个接口；业务接口取 principal；bus 广播 `session_expired`/`destination_changed` |
| `frontend/packages/shared/src/{session.ts,events.ts,api/*}` | 新文件 + 事件扩展 + `X-Surface` 头 |
| `frontend/packages/kiosk/*` | 状态条角色徽标 + 管理员登录入口 + 管理员态面板 |
| `frontend/packages/admin/*` | 登录门 + Header 身份 + 两个新页签 |
| `docs/log.md` | 追加实现日志 |
| `AGENTS.md` | 「关键约定」补角色闸门三条（R1-R3） |
