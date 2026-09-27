# 小车 Plan 表设计（自动连续执行版）

> 状态：设计定稿，可进入实现计划
>
> 目标：让小车把跨动作、跨对话的任务持久化，并由后端确定性地连续执行。LLM 只生成候选计划；后端状态机是执行状态的唯一事实来源。

## 〇、依赖的现有规格

本文只定义 Plan 编排层，不重复定义下层协议。发生冲突时以下规格优先：

- `docs/superpowers/specs/2026-09-19-car-mcp-design.md`：车控工具、`task_id`、状态确认、急停及车控安全校验；
- `docs/superpowers/specs/2026-09-18-bounded-react-agent-design.md`：对话工具循环边界；
- `docs/superpowers/specs/2026-09-18-nurse-console-design.md`：护士台部署、通知和免登录边界。本文会扩展护士台职责，落地时必须同步修订该规格；
- `docs/superpowers/specs/2026-09-14-layered-user-roles-design.md`：对话主体和普通工具权限。Plan 执行器不属于该角色体系。

以下原则应原样进入实现代码附近的注释：

- LLM 只提候选，后端状态机是唯一事实来源。
- 不能把 `started` 当 `succeeded`。
- 不能谎报已到达。
- 不让 LLM 循环到“感觉完成”。

## 一、范围与边界

Plan 是 ReAct 外层的任务编排器，负责顺序、等待、抢占、恢复、回报和生命周期；现有工具负责执行单个动作。

第一版支持真正的自动连续执行，但只执行有可靠完成信号的动作。明确边界：

- Plan 数据存入 `LLM/data/brain.db`，不进入地图目录；
- 不重写车控，不直接操作 rosbridge；
- 急停不是普通 Plan 步骤，任何时候独立放行；
- 一辆车只有一个共享动作槽位，不执行并行车控；
- 重启后不自动发车；
- 视觉判断、自由聊天和讲新闻不进入自动执行白名单；
- 第一期只支持线性步骤，动态重规划放到第三期。

## 二、什么时候创建 Plan

满足任一条件时创建：

1. 包含两个或以上需要顺序执行的可靠动作；
2. 需要等待时间、人员、设备或前一步结果；
3. 需要跨对话或跨语音轮次继续执行；
4. 需要完成后向老人、护士或管理员回报；
5. 用户明确要求将一个动作纳入计划跟踪。

普通问答、一次只读查询和一次性播报不创建 Plan。预计时长只能作为辅助信息，不能单独决定是否建表。

### 2.1 对话创建入口：`create_plan`

新增本地工具 `create_plan`，这是 LLM 创建 Plan 的唯一入口。`chat_stream()` 不解析自由文本中的
“计划格式”，也不允许模型直接写数据库或设置步骤状态。

`create_plan` 对 `ward/elder/admin` 三层均可见；它只表达“提交候选 Plan”，不授予调用者普通车控
权限。未知主体按 ward 处理仍可提交，但能否自动执行只看第五节的 Plan 独立权限与安全校验。

工具参数固定为：

```json
{
  "title": "先去 A 点，再去护士站",
  "priority": "P2",
  "owner_uid": "elder_001",
  "steps": [
    {"type": "action", "action": "robot_goto_place", "args": {"place": "A点"}, "label": "前往 A 点"},
    {"type": "action", "action": "robot_goto_place", "args": {"place": "护士站"}, "label": "前往护士站"}
  ],
  "report": {"notify": true, "speak_if_present": false}
}
```

后端忽略模型传入的执行权限、完成判据、创建者、状态、attempt、重试次数和时间戳。它根据当前
principal 记录审计来源，但 Plan 执行权限不继承该 principal。`priority` 缺省为 P2；LLM 不能创建
P0，涉及跌倒、呼救等安全事件时仍先走既有告警链路，由后端规则或人工创建/提级 P0。

创建流程：schema 校验 → 动作白名单校验 → 名称/地图目标解析与规范化 → 单事务写 Plan 和 Steps →
广播 `plan_created` → 返回 `{ok, plan_id, display_no, status, summary}`。任一步失败均不落半张 Plan，
工具向用户返回具体错误。包含多个动作、等待或跨轮次回报的请求必须优先使用 `create_plan`；明确的
单次即时车控仍可走原车控工具，但共享仲裁忙时应建议排成 Plan。

## 三、优先级与抢占策略

优先级只决定调度顺序；抢占后的处理由 `preemption` 单独决定。

| 级别 | 含义 | 默认来源 | 默认行为 |
|---|---|---|---|
| P0 | 安全事件后的处置任务 | 告警链路或人工明确创建 | 请求停止当前可中断动作，等待车况重新可信后再调度 |
| P1 | 明确且有时限的照护请求 | 人工、提醒规则 | 可暂停低级别任务 |
| P2 | 普通用户请求 | 对话或人工 | 排队；可暂停 P3 |
| P3 | 巡逻、闲逛等后台任务 | 后端或人工 | 可被 P0-P2 暂停或取消 |

同级按 `created_at` 先后执行。P0 不得由 LLM 降级。人工修改优先级必须带 `version`，避免护士台与 admin 相互覆盖。

`preemption` 取值：

- `resume`：被抢占后保留断点，之后重新校验并继续；
- `cancel`：被抢占后取消，不再恢复；
- `queue`：不抢占当前动作，只排队等待。

第一版没有“安全停靠”能力。抢占正在移动的任务只能调用 `robot_stop` 请求原地停止，随后进入待确认；不得表述为已经安全停靠。是否载人也没有可靠传感器或状态来源，第一版不根据 LLM 填写的“载人”标记改变安全规则。

## 四、数据模型

数据库使用自增整数主键；`display_no` 仅用于界面展示，不能承担唯一性。

### 4.1 `plans`

| 字段 | 说明 |
|---|---|
| `id` | SQLite 自增主键 |
| `display_no` | 如 `PL-0012`，只展示 |
| `kind` | `care/patrol/reminder/report`；不使用语义不清的 `recovery` |
| `title` | 人类可读标题 |
| `priority` | `P0/P1/P2/P3` |
| `preemption` | `resume/cancel/queue` |
| `status` | 见 4.3 |
| `owner_uid` | 服务对象，可空，不决定执行权限 |
| `source_kind` | `chat/reminder/notification/manual/system` |
| `source_id` | 来源记录 ID，可空 |
| `creator_uid` | 创建主体 uid，可空 |
| `creator_role` | 创建时角色快照，可空；只用于审计 |
| `creator_surface` | `kiosk/admin/nurse/system`；`nurse` 是操作来源，不是会话角色 |
| `current_step_id` | 当前步骤，可空 |
| `wake_at` | 等待到指定时间时使用，可空 |
| `deadline_at` | 过期时间，可空 |
| `version` | 乐观锁版本，每次变更递增 |
| `created_at/updated_at` | 时间戳 |

### 4.2 `plan_steps`

| 字段 | 说明 |
|---|---|
| `id/plan_id/seq` | 主键、所属 Plan、顺序 |
| `step_type` | `action/wait/manual` |
| `action` | `action` 步骤的规范化动作名；其他类型为空 |
| `label` | 给人看的步骤说明，不参与执行 |
| `args_json` | 已校验的结构化参数 |
| `status` | 见 4.3 |
| `wait_kind/wake_at` | 等时间、人工、设备或外部事件，可空 |
| `map_name` | 创建时确认的运行地图，可空 |
| `target_json` | 地点/区域解析后的绝对目标，可空 |
| `tags_fingerprint` | 创建时地图标记指纹，可空 |
| `timeout_sec` | 执行超时 |
| `retry_policy` | `none/safe_goto_only` |
| `max_attempts` | 与动作权限清单一致 |
| `started_at/finished_at/last_progress_at` | 卡住检测与排障 |
| `last_error` | 最近一次错误 |

建 Plan 时能解析地图目标就固化 `map_name + target_json + tags_fingerprint`；暂时无法解析时步骤进入 `waiting`，禁止编造坐标。执行前必须重新比对当前地图、目标安全性和指纹，不一致进入 `needs_review`。

三类步骤的合法形状：

- `action`：必须有白名单 `action` 和 `args_json`，完成由车控可靠结果决定；
- `wait`：`action` 必须为空，`wait_kind=time|device|external`；时间等待必须有 `wake_at`，条件满足后自动成功；
- `manual`：`action` 必须为空，`wait_kind=manual`，只有护士台/admin 调 `/confirm` 才能成功。

`robot_goto_place` 和 `robot_goto_zone` 只允许出现在创建请求中。创建时调用既有解析与安全校验，
成功后统一编译为 `action="robot_goto_point"`，`args_json` 保存解析出的 `x/y/yaw_deg`，并在
`target_json` 保留 `source_action`、原始地点/区域名和解析来源用于展示与审计。执行前重新解析原始
名称并比对固化目标；名称消失、地图/指纹变化或坐标超出允许误差时进入 `needs_review`，不得静默
驶向旧坐标。

### 4.3 状态

Plan 状态：

`draft / queued / running / waiting / paused / cancelling / needs_review / succeeded / failed / cancelled / expired`

Step 状态：

`pending / dispatching / running / waiting / paused / interrupting / needs_review / succeeded / failed / skipped / cancelled / interrupted`

- `waiting`：已知等待条件，例如 `wake_at`、人工确认、设备恢复；条件满足后可重新参与调度。
- `paused`：被抢占或人工暂停；必须显式恢复或由抢占策略恢复。
- `cancelling`：运行中 Plan 已收到取消请求，正在等待急停与车况确认。
- `needs_review`：结果不确定、重启对账失败、地图变化或急停后状态未恢复；不得自动推进。
- `interrupting`：已请求急停，等待新鲜车况确认动作槽位已无活动任务。
- `expired`：超过 `deadline_at`；`cancelled`：被人工、来源撤销或抢占策略明确取消。

界面符号仅用于显示，不写数据库：`-` 待执行、`>` 执行中、`x` 成功、`*` 被中断、`!` 失败或待复核、`⊘` 取消。

### 4.4 `plan_step_attempts`

每次准备派发动作时先插入一行：

| 字段 | 说明 |
|---|---|
| `id/plan_id/step_id/attempt_no` | 唯一定位一次尝试 |
| `idempotency_key` | `plan_id:step_id:attempt_no`，保证同一次 attempt 只派发一次 |
| `dispatch_state` | `prepared/dispatched/finished/uncertain` |
| `car_task_id` | 车控受理返回的 `task_id` |
| `request_json/accept_json/result_json` | 请求、受理、最终状态快照 |
| `started_at/last_checked_at/finished_at` | 时间戳 |
| `outcome` | `succeeded/failed/uncertain/rejected` |

`idempotency_key` 只解决同一次尝试重复派发；`car_task_id` 用来判断结果属于哪次车控任务，两者不能混用。重启时不新增 attempt；只有人工确认重新执行后才创建下一次尝试。

## 五、Plan 独立执行权限

Plan 自动执行不继承创建人的 `ward/elder/admin` 权限，也不伪装成 admin，不向 `session.derive_role()` 增加 `system` 角色。实现一条专用 `plan_executor` 调用路径，权限默认拒绝。

权限由以下交集决定：

1. 动作在 Plan 自动执行清单中；
2. MCP 总开关已开启且工具实际已注册；
3. 动作参数通过该动作的 Plan 约束；
4. Car MCP 的 readiness、地图、占用和安全校验全部通过。

普通对话的 `policy.allowed_tools ∩ MCP server roles` 仍只控制对话可见性；Plan 清单只控制调度器能否自动推进，不成为普通角色权限的第三把尺子。

### 5.1 自动执行清单

| 动作 | 自动执行 | 完成判据 | 自动重试 |
|---|---:|---|---:|
| `robot_goto_place` | 是 | 匹配 `car_task_id` 的明确成功结果 | 仅确认旧任务已结束且重新校验目标后允许 |
| `robot_goto_zone` | 是 | 同上 | 同上 |
| `robot_goto_point` | 是 | 同上 | 同上 |
| `robot_move` | 是 | 匹配 `car_task_id` 的明确成功结果 | 否；超时或不确定直接人工复核 |
| `robot_turn` | 是 | 匹配 `car_task_id` 的明确成功结果 | 否；超时或不确定直接人工复核 |
| `robot_stop` | 特殊放行 | 只确认停止消息已写入，不代表车已停稳 | 不适用 |
| `robot_status` | 只读探测 | 返回可解析的新鲜状态 | 不作为步骤 |
| 视觉观察/人数识别 | 否 | 当前无统一可靠结果契约 | 否 |
| 自由对话/讲新闻 | 否 | 当前无确定性完成事件 | 否 |

新增自动动作必须同时提供：参数 schema、完成字段、超时、幂等语义、失败分类、离线测试和权限评审。

表中的 `robot_goto_place/zone` 表示 `create_plan` 接受的高级动作；进入 `plan_steps` 后二者均已编译
成 `robot_goto_point`。调度器实际只派发 `robot_goto_point/move/turn` 三类动作。

## 六、车控返回与完成判定

### 6.1 三层返回必须逐层解析

`tools.run_tool()` 调 MCP 时存在三层语义：

1. 外层 `{"ok": false, "message": ...}`：MCP 协议、连接或调用失败；
2. 外层 `{"ok": true, "result": "{...}"}`：只表示拿到了 MCP 文本，必须 `json.loads(result)`；
3. 内层车控结果的 `ok/status/current/last` 才表示动作状态。`robot_status` 的 `ok:true,status:"unavailable"` 仍是不可用，不是成功。

解析失败、字段缺失或类型不符一律 fail-closed，进入 `needs_review` 或明确失败，不得当成功。

### 6.2 `task_id` 绑定

Car MCP 的动作受理返回必须补充 `task_id`。调度器收到 `status:"started"` 后，将该值写入当前 attempt，再轮询 `robot_status`。

完成判据：

- `robot_status.status == "unavailable"`：保持等待；超过恢复宽限期进入 `needs_review`；
- 匹配 `last.task_id == attempt.car_task_id` 且 `last.status == "uncertain"`：立即 `needs_review`；
- 匹配 `last.task_id` 且 `last.ok == true`：步骤成功；
- 匹配 `last.task_id` 且 `last.ok == false`：明确失败；
- `current.task_id` 匹配：仍在执行，刷新 `last_progress_at`；
- 返回了其他任务：视为槽位冲突或迟到状态，不污染当前步骤，转人工复核或继续等待至超时。

不得使用 `arrived` 判断完成，它只在变化时发布。不得只看到 `idle` 就宣告完成。Car MCP 子进程重启会丢失内存任务代次，因此跨进程重启的执行中步骤一律人工对账，不尝试仅凭 `task_id` 自动续接。

## 七、调度器与共享动作槽位

### 7.1 运行形态

Plan 调度器使用独立 daemon 线程，不能挂进 `session.tick()` 的 1 秒 asyncio 循环。默认配置：

- `PLAN_TICK_S=1.0`；
- `PLAN_STATUS_GRACE_S=15.0`：`unavailable`、急停后的重新探测及短暂 readiness 忙的恢复宽限期；
- `PLAN_GOTO_TIMEOUT_S=200`；
- `PLAN_MOVE_TIMEOUT_S=45`；
- `PLAN_TURN_TIMEOUT_S=45`；
- `PLAN_RESOLVE_RETRY_S=15.0`：`waiting` 中的导航步骤重解析目标的间隔（§4.2 的「暂时无法解析」；
  太小会每个 tick 都打一次地图/rosbridge）。

这些常量放在 `LLM/conf.py`，环境变量可覆盖，解析非法值时回退默认值并审计。上一轮 tick 仍在执行
时跳过，不允许并发 tick。所有慢调用均在调度线程内，不阻塞 FastAPI 事件循环。步骤超过动作超时
后：相对动作进入 `needs_review`；goto 只有在车控给出明确终态后才允许按策略创建下一 attempt，
单纯超时或断线不得重发。

`start()`/`stop()` 接入主后端 lifespan；`stop()` 用事件唤醒并退出，不等待完整 tick。第一期只允许**一个被配置为执行端的主后端实例**运行调度器；其他实例只读。跨 PC/板卡的共享租约需要共同的权威存储，本地 SQLite 租约无法协调两台机器，不在第一期伪实现。

### 7.2 调度流程

1. 选择最高优先级、最早创建且等待条件满足的 Plan；
2. 校验 Plan 独立权限、参数、地图、车况和共享动作槽位；
3. 事务内创建 attempt，将 Step 改为 `dispatching`；
4. 调用动作，逐层解析返回；受理后保存 `car_task_id` 并改为 `running`；
5. 轮询状态，只有第六节的明确成功判据成立后才推进下一步；
6. 明确失败按动作级重试策略处理；不确定结果进入 `needs_review`；
7. 全部步骤成功后 Plan 进入 `succeeded` 并触发回报。

### 7.3 对话与 Plan 共享仲裁

车控槽位属于整个系统，不只属于 Plan：

- Plan 动作运行时，对话侧 `robot_move/turn/goto_*` 不直接发车，应转成新 Plan 或明确回复正在执行其他任务；
- 对话动作已运行时，Plan 遇到忙状态进入等待，不记为步骤失败；
- `robot_stop` 永远放行；
- 仲裁必须位于对话与 Plan 两条调用路径都经过的位置，不能只使用调度器内部锁；
- 急停后车控会进入需要重新探测的短暂状态，`unavailable` 或 readiness 忙视为等待恢复，不立即记失败，也不立即发下一步。

## 八、抢占、取消和重启

- P0 到达时可以发出 `robot_stop`。当前 Step 先进入 `interrupting`；确认车况后改为 `interrupted`，Plan 按抢占策略进入 `paused/cancelled`。新 Plan 必须等待新鲜车况和 readiness 恢复。
- 取消排队 Plan 只更新状态。取消正在执行的 Plan 必须先发 `robot_stop`；Plan 进入 `cancelling`、Step 进入 `interrupting`，车况恢复并确认无活动任务后才成为 `cancelled`。
- `robot_stop` 成功只代表消息已写入 websocket，不能声称车已确认停车。
- Plan 不得自动解除急停或恢复移动。

取消是幂等操作：已处于 `cancelling/cancelled` 时重复请求返回当前状态，不再次发送急停。急停发送
失败时 Plan 与 Step 进入 `needs_review` 并发 critical 通知；急停写入后 15 秒仍无法获得新鲜车况，
同样进入 `needs_review`，绝不直接标记 `cancelled`。

后端重启时，所有 `dispatching/running` Step 进入 `needs_review`，绝不自动重新发车：

1. 查询 `robot_status` 和最近 attempt；
2. 只有可靠的匹配结果才能补记成功或失败；
3. 状态未知、MCP 子进程已重启、地图变化或任务超时均保持 `needs_review`；
4. P0/P1 恢复告警和人工核对，不恢复自动移动；
5. P2/P3 默认取消，除非人工明确重新执行。

## 九、人工操作权限与页面

### 9.1 Admin

沿用现有 admin 登录和 TTL。管理员可查看全部 Plan、人工创建、调整优先级、取消、人工确认、重新执行待复核步骤。

### 9.2 护士台

护士台职责扩展为“通知 + Plan 操作面板”：

- 进入页面时必须输入护士台 PIN；
- 护士台 PIN 与**当前有效管理员口令**完全相同。`POST /api/nurse/page-unlock` 复用
  `db.verify_admin_password()`，不另建 `NURSE_PAGE_PIN`、盐或哈希；管理员在 UI 修改口令后护士台
  立即使用新口令；`.env` 的 `PASSWORD` 仍只是现有管理员出厂恢复口令；
- 前端调用独立的
  `POST /api/nurse/page-unlock` 校验，接口只返回 `ok`，不签发 cookie、token 或权限会话；
- 校验成功后前端只在当前标签页的 `sessionStorage` 写入已解锁标志：刷新页面仍保持解锁，
  没有超时；关闭该标签页后标志消失，下次打开需要重新输入；
- PIN 只保护护士台页面入口，不传给任何 Plan API，也不参与 Plan API 的权限判断；
- Plan 读取和写接口均免鉴权；
- 因此局域网内知道接口的人可以绕过页面 PIN，直接查看、创建、调级、取消和确认 Plan。该风险为用户明确接受的产品边界；
- 不新增 `nurse` 会话角色，不修改 `session.derive_role()` 或 `X-Surface` 取值域。

护士台和 admin 均实时显示：当前 Plan、P0-P3 队列、等待/暂停/待复核项、步骤进度、最近尝试和失败原因。两端支持人工创建、调节优先级、取消和确认；SSE 断线后必须重新拉取列表，不能只依赖事件恢复状态。

人工创建使用受限步骤编辑器：从 Plan 自动执行清单选择动作并填写经过 schema 校验的参数，不能提交任意工具名或原始 JSON。创建者信息只用于审计，不决定自动执行权限。

## 十、API 与实时事件

以下 Plan API 在护士台需求下全部免鉴权；admin 也复用同一组接口。每次写操作记录来源 IP、`User-Agent`、可选 `X-Surface` 和请求体摘要，但不把这些字段当身份认证。

护士台另有 `POST /api/nurse/page-unlock`，请求体只含 `pin`。它调用现有管理员口令哈希校验，
只服务于页面门禁，不读取 `admin_auth_required`，也不返回或建立任何可用于 Plan API 的认证状态。
即使 admin 口令门被关闭，护士台页面仍要求当前管理员口令。数据库尚未初始化出管理员口令时，
护士台保持锁定并显示“管理员口令尚未初始化”，主后端仍正常启动。该接口沿用管理员登录的
失败限速口径：连续 3 次错误后冷却 10 秒，但使用独立计数，不影响 admin 登录槽位。

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/api/plans?state=&limit=&before_id=` | 列表和状态计数 |
| `GET` | `/api/plans/{id}` | Plan、步骤和 attempts 详情 |
| `POST` | `/api/plans` | 人工创建受限 Plan |
| `POST` | `/api/plans/{id}/priority` | 修改优先级，必带 `version` |
| `POST` | `/api/plans/{id}/cancel` | 请求取消，必带 `version` 和原因 |
| `POST` | `/api/plans/{id}/confirm` | 人工确认等待或待复核步骤 |
| `POST` | `/api/plans/{id}/retry` | 人工重新执行允许重试的步骤 |

人工创建接口不得接受 `creator_role`、`plan_executor` 权限或完成判据；这些均由后端生成。非法状态迁移返回 `409`，版本冲突返回当前最新 Plan。

`/confirm` 必须带 `version`、`step_id` 和 `decision`：

- `manual` 步骤只接受 `decision="complete"`，将 Step 置 `succeeded` 并推进；
- `wait` 步骤只在 `wait_kind=manual` 时允许确认；时间/设备等待不能人工伪造成条件已满足；
- `needs_review` 只接受 `decision="mark_succeeded|mark_failed|retry"` 并要求 `note`；
- `mark_succeeded/failed` 写审计但不伪造车控 result；`retry` 必须重新走动作权限和安全校验，并创建新 attempt；
- `running/dispatching/interrupting` 不允许确认，返回 `409`。

总线事件统一为：

- `plan_created`：新增 Plan 摘要；
- `plan_updated`：优先级、状态或当前步骤变化；
- `plan_step_changed`：步骤及 attempt 摘要变化；
- `plan_needs_review`：需要人工介入。

事件 payload 的 `type` 保留给总线事件名，业务分类使用 `kind`。后端 publish 点与 `frontend/packages/shared/src/events.ts` 同步修改，并补解析测试。

## 十一、提醒、通知与回报

- Reminder 只负责到点。在 `reminder._tick_once()` 把状态从 `pending` 更新为 `triggered` 或 `missed` 的那次迁移中，按规则幂等创建一次 Plan；不要复制提醒状态机。
- Plan 的需要复核、失败和完成通知统一调用：
  - `notify.ingest(source="plan", type="plan_needs_review", ...)`
  - `notify.ingest(source="plan", type="plan_failed", ...)`
  - `notify.ingest(source="plan", type="plan_done", ...)`
- `ref` 使用 `plan:<id>`，正文包含 Plan 展示号和结果。通知中心仍按既有 `(source,type,uid,正文)` 合并。

第一期默认回报渠道是护士台/admin 实时状态和通知中心。主动语音播报不作为 Plan 完成的必要条件：只有能确认当前 kiosk 活动主体仍是 `owner_uid`，且没有更高优先级语音时才可播报；否则只发通知，避免对着空房间或错误老人播报。语音失败不得把已经完成的动作 Plan 改回失败。

## 十二、动态重规划（第三期）

LLM 只接收当前 Plan、当前/下一步骤、可靠状态、错误原因和允许动作清单。它只能提出结构化候选变更，后端必须验证：

- 不删除或改写已成功步骤；
- 不加入自动执行清单外的动作；
- 不降低 P0/P1；
- 不绕过地图、车况、超时和参数校验；
- `version` 必须匹配；
- 每次最多新增 3 步，单个 Plan 最多重规划 2 次。

观察失败不能被模型改写为“已经看到”。没有可靠视觉契约时，该步骤只能等待人工或结束失败。

## 十三、分期实现

### 第一期：确定性线性执行

- Plan/Step/Attempt 表和状态迁移；
- `task_id` 受理返回与三层结果解析；
- 独立 `plan_executor` 权限和五个动作清单；
- 独立调度线程、共享车控仲裁、重启对账；
- Plan API、SSE 事件、admin 与护士台 Plan 页面；
- 护士台页面 PIN 且无超时，Plan API 免鉴权；
- 提醒触发和 Plan 通知接线。

第一期是开发版，不仓促实现不完整的优先级抢占底座。P0-P3 可以创建、展示和排序，但运行中抢占
与自动急停留到第二期；第一期遇到更高优先级新 Plan 时只入队。人工 `robot_stop` 仍按既有链路
永远可用。这一取舍以状态机逻辑完整和可测试为先，不用半成品抢占制造错误安全承诺。

### 第二期：完整抢占

- P0-P3 和 `resume/cancel/queue`；
- 急停后的恢复等待、取消待确认；
- 更多人工恢复操作。

### 第三期：受限动态重规划

- 结构化计划变更；
- 有限重规划和人工确认；
- 视觉模块提供可靠结果契约后再加入观察类动作。

## 十四、验收

所有自动化测试必须使用假 MCP/假状态，不连接真实 rosbridge、不动车。

1. “先去 A 点，再去护士站”按 `task_id` 串行执行，第一步明确成功后才能发第二步；
2. MCP 外层失败、内层 `ok:false`、`status:unavailable` 和 `status:uncertain` 分别得到正确状态；
3. `robot_move/turn` 超时或结果不确定后不自动重复发车；
4. 同一状态结果重复到达不会重复推进；迟到结果不污染新 attempt；
5. 对话动作与 Plan 抢槽位时不会双重发车，忙状态不会被误记为 Plan 失败；
6. 急停后立即尝试下一步时进入等待，不把短暂 unavailable 当失败，也不直接发车；
7. 后端或 MCP 子进程重启后不自动发车，运行步骤进入人工复核；
8. 地图名、解析目标或 tags 指纹变化时拒绝执行；
9. 护士台和 admin 实时看到相同 Plan；版本冲突返回 409，不互相覆盖；
10. 护士台页面需要 PIN、页面内不超时；直接调用免鉴权 Plan API 仍可操作；
11. 取消排队项不发急停；取消运行项先急停并进入取消待确认；
12. Plan 执行不继承创建者权限，白名单外动作始终被拒绝并审计；
13. 调度器、MCP、地图或可选能力不可用时主后端仍能降级启动。

## 十五、明确不做

- 不让 LLM 直接控制步骤状态或循环调用到“感觉完成”；
- 不根据 `arrived` 或单个 `idle` 判断动作完成；
- 不自动重试结果不确定的相对动作；
- 不实现尚无基础的“安全停靠”或载人识别；
- 不在重启后自动恢复移动；
- 不在本地 SQLite 上伪造跨主机调度租约；
- 不允许人工创建接口提交任意工具或完成判据；
- 不为巡逻维护固定、可能过期的 Plan 模板。

## 十六、护士台旧规格覆盖关系

本文经用户重新决策后覆盖 `2026-09-18-nurse-console-design.md` 的 D9-D11：护士台不再局限于
“只看通知/确认通知”，而是增加 Plan 查看和人工操作；页面增加无超时 PIN 门，但通知与 Plan API
仍免鉴权。实现时必须同步回填旧规格、`AGENTS.md` 和护士台源码中的 D9-D11 注释，避免两套有效
口径并存。其他通知中心契约不变。
