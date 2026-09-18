# 车控 MCP 设计（PC 侧 · rosbridge 通道）

> **目的：** 让大模型能真的开动小车 —— 前进/后退、原地转向指定角度、去指定坐标点、去指定区域（以及已标地点），并且随时叫停。
> **状态：** 设计定稿待评审（未开工）。
> **日期：** 2026-09-19
> **用户决策（2026-09-19，原话）：**
> - 「enabled 默认开。」
> - 「区域停靠点写 goal 时，落在障碍像素上警告。」
> - 「要在 LLM 侧登记"当前无任务"，让 robot_status 立刻回 idle。」
> **前置依赖：** 板卡 `ros2_car` 的 `/robot/*` 服务与话题（动作接口已实测存在；本批次补 readiness 服务与状态心跳，见 §三）；地图标记（`<图名>.tags.json`，第一期《地图编辑器》已落地）。
> **相关规格：** `2026-09-14-robot-navigate-by-name-design.md`（通道选型与安全层口径的来源）、`2026-09-14-map-editor-design.md`（tags 与区域）、`2026-09-14-layered-user-roles-design.md`（角色闸门 R1–R5）。

---

## 一、目标与验收

1. 模型调 `robot_move(direction="forward", distance_m=0.5)` → 车前进 0.5m 后自动停，**工具立即返回受理**，LLM 当场就能出声回话。
2. 模型调 `robot_turn(angle_deg=90)` → 原地左转 90° 后自动停。
3. 模型调 `robot_goto_point(x=..., y=...)` → 查边界/障碍后下发 Nav2 目标，车自主导航过去。
4. 模型调 `robot_goto_zone(zone="房间A")` → 解析出该区域的**停靠点**，导航过去；返回值里能说明用的是"显式停靠点"还是"回退点"。
5. 模型调 `robot_goto_place(place="A点")` → 命中地点表（含别名）后导航过去。
6. 任何时候 `robot_stop` → 板卡收急停、取消导航，**且 LLM 侧立刻显示 `exec_state=idle`、无进行中任务**（用户决策 3）。
7. 板卡不在 / rosbridge 没起 / 导航栈没起 → 工具返回 `ok:false` + **真实原因**，后端照常运行，**绝不假装成功**。
8. 没有 ROS、没有板卡的 Windows 开发机上，§十一 的全部单测可跑。

---

## 二、范围

### 2.1 本轮做

| 能力 | 说明 |
|---|---|
| 七个 MCP 工具 | `robot_move` / `robot_turn` / `robot_goto_point` / `robot_goto_zone` / `robot_goto_place` / `robot_stop` / `robot_status` |
| PC 侧通道 | 后端所在机器上的 stdio MCP 子进程 → websocket → 板卡 rosbridge → `/robot/*` |
| LLM 侧安全层 | 六条前置校验（§七），含地图占用/边界校验 |
| 区域停靠点 | `tags.json` 的 zone 增加可选 `goal` 字段 + 地图编辑器标注 UI + 运行时两级解析（§八） |
| 异步受理模型 | 五个动作工具在 readiness 通过后“受理即返回”，结果落 `_last` 由 `robot_status` 回报（§六） |
| 板卡就绪契约 | 新增 `/robot/readiness` 快速服务 + `/robot/exec_state` 1 Hz 心跳，区分“PC 已排队”与“板卡可受理” |
| 角色闸门 | `MCP_SERVERS["car"]` 的 `roles` 与 `policy.allowed_tools` 两把尺子对齐（§九） |
| 降级 | 依赖缺失 / 通道断开 / 服务不在，一律只降级不崩（§十） |

### 2.2 明确不做

| 不做 | 理由 |
|---|---|
| 到达后**主动播报**「到了，XX」 | `navigate-by-name` 规格 §6.3 的 `nav_watch.py` + 语音排队自成一批；本轮只保证"发得出、查得到、停得下"。`robot_status` 能查到 `arrived`/`exec_state`，够模型在被问到时回答 |
| 多目标巡逻（"先去 A 再去 B"） | 板卡服务是单目标阻塞式，序列调度属新增机制 |
| 二次确认 / `check_action()` 动作分级 | `navigate-by-name` 用户决策「一律直接走，不需确认」；`policy.py` 注释已写明该能力是 P1，本批次**不留空壳** |
| `destinations.elder_allowed` 闸门 | 见 §十二 风险 2：MCP 工具契约里**没有 principal**，服务端无法区分调用者角色；强行做只能靠伪造参数。真正的动作分级留给 P1 |
| `car_controller.py` 进生产链路 | 它依赖 rclpy，**只能跑在板卡上**，而后端在 PC；且它与板卡 `robot_actions` 争夺 `/cmd_vel` 写权。保留为板卡本地/VM 自测工具，README 标注清楚 |
| 远程启停 Nav2 / 建图 | 沿用既有铁律：启停一律走板卡 `~/tools/nav_screen.sh` |

---

## 三、架构与通道

```
LLM 工具循环（chat_stream）
   │  mcp_client.call_tool（同步阻塞等 MCP 返回）
   ▼
PC: LLM/car_mcp/car_server.py   （stdio MCP 2.0 子进程，无 rclpy）
   │  websocket-client
   ▼
板卡 rosbridge :9090（`~/tools/nav_screen.sh lat` 起的会话）
   ├── 服务：/robot/move、/robot/turn、/robot/navigate_to
   ├── 快速服务：/robot/readiness（ready、exec_state、nav_available、message）
   ├── 话题：/robot/cmd_stop（出）、/robot/exec_state、/robot/arrived、/amcl_pose（入）
   └── 板卡 robot_actions 节点 → /cmd_vel → chassis_driver → STM32
```

动作仍下发到板卡**已有**接口，不改 STM32 固件。本批次只在 ROS2 层补充一个不执行动作的
`robot_interfaces/srv/RobotReadiness` 服务，并把 `/robot/exec_state` 改为状态变化立即发布、空闲时也
以 1 Hz 重发。这样 MCP 子进程晚于板卡启动或中途重连时，仍能恢复权威状态。

### 3.1 四条独立连接（关键设计决策）

`LLM/maps/roslink.py` 是"一条全局 socket + 一个 `_call()` 循环 recv"的连接层。它**不能**用于车控：`/robot/navigate_to` 会阻塞到车开到为止（板卡侧 `robot_actions.py::_await_goal` 超时 180s），这一条 socket 会被独占，期间任何位姿查询、状态读取全部堵死；而急停必须能在导航途中随时发出。

因此 `car_server` 自管四条连接（rosbridge 支持多客户端）：

| 连接 | 承载 | 特点 |
|---|---|---|
| `ctrl` | 调 `/robot/move`、`/robot/turn`、`/robot/navigate_to` | **互斥**（同一时刻只有一个动作，与板卡服务的互斥回调组同口径）、允许长时间阻塞 |
| `stop` | `publish /robot/cmd_stop`（`std_msgs/Bool` true） | **永不阻塞**、不与其他连接共享读循环，任何线程可随时调用 |
| `state` | `subscribe /robot/exec_state`、`/robot/arrived`、`/amcl_pose` | 后台专用线程持续 `drain` 更新内存缓存，供 `robot_status` 与校验读取 |
| `probe` | 调 `/robot/readiness` | 快速前置探测；不与长时间占用的 `ctrl` 共用 socket |

`RobotReadiness.srv` 为空请求，响应字段固定为：

```text
---
bool ready
string exec_state
bool nav_available
string message
```

`ready` 表示 `robot_actions` 已就绪；`nav_available` 由板卡端以非阻塞方式检查
`navigate_to_pose` action server。move/turn 要求 `ready=true`，三个 goto 工具还要求
`nav_available=true`。服务不存在、超时或返回未就绪时，动作不登记任务、不返回 `started`。

`maptags` 模块级的连接层（roslink）保持原样不动，maps 域继续用它。

### 3.2 位姿来源与其局限

区域判定需要 **map 系**坐标，故 `state` 连接订阅 `/amcl_pose`，而**不是** `/odom`（odom 系会漂移，与 tags 的米坐标不同源）。

**已知局限（写进注释，不掩饰）**：AMCL 只在位移 ≥0.25m 或转角 ≥0.2rad 时才发布位姿，车停着不动时 `/amcl_pose` 可能滞后。用于"我现在在哪个区域"足够（区域尺度是房间），但**不能**当作实时厘米级定位。`robot_status` 会带 `pose_at`（该位姿的收报时刻）让模型知道新鲜度。

---

## 四、文件布局

```
LLM/car_mcp/
  car_link.py       ★新  rosbridge 连接层：四条连接 + readiness/动作服务调用 + 话题缓存 + 全部降级
  car_nav.py        ★新  目标解析：地点/区域 → 坐标（查完整 tags、两级解析、调 mapserver 校验）
  car_server.py     改    MCP 2.0 stdio 服务端：只做工具注册与参数归一，无 rclpy、无业务
  car_controller.py 留   板卡本地/VM 自测用（MCP 不再 import 它）
  odom_sim_driver.py / car_cli_test.py / visual_drive.py   留（自测工具不动）
  README.md         改   重写为"两条路径"：生产走 car_link，板卡自测走 car_controller
LLM/tests/test_car_mcp.py        ★新
LLM/tests/test_maptags_goal.py   ★新
```

**导入方式**：`car_server.py` 按 `notice_server.py` 的既有模式（`if __package__:` 相对导入 + 脚本直跑回退）加载同目录模块；对项目包的依赖（`LLM.conf`、`LLM.maps.maptags`、`LLM.maps.mapserver`、`LLM.core.zonegeo`）用**从 `__file__` 推出 BASE_DIR 再插 `sys.path`** 的方式导入，不假设 cwd。

`LLM/car_mcp/` 属 AGENTS「不在后端导入链顶层」的外部 MCP 工具，被 stdio 子进程以独立进程加载，不参与 `core→store→agent→server` 的分层约束。

---

## 五、工具契约（七个）

MCP 工具**一律返回 JSON 字符串**（`mcp_client._call` 只读文本块，返回 dict 会被上游读成"（MCP 工具无文本返回）"—— `car_server.py` 顶部注释已记录该坑）。

| 工具 | 参数 | 说明 |
|---|---|---|
| `robot_move` | `direction`(forward/back/left/right)、`distance_m` | 直线/横移，到位自动停 |
| `robot_turn` | `angle_deg`（正=左、负=右） | 原地转向，到位自动停 |
| `robot_goto_point` | `x`、`y`、`yaw_deg`(可选，缺省 0) | 去 map 系坐标点 |
| `robot_goto_zone` | `zone`（区域名） | 去区域停靠点 |
| `robot_goto_place` | `place`（地点名或别名） | 去已标地点 |
| `robot_stop` | — | 立即急停（老人喊停/异常必调） |
| `robot_status` | — | 读状态、位姿、所在区域、上一次动作结果 |

### 5.1 受理返回（`ok:true`）

```json
{"ok": true, "status": "started", "action": "goto_zone",
 "summary": "已出发前往区域「房间A」（停靠点 -1.88, 1.29，来源：区域内地点「A点」）",
 "exec_state": "navigating",
 "target": {"x": -1.8827, "y": 1.2857, "yaw_deg": 0.0, "goal_source": "destination"},
 "warnings": []}
```

- `summary` 是**给模型直接复述的一句话**，避免模型自己编造坐标与来源。
- `warnings` 非空表示"能走但有疑虑"（例如用了回退停靠点），模型应如实向老人说明而不是当作正常状态。
- `action` 取值：`move_forward|move_back|move_left|move_right|turn|goto_point|goto_zone|goto_place`。

### 5.2 拒绝/失败返回（`ok:false`）

```json
{"ok": false, "status": "rejected", "error": "目标点 (3.20, -5.10) 在地图有效范围外",
 "hint": "可用地点/区域见 robot_status 或地图编辑器", "exec_state": "idle"}
```

`status` 三态（**动作工具**）：`started`（已受理）/ `rejected`（**发车前**被 LLM 侧安全层拦下）/ `error`（发车后失败，或通道/服务不可用）。三者对模型的含义不同，必须区分。

查询工具 `robot_status` 额外用第四态 `unavailable`（通道不可用），且 `ok` 保持 `true` —— 见 5.3。

### 5.3 `robot_status`

```json
{"ok": true, "exec_state": "idle", "moving": false,
 "pose": {"x": -1.90, "y": 1.28, "yaw_deg": 2.1, "at": "2026-09-19 00:20:31"},
 "zone": "房间A", "map": "my_map",
 "last": {"action": "goto_place", "ok": true, "detail": "导航到达", "at": "..."} }
```

- `zone`：当前坐标落在哪个区域（多命中时取**面积最小的那个**，即最细粒度；`parent` 链上溯留给后续）。
- `exec_state` 取值：`idle | moving | navigating | error`；`error` 由板卡发布，或由 LLM 侧在"发车失败"时登记。
- 通道不可用时返回 `{"ok": true, "status": "unavailable", "reason": "..."}`（查询类接口 `ok` 保持 true —— 服务健康 ≠ 功能可用，遵循 AGENTS「系统稳健性」）。

---

## 六、异步模型（受理即返回）

五个动作工具调用时**不等待动作完成**：

1. 参数归一 → §七 六条前置校验（含 `/robot/readiness`）→ 全部通过后，在同一把状态锁内分配递增
   `task_id` 并登记任务（`_current = {task_id, ...}`）；
2. 把服务调用投到后台线程（`ctrl` 连接互斥，后到的动作在同一状态锁内因 `_current` 非空而被拒绝）；
3. 立刻返回 `status:"started"`；
4. 后台线程拿到结果后，只有 `task_id == _current.task_id` 时才可清空 `_current`、写 `_last`
   （`{task_id, action, ok, detail, at}`）并更新状态；旧任务迟到的结果只记日志，不得污染新任务。

**为什么必须异步**：板卡 `/robot/move` 是同步服务（5m @0.15m/s ≈ 33s），`/robot/navigate_to` 最长 180s；而后端 `MCP_TOOL_TIMEOUT=30`、`LLM_TIMEOUT=60`。同步等待会同时踩中两个坑 —— 超时链错配，以及老人说完话要等车开到了才听到回应（`navigate-by-name` 规格 §6.2 已论证）。

**"当前无任务"的登记（用户决策 3）**：`robot_stop` 在 `stop` socket 成功发出急停消息后，
在状态锁内递增任务代次、清空 `_current`、把 `_last` 写成 `{"action":"stop","ok":true}`，于是
`robot_status` 立刻回报 `exec_state=idle`、无进行中任务 —— 不等板卡状态回传，避免“喊停了但界面
还在 navigating”的错觉。这里的 `ok:true` 只表示消息已成功写入 websocket，不能表述成“板卡已确认停车”。
旧后台线程随后返回时因 task_id 已失效，只记“被打断”的审计/日志，不得覆盖 stop 或新任务状态。

板卡侧状态以 `/robot/exec_state` 心跳做**校正**，但状态消息不带 task_id，不能单凭一个 `idle` 宣告
刚登记的任务完成：登记任务后必须先观察到该任务对应的 `moving|navigating`，之后的 `idle|error`
才能结束任务。`/robot/arrived` 只作为结果佐证。MCP 启动/重连时先调 readiness 建立初始状态；状态
未知或心跳过期时拒绝新动作，不把“没收到状态”当作 idle。

---

## 七、前置校验（LLM 侧安全层，六条）

| # | 校验 | 不通过时 |
|---|---|---|
| 1 | 通道与板卡就绪：`websocket-client` 已装、rosbridge 能建连、`/robot/readiness` 成功；goto 还要求 `nav_available=true` | `ok:false` + 真实原因（依赖缺失 / rosbridge 断开 / robot_actions 未启动 / Nav2 未启动），不登记任务 |
| 2 | 参数合法：`direction` ∈ 四方向；`0 < distance_m ≤ 5.0`；`0 < &#124;angle_deg&#124; ≤ 360` | `rejected` + 上限说明（与板卡服务同口径，双保险） |
| 3 | 目标点合法：`mapserver.validate_point(当前图, x, y)` —— 越界 / 障碍 / unknown / 距边界余量 < 0.3m | `rejected` + 具体原因（实测越界 3cm 就会 ABORTED，提前拦住） |
| 4 | 车空闲：状态心跳新鲜、readiness 与本地 `_current` 都表明 idle；检查与任务登记在同一状态锁内 | `rejected` +「我正在去 X 的路上」；状态未知/过期则 `error`。**`robot_stop` 永远放行**（R3） |
| 5 | 名称可解析：区域名/地点名（含别名）在当前图命中且唯一 | `rejected` + **可用清单**（让模型能回答"我认得 A点、房间A…"） |
| 6 | 坐标可信：能唯一识别当前图，且 tags 指纹（`resolution`/`origin`）与当前 yaml 一致（`maptags.fingerprint_check`） | 当前图识别失败、地图/PGM 读不到、指纹明确变化 → `rejected`；只有“旧 tags 无指纹”可告警放行并写审计 |

- **不依赖 rosapi 列举服务**：直接调用本批次新增的 `/robot/readiness`。服务不存在、超时或返回
  `ready=false` 都在发车前失败；动作服务发出后的运行期失败由后台结果写入 `_last`。
- “当前是哪张图”复用 `LLM/agent/session.py::running_map_name()`（**唯一权威**，自带 30s 缓存），不自己再写一套判定。
- 校验 3 需要读地图文件（`MAPS_IO=ssh` 下很贵）→ 走 `mapserver` 既有缓存（`_DT_CACHE` / PGM 缓存）；
  读不到时三个 goto 工具 fail-closed，move/turn/stop/status 不受地图可用性影响。
- 所有拒绝都 `audit.log("tool", ...)` 落审计。

---

## 八、区域停靠点

区域只有多边形、没有"车该停在哪儿"。本轮给它一个**显式停靠点**（用户决策：改 tags + 地图编辑器）。

### 8.1 数据格式（`<图名>.tags.json`）

```json
{"uid": "z3", "name": "房间A", "kind": "room", "shape": "polygon",
 "polygon": [[-1.92, 1.95], [-2.63, 1.17], [-1.84, 0.46], [-1.24, 1.54]],
 "goal": {"x": -1.88, "y": 1.29, "yaw_deg": 0.0},
 "parent": "", "note": ""}
```

- `goal` **可选**：旧文件没有它 → 向后兼容，不报错、不丢数据。
- `goal` 存在但形状非法（不是对象 / x,y 非数值）→ 归一层丢弃该字段并记 `warnings`，**不抛异常**（沿用 `maptags._norm_tags` 既有口径）。

### 8.2 后端改动

| 文件 | 改动 |
|---|---|
| `LLM/maps/maptags.py` | `_norm_tags()` 的区域分支归一 `goal`；`upsert_zone()` 接受 `data["goal"]`；`empty_tags()` 文档化该字段 |
| `LLM/maps/mapapi.py` | `ZoneIn` 增加 `goal: dict \| None = None`（`POST /api/zones`、`POST /api/zones/{uid}` 自动可用） |
| `ros2_car/src/robot_interfaces/srv/RobotReadiness.srv` | 新增空请求的快速就绪/状态响应契约 |
| `ros2_car/src/robot_interfaces/CMakeLists.txt` | 注册新 srv |
| `ros2_car/src/robot_navigation/robot_navigation/robot_actions.py` | 提供 `/robot/readiness`，保存当前状态并以 1 Hz 发布心跳；readiness 使用独立回调组，不被长动作阻塞 |

**写入校验口径（用户决策 2）**：`upsert_zone` 在落盘前用 `mapserver.validate_point(map_name, goal.x, goal.y)` 检查，**落在障碍/unknown 像素上只警告、不拒收** —— 与既有 `validate_point` "只警告不阻止"的口径一致；另用 `zonegeo.zone_hit()` 检查 goal 是否落在所属区域内，区域外同样保存但醒目警告。`MapStoreError`（读不到地图/元数据不可用）不阻断保存，按"无法校验"处理。接口在 `{"ok": true, "uid": ...}` 之上追加 `"goal_validation": {...}` 回显校验结论。

### 8.3 地图编辑器改动

- `frontend/packages/mapeditor/src/lib/types.ts`：`Zone` / `ZoneIn` 增加 `goal?: {x, y, yaw_deg} | null`。
- `ZonePanel.vue`：编辑区新增一块「停靠点」—— 显示当前值（无 / `x,y yaw°`）、「在图上点选」按钮、「清除」按钮；点选态与现有 `shape-mode` 同构，新增一个 `goal` 模式。
- `MapCanvas.vue` + `App.vue`：新增一条与 `donePolygon` / `doneRect` 同构的单点回传通道（`goalPoint`），画布在 `goal` 模式下单击取一个 map 米坐标后自动回 idle。
- 列表行：没有 `goal` 的区域显示一个浅色提示「未标停靠点」，并在站点保存后用 `goal_validation` 的结果提示"该点在障碍上"。

### 8.4 运行时解析优先级（两级解析）

`robot_goto_zone` 的目标点按顺序取：

1. **显式 `goal`** → `goal_source: "explicit"`，`warnings` 为空；
2. **落在该区域内的已标地点**（用 `zonegeo.zone_hit` 判定；多命中取离区域质心最近的）→ `goal_source: "destination"`，`warnings: ["该区域未标停靠点，已改用区域内地点「A点」"]`；
3. 前两级都没有可用点 → **拒绝**，提示“该区域还没标停靠点，请在地图编辑器里标一个”。

两级候选都必须同时满足“属于目标区域”和 §七校验 3；校验不过就拒绝。这里不使用“多边形顶点
平均值”：凹多边形的平均值可能落在区域外，地图像素可通行也不代表属于目标房间。任何情况下
不把“选了个点”当成“能去”。

`goal` 不写入 `brain.db.zones` 索引缓存；`car_nav` 必须通过 `maptags.resolve()` 读取完整 tags 真相，
不得用 `get_zones()` 读取 goal。

---

## 九、后端接线与角色闸门

### 9.1 `LLM/conf.py`

```python
"car": {
    "command": _sys.executable,                      # 后端同一解释器（换别的会缺 mcp，子进程起来就退出）
    "args": [str(BASE_DIR / "LLM" / "car_mcp" / "car_server.py")],
    "env": _car_mcp_env(),                           # ROSBRIDGE_URL 等
    "enabled": True,                                 # 用户决策 1：默认开
    "roles": ["ward", "elder", "admin"],             # 见 9.2 —— 含 ward 是 R3 的硬要求
},
```

`_car_mcp_env()` 照 `_vision_mcp_env()` / `_notice_mcp_env()` 的惯例：只传实际设置过的环境变量，避免用空串覆盖子进程内默认值。

### 9.2 角色闸门（两把尺子，必须对齐）

`tools.py::_mcp_roles()` 的语义是"**服务器声明的角色集之外一律拒绝**"，而 `policy.role_policy()["allowed_tools"]` 是**天花板**。两者是"与"关系，配错哪一边都会出错：

| 角色 | 服务器 `roles` 放行？ | `allowed_tools` 天花板 | 实际可见 |
|---|---|---|---|
| `ward`（含声纹未识别） | ✅（必须） | 已有 `robot_status`、`robot_stop` | **只有状态 + 急停** |
| `elder` | ✅ | 需**新增** 5 个移动工具 | 全部 7 个 |
| `admin` | ✅ | `None`（不裁剪） | 全部 7 个 |

**为什么 `roles` 必须含 `ward`**：若不含，集体层连 `robot_stop` 都看不见 —— 而 R3 规定急停/呼救**永远放行**。分层靠天花板实现，不靠服务器闸门。

**`policy.py` 改动**（`allowed_tools` 是唯一要改的地方）：
- `ward`：**不动**（`["robot_status", "robot_stop", "notify_nurse"]`）；
- `elder`：追加 `robot_move`、`robot_turn`、`robot_goto_point`、`robot_goto_zone`、`robot_goto_place`；
- `admin`：**不动**（`None`）。

> `policy.py` 顶部注释里"P1 的 `check_action()` 依赖 car MCP，不在这里留空壳"保持有效 —— 本批次实现的是工具与闸门，动作分级仍属 P1。

### 9.3 提示词

`LLM/agent/prompt/base.md` 补一小段工具使用时机（去哪个文件、写多长，留给实现计划）：
- 老人说要去哪儿 → 优先 `robot_goto_place`（有名字的地点），其次 `robot_goto_zone`；
- 只有明确坐标时才用 `robot_goto_point`（模型不该自己编坐标）；
- 下移动指令前若不确定车在不在动，先 `robot_status`；
- 老人喊停/异常 → **立刻** `robot_stop`；
- **红线：工具返回失败必须如实说明原因，不许假装成功、不许说"正在过去"**。

---

## 十、降级（AGENTS「系统稳健性」）

| 情况 | 行为 |
|---|---|
| `websocket-client` 没装 | 工具**照常注册**（模型看得见），调用返回 `ok:false`「车控依赖缺失：…」；后端启动不受影响 |
| rosbridge 没起 / 板卡不在 | 同上，**启动时不做网络探测**（与 `notice_server.py` 同口径：后端可能稍后才起），连接失败只在调用时暴露 |
| 板卡跑的是 `base` 模式（`robot_actions` 没起） | `ok:false`「车控服务没起来（机器人是否在导航模式？）」 |
| 导航栈没起（`/navigate_to_pose` 不存在） | `ok:false`「导航模式没启动」，**不进入"发送中"假状态** |
| 地图 PGM 读不到（SSH 断） | 三个 goto 工具 `rejected`，明确说明无法安全校验；move/turn/stop/status 继续可用 |
| `mcp_enabled` 总开关关闭 | 整台服务器不被拉起（`mcp_client.start` 既有逻辑） |
| `python-mcp` 缺失 | `car_server` 写 stderr + **退出码 2**（`mcp_client` 记 `connect_error`，后端照常启动） |

**不 print 到 stdout**：MCP 走 stdio，业务日志写 `car_mcp.log`（沿用 `car_server.py` 既有 helper）。

---

## 十一、测试

**Windows / 无 ROS / 无板卡（主力，全部可跑）**

1. **通道层** `car_link`：假 websocket（monkeypatch）→ 服务调用成功 / 返回失败 / 超时 / 连不上，四条路径的返回值；
2. **四条连接隔离**：`ctrl` 上挂着一个未返回的 180s 调用时，`stop` 仍能立即发出急停、`state` 仍能读到缓存、`probe` 仍能调用 readiness（**回归“一条 socket 会被独占”这个设计动因**）；
3. **参数校验**：方向非法、距离 0/负/超 5m、角度 0/超 360；
4. **地图校验**：越界、障碍像素、unknown、边界余量不足，逐条；**指纹校验**：tags 指纹与 yaml 不一致、`running_map_name` 返回异常原因、PGM 读不到 → goto 拒绝；旧 tags 无指纹 → 告警放行；
5. **名称解析**：地点精确名 / 别名 / 未知（回可用清单）/ 重名；区域精确名 / 未知；
6. **区域两级解析**：显式 goal → 区域内地点；每级的 `goal_source` 与 `warnings`；两级均无结果或候选不在区域内/不可通行时拒绝；
7. **忙时拒绝**：`exec_state != idle` 或 `_current` 非空时五个动作工具都被拒；`robot_stop` 仍放行（R3 回归）；
8. **急停与竞态**（用户决策 3）：受理导航 → `robot_stop` → `robot_status` **立刻**回 `idle` 且无进行中任务；旧后台结果、迟到的旧 `idle`、stop 后立即开始的新任务都不得被旧 task_id 污染；
9. **降级**：依赖缺失、通道断开、服务不在、PGM 读不到，逐条断言"只降级不崩、不假装成功"；
10. **角色闸门**：`effective_tools()` 在 ward / elder / admin 下的可见工具集（含"ward 必须有 robot_stop"这条回归）；`run_tool` 直调越权工具被拒并落 `policy_deny` 审计；
11. **MCP 装配**：`mcp_enabled` 打开后 `/api/tools` 能看到 7 个工具；工具的 `roles` 与 `MCP_SERVERS["car"]` 一致。

**tags 停靠点**

12. `goal` 归一：缺字段（旧文件）/ 合法 / 形状非法 / 非数值，四种情况的读取结果与 warnings；
13. 写入校验：goal 落在障碍上或区域外 → **保存成功** + `goal_validation` 警告；地图元数据不可用 → 保存成功 + “无法校验”。

**板卡 ROS2 包（可在有 ROS2 的板卡/容器运行）**

14. `RobotReadiness.srv` 能生成接口；动作执行期间 readiness 仍能快速返回；Nav2 未启动时
    `nav_available=false`；`exec_state` 无变化时仍以 1 Hz 重发。

**板卡真机（需现场安全确认，车前方净空）**

15. `robot_move(forward, 0.3)` → 车前进约 0.3m 后停；
16. `robot_turn(30)` → 左转约 30°；
17. `robot_goto_point` 发一个地图上的开阔点 → 车自主到达；
18. `robot_goto_zone("房间A")` → 到达停靠点；
19. 导航途中 `robot_stop` → **1 秒内**停住，且取消 Nav2 目标；
20. 拔掉 rosbridge（`kill lat`）后重试 → 后端不崩、工具如实报失败。

---

## 十二、风险与已知缺口

1. **rosbridge 是单点依赖**：`kill all` 会把它一起停掉且不自动恢复（既有坑 19）。车控全靠它 → `robot_status` 会把 `reason` 说清楚是"连不上"而不是"车坏了"。
2. **`elder_allowed` 闸门缺失**（§2.2）：MCP 工具契约里没有 `principal`，服务端无法按角色收紧。当前 `my_map` 的 A点/B点都是 `elder_allowed=1`，实际不影响；P1 的 `check_action()` 落地时必须一并解决。
3. **`/amcl_pose` 稀疏**（§3.2）：停车时位姿可能滞后，"我在哪个区域"够用，实时定位不够。
4. **区域内地点仍可能不可达**：显式 goal 与区域内地点都走区域归属和地图校验，不通就拒绝 —— 宁可让模型如实说“还没标好”，也不发一个必然失败或属于别处的目标。
5. **`car_controller.py` 与板卡 `robot_actions` 功能重复**：本轮不让前者进生产链路，但两份代码并存仍需在 README 里写清"谁是生产路径"，否则下一个人会改错那一份。

---

*本规格为设计定稿，实现计划由 writing-plans 展开；落地后在本文件末尾追加「实现台账与偏差」。*

## 十三、实现台账与偏差（2026-09-19）

### 13.1 已落地

- 板卡契约：`RobotReadiness.srv`、`robot_actions.py` readiness 服务与 1 Hz 状态心跳。
- tags 真相：`maptags.py` 归一、保存和校验区域 `goal`；SQLite 区域表仍只是无 goal 的索引缓存。
- PC 通道：`car_link.py` 四连接、任务代次、状态缓存、急停清空当前任务及可选依赖降级。
- 安全解析：`car_nav.py` 校验当前地图、指纹、点位、地点别名及区域两级目标来源。
- MCP 工具：`car_server.py` 注册七个 JSON 文本工具，五个动作异步受理，生产链不导入 rclpy。
- 后端装配：`conf.py` 默认登记 car MCP；ward 仅状态/急停，elder/admin 可见七个工具。
- 地图编辑器：区域停靠点支持回填、图上点选、清除、独立标记和保存后警告。

### 13.2 自动化验证

本批次的聚焦验收命令包括：

```text
.venv\Scripts\python.exe -m pytest LLM/tests/test_car_mcp.py -q
.venv\Scripts\python.exe -m pytest LLM/tests/test_maptags_goal.py -q
.venv\Scripts\python.exe -m pytest LLM/tests/test_policy_roles.py LLM/tests/test_policy_tools.py LLM/tests/test_notice_mcp.py -q
cd frontend && pnpm --filter mapeditor build
cd frontend && pnpm --filter mapeditor test:startup
```

最终执行结果记录在 `.superpowers/sdd/finish-report.md`，该运行报告不提交到仓库。

### 13.3 偏差与待验收项

- timeout/心跳 TTL 环境变量由 `conf.py` 透传，并由 `CarLink` 在构造时读取；非法值回退默认值。
- 地图编辑器 startup 契约测试增加了 goal 跨组件接线断言，作为构建之外的回归保护。
- **真机验收未在本工作树执行**。readiness、0.3 m 前进、30 度转向、开阔点/区域导航、导航中
  1 秒内急停及断开 rosbridge 后诚实失败，均待主代理在现场安全条件满足后验证；当前不得标记为通过。
