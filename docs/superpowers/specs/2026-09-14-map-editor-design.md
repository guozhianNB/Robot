# 地图编辑器（第三个前端）设计

> **目的：** 让「地图文件」和「位置语义」第一次有地方可视化管理：看图、标地点（名字→坐标）、划区域（坐标→名字）、管地图文件。
> **状态：** 设计定稿待评审（未开工）。
> **日期：** 2026-09-14
> **用户决策（2026-09-14，原话）：** 「后续再设计第三个前端"地图编辑器"用于编辑地图文件和位置划线」→ 后改为「那本轮还是做个地图编辑器吧。」
> **顺序：** 本文档是**第一期**；第二期《语音"指哪到哪"》见 `2026-09-14-robot-navigate-by-name-design.md`，它复用本文档的 `destinations` 表与位姿源。

---

## 一、目标与验收

**目标：** 在浏览器里打开地图，看见车在哪，点到哪儿就把那儿命名成「护士办公室」，画个框就是「2 号病房」。

**验收标准（可逐条打勾）：**

1. `/mapeditor` 能列出板卡 `ros2_car/maps/` 下所有地图（含未入 git 的 `my_map2` / `my_map3`），并显示每张图的尺寸、`resolution`、`origin`、未知率。
2. 选中一张图，画布上能看到地图位图、米网格、坐标轴、图上已知地点与区域，以及**小车当前位置与朝向**。
3. 在画布上点一下 → 填名字 → 落 `destinations` 表；刷新后点位还在、可改可删。
4. 拖矩形或点多边形 → 命名 → 落 `zones` 表；可选中高亮、可删。
5. 标点时即时校验：越出地图有效范围（各边缩进 0.3m）或在障碍像素上 → 红字警告，仍允许保存但标注风险。
6. 能让车上显示的"我在跑哪张图"自动识别出来（不靠人工声明，见 §5.3）。
7. 地图文件可重命名、复制、删除、下载；改 `resolution`/`origin` 前弹出"将导致本图 N 个地点失效"的确认。
8. **无 ROS 环境也能开发与测试**：注入假位姿、假地图元数据即可跑通全部前端逻辑。

---

## 二、范围

### 2.1 本轮做

| 能力 | 说明 |
|---|---|
| 地图文件管理 | 列出 / 查看 / 重命名 / 复制 / 删除 / 下载（`.pgm`+`.yaml` 成对）；改 yaml 元数据 |
| 看图 | 位图 + 米网格 + 坐标轴 + 缩放平移 + 鼠标处实时米坐标 |
| 标地点 | 单击落点 → 名字 / 别名 / 朝向 / 风险 / 是否允许老人 → `destinations` 表 |
| 记录当前位置 | 一点按钮把车此刻位姿存成地点（位姿经 rosbridge 读，见 §5.4） |
| 区域划线 | 矩形（拖拽）/ 多边形（连点+双击结束）→ 名字 / 类型（房间·病房·床位·其他）/ 父区域 → `zones` 表 |
| 车上图 | 车位置与朝向实时叠加在图上 |
| 当前地图识别 | 用 `/map` 元数据指纹反查是哪张 yaml（§5.3） |
| 目标地图设置 | 写 `settings.current_map`，并显示要执行的换图命令（**不由后端重启 Nav2**，见 §7.3） |

### 2.2 本轮明确不做

| 不做 | 理由 |
|---|---|
| **像素级修图**（擦噪点 / 补墙 / 裁剪 / 改分辨率后重存） | 三张图未知率 60.7%~85.1% 是"**没扫到**"而不是"**画错了**"，修图救不了；且改 `resolution`/`origin` 会让本图已标地点全部失效。重扫一张远比修图有效。若将来确实要，作为独立加装项 |
| 上传地图文件 | 板卡上 `scp` 或 `nav_screen.sh save` 更实际；上传要处理任意文件与配对校验，收益低 |
| 从 SLAM 直接存图 | 已有 `nav_screen.sh save <前缀>`，编辑器只给命令提示 |
| 后端远程启停 Nav2 / 建图 | 启停一律走 `nav_screen.sh`（串口独占，手动起第二份会抢 `/dev/ttyACM0` 并让雷达驱动崩）；后端 SSH 去杀/起 screen 会话风险大 |
| 权限与登录 | 本轮没有角色系统；接口暂不鉴权，待《分层用户体系》落地后整体归 `admin` 层（见 §10） |
| 地点回读（"我在哪"） | 区域划好后做坐标→区域匹配（规格 D17 病房自动切换要用），属后续批次 |

---

## 三、架构与数据流

```
浏览器 /mapeditor  ──HTTP──▶  LLM 后端（FastAPI，同 8000 端口）
     ▲                            │
     │                            ├─ db.py        destinations / zones 表
     │                            ├─ mapserver.py 读 maps/*.pgm|yaml → 灰度 PNG（**纯 stdlib**）
     │                            └─ roslink.py/locator.py ──websocket──▶ 板卡 rosbridge :9090
     └──────── /api/map/* /api/destinations /api/zones /api/robot/pose ──┘   （读 /amcl_pose、TF、/map）
```

- 第三个前端包：`frontend/packages/mapeditor`，dev `:5175`，生产 `base: "/mapeditor/"`，由 `server.py` 静态挂载到 `/mapeditor`（与 admin/kiosk 同一套做法）。
- 地图文件真相在**板卡** `ros2_car/maps/`（`conf.MAPS_DIR = BASE_DIR / "ros2_car" / "maps"`），后端在板卡上跑时自然指向正确目录；Windows 端用仓库里的副本开发。
- 位姿真相在 ROS 侧；后端只经 rosbridge 只读订阅，**不引入 rclpy 依赖**（Windows 开发机无 ROS 也能跑）。

---

## 四、数据模型（`LLM/data/brain.db`，沿用 `db.py::SCHEMA` + `_ensure_columns` 模式）

### 4.1 `destinations`（地点：名字 → 坐标）

```sql
CREATE TABLE IF NOT EXISTS destinations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  map_name TEXT NOT NULL,               -- 地点绑定的地图名（三张图坐标系不通用，必须绑）
  name TEXT NOT NULL,                   -- 标准名（老人可说的名字）
  aliases TEXT DEFAULT '',              -- 逗号分隔别名：护士站,办公室
  x REAL DEFAULT 0, y REAL DEFAULT 0,   -- 米，map 坐标系
  yaw_deg REAL DEFAULT 0,               -- 到达朝向（度，逆时针为正）
  risk TEXT DEFAULT 'low',              -- low | high（高风险=离房/跨区，供后续角色系统分级）
  elder_allowed INTEGER DEFAULT 1,      -- 是否允许老人用语音让车前往（第二期的唯一闸门）
  note TEXT DEFAULT '',
  learned_by TEXT DEFAULT '',           -- editor | learn_button | manual
  created_at TEXT, updated_at TEXT,
  UNIQUE(map_name, name)
);
```

> 与《分层用户体系》规格 §7.1 的差异：**新增 `map_name` 与 `aliases`**。前者因为三张图坐标系不通用（规格里没有，属必须补的洞）；后者因为老人会说"护士站""办公室"，需要别名归一。规格 §7.1 的 `goal_json` 拆成 `x/y/yaw_deg` 三列（SQLite 里可直接索引、可校验，不必在 JSON 里绕）。

### 4.2 `zones`（区域：坐标 → 名字）

```sql
CREATE TABLE IF NOT EXISTS zones (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  map_name TEXT NOT NULL,
  name TEXT NOT NULL,
  kind TEXT DEFAULT 'room',             -- room | ward | bed | other
  shape TEXT DEFAULT 'polygon',         -- polygon | rect
  polygon_json TEXT DEFAULT '[]',       -- [[x,y], ...] 米坐标（世界系，非像素）
  parent_id INTEGER DEFAULT 0,          -- 上级区域（病房→床位）；0=无
  note TEXT DEFAULT '',
  created_at TEXT, updated_at TEXT,
  UNIQUE(map_name, name)
);
```

**为什么几何存米坐标而不是像素：** 像素坐标会随 `resolution` 变化而失效；米坐标只与地图 `origin`/`resolution` 绑定，改分辨率时能统一换算或统一告警（§7.2）。区域支持多层级（`parent_id`），因为陪护场景是"2 号病房 / 2 号病房 13 号床"两级。

> 与《分层用户体系》规格 D17 的关系：D17 计划用 `profiles.zone_json` 承载病房范围。本设计**以 `zones` 表为此类几何的唯一真相**，`profiles` 若需要关联只存 `zone_id` 引用，不复制几何，避免两处几何不一致（见 §10 回填项）。

### 4.3 设置项（`conf.DEFAULT_SETTINGS`）

| 键 | 默认 | 含义 |
|---|---|---|
| `current_map` | `"my_map"` | 目标地图名（下次启动导航用；不等于"车此刻在跑哪张图"，后者自动识别见 §5.3） |
| `map_boundary_margin_m` | `0.3` | 地点校验时各边缩进（沿用 `where_am_i.py` 口径） |
| `map_topic_fingerprint_enabled` | `True` | 是否用 `/map` 元数据反查当前地图 |

---

## 五、后端接口（`server.py` 路由 + `db.py` 数据 + `conf.py` 配置，遵 AGENTS「新增功能三件套」）

### 5.1 地图文件

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/map/list` | `[{name, width, height, resolution, origin, unknown_ratio, has_pgm, mtime}]` |
| GET | `/api/map/{name}/meta` | yaml 全字段 + 尺寸 + 未知率 + 本图地点/区域数量 |
| POST | `/api/map/{name}/meta` | 改 `resolution`/`origin`/`negate`/`occupied_thresh`/`free_thresh`；**返回将失效的地点/区域数量并要求 `confirm=true`** |
| POST | `/api/map/{name}/rename` | `{new_name}`；同时改 yaml 里的 `image:` 字段并成对重命名 `.pgm` |
| POST | `/api/map/{name}/copy` | `{new_name}` 复制成对文件；**不复制地点/区域**（新图视为空白，避免坐标错配） |
| DELETE | `/api/map/{name}` | 删除成对文件；若本图有地点/区域则要求 `confirm=true`（连同级联删除并写审计） |
| GET | `/api/map/{name}/download?file=yaml\|pgm` | `file` 默认 `yaml`，只接受 `yaml`/`pgm` 两个取值（其他值报错，避免变成任意文件读取） |
| GET | `/api/map/{name}/image.png` | **后端把 PGM 转成灰度 PNG**（前端按阈值着色）；按 `mtime+size` 缓存 |
| GET | `/api/map/current` | `{name, source: "map_topic"|"unknown", detail}`，见 §5.3 |

### 5.2 地点与区域

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/destinations?map=<name>` | 列表 |
| POST | `/api/destinations` | 新增；服务端做完整校验（名称唯一、坐标在地图内、障碍检查）并返回 `warnings[]` |
| POST | `/api/destinations/{id}` | 改名/别名/坐标/朝向/risk/elder_allowed/note |
| DELETE | `/api/destinations/{id}` | 删除（写审计） |
| POST | `/api/destinations/learn` | `{name, aliases, risk, elder_allowed, note}` → 取**当前位姿**落库（位姿不可用时返回失败并提示"可改为在图上点选"） |
| POST | `/api/destinations/validate` | `{map_name, x, y}` → `{ok, clearance_m, on_obstacle, reasons[]}`（前端拖动/点选时实时调用） |
| GET | `/api/zones?map=<name>` | 列表（含 `parent_id` 层级） |
| POST | `/api/zones` / `POST /api/zones/{id}` / `DELETE /api/zones/{id}` | 增改删 |

### 5.3 当前地图识别（不靠人工声明）

订阅 rosbridge 的 `/map`，拿 `info.width / height / resolution / origin` 四项与本地每张 yaml 的元数据做比对：

- 唯一命中 → `source: "map_topic"`，认定此刻在跑该图；
- 多项命中或零命中 → `source: "unknown"` 并提示"地图元数据不唯一，请核对"；
- rosbridge 不可用 → `source: "unknown"`，编辑器顶部提示"导航未运行或 rosbridge 未启动"，但**编辑功能照常可用**。

这样避免了"人工声明当前地图"这类不可靠状态（也是 §7.4 换图后必须重标的前提检查）。

### 5.4 位姿（`LLM/roslink.py` + `LLM/locator.py`，只读）

- 订阅 `/amcl_pose`（静止时不发布，见坑），**并**读 TF `map→base_link` 兜底；
- 1Hz 轮询即可（编辑器不需要高频）；`GET /api/robot/pose` 返回 `{ok, x, y, yaw, source}`；
- **降级（遵 AGENTS「系统稳健性」）**：rosbridge 未配置/连不上/超时 → `{"ok": True, "status": "unavailable", "reason": "..."}`，查询类保持 `ok: True`；
- **可注入假位姿**：`locator` 支持注入（测试与无 ROS 开发用），与《分层用户体系》实现计划任务 6 的 `locator.py` 是**同一模块**——以先开工者为准，后开工者复用，接口保持 `available(settings)` / `get_pose()` 不变。

> `LLM/roslink.py` 只做"连接 + 降级 + 假数据注入"这一层；第二期的动作下发（服务调用、`/robot/cmd_stop`）复用它，不另建连接。

---

## 六、前端设计（`frontend/packages/mapeditor`）

```
packages/mapeditor/
  package.json          依赖 shared + vue（与 admin 同构）
  vite.config.ts        base "/mapeditor/"、端口 5175、proxy /api→8000、alias shared→源码
  index.html
  src/main.ts / App.vue
  src/pages/MapCanvas.vue   画布：位图 + 网格 + 坐标轴 + 地点/区域 + 小车
  src/pages/PlacePanel.vue  地点列表与编辑表单
  src/pages/ZonePanel.vue   区域列表与绘制工具
  src/pages/MapFiles.vue    地图文件管理（列出/改名/复制/删除/下载/元数据）
  src/lib/coords.ts         ★ 像素↔米换算（与后端同一套公式，见 §7.1）
  src/lib/colorize.ts       按 occupied/free 阈值给灰度图上色（阈值来自 yaml）
```

- 旧工程接线：`frontend/package.json` 加 `"dev:mapeditor": "pnpm --filter mapeditor dev"`；`pnpm build`（`pnpm -r build`）自动带上新包；`scripts/build_frontend.ps1` 的提示语改为 `{admin,kiosk,mapeditor}`；`server.py` 加 `/mapeditor` 挂载（仍保留 `/` → admin 的现有行为）。
- 画布**不引第三方地图库**（Leaflet/OpenLayers 都带投影假设，室内米坐标用不上）：纯 `<canvas>` + 自绘，缩放/平移自己实现，避免新增前端依赖。
- 交互：鼠标处实时显示米坐标；单击标点 → 侧栏表单（朝向可再点一下图上定）；矩形拖拽、多边形连点+双击结束；选中区域半透明高亮。
- 顶部状态条：当前地图（含自动识别结果）、rosbridge 状态、车位置与朝向。

---

## 七、关键算法与已知坑

### 7.1 像素 ↔ 米换算（最容易翻车的地方）

以 `my_map.yaml` 为例：`resolution: 0.05`、`origin: [-4.6, -1.91, 0]`、`mode: trinary`。

```
x_m = origin_x + px * resolution
y_m = origin_y + (H - 1 - py) * resolution      # py 是图像行号（从上往下），y 轴要翻转
px  = (x_m - origin_x) / resolution
py  = H - 1 - (y_m - origin_y) / resolution
```

- **y 轴翻转**是最高频错误：漏掉它所有点会整体镜像到图外；
- 仅支持 `origin` 第三分量（yaw）= 0（本项目全部为 0）；读到非 0 时接口返回明确错误而不是硬算；
- 换算公式在前后端各实现一份，**必须有往返单测**（米→像素→米误差 < 半个像素）。

### 7.2 改 `resolution` / `origin` 会作废已标位置

地点与区域按米坐标存，改 `resolution` 本身不必然作废（可以按比例换算），但 `origin` 变了会让所有位置整体平移，而**我们无法判断作者意图**（是标错了要修正，还是地图换了基准）。因此：改这两个字段一律弹出"本图有 N 个地点、M 个区域，保存后坐标含义将改变"，要求显式确认，并把受影响条目写进审计日志。

### 7.3 换图不由后端重启导航

后端**不**去 SSH 杀/起 screen 会话：`nav_screen.sh` 是按会话名去重的启动器，手动起第二份会抢 `/dev/ttyACM0` 并让雷达驱动崩。编辑器只做两件事：① 写 `settings.current_map`（作为下次启动的默认）；② 在界面上给出可一键复制的命令：`~/tools/nav_screen.sh nav <地图名>`（换图只需这一个参数——`map_server` 的 `yaml_filename` 会被 launch 参数覆盖，`nav2_params.yaml` 里那行不生效）。

### 7.4 PGM 解析与 PNG 生成（纯 stdlib，零新增依赖）

- 支持 `P5`（二进制）与 `P2`（ASCII）两种 PGM；其他魔数报明确错误；
- 用 `zlib` + `struct` 手写最小 PNG 编码（灰度 8bit），**不引入 Pillow**（板卡环境依赖不可信，AGENTS 要求能用 stdlib 就不引外部依赖）；
- 缓存键 `path + mtime + size`，避免每次刷新重解码整图；
- 未知率/覆盖率 = 按 yaml 的 `occupied_thresh`/`free_thresh`/`negate` 对直方图三分，口径与既有 `map_progress_check.py` 一致（口径不一致会让人怀疑工具）。

### 7.5 位姿源的两个坑（已在板卡实测过）

- `/amcl_pose` **静止时不发布**（AMCL 只在移动超 `update_min_d`/`update_min_a` 时触发）→ 必须用 TF `map→base_link` 兜底，否则编辑器上"车不见了"；
- AMCL 初始位姿没对齐时 `/amcl_pose` 恒在 `(0,0,0)`（当前板卡实测快照）→ 编辑器要把这种"位姿可疑"显式标出来（位姿长时间恒为原点时给黄字提示），这正是编辑器最容易帮上忙的地方。

### 7.6 标点即校验（把 ABORT 挡在标点阶段）

- 边界：距各边 <`map_boundary_margin_m`(0.3m) 或在地图外 → 警告（实测发目标越界 3cm 就 `status=6 ABORTED`）；
- 障碍：该像素按阈值判定为 occupied → 警告"该点在障碍上，Nav2 可能拒绝"；
- 未知区域：该点在 unknown 像素上 → 提示"该处未扫到，导航可能失败"；
- 以上均为**警告而非阻止**（现场可能确实需要在障碍边缘标点），但必须在 UI 上留痕。

---

## 八、错误处理与降级

| 情况 | 行为 |
|---|---|
| rosbridge 未配置/连不上 | 编辑、看图、标点**全部照常**；仅"车在哪""记录当前位置""当前地图识别"显示不可用（`ok: True, status: "unavailable"`） |
| maps 目录为空 / yaml 缺 pgm | 列表里标红该条目并给出缺失文件名，不整体报错 |
| yaml 字段缺失或非法 | 该图元数据标注为"不可用"，不参与"当前地图识别" |
| 删除有引用 | 返回受影响地点/区域列表并要求 `confirm=true` |
| 写文件失败（板卡权限） | 明确返回错误字符串（含路径）；不做静默重试 |
| 一切状态变更 | `log.py::log()` 落审计（沿用 `memory_change` 风格，新增事件名 `map_change`/`zone_change`） |

---

## 九、测试与验收

**无 ROS 环境（Windows 开发机，主力测试口径）：**

1. `coords.ts` 与后端换算的往返单测（含 y 翻转、边界像素）；
2. PGM 解析：`P5`/`P2` 各一份样本 + 坏魔数 → 正确报错；PNG 输出用固定样本比对字节；
3. 未知率计算与 `map_progress_check.py` 对同一张 `my_map.pgm` 的结果一致；
4. 假位姿注入 → `/api/robot/pose` 返回注入值；注入关闭 → `status: "unavailable"`；
5. `destinations`/`zones` 的 CRUD + 唯一约束 + 校验警告（越界/障碍/未知）；
6. 改 `resolution`/`origin` 的失效确认流程。

**板卡真机（需现场安全确认后再动）：**

7. 起 rosbridge（`~/tools/nav_screen.sh lat`）→ 编辑器显示车在图上、朝向正确；
8. 车推到某处 → 点"记录当前位置" → 与 `where_am_i.py` 打印的位姿比对（误差应在厘米级）；
9. 自动识别当前地图：`/map` 元数据指纹命中正在跑的那张 yaml；
10. 标 2~3 个地点 + 1 个区域 → 落库 → 板卡上 `GET /api/destinations` 复核。

---

## 十、与既有规格的关系（要做回填）

| 既有文档 | 回填内容 |
|---|---|
| `2026-09-14-layered-user-roles-design.md` §7.1 | `destinations` 增加 `map_name` / `aliases`；`goal_json` 拆成 `x/y/yaw_deg` |
| 同上 D17 | 病房/床位几何的真相改在 `zones` 表；`profiles.zone_json` 只存 `zone_id` 引用（不复制几何） |
| `2026-08-27-frontend-multi-end-design.md` | 登记第三个前端包 `mapeditor`（dev 5175、生产 `/mapeditor`、与 admin/kiosk 并列） |
| `ros2_car/建图与导航操作手册.md` | 补一节"地图与位置语义在哪标"（指向 `/mapeditor`）+ 换图命令提示 |

**权限归属（明确留白）：** 本设计的接口本轮不鉴权。《分层用户体系》落地后，`mapeditor` 的全部写接口归 `admin` 层，老人层不可见——该结论写在此处以免遗漏。

---

## 十一、任务分解（粗粒度，供实现计划展开）

| # | 任务 | 依赖 |
|---|---|---|
| 1 | `db.py` 建 `destinations`/`zones` 表 + CRUD + 校验函数 | — |
| 2 | `conf.py` 增设 `MAPS_DIR` 与 §4.3 设置项 | — |
| 3 | `mapserver.py`：PGM 解析 + 灰度 PNG + 未知率（纯 stdlib，带缓存） | — |
| 4 | 地图文件接口（§5.1） | 2、3 |
| 5 | `roslink.py` + `locator.py`（只读位姿、降级、假位姿注入） | 2 |
| 6 | `/api/map/current` 指纹识别（§5.3） | 3、5 |
| 7 | 地点/区域接口（§5.2） | 1、3 |
| 8 | 前端包骨架（vite/base/挂载/`dev:mapeditor`/构建脚本提示） | — |
| 9 | `coords.ts` + 画布（位图、网格、坐标轴、缩放平移、鼠标米坐标） | 8 |
| 10 | 地点标点/编辑/删除 + 即时校验提示 | 7、9 |
| 11 | 区域绘制（矩形/多边形）+ 列表 | 7、9 |
| 12 | 车上图 + 当前地图状态条 + "记录当前位置"按钮 | 5、6、9 |
| 13 | 地图文件管理页（改名/复制/删除/下载/元数据+失效确认） | 4、8 |
| 14 | 测试（§9 的 1~6） | 全部 |
| 15 | 文档回填（§10）+ 板卡真机验收（§9 的 7~10） | 全部 |

---

## 十二、动手前的前置条件（不解决则验收不了）

1. **重扫一张地图**：现有三张图未知率 `my_map` 73.3% / `my_map2` 85.1% / `my_map3` 60.7%，覆盖都不够；
2. **对齐 AMCL 初始位姿**：板卡实测 `/amcl_pose` 恒在 `(0,0,0)`，未对齐时"车在哪"和所有地点的正确性都无从判断；
3. 板卡上 rosbridge 需在跑（`~/tools/nav_screen.sh lat`；注意 `kill all` 后它不会自动回来，`ss -ltnp | grep 9090` 可复核）。
