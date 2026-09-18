# 车控 MCP 设计（PC 侧 · rosbridge 通道）

> **目的：** 让大模型能真的开动小车 —— 前进/后退、原地转向指定角度、去指定坐标点、去指定区域（以及已标地点），并且随时叫停。
> **状态：** 设计定稿待评审（未开工）。
> **日期：** 2026-09-19
> **用户决策（2026-09-19，原话）：**
> - 「enabled 默认开。」
> - 「区域停靠点写 goal 时，落在障碍像素上警告。」
> - 「要在 LLM 侧登记"当前无任务"，让 robot_status 立刻回 idle。」
> **前置依赖：** 板卡 `ros2_car` 的 `/robot/*` 服务与话题（已实测存在，见 §三）；地图标记（`<图名>.tags.json`，第一期《地图编辑器》已落地）。
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
| 区域停靠点 | `tags.json` 的 zone 增加可选 `goal` 字段 + 地图编辑器标注 UI + 运行时三级回退解析（§八） |
| 异步受理模型 | 四个动作工具"受理即返回"，结果落 `_last` 由 `robot_status` 回报（§六） |
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
   ├── 话题：/robot/cmd_stop（出）、/robot/exec_state、/robot/arrived、/amcl_pose（入）
   └── 板卡 robot_actions 节点 → /cmd_vel → chassis_driver → STM32
```

**板卡零改动**：所有动作都下发到板卡**已有**的接口，不新增 ROS 服务、不改固件。

### 3.1 三条独立连接（关键设计决策）

`LLM/maps/roslink.py` 是"一条全局 socket + 一个 `_call()` 循环 recv"的连接层。它**不能**用于车控：`/robot/navigate_to` 会阻塞到车开到为止（板卡侧 `robot_actions.py::_await_goal` 超时 180s），这一条 socket 会被独占，期间任何位姿查询、状态读取全部堵死；而急停必须能在导航途中随时发出。

因此 `car_server` 自管三条连接（rosbridge 支持多客户端）：

| 连接 | 承载 | 特点 |
|---|---|---|
| `ctrl` | 调 `/robot/move`、`/robot/turn`、`/robot/navigate_to` | **互斥**（同一时刻只有一个动作，与板卡服务的互斥回调组同口径）、允许长时间阻塞 |
| `stop` | `publish /robot/cmd_stop`（`std_msgs/Bool` true） | **永不阻塞**、不与其他连接共享读循环，任何线程可随时调用 |
| `state` | `subscribe /robot/exec_state`、`/robot/arrived`、`/amcl_pose` | 后台专用线程持续 `drain` 更新内存缓存，供 `robot_status` 与校验读取 |

`maptags` 模块级的连接层（roslink）保持原样不动，maps 域继续用它。

### 3.2 位姿来源与其局限

区域判定需要 **map 系**坐标，故 `state` 连接订阅 `/amcl_pose`，而**不是** `/odom`（odom 系会漂移，与 tags 的米坐标不同源）。

**已知局限（写进注释，不掩饰）**：AMCL 只在位移 ≥0.25m 或转角 ≥0.2rad 时才发布位姿，车停着不动时 `/amcl_pose` 可能滞后。用于"我现在在哪个区域"足够（区域尺度是房间），但**不能**当作实时厘米级定位。`robot_status` 会带 `pose_at`（该位姿的收报时刻）让模型知道新鲜度。

---

## 四、文件布局

```
LLM/car_mcp/
  car_link.py       ★新  rosbridge 连接层：三条连接 + 服务调用 + 话题缓存 + 全部降级
  car_nav.py        ★新  目标解析：地点/区域 → 坐标（查 tags、三级回退、调 mapserver 校验）
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

四个动作工具调用时**不等待动作完成**：

1. 参数归一 → §七 六条前置校验 → 全部通过后**登记任务**（`_current = {...}`）；
2. 把服务调用投到后台线程（`ctrl` 连接互斥，后到的动作在 `exec_state != idle` 时已被校验 4 拒绝）；
3. 立刻返回 `status:"started"`；
4. 后台线程拿到结果后写 `_last`（`{action, ok, detail, at}`），并在失败时把任务状态置 `error`。

**为什么必须异步**：板卡 `/robot/move` 是同步服务（5m @0.15m/s ≈ 33s），`/robot/navigate_to` 最长 180s；而后端 `MCP_TOOL_TIMEOUT=30`、`LLM_TIMEOUT=60`。同步等待会同时踩中两个坑 —— 超时链错配，以及老人说完话要等车开到了才听到回应（`navigate-by-name` 规格 §6.2 已论证）。

**"当前无任务"的登记（用户决策 3）**：`robot_stop` 在发出急停后，**无条件**把 `_current` 清空、`_last` 写成 `{"action":"stop","ok":true}`，于是 `robot_status` 立刻回报 `exec_state=idle`、无进行中任务 —— 不等板卡的状态回传，避免"喊停了但界面还在 navigating"的错觉。后台线程随后若拿到"动作被打断"的结果，只更新 `_last.detail`，**不得**把状态改回 `navigating`。

板卡侧状态以 `/robot/exec_state` 的回传为准做**校正**：若 LLM 侧认为有任务而板卡回 `idle`，把任务标记为已完成（写 `_last`）。

---

## 七、前置校验（LLM 侧安全层，六条）

| # | 校验 | 不通过时 |
|---|---|---|
| 1 | 通道可用：`websocket-client` 已装 **且** rosbridge 能建连 | `ok:false` + 真实原因（"车控依赖缺失：…" / "车控服务连不上"），**绝不假装成功** |
| 2 | 参数合法：`direction` ∈ 四方向；`0 < distance_m ≤ 5.0`；`0 < &#124;angle_deg&#124; ≤ 360` | `rejected` + 上限说明（与板卡服务同口径，双保险） |
| 3 | 目标点合法：`mapserver.validate_point(当前图, x, y)` —— 越界 / 障碍 / unknown / 距边界余量 < 0.3m | `rejected` + 具体原因（实测越界 3cm 就会 ABORTED，提前拦住） |
| 4 | 车空闲：`exec_state == idle` | `rejected` +「我正在去 X 的路上」。**`robot_stop` 永远放行**（R3） |
| 5 | 名称可解析：区域名/地点名（含别名）在当前图命中且唯一 | `rejected` + **可用清单**（让模型能回答"我认得 A点、房间A…"） |
| 6 | 坐标可信：该图 tags 的指纹（`resolution`/`origin`）与当前 yaml 一致（`maptags.fingerprint_check`） | 指纹变了 → `rejected` +「地图元数据被改过，地点坐标不可信」；tags 无指纹或**当前图识别不出来**（`running_map_name` 返回非正常原因）→ **降级为只告警不拦截**，并把该情况写审计（避免识别失败把功能整体卡死） |

- **不探测服务是否存在**：rosbridge 没有廉价的"列服务"接口（要依赖 rosapi 节点）。"板卡车控服务没起来"由**发车返回**判定（板卡服务回 `success=false` + message）→ 归为 `status:"error"`，见 §十。这是有意的诚实取舍，不假装能前置探测。
- "当前是哪张图"复用 `LLM/agent/session.py::running_map_name()`（**唯一权威**，自带 30s 缓存），不自己再写一套判定。
- 校验 3 需要读地图文件（`MAPS_IO=ssh` 下很贵）→ 走 `mapserver` 既有缓存（`_DT_CACHE` / PGM 缓存）；读不到（SSH 断）时**降级为只告警**，不拦死。
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

**写入校验口径（用户决策 2）**：`upsert_zone` 在落盘前用 `mapserver.validate_point(map_name, goal.x, goal.y)` 检查，**落在障碍/unknown 像素上只警告、不拒收** —— 与既有 `validate_point` "只警告不阻止"的口径一致。`MapStoreError`（读不到地图/元数据不可用）不阻断保存，按"无法校验"处理。接口在 `{"ok": true, "uid": ...}` 之上追加 `"goal_validation": {...}` 回显校验结论。

### 8.3 地图编辑器改动

- `frontend/packages/mapeditor/src/lib/types.ts`：`Zone` / `ZoneIn` 增加 `goal?: {x, y, yaw_deg} | null`。
- `ZonePanel.vue`：编辑区新增一块「停靠点」—— 显示当前值（无 / `x,y yaw°`）、「在图上点选」按钮、「清除」按钮；点选态与现有 `shape-mode` 同构，新增一个 `goal` 模式。
- `MapCanvas.vue` + `App.vue`：新增一条与 `donePolygon` / `doneRect` 同构的单点回传通道（`goalPoint`），画布在 `goal` 模式下单击取一个 map 米坐标后自动回 idle。
- 列表行：没有 `goal` 的区域显示一个浅色提示「未标停靠点」，并在站点保存后用 `goal_validation` 的结果提示"该点在障碍上"。

### 8.4 运行时解析优先级（三级回退）

`robot_goto_zone` 的目标点按顺序取：

1. **显式 `goal`** → `goal_source: "explicit"`，`warnings` 为空；
2. **落在该区域内的已标地点**（用 `zonegeo.zone_hit` 判定；多命中取离区域质心最近的）→ `goal_source: "destination"`，`warnings: ["该区域未标停靠点，已改用区域内地点「A点」"]`；
3. **多边形顶点质心** → `goal_source: "centroid"`，`warnings: ["该区域未标停靠点，已用区域中心，建议在地图编辑器里标一个"]`。

**不静默选择**：三级都必须通过 §七 校验 3（越界/障碍）；回退得到的目标**校验不过就拒绝**，并明确告知"该区域还没标停靠点，且区域中心不可达，请在地图编辑器里标一个"。任何情况下不把"选了个点"当成"能去"。

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
| 地图 PGM 读不到（SSH 断） | 校验降级为只告警；区域有显式 `goal` 时不受影响 |
| `mcp_enabled` 总开关关闭 | 整台服务器不被拉起（`mcp_client.start` 既有逻辑） |
| `python-mcp` 缺失 | `car_server` 写 stderr + **退出码 2**（`mcp_client` 记 `connect_error`，后端照常启动） |

**不 print 到 stdout**：MCP 走 stdio，业务日志写 `car_mcp.log`（沿用 `car_server.py` 既有 helper）。

---

## 十一、测试

**Windows / 无 ROS / 无板卡（主力，全部可跑）**

1. **通道层** `car_link`：假 websocket（monkeypatch）→ 服务调用成功 / 返回失败 / 超时 / 连不上，四条路径的返回值；
2. **三条连接隔离**：`ctrl` 上挂着一个未返回的 180s 调用时，`stop` 仍能立即发出急停、`state` 仍能读到缓存（**回归"一条 socket 会被独占"这个设计动因**）；
3. **参数校验**：方向非法、距离 0/负/超 5m、角度 0/超 360；
4. **地图校验**：越界、障碍像素、unknown、边界余量不足，逐条；**指纹校验**：tags 指纹与 yaml 不一致 → 拒绝；`running_map_name` 返回异常原因 → 只告警不拦截；
5. **名称解析**：地点精确名 / 别名 / 未知（回可用清单）/ 重名；区域精确名 / 未知；
6. **区域三级回退**：显式 goal → 区域内地点 → 质心；每级的 `goal_source` 与 `warnings`；回退点校验不过时**拒绝**而不是硬发；
7. **忙时拒绝**：`exec_state != idle` 时四个动作工具都被拒；`robot_stop` 仍放行（R3 回归）；
8. **急停语义**（用户决策 3）：受理导航 → `robot_stop` → `robot_status` **立刻**回 `idle` 且无进行中任务；随后后台回来的"被打断"结果不得把状态改回 `navigating`；
9. **降级**：依赖缺失、通道断开、服务不在、PGM 读不到，逐条断言"只降级不崩、不假装成功"；
10. **角色闸门**：`effective_tools()` 在 ward / elder / admin 下的可见工具集（含"ward 必须有 robot_stop"这条回归）；`run_tool` 直调越权工具被拒并落 `policy_deny` 审计；
11. **MCP 装配**：`mcp_enabled` 打开后 `/api/tools` 能看到 7 个工具；工具的 `roles` 与 `MCP_SERVERS["car"]` 一致。

**tags 停靠点**

12. `goal` 归一：缺字段（旧文件）/ 合法 / 形状非法 / 非数值，四种情况的读取结果与 warnings；
13. 写入校验：goal 落在障碍上 → **保存成功** + `goal_validation` 警告；地图元数据不可用 → 保存成功 + "无法校验"。

**板卡真机（需现场安全确认，车前方净空）**

14. `robot_move(forward, 0.3)` → 车前进约 0.3m 后停；
15. `robot_turn(30)` → 左转约 30°；
16. `robot_goto_point` 发一个地图上的开阔点 → 车自主到达；
17. `robot_goto_zone("房间A")` → 到达停靠点；
18. 导航途中 `robot_stop` → **1 秒内**停住，且取消 Nav2 目标；
19. 拔掉 rosbridge（`kill lat`）后重试 → 后端不崩、工具如实报失败。

---

## 十二、风险与已知缺口

1. **rosbridge 是单点依赖**：`kill all` 会把它一起停掉且不自动恢复（既有坑 19）。车控全靠它 → `robot_status` 会把 `reason` 说清楚是"连不上"而不是"车坏了"。
2. **`elder_allowed` 闸门缺失**（§2.2）：MCP 工具契约里没有 `principal`，服务端无法按角色收紧。当前 `my_map` 的 A点/B点都是 `elder_allowed=1`，实际不影响；P1 的 `check_action()` 落地时必须一并解决。
3. **`/amcl_pose` 稀疏**（§3.2）：停车时位姿可能滞后，"我在哪个区域"够用，实时定位不够。
4. **区域内地点优先于质心，但仍可能不可达**：三级回退全部走校验 3，不通就拒绝 —— 宁可拒绝并让模型如实说"还没标好"，也不发一个必 ABORTED 的目标。
5. **`car_controller.py` 与板卡 `robot_actions` 功能重复**：本轮不让前者进生产链路，但两份代码并存仍需在 README 里写清"谁是生产路径"，否则下一个人会改错那一份。

---

*本规格为设计定稿，实现计划由 writing-plans 展开；落地后在本文件末尾追加「实现台账与偏差」。*
