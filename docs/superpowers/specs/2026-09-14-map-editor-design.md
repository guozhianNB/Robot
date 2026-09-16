# 地图编辑器（第三个前端）设计

> **目的：** 让「地图文件」和「位置语义」第一次有地方可视化管理：看图、标地点（名字→坐标）、划区域（坐标→名字）、管地图文件。
> **状态：** **已实现（2026-09-14）**，见文末**「实现台账与偏差（2026-09-14 落地）」**。
> **本轮实做：** A 篇的 `mapstore.py` / `maptags.py` / `mapserver.py` / `roslink.py` / `locator.py`、`db.py` 三张只读索引缓存表 + `conf.py` 地图配置 + `server.py` 全部地图编辑器路由 + `frontend/packages/mapeditor`（Vue 应用 + `public/pixel-editor.html` 接线层 + vendor 自托管）；B 篇的像素修图 `POST /api/map/{name}/save` 全流程。
> **仍未做（未验）：** 真实开发机上的 `vite build` 复验、真实浏览器联调、板卡真机验收（§九 7~10、§B11.3 16~19）；§十二 与 §B十四 的前置条件（重扫地图、对齐 AMCL 初始位姿、板卡可达 —— 实测 `ssh sunrise@100.65.82.93` 连接超时）依然成立。**详见文末「未做/待验」清单。**
> **日期：** 2026-09-14
> **用户决策（2026-09-14，原话）：** 「后续再设计第三个前端"地图编辑器"用于编辑地图文件和位置划线」→ 后改为「那本轮还是做个地图编辑器吧。」
> **本文结构（2026-09-14 合并）：** 本文档**分两篇**——
> - **A 篇（§〇～§十二）**：地图编辑器本体 = 看图 / 标地点 / 划区域 / 管地图文件（原「第一期」文档）；
> - **B 篇（文末，编号一律带 `B` 前缀）**：像素修图 = **在开源项目 `D:\_project\Robot\ROS-SLAM-Map-Editor` 上改进**（原独立文档 `2026-09-14-mapeditor-pixel-edit-design.md` 已并入本文件并删除，不再单独维护）。
>
> 两篇编号各自独立：A 篇用「〇～十二」，B 篇一律带 `B` 前缀（`B〇`、`B一`…、`§B6.1`）。**看到 `§Bx` 就翻到文末。**
> **顺序：** 第二期《语音"指哪到哪"》见 `2026-09-14-robot-navigate-by-name-design.md`，它复用 A 篇的地点标记（`<图名>.tags.json` / `maptags`，见 §四）与位姿源。

---

## 〇、口径变更与加装项（2026-09-14 追加，开工前必读）

> 本节是**唯一的口径台账**：正文未逐处改写，凡与本节冲突的以本节为准。

| # | 变更 | 影响与去向 |
|---|---|---|
| 1 | A 篇**尚未开工**；其「像素级修图」已并入**本文 B 篇**（原独立文档 `2026-09-14-mapeditor-pixel-edit-design.md` 已合并进本文件并删除，不再单独维护）。**改造对象**：上游工程本地副本 `D:\_project\Robot\ROS-SLAM-Map-Editor`（GyroPalm/ROS-SLAM-Map-Editor，MIT，定版 `646104e`，自带 `.git` 已 gitignore）——位置、目录清单、换行符约定见 **§B〇** | §2.2 该行已就地标注 |
| 2 | **后端主跑在 PC 上**（用户 2026-09-14：「我想了想，还是把地图工作放到PC上吧，因为板卡性能不是很好」）。`MAPS_IO` 默认 `ssh`，地图文件经 SSH 读写板卡 `ros2_car/maps/`；`local`（后端跑板卡）退为备用 | 影响 §三 部署口径、§5.1 地图文件接口、§7.4 的算力落点（PGM 解码 / PNG 编码 / 未知率统计现在都归 PC，板卡只留串口与 ROS 本职） |
| 3 | 新增前端静态资源与接口（B 篇） | `frontend/packages/mapeditor/public/{pixel-editor.html, pixel-netio.js, vendor/}`；`POST /api/map/{name}/save`、`GET /api/mapeditor/io`、`POST /api/mapeditor/io/test` |
| 4 | `GET /api/map/list` 必须**排除 `maps/.backup/`**（B 篇保存前的自动备份目录），否则备份会被当成地图列出来 | 影响 §5.1 |
| 5 | **✅ 已拍板（2026-09-14，用户原话：「病房区域按 zones 表（多边形、唯一真相）走」）**：病房区域几何一律用 **`zones`（多边形/矩形、米坐标）**，不复制几何。⚠️ **随后被第 7 条再次变更**：`zones` 的**载体**从 `brain.db` 的独立表改为地图文件夹里的 `<地图名>.tags.json`（`brain.db` 降为索引缓存），且自增 `id` 换成稳定 `uid`。故《分层用户体系》P0 计划里的 `zone_id` 写法**需要按第 7 条再改一次**（改为「地图名 + 区域 uid」） | 已回填《2026-09-14-layered-user-roles-design.md》与其 P0 计划；**第 7 条后需二次回填** |
| 6 | ~~B 篇修图另存新图后标记不跟随~~ **✅ 已解（2026-09-14，随第 7 条）**：标记与地图同目录同前缀，另存新图时**连带复制** `<原名>.tags.json` → `<新名>.tags.json` 并做指纹比对（§B5.2 第 6 步） | 无需再靠"作业顺序"绕开 |
| 7 | **地图标记（地点 / 区域）的真相从 `brain.db` 迁到地图文件夹**（2026-09-14 用户提议：「能不能让地图标记（哪个区域是什么房间）放在地图文件夹？这样方便迁移。然后 llm 通过 sqlite 桥接到地图标记」，随后「开改！」）。新唯一真相 = **`<地图名>.tags.json`**（与 `.pgm`/`.yaml` 同级同前缀，内含 `resolution`/`origin` 指纹）；`brain.db` 的 `destinations`/`zones` 两表**降级为只读索引缓存**，严格单向、可丢弃可重建（三条红线与完整设计见 §四） | 影响 §四（数据模型重写）、§5.1/§5.2（接口改为"改文件+刷缓存"、地图变三件套）、§7.2（元数据变更**变为可检测**）、§九、§十一、§B5.2（标记随行）、§B九、§B十二；`AGENTS.md` 已补一条 |
| 8 | **编辑器改为独立进程按需启动（2026-09-15）**：本文 A/B 两篇描述的编辑器**不再由主后端挂载**——后端侧编辑器路由搬进 `LLM/mapapi.py`，由薄壳 app `LLM/mapeditor_server:app`（默认端口 `conf.MAP_EDITOR_PORT`=8010）承载；主后端只用 `LLM/mapctl.py` 的 3 条**仅管理员**接口 `GET|POST /api/mapeditor/service{,/start,/stop}` 按需拉起/探活/停止，入口在 admin →「地图编辑器」页签。故本文里凡「挂在 8000 上的 `/mapeditor`」「`/api/map/*`、`/api/destinations*`、`/api/zones*`、`/api/robot/pose`、`/api/mapeditor/{status,io,io/test,pose/inject}` 在 `server.py`」的写法**一律以本条为准（这些路径现在只在 :8010 上）**；§4 的地图标记真相（`<地图名>.tags.json`）、`MAPS_IO` 语义、§B 篇像素修图全流程**均不变** | 规格：`docs/superpowers/specs/2026-09-15-map-editor-on-demand-service-design.md`（§十一 = 实现台账与偏差）；本文 §5.1/§5.2/§七/§B6.1 的**接口契约与文件布局仍有效**，只是**部署拓扑**（谁在哪个进程/端口上提供）换了；`AGENTS.md` 已同步 |

---

## 一、目标与验收

**目标：** 在浏览器里打开地图，看见车在哪，点到哪儿就把那儿命名成「护士办公室」，画个框就是「2 号病房」。

**验收标准（可逐条打勾）：**

1. `/mapeditor` 能列出板卡 `ros2_car/maps/` 下所有地图（含未入 git 的 `my_map2` / `my_map3`），并显示每张图的尺寸、`resolution`、`origin`、未知率。
2. 选中一张图，画布上能看到地图位图、米网格、坐标轴、图上已知地点与区域，以及**小车当前位置与朝向**。
3. 在画布上点一下 → 填名字 → 写入该图的 **`<地图名>.tags.json`**（并刷新索引缓存）；刷新后点位还在、可改可删。
4. 拖矩形或点多边形 → 命名 → 同样写入 `<地图名>.tags.json`；可选中高亮、可删。
5. 标点时即时校验：越出地图有效范围（各边缩进 0.3m）或在障碍像素上 → 红字警告，仍允许保存但标注风险。
6. 能让车上显示的"我在跑哪张图"自动识别出来（不靠人工声明，见 §5.3）。
7. 地图文件可重命名、复制、删除、下载；改 `resolution`/`origin` 前弹出"将导致本图 N 个地点失效"的确认。
8. **无 ROS 环境也能开发与测试**：注入假位姿、假地图元数据即可跑通全部前端逻辑。
9. **整体迁移验收**（这次改动的核心目的）：把 `my_map.pgm` + `my_map.yaml` + `my_map.tags.json` **三件一起**拷到另一个目录/另一台机器，编辑器打开即能看到全部地点与区域 —— **不依赖 `brain.db`**（`brain.db` 删掉后重建即可）。

---

## 二、范围

### 2.1 本轮做

| 能力 | 说明 |
|---|---|
| 地图文件管理 | 列出 / 查看 / 重命名 / 复制 / 删除 / 下载（`.pgm`+`.yaml` 成对）；改 yaml 元数据 |
| 看图 | 位图 + 米网格 + 坐标轴 + 缩放平移 + 鼠标处实时米坐标 |
| 标地点 | 单击落点 → 名字 / 别名 / 朝向 / 风险 / 是否允许老人 → 该图的 `<地图名>.tags.json`（§4.1） |
| 记录当前位置 | 一点按钮把车此刻位姿存成地点（位姿经 rosbridge 读，见 §5.4） |
| 区域划线 | 矩形（拖拽）/ 多边形（连点+双击结束）→ 名字 / 类型（房间·病房·床位·其他）/ 父区域 → 该图的 `<地图名>.tags.json` |
| 车上图 | 车位置与朝向实时叠加在图上 |
| 当前地图识别 | 用 `/map` 元数据指纹反查是哪张 yaml（§5.3） |
| 目标地图设置 | 写 `settings.current_map`，并显示要执行的换图命令（**不由后端重启 Nav2**，见 §7.3） |

### 2.2 本轮明确不做

| 不做 | 理由 |
|---|---|
| **像素级修图**（擦噪点 / 补墙 / 裁剪 / 改分辨率后重存） | 三张图未知率 60.7%~85.1% 是"**没扫到**"而不是"**画错了**"，修图救不了；且改 `resolution`/`origin` 会让本图已标地点全部失效。重扫一张远比修图有效。~~若将来确实要，作为独立加装项~~ → **2026-09-14 用户改变主意，已由本文 B 篇（见文末）实现；B 篇只改像素、不动 `resolution`/`origin`，故"改元数据使地点失效"这一顾虑不适用。** |
| 上传地图文件 | 板卡上 `scp` 或 `nav_screen.sh save` 更实际；上传要处理任意文件与配对校验，收益低 |
| 从 SLAM 直接存图 | 已有 `nav_screen.sh save <前缀>`，编辑器只给命令提示 |
| 后端远程启停 Nav2 / 建图 | 启停一律走 `nav_screen.sh`（串口独占，手动起第二份会抢 `/dev/ttyACM0` 并让雷达驱动崩）；后端 SSH 去杀/起 screen 会话风险大 |
| 权限与登录 | 本轮没有角色系统；接口暂不鉴权，待《分层用户体系》落地后整体归 `admin` 层（见 §十） |
| 地点回读（"我在哪"） | 区域划好后做坐标→区域匹配（规格 D17 病房自动切换要用），属后续批次 |

---

## 三、架构与数据流

```
浏览器 /mapeditor  ──HTTP──▶  LLM 后端（FastAPI，同 8000 端口）
     ▲                            │
     │                            ├─ maptags.py   读/写 maps/<名>.tags.json（★ 标记的唯一真相，§四）
     │                            ├─ db.py        destinations / zones 索引缓存（只读，仅 maptags 刷）
     │                            ├─ mapserver.py 读 maps/*.pgm|yaml → 灰度 PNG（**纯 stdlib**）
     │                            └─ roslink.py/locator.py ──websocket──▶ 板卡 rosbridge :9090
     └──────── /api/map/* /api/destinations /api/zones /api/robot/pose ──┘   （读 /amcl_pose、TF、/map）
```

- 第三个前端包：`frontend/packages/mapeditor`，dev `:5175`，生产 `base: "/mapeditor/"`，由 `server.py` 静态挂载到 `/mapeditor`（与 admin/kiosk 同一套做法）。
- 地图文件真相在**板卡** `ros2_car/maps/`（`conf.MAPS_DIR = BASE_DIR / "ros2_car" / "maps"`）。**2026-09-14 口径变更（详见 §〇 第 2 条）：后端主跑在 PC 上、`MAPS_IO` 默认 `ssh`，地图文件经 SSH 读写；仓库里的 `ros2_car/maps/` 只作离线样本与开发期的 `MAPS_DIR` 替代，不再是开发期真相。**
- 位姿真相在 ROS 侧；后端只经 rosbridge 只读订阅，**不引入 rclpy 依赖**（Windows 开发机无 ROS 也能跑）。

---

## 四、数据模型：标记的真相在地图文件夹，`brain.db` 只做索引缓存

> **2026-09-14 变更（见 §〇 第 7 条）：** 地点与区域**不再以 `brain.db` 为真相** —— 它们改为与地图文件**同级同前缀**的边车文件 **`<地图名>.tags.json`**，随地图文件夹整体迁移。`brain.db` 里保留两张表，但**降级为只读索引缓存**，只用于快速查询与多边形判定。
>
> **三条红线（违反就会重演"两套真相"事故 —— 我们刚在 `profiles.zone_json` vs `zones` 表上吃过一次）：**
> 1. **唯一真相 = `<地图名>.tags.json`**（与 `.pgm`/`.yaml` 同级同前缀）。要改标记就改这个文件。
> 2. **SQLite 只做索引缓存，严格单向（文件 → SQLite），永不反向写。** 所有写接口都是「改文件 + 刷缓存」，**绝不允许"只改库不改文件"**。
> 3. **缓存可丢弃、可完整重建**：清空两张表不影响任何数据；`POST /api/map/{name}/tags/reindex` 可手动重建。读之前比 `mtime+size`，不一致就重建。

### 4.1 边车文件 `<地图名>.tags.json`（**唯一真相**）

```json
{
  "version": 1,
  "map": "my_map",                  // 必须与文件名一致；不一致视为残缺态
  "resolution": 0.05,               // ★ 指纹：从本图 yaml 抄来，用于检测"元数据被改过"
  "origin": [-4.6, -1.91, 0],       // ★ 指纹
  "destinations": [
    { "uid": "d1", "name": "护士办公室", "aliases": ["护士站", "办公室"],
      "x": 1.2, "y": 3.4, "yaw_deg": 90, "risk": "low", "elder_allowed": 1,
      "note": "", "learned_by": "editor", "created_at": "…", "updated_at": "…" }
  ],
  "zones": [
    { "uid": "z1", "name": "101", "kind": "ward", "shape": "polygon",
      "polygon": [[-1.0, -1.0], [1.0, -1.0], [1.0, 1.0], [-1.0, 1.0]],
      "parent": "", "note": "", "created_at": "…", "updated_at": "…" }
  ]
}
```

**为什么放这里（这是这次改动的全部理由）：**
1. **「拷走一个文件夹 = 完整资产」成立**：几何（pgm/yaml）+ 语义（哪间是 101、护士办公室在哪）在一起。现状下 `brain.db` 跟着后端跑（已定后端在 PC），而地图在板卡 —— **语义与地图天然分家**，迁移/备份/交接必丢一半。
2. **顺手解掉"另存新图后区域全丢"**（原 §〇 第 6 条的坑）：标记与地图同目录同前缀，另存新图时连带复制即可（见 §B5.2 第 6 步）。
3. **人工可读、可 diff**：出问题能直接 `cat` 看，也能对着地图核。

**格式选 JSON 不选 YAML**：一期已定「能用 stdlib 就绝不引外部依赖」，而本项目依赖清单里**没有 pyyaml**；JSON 由 stdlib 直接支持，前端也直接吃。

**`uid` 是稳定身份**：`destinations` 用 `d<N>`、`zones` 用 `z<N>`，生成后**永不改变**（改名不改 uid）。**它替代了原来自增 `id` 的角色** —— 外部引用（如 `profiles` 里的病房↔区域关联）必须按 `uid` 走，不能再指望自增号。

**几何仍存米坐标而非像素**：像素会随 `resolution` 变而失效；米坐标只与 `origin`/`resolution` 绑定 —— 而这正是我们抄进指纹的那两项，于是"元数据被改过"**第一次变得可检测**（§7.2）。

### 4.2 `brain.db` 的两张索引缓存表

```sql
CREATE TABLE IF NOT EXISTS destinations (
  uid TEXT PRIMARY KEY,                 -- 来自 tags.json，稳定身份
  map_name TEXT NOT NULL,               -- 绑图（三张图坐标系不通用）
  name TEXT NOT NULL,
  aliases TEXT DEFAULT '',              -- 逗号分隔（冗余自 JSON 数组，便于 LIKE 查询）
  x REAL DEFAULT 0, y REAL DEFAULT 0,   -- 米，map 坐标系
  yaw_deg REAL DEFAULT 0,               -- 到达朝向（度，逆时针为正）
  risk TEXT DEFAULT 'low',              -- low | high
  elder_allowed INTEGER DEFAULT 1,      -- 第二期的唯一闸门
  note TEXT DEFAULT '',
  learned_by TEXT DEFAULT '',           -- editor | learn_button | manual
  created_at TEXT, updated_at TEXT,
  UNIQUE(map_name, name)
);

CREATE TABLE IF NOT EXISTS zones (
  uid TEXT PRIMARY KEY,
  map_name TEXT NOT NULL,
  name TEXT NOT NULL,
  kind TEXT DEFAULT 'room',             -- room | ward | bed | other
  shape TEXT DEFAULT 'polygon',         -- polygon | rect
  polygon_json TEXT DEFAULT '[]',       -- [[x,y], ...] 米坐标（世界系，非像素）
  parent TEXT DEFAULT '',               -- 上级区域的 **uid**（原 parent_id 自增号已不存在）
  note TEXT DEFAULT '',
  created_at TEXT, updated_at TEXT,
  UNIQUE(map_name, name)
);
```

**缓存怎么刷（`LLM/maptags.py` 的唯一职责）：**
- **读**：比 `tags.json` 的 `mtime+size` 与缓存表里记的那份元数据 → 一致就直接查表；不一致则**整图重建**（一个事务内 `DELETE FROM … WHERE map_name=?` + 批量 INSERT）。
- **写**：业务代码**不允许直接 INSERT/UPDATE 这两张表** —— 唯一写入口是 `maptags.sync_map(map_name)`，且它只能在"文件已经改完"之后被调用。
- 板卡不可达时**读缓存仍可用**（与 §B4.2 的 `stale` 口径一致）；缓存随 `brain.db` 放在后端所在机器（现在是 PC），删了不影响任何数据。

> 与《分层用户体系》规格 D17 的关系（**口径已第二次变更**）：第一版是 `profiles.zone_json`（存圆几何）；2026-09-14 拍板改用 `zones` 表的自增 `id`；**本条改动后自增 id 不存在了**，改为 `profiles` 记 **（地图名, 区域 `uid`）** 两列（或一个 `{"map":"my_map","zone":"z1"}` 的 JSON 列），解析时经 `maptags.get_zone(map, uid)` 取几何。**仍需回填**《2026-09-14-layered-user-roles-design.md》与其 P0 计划里所有 `zone_id` 的写法（见 §十 回填项）。

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

> **⚠️ 地图现在是"三件套"**：`.pgm` + `.yaml` + **`.tags.json`**（§4.1）。因此本表里 `rename` / `copy` / `delete` 三条**必须连带处理 `.tags.json`**：`cp` 时先做指纹比对再复制；`delete` 要 `confirm=true` 并把受影响标记数写进响应与审计；`list` 要能给出每张图的标记数量，以及"有 pgm 没 tags / 有 tags 没 pgm"的**残缺态**标识。

### 5.2 地点与区域

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/destinations?map=<name>` | 列表 |
| POST | `/api/destinations` | 新增；服务端做完整校验（名称唯一、坐标在地图内、障碍检查）并返回 `warnings[]` |
| POST | `/api/destinations/{uid}` | 改名/别名/坐标/朝向/risk/elder_allowed/note |
| DELETE | `/api/destinations/{uid}` | 删除（写审计） |
| POST | `/api/destinations/learn` | `{name, aliases, risk, elder_allowed, note}` → 取**当前位姿**写入该图 `<图名>.tags.json`（并刷缓存；位姿不可用时返回失败并提示"可改为在图上点选"） |
| POST | `/api/destinations/validate` | `{map_name, x, y}` → `{ok, clearance_m, on_obstacle, reasons[]}`（前端拖动/点选时实时调用） |
| GET | `/api/zones?map=<name>` | 列表（含 `parent` 层级，按 `uid` 串联） |
| POST | `/api/zones` / `POST /api/zones/{uid}` / `DELETE /api/zones/{uid}` | 增改删（按 `uid`；自增号已不存在） |
| GET | `/api/map/{name}/tags` | **直读该图 `.tags.json` 原文**（调试 / 迁移 / 人工核对用） |
| PUT | `/api/map/{name}/tags` | **整份替换**标记文件（高级用途，做 schema 校验）；写文件 + `sync_map()` |
| POST | `/api/map/{name}/tags/reindex` | 强制重建该图的索引缓存（缓存丢失或怀疑不一致时用） |

> **写接口的统一口径（红线 2）：** 所有增改删都是「**改 `<图名>.tags.json`（原子写）→ `maptags.sync_map()` 刷缓存 → 审计**」这三步。**任何接口都不允许只改 SQLite 表**；`{id}` 一律换成 `{uid}`。

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

### 7.2 改 `resolution` / `origin` 会使已标位置失准 —— **现在可检测了**

`tags.json` 里抄了本图的 `resolution` / `origin` 作**指纹**（§4.1）。于是：

- **能检测**：读到标记时先比对 `tags.json` 的指纹与当前 `yaml` 的实际值。不一致 → 直接给出「本图元数据已变（原 `origin=[…]` vs 现 `[…]`），N 个地点 / M 个区域可能整体失准」，**不再需要靠"事后发现标点全偏了"**。
- **仍需显式确认**：改 `resolution`/`origin` 一律弹出「本图有 N 个地点、M 个区域，保存后坐标含义将改变」，要求确认，并把受影响条目写进审计。理由是**我们无法判断作者意图**（是标错了要修正，还是地图换了基准）。
- **改完后**：确认保存时用**新**的 `resolution`/`origin` 覆写 `tags.json` 的指纹 —— 于是"已确认"这件事本身也被记录下来了。

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
5. `maptags` 往返：写 `<图名>.tags.json` → 读回一致；**外部直接改 `tags.json` 后缓存能自动重建**；把两张缓存表清空后仍能重建出同样结果（红线 3）；指纹不一致时给出警告而不是静默按旧坐标用；
6. 改 `resolution`/`origin` 的失效确认流程。

**板卡真机（需现场安全确认后再动）：**

7. 起 rosbridge（`~/tools/nav_screen.sh lat`）→ 编辑器显示车在图上、朝向正确；
8. 车推到某处 → 点"记录当前位置" → 与 `where_am_i.py` 打印的位姿比对（误差应在厘米级）；
9. 自动识别当前地图：`/map` 元数据指纹命中正在跑的那张 yaml；
10. 标 2~3 个地点 + 1 个区域 → 板卡上 `cat ros2_car/maps/my_map.tags.json` 复核（**内容应在文件里**，而不是只在库里），并确认 `brain.db` 只是缓存（清空表内容后仍能重建）。

---

## 十、与既有规格的关系（要做回填）

| 既有文档 | 回填内容 |
|---|---|
| `2026-09-14-layered-user-roles-design.md` §7.1 | `destinations` **增加 `map_name` / `aliases`**；`goal_json` 拆成 `x/y/yaw_deg`。**2026-09-14 二次变更**：这些字段现在住在 `<图名>.tags.json` 里（§4.1），`brain.db` 那份只是缓存 —— 分层体系若要读地点，走 `maptags` |
| 同上 D17 | 病房/床位几何用**多边形、米坐标**，`profiles` 不复制几何 **✅ 已回填（2026-09-14）**；**但载体又变了一次**：几何从 `zones` 表搬到 `<图名>.tags.json`（§〇 第 7 条），自增 `id` 换成稳定 `uid` → **`profiles` 的 `zone_id` 需二次回填为「地图名 + 区域 uid」**（⏳ 待做） |
| `2026-08-27-frontend-multi-end-design.md` | 登记第三个前端包 `mapeditor`（dev 5175、生产 `/mapeditor`、与 admin/kiosk 并列） |
| `ros2_car/建图与导航操作手册.md` | 补一节"地图与位置语义在哪标"（指向 `/mapeditor`）+ 换图命令提示 |

**权限归属（明确留白）：** 本设计的接口本轮不鉴权。《分层用户体系》落地后，`mapeditor` 的全部写接口归 `admin` 层，老人层不可见——该结论写在此处以免遗漏。

---

## 十一、任务分解（粗粒度，供实现计划展开）

| # | 任务 | 依赖 |
|---|---|---|
| 1 | `LLM/maptags.py`：`<图名>.tags.json` 的读写（**原子写**）+ 索引缓存同步（**单向、可重建**）+ 指纹校验；`db.py` 只建两张**缓存**表 + `sync_map()` 唯一写入口 + 校验函数（越界/障碍/未知） | — |
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
| 14 | 测试（§九 的 1~6） | 全部 |
| 15 | 文档回填（§十）+ 板卡真机验收（§九 的 7~10） | 全部 |

---

## 十二、动手前的前置条件（不解决则验收不了）

1. **重扫一张地图**：现有三张图未知率 `my_map` 73.3% / `my_map2` 85.1% / `my_map3` 60.7%，覆盖都不够；
2. **对齐 AMCL 初始位姿**：板卡实测 `/amcl_pose` 恒在 `(0,0,0)`，未对齐时"车在哪"和所有地点的正确性都无从判断；
3. 板卡上 rosbridge 需在跑（`~/tools/nav_screen.sh lat`；注意 `kill all` 后它不会自动回来，`ss -ltnp | grep 9090` 可复核）。

---

# B 篇：像素修图 —— 在开源项目 `D:\_project\Robot\ROS-SLAM-Map-Editor` 上的改进

> **本文档定义的工作 = 对一个已有的开源地图编辑器的改进，不是从零开发。**
> 改造对象是用户放在本仓内的开源工程副本 **`D:\_project\Robot\ROS-SLAM-Map-Editor`**（上游 [GyroPalm/ROS-SLAM-Map-Editor](https://github.com/GyroPalm/ROS-SLAM-Map-Editor)，MIT，定版 `646104e`）——它已经是一个可用的**像素级**地图编辑器；我们要做的是**给它接上"网络 IO + 后端落盘"**，让它能直接读写板卡上的地图文件，而不用把文件拖来拖去。
>
> **目的：** 打开 `/mapeditor/pixel-editor.html?map=my_map`，毛笔刷擦掉走廊尽头的噪点、补一堵漏掉的墙、把门口一小块 unknown 涂成 free，点「Download Map」，改动**经网络落到板卡** `ros2_car/maps/`。后端与浏览器都跑在 PC 上（板卡性能有限，地图工作不下放板卡），全程不碰板卡键盘。
> **状态：** 设计定稿待评审（未开工）。→ **已实现（2026-09-14）**：B 篇的 3 个新增接口（`POST /api/map/{name}/save`、`GET /api/mapeditor/io`、`POST /api/mapeditor/io/test`）、`mapstore.py` 双实现与缓存降级、`public/pixel-editor.html`（相对上游只改 6 处）+ `pixel-netio.js` + `public/vendor/` 自托管全部落地；**未验**的是真实 `vite build` 复验、真实浏览器联调与板卡真机（§B11.3 16~19）。见文末「实现台账与偏差」。
> **日期：** 2026-09-14
> **依赖：** 本文是《2026-09-14-map-editor-design.md》（A 篇：看图 / 标地点 / 划区域）的**加装项**，复用它的 `/mapeditor` 挂载点、`mapserver.py`（PGM 解析）与地图文件接口。**A 篇尚未开工**，故本文 §B十三 任务 1 自带「最小可用子集」，保证加装项不被 A 篇阻塞。
> **用户决策（2026-09-14，原话）：**
> ① 「网上有没有已经开源的 slam 地图编辑器？可以改像素的那种，我直接拿过来改造」
> ② 「我想走路线 a，写一份文档吧。我还想让 io 通过网络连接，这样就不用在开发板上编辑，而是可以在上位机编辑」
> ③ 拓扑选择：**「两个都要，加一个 `MAPS_IO=local|ssh` 开关」**
> ④ 「我想了想，还是把地图工作放到PC上吧，因为板卡性能不是很好。」
> ⑤ **「记住，从这个开源工具改，大部分代码不需要你自己写」**
> **拓扑定案（按 ④ 重排）：** 主形态 = **后端跑在 PC 上**（`MAPS_IO=ssh`，**默认值**），地图文件真相仍在板卡，经 SSH 读写；备用形态 = 后端跑在板卡上（`MAPS_IO=local`）。板卡因此不再承担 PGM 解码、灰度 PNG 编码、未知率统计、静态文件服务这类 CPU 活——顺带解掉了 A 篇 §7.4 对板卡算力的隐忧。
> **选型结论：** 用 **GyroPalm/ROS-SLAM-Map-Editor**（MIT，浏览器单文件 `editor.html`，工作区 43 KB），**不自己写、不用 RViz 系插件**（RViz 系要 ROS 环境，违背 A 篇「Windows 无 ROS 也能开发」的立身之本）。

---

## B〇、改进对象：开源工程在哪、用哪一版、我们改什么

### B0.1 这次改进的性质与三条原则

**性质：** 上游是**一个成品**——一个能画、能撤销、能存 PGM 的浏览器地图编辑器。**它不是半成品，我们不是去补齐它，而是给它换"水管"**：把它的输入从「用户拖进来的文件」换成「后端从板卡拉来的文件」，把它的输出从「下载到浏览器」换成「POST 回后端落盘到板卡」。编辑器本体几乎不动。

**三条原则（全部来自用户 ⑤：「大部分代码不需要你自己写」）：**

1. **上游已有的能力，一行不改**：画笔、擦除、Un-Scan、线/矩形/填充、多级撤销重做、PGM 编解码、keepout 掩膜、动态量距。
2. **只在上游本体的 6 处动手**（5 条 CDN URL + 1 行补丁入口，见 §B6.1），其余字节原样保留 —— 这样才能逐字节 diff，也才能在将来同步上游。
3. **我们的代码全部集中在"接线层"**：`pixel-netio.js`（前端 IO 桥）+ `LLM/mapstore.py`（文件读写抽象）+ 3 个后端接口（见 §B5.2）。**不新增任何编辑器功能。**

### B0.2 上游工程位置与定版

| 项 | 值 |
|---|---|
| **本地路径（用户 2026-09-14 放置，要改的就是这一份）** | `D:\_project\Robot\ROS-SLAM-Map-Editor` |
| 上游仓库 | <https://github.com/GyroPalm/ROS-SLAM-Map-Editor> |
| **定版 commit** | `646104ee80570d66ce86d51fd19fb44b31d936a0`（短 `646104e`，分支 `main`） |
| 工作区状态 | clean（`git -C ROS-SLAM-Map-Editor status` 无输出） |
| 许可 | **MIT**（`ROS-SLAM-Map-Editor/LICENSE`，首行 `MIT License`） |
| 本仓是否跟踪它 | **否** —— 该目录自带独立 `.git`，已加入本仓 `.gitignore` 的 `/ROS-SLAM-Map-Editor/`（与 `/linorobot2/`、`/localization-with-ROS/` 同类先例）。**不要把嵌套仓库提交进来。** |

**目录清单（不含 `.git`）：**

| 文件 | 字节 | 说明 |
|---|---|---|
| `editor.html` | 44280（工作区）/ **43165（git blob，LF）** | **唯一需要改造的文件** —— 整个编辑器（HTML + CSS + 单个 IIFE 的 JS）都在里面 |
| `index.html` | 5866 | 上游项目介绍落地页，**不用** |
| `maps/map.pgm` + `maps/map.yaml` | 250052 + 138 | 上游自带样例地图，可用于跑通"加载 → 画 → 保存"链路 |
| `img/screenshot-main.png` | 73684 | README 截图，**不用** |
| `LICENSE` / `README.md` | 1216 / 4789 | MIT 原文（**必须随衍生作品保留**）/ 上游说明 |

**四条使用约定（照做，别自由发挥）：**

1. **原样复制，再改**：把 `editor.html` **复制到** `frontend/packages/mapeditor/public/pixel-editor.html` 之后再动它；**不要原地改上游 clone** —— 保持上游副本干净，将来同步上游才能做 diff。
2. **换行符坑（不处理就会重演 2026-09-11 的事故）**：上游工作区是 **CRLF**（44280 字节 / 1115 行），git blob 是 **LF**（43165 字节）。复制进本仓**必须转成 LF** —— 本仓 `.gitattributes` 强制 `* text=auto eol=lf`，背景正是 2026-09-11 板上提交把 32 个文件写成 CRLF、导致"整文件假冲突"（git 认为每一行都变了）。**验收标准：复制后 `git diff --stat` 不得出现"整个文件重写"**（只应是 §B6.1 那 6 处的几十行）。
3. **判断上游文件的权威字节数/换行符，看 git blob 而不是工作区**：`git cat-file -s HEAD:editor.html`。
4. **同步上游的代价**：上游更新后，按 §B6.1 的 6 处重做即可；其余字节保持原样，便于逐字节 diff。

### B0.3 上游给什么 / 我们改什么（能力对账表）

**这是本文档的核心：左两列是上游的既有资产（照用），右两列才是我们要写的。**

| 能力 | 上游现状（**照用**） | 我们的改动 | 落在哪 |
|---|---|---|---|
| 打开地图文件 | `handleFiles(fileList)` 收拖入的 `yaml`+`pgm`；`#yamlInput`/`#pgmInput` 的 `change` 与 `#drop` 的 drop 汇入同一函数 | **改用注入**：从后端 fetch 到字节 → `new File(...)` → `DataTransfer` 塞进 input → 派发 `change` | §B6.2 读通道 |
| 画笔 / 擦除 / Un-Scan（标未知） | 原生，含笔刷尺寸与 live preview | 无 | — |
| 线 / 矩形 / 填充矩形 | 原生 | 无 | — |
| 多级撤销 / 重做 | 原生（`Ctrl+Z` / `Ctrl+Y`） | 无 | — |
| 像素数据模型 | `pgm = {magic,width,height,maxval,pixels}`，**直接改数组**再 `redrawMap()` 全量重绘（不回读 canvas） | 无 | — |
| PGM 编解码 | `parsePGM`（P5/P2、>255 走 2 字节大端）/ `encodePGM` | 无 | — |
| keepout 掩膜 | 原生（`*_keepout.pgm`） | 无（但**我们的地图名禁用 `keepout` 字样**，否则会被它误判成掩膜，见 §B7.1） | — |
| 动态量距 | 原生（米/英尺） | 无 | — |
| yaml 解析 / 生成 | `jsyaml.load` / `jsyaml.dump` —— **CDN 硬依赖**；且 `dump()` 是**重新序列化**，注释/引号风格会丢 | **改 CDN → 本地 vendor**（§B6.1 改动 2）；**yaml 回写不走它的 dump** —— 后端以磁盘原文为本，只替换 `image:` 一行 | §B6.1 / §B5.2 第 7 步 |
| 5 条外部资源 | jQuery 3.4.1、js-yaml 4.1.0、Bootstrap 4.4.1（CSS+JS）、Font Awesome 4.7.0，全部走 CDN | **全部替换为本地自托管**（jQuery 与 js-yaml 是硬依赖，不通就整页白屏） | §B6.1 改动 1~5 |
| 输出（保存） | `dlBytes` → `createObjectURL` → 游离 `<a download>` → `click()` → 同步 `revokeObjectURL`，**只能下载到浏览器下载目录** | **截获**：覆写 `URL.createObjectURL`（存 Blob）+ `revokeObjectURL`（no-op）+ `HTMLAnchorElement.prototype.click` → 改走 `POST` | §B6.2 写通道 |
| 落盘 / 备份 / 校验 | 无（纯前端，不碰文件系统） | **新增后端**：名字白名单、体积上限、yaml 白名单校验、自动备份、原子写、审计 | §B5.2 |
| "地图文件在哪" | 无概念 | **新增** `MapStore` 抽象（`local` / `ssh` 双实现）+ 离线缓存 | §B四 |
| 与车/ROS 的关系 | 无 | 本加装项**不碰 ROS**；位姿与"当前地图识别"属 A 篇 | — |
| 鉴权 | 无 | 本轮不做（沿用 A 篇 §十 留白，仅 Tailscale/局域网内使用） | §B2.2 |

**一句话总结这张表：我们只写两样东西——「怎么把文件从板卡喂进编辑器」，和「怎么把编辑器吐出来的字节存回板卡」。其余全是上游的。**

---

## B一、目标与验收

**目标：** 打开 `/mapeditor/pixel-editor.html?map=my_map`，毛笔刷擦掉走廊尽头的噪点、补一堵漏掉的墙、把门口一小块 unknown 涂成 free，点保存，改动**通过网络落到板卡** `ros2_car/maps/`；全程在 Windows 上完成，不碰板卡键盘。

**验收标准（可逐条打勾）：**

1. `/mapeditor/pixel-editor.html?map=<name>` 能打开**上游那个编辑器**，并且**自动加载**该图的 `yaml`+`pgm`（不需要用户拖文件）。
2. 加载后画笔、擦除、Un-Scan（标未知）、线、矩形、撤销重做、keepout 掩膜、量距**全部可用** —— 即上游原生功能**零损失**（§B0.3 左两列逐条对过）。
3. **完全离线可用**：板卡/PC 断开公网，页面照常打开与绘制（5 个 CDN 资源全部本地自托管）。
4. 点「Download Map」后**不弹出浏览器下载框**，而是被截获 → `POST` 回后端 → 落盘到板卡 maps 目录；页面上给出「已保存到 `<路径>`，备份 `<路径>`」的明文反馈。
5. 保存目标的**默认行为是另存为新图**（`<原名>_edited`），覆盖原图必须显式勾选「覆盖原图」且后端先做自动备份。
6. 后端对回传 yaml 做**白名单校验**：除 `image` 字段外，任何字段的磁盘原值与回传值不一致 → 拒绝保存并说明差异（防止 `resolution`/`origin` 被静默改掉、让 A 篇已标地点全部失效）。
7. **主形态可用（默认）**：后端跑在 PC 上、`MAPS_IO=ssh` 时，能列出、读取、写回板卡 `/home/sunrise/Robot/ros2_car/maps/`；`MAPS_IO=local`（后端跑在板卡上）作为备用形态，行为与 A 篇完全一致。
8. **远程不可用不崩**：ssh 连不上/依赖缺失时，接口返回 `{"ok": True, "status": "unavailable", "reason": "..."}`（查询类保持 `ok: True`，遵 AGENTS「系统稳健性」），且**离线缓存副本仍可看图**并明确标注「当前离线，显示缓存（时间戳）」。
9. 路径与命令安全：地图名白名单 `^[A-Za-z0-9_-]{1,64}$`，任何含 `..`、`/`、空格的输入一律拒绝；ssh 子进程模式下这条是**防命令注入红线**。
10. 对上游本体的改动**只有 6 处**，`git diff --stat` 不出现"整个文件重写"（§B0.2 约定 2）。
11. 保存 / 拒绝 / IO 模式切换全部落审计日志（事件名 `map_edit_save` / `map_edit_reject` / `map_io_change`）。

---

## B二、范围

### B2.1 本轮做

| 能力 | 来源 | 说明 |
|---|---|---|
| 像素级修图（浏览器） | **上游照用** | 画笔/擦除/Un-Scan/线/矩形/填充/撤销重做/keepout/量距 |
| PGM 编解码、像素模型 | **上游照用** | 不改一行 |
| 资源自托管 | 我们替换 | jQuery 3.4.1、js-yaml 4.1.0、Bootstrap 4.4.1（CSS+JS）、Font Awesome 4.7.0（含 webfonts）落入 `public/vendor/` |
| 网络 IO（读） | 我们新增 | 页面按 URL 参数自动从后端拉 `yaml`+`pgm`，不走拖拽 |
| 网络 IO（写） | 我们新增 | 覆写下载通道 → 回传后端 → 落盘 + 备份 + 审计 |
| `MapStore` 抽象 | 我们新增 | `ssh`（**默认**，PC 上跑后端 → 远程读写板卡）/ `local`（备用，板卡上跑后端）双实现 |
| 远程缓存 | 我们新增 | 本地缓存目录 + 远程 `mtime/size` 新鲜度判断；离线可看 |
| 安全 | 我们新增 | 地图名白名单、上传体积上限、yaml 白名单校验、备份保留 N 份 |

### B2.2 本轮明确不做

| 不做 | 理由 |
|---|---|
| **给编辑器加新功能**（自己画笔刷/撤销栈/线算法） | 上游全都有。用户拍板的「路线 a」与⑤「大部分代码不需要你自己写」 |
| 把绘制逻辑移植进 `MapCanvas.vue`（路线 b） | 用户已明确否决；且会牵动 A 篇画布的坐标换算，风险集中 |
| 多边形工具、速度限制掩膜 | 上游自己也列为 Future Work，与本次痛点无关 |
| 裁剪 / 改分辨率后重存 / 改 origin | A 篇 §7.2 已定：改这两个字段一律要显式确认；本加装项**只改像素，不动元数据** |
| 建图、从 SLAM 直接存图、远程启停 Nav2 | 沿用 A 篇 §2.2：一律走 `~/tools/nav_screen.sh` |
| 权限与登录 | 沿用 A 篇 §十 留白：接口暂不鉴权，仅 Tailscale/局域网内使用 |
| ⚠️ **重扫一张地图** | **不是「不做」，是「必须先做」** —— 见 §B十四 前置条件 |

---

## B三、拓扑与数据流

用户拍板：**`MAPS_IO=local|ssh` 双模式，两个都要**；并在 2026-09-14 进一步定为**以 PC 为主**（④「还是把地图工作放到PC上吧，因为板卡性能不是很好」）。

### B3.1 主形态：后端跑在 PC 上（`MAPS_IO=ssh`，**默认**）

```
PC 浏览器 ──HTTP──▶ PC :8000（LLM 后端，工作目录 D:\_project\Robot）
                     │
                     ├─ MapStore(ssh) ──SSH/SFTP──▶ sunrise@100.65.82.93
                     │                               /home/sunrise/Robot/ros2_car/maps/
                     ├─ 本地缓存 LLM/data/mapcache/（离线看图）
                     └─（A 篇）rosbridge ws://100.65.82.93:9090 读位姿
```

- **为什么放 PC**：板卡（RDK X5）性能有限，而地图工作里吃 CPU 的活（PGM 解码、灰度 PNG 编码、未知率直方图统计、静态文件服务）现在全落在 PC 上；板卡只留下它必须独占的本职（串口、建图/定位/导航）。
- 好处：浏览器与后端同在 PC（localhost，零网络延迟）；板卡不在、Tailscale 只通一半、临时断网时仍能看图（缓存）与画图，改动攒在缓存里、恢复连接后写入。
- 代价：多一层网络文件代码，本身会成为新的故障点 —— 因此**所有失败都必须降级而非报错**（§B八）；同时**板卡可达性从「验收相关」升级为「开工前置」**（§B十四）。
- 附带要求：A 篇的「车在哪」「当前地图识别」也随之改由 PC 侧读板卡，因此**板卡上的 rosbridge 必须从 PC 可达**（Tailscale 通 + 板卡上起 `~/tools/nav_screen.sh lat`，用 `ss -ltnp | grep 9090` 复核）。但看图 / 标点 / 像素修图**不依赖 rosbridge** —— 它挂了照样能编辑。

### B3.2 备用形态：后端跑在板卡上（`MAPS_IO=local`）

```
PC 浏览器 ──HTTP──▶ 板卡 :8000（LLM 后端，工作目录 /home/sunrise/Robot）
                      │
                      ├─ MapStore(local) ──▶ /home/sunrise/Robot/ros2_car/maps/
                      └─（A 篇）rosbridge :9090 读位姿
```

- 地图文件对后端就是本地文件，**零新增依赖、零远程代码**，实现最简单（纯 stdlib）。
- 保留它的三个用途：① 作 `SshMapStore` 的**行为基准与契约测试对照组**（`MapStore` 接口对上层完全一致，上层代码一行不用改）；② 主形态断网时，开发期仍可用它 + `MAPS_DIR` 指向仓库副本推进前端；③ 万一 PC 不在场，一台能上网的终端也能临时顶上。
- 此形态下前端开发仍可 `vite dev`(:5175) 把 `/api` 代理到 `http://100.65.82.93:8000`。

### B3.3 改造产物的落点

```
frontend/packages/mapeditor/public/
  pixel-editor.html          ← 复制自 ROS-SLAM-Map-Editor/editor.html（转 LF）+ 仅 6 处改动（§B6.1）
  pixel-netio.js             ← 我们的接线层（同文档加载，覆写浏览器 API 截获 IO）
  vendor/
    jquery-3.4.1.min.js
    js-yaml-4.1.0.min.js
    bootstrap-4.4.1.min.css / bootstrap-4.4.1.bundle.min.js
    font-awesome-4.7.0/css/font-awesome.min.css
    font-awesome-4.7.0/fonts/*            ← CSS 用相对路径引 webfonts，目录结构必须照搬
    ROS-SLAM-Map-Editor.LICENSE          ← 上游 MIT 原文，必须保留（§附录）
```

生产路径：`http://<后端地址>:8000/mapeditor/pixel-editor.html?map=my_map`。

---

## B四、后端：`LLM/mapstore.py`（新增）

一期一责：**只负责「地图文件在哪、怎么读写」，不含任何 HTTP 与业务校验**。

```python
class MapStore(Protocol):
    def available(self) -> tuple[bool, str]: ...        # (是否可用, 不可用原因)
    def list(self) -> list[MapEntry]: ...               # name/width/height/resolution/origin/
                                                        # unknown_ratio/has_pgm/mtime/source
    def read(self, name: str, ext: str) -> bytes: ...    # ext ∈ {"yaml","pgm"}
    def write(self, name: str, ext: str, data: bytes) -> None
    def remove(self, name: str, ext: str) -> None
    def rename(self, name: str, new_name: str) -> None
    def stat(self, name: str, ext: str) -> tuple[float, int] | None   # (mtime, size)
```

### B4.1 两个实现

| 实现 | 传输 | 依赖 | 说明 |
|---|---|---|---|
| `LocalMapStore(root)` | `pathlib` 直接读写 | **纯 stdlib** | **备用形态**（板卡上跑后端），兼作契约测试基准；`root = conf.MAPS_DIR = BASE_DIR/"ros2_car"/"maps"` |
| `SshMapStore(host, user, port, root, auth)` | SFTP | ① 优先 `paramiko`（**可选依赖**，`try/except` 顶层引入）；② 降级为 `ssh.exe`/`scp.exe` 子进程（密钥认证） | **主形态（默认）**（PC 上跑后端）；两种通道都不可用 → `available() = (False, "缺少 paramiko 且 ssh 客户端不可用")` |

**`SshMapStore` 的两条通道为什么都要留：**
- `paramiko` 走 SFTP，能流式读写、能改密码认证，最省事，但它是外部依赖 → 必须遵 AGENTS「可选依赖顶层 try/except、缺失只降级不崩」；
- 子进程 `ssh.exe`/`scp.exe` 是 Windows 自带的（本机已确认存在 `C:\Windows\System32\OpenSSH\ssh.exe`），零依赖，但**只支持密钥认证**（明文密码无法自动化，Windows 没有 `sshpass`）。
- 选择顺序在配置里可强制：`MAPS_SSH_TRANSPORT=auto|paramiko|cli`。

> **子进程模式的硬约束：** 命令由字符串拼接 `host`/`user`/`path` 而成，因此 `name` 必须先过 §B7.1 的白名单正则 —— **这是本加装项唯一的一处命令注入面**。

### B4.2 缓存与新鲜度

- 缓存目录 `DATA_DIR/mapcache/`，键 = `sha1(io_mode + root + name + ext)`，值为 `{data, mtime, size, at}`。
- 远程模式下读流程：`stat()`（一次轻量 SSH 往返）→ 与缓存记录的 `mtime/size` 一致则直接用缓存 → 否则 `read()` 并刷新缓存。
- **`stat()` 失败（断网）时**：如果缓存里有值，返回缓存并置 `stale=True`（前端显示「当前离线，显示缓存（2026-09-14 11:00）」）；缓存也没有 → `status: "unavailable"`。
- 缓存**永不**用于 `list()` 的权威结论：列表以远程为准，失败时才用缓存兜底并标 `stale`。

### B4.3 依赖与启动

`server.py` 的 `lifespan` 里**不新增任何启动步骤**（`MapStore` 是惰性构造、按需连接），避免拖累后端启动；只加一行 `audit.log("map_io_change", mode=...)` 记录生效模式。

---

## B五、后端接口（`server.py` 路由）

### B5.1 复用 A 篇（本加装项不重新定义，只要它存在）

| 方法 | 路径 | 本文用途 |
|---|---|---|
| GET | `/api/map/list` | 页面顶部的选图下拉 |
| GET | `/api/map/{name}/download?file=yaml\|pgm` | **读原始字节**。`fetch()` 忽略 `Content-Disposition: attachment`，直接拿 bytes 即可，故**不再新增 `/raw` 接口** |

> 若 A 篇尚未开工，任务 1 只需实现这两条（约 60 行，`mapstore.py` + 两个路由），即可支撑本加装项独立落地。

### B5.2 新增

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/map/{name}/save` | 保存像素改动。**JSON 体**：`{pgm_b64, yaml_text, mode:"saveas"\|"overwrite", new_name?, confirm?}`。~~返回 `{ok, wrote:[...], backup, rejected[]}`~~ → **实际返回字段见文末「实现台账与偏差」D7：`{ok, wrote[], backup[], target, tags_copied, warnings[], restart_hint, note}`**（**没有 `rejected[]`**；拒绝走 `4xx` + `{ok:false, error, ...}`） |
| GET | `/api/mapeditor/io` | `{mode, root, available, reason, transport, stale, cached_at}`，供页面状态条显示 |
| POST | `/api/mapeditor/io/test` | 主动做一次连通性自检（`list()` 一次），返回耗时与错误原因；**不修改任何文件** |

**`/api/map/{name}/save` 的执行顺序（每一步都不可省）：**

1. 白名单校验 `name` 与 `new_name`（§B7.1），拒绝路径穿越与命令注入；
2. 体积校验：解码后 `pgm` ≤ `MAPS_MAX_PGM_BYTES`（默认 10 MB），`yaml_text` ≤ 64 KB；
3. 解析上传的 `yaml_text`（**纯 stdlib 的扁平 `key: value` 解析器** —— A 篇 §7.4 已定「能用 stdlib 就绝不引外部依赖」，本项目依赖清单里没有 pyyaml），与磁盘原 yaml 逐字段比对 —— **除 `image` 外任何字段不一致 → `map_edit_reject` + 409**，响应里列出差异键；
4. 决定落盘名：`mode=saveas` → `new_name or f"{name}_edited"`；`mode=overwrite` 且 `confirm=true` → `name`；
5. 备份：把目标图**当前**的 `pgm`+`yaml` 复制到 `maps/.backup/<name>.<YYYYmmdd-HHMMSS>.{pgm,yaml}`，保留最近 `MAPS_BACKUP_KEEP`（默认 10）组，超出即删最旧（备份目录的清理**只按本加装项自己的命名规则删**，绝不递归删目录）；
6. **标记随行**（本次新增，解掉"另存新图后区域全丢"）：把 `<原名>.tags.json` 的 `resolution`/`origin` **指纹**与本次要写出的 yaml 实测值比对 —— 一致则复制成 `<新名>.tags.json`（`mode=overwrite` 时原地保留、只更新指纹）；**不一致则不复制**，并在响应 `warnings[]` 里说明「该图元数据已变，标记未随行」。最后调 `maptags.sync_map(目标名)` 刷新索引缓存；
7. 写 `pgm`：先写 `<目标>.pgm.tmp` 再 `rename` 原子替换（远程模式先传 tmp 再 mv）；
8. 写 `yaml`：**以磁盘原文为本**，只替换 `image:` 那一行（无该行则追加），使注释与键顺序 100% 保留；`image:` 值写成与第 4 步一致的实际文件名；
9. `audit.log("map_edit_save", ...)`，返回 `wrote` 与 `backup` 路径。

> **为什么 yaml 以磁盘原文为本、而不是用前端回传的文本：** 上游内部用 `js-yaml` 的 `dump()` **重新序列化**，注释、缩进风格、引号风格全部丢失（键值本身保留）。它对我们的 7 行 yaml 无害，但一旦将来有人往 yaml 里写注释，就会被静默清掉。以磁盘原文改一行为准，成本更低、更安全。

---

## B六、前端：把上游编辑器接进本仓

### B6.1 `pixel-editor.html`：相对上游原文件**只改 6 处**

> 「原文件」= **`ROS-SLAM-Map-Editor/editor.html`**（本地路径、定版 commit 与复制/换行符约定见 **§B0.2**）。先复制成 `frontend/packages/mapeditor/public/pixel-editor.html`（**转 LF**），再按下表改。

保持其余字节完全不动，方便将来上游更新时做「重新替换这 6 处」的机械操作（改动清单同时抄在 §附录，便于 diff 校验）：

| # | 改动 | 原内容 → 新内容 |
|---|---|---|
| 1 | jQuery | `https://code.jquery.com/jquery-3.4.1.min.js` → `./vendor/jquery-3.4.1.min.js` |
| 2 | js-yaml | `https://cdn.jsdelivr.net/npm/js-yaml@4.1.0/dist/js-yaml.min.js` → `./vendor/js-yaml-4.1.0.min.js` |
| 3 | Bootstrap CSS | stackpath CDN → `./vendor/bootstrap-4.4.1.min.css` |
| 4 | Bootstrap JS | stackpath CDN → `./vendor/bootstrap-4.4.1.bundle.min.js` |
| 5 | Font Awesome CSS | cdnjs CDN → `./vendor/font-awesome-4.7.0/css/font-awesome.min.css` |
| 6 | 补丁入口 | `</body>` 前加一行 `<script src="./pixel-netio.js"></script>` |

> 第 1、2 条是**硬依赖**：读源码确认整页用 `$` 绑事件、用 `jsyaml.load/dump` 解析与生成 yaml，任一 CDN 不可达即整页不可用。第 3~5 条挂了只是样式/图标退化。

### B6.2 `pixel-netio.js`：接线层的挂钩机制（为什么必须在**同文档**里）

**关键事实（来自对上游 `editor.html` 的源码取证）：** 全部 JS 内联在**一个 IIFE** 里，状态（`yamlObj/pgm/mask/tool/brush/zoom`）与函数（`handleFiles/parsePGM/encodePGM/dlBytes/dlText/redrawMap`）都是闭包局部量，**没有挂到 `window`**。

推论：**「外部脚本替换内部函数」这条路根本不存在**（等于改源码）。补丁只能挂在 **DOM / 浏览器 API 层**。而跨源 iframe 里每个 realm 有各自的 `URL` 构造器，父页覆写不了子页的 —— 所以补丁脚本必须与 `pixel-editor.html` **同文档**（§B6.1 第 6 处改动就是为此）。

**读通道（进）：** 注入文件对象。
```js
function inject(sel, file) {
  const dt = new DataTransfer();        // 每个 input 各建一个 dt，避免互相污染
  dt.items.add(file);
  const input = document.querySelector(sel);
  input.files = dt.files;               // 直接赋值 FileList（需 DataTransfer 构造器可用）
  input.dispatchEvent(new Event('change', {bubbles: true}));
}
inject('#yamlInput', new File([yamlBytes], `${name}.yaml`, {type: 'text/yaml'}));
inject('#pgmInput',  new File([pgmBytes],  `${name}.pgm`,  {type: 'application/octet-stream'}));
```
- 依据：`$('#yamlInput').on('change', e => handleFiles(e.target.files))`，pgm 同；`#drop` 的 drop 也汇入同一个 `handleFiles`。**不需要**先伪造 `dragover`。
- **必须 yaml 与 pgm 都注入**：两个下载按钮都有 `if(!pgm || !yamlObj){ alert('Load YAML and PGM first.'); return; }`。
- 文件名必须**以 `.pgm` 结尾**（`isPgm = /\.pgm$/i`）；且**名字里不能出现 `keepout`**（`isKeepoutName` 正则会把含 keepout 的一律当掩膜处理）。→ 后端限制 + 文档告知。
- 注入顺序：**base pgm 先、mask 后**（`loadBasePGM` 在尺寸不一致时会把 mask 重置成全 255）；`yaml` 与 `pgm` 之间无顺序要求。本轮不涉及掩膜文件，顺序无影响，但注释里留痕。

**写通道（出）：** 覆写下载。
```js
const blobURLs = new Map();                     // url -> Blob
const _create = URL.createObjectURL.bind(URL);
URL.createObjectURL = (blob) => { const u = _create(blob); blobURLs.set(u, blob); return u; };
URL.revokeObjectURL = () => {};                 // ← 必须 no-op：上游紧随其后同步 revoke
const _click = HTMLAnchorElement.prototype.click;
HTMLAnchorElement.prototype.click = function () {
  if (this.download && /\.(pgm|ya?ml)$/i.test(this.download)) {
    const blob = blobURLs.get(this.href);
    if (blob) { upload(blob, this.download); return; }   // 不发真实下载
  }
  return _click.apply(this, arguments);
};
```
- 依据：两个下载入口都汇入 `dlBytes(bytes, filename, mime)` / `dlText(...)`：`new Blob` → `createObjectURL` → 生成游离 `<a href download>` → `a.click()` → 同步 `revokeObjectURL`；**无 `window.open`、无 `msSaveBlob`、无 `appendChild`**，通道单一封闭。
- **覆写时机**：补丁在文档尾部执行，用户点击必然发生在之后，时机足够。
- 若 `blobURLs` 查不到（上游改版），**兜底放行原始下载**并弹提示「补丁可能已失效，已退回浏览器下载」，不静默丢数据。

**状态条与保存目标：** 补丁在页面顶部插一条自己的状态栏（脱离 Bootstrap 样式，独立 `<div>`），显示：当前图名 / IO 模式（local·ssh）/ 是否离线缓存 / 保存目标（下拉：另存 `<名>_edited` ← 默认、覆盖原图）；并接管保存流程的提示与错误显示。

### B6.3 开发与生产接线

- `frontend/packages/mapeditor/vite.config.ts`：`base: "/mapeditor/"`、端口 `5175`、`proxy: { "/api": { target: process.env.VITE_API_TARGET || "http://127.0.0.1:8000", changeOrigin: true } }`。上位机开发时 `VITE_API_TARGET=http://100.65.82.93:8000`。
- `frontend/package.json` 加 `"dev:mapeditor": "pnpm --filter mapeditor dev"`。
- `server.py` 的 `_ADMIN_DIST/_KIOSK_DIST` 之后加 `_MAPEDITOR_DIST` 并挂 `/mapeditor`（`StaticFiles` 的 `html=True` 只能 index 回退，故本页用**显式文件名** `pixel-editor.html`，不做 SPA 路由）。

---

## B七、安全与保存策略

### B7.1 名字白名单（红线）

`^[A-Za-z0-9_-]{1,64}$`，且不含 `keepout`（§B6.2）。`name` 与 `new_name` 都要过。用途：
- 防路径穿越（`../../etc/passwd`）；
- **防 ssh 子进程模式的命令注入**（§B4.1）；
- 避免触发上游的 `keepout` 文件名误判。

拒绝时返回 `{"ok": False, "error": "地图名不合法"}` 并写 `map_edit_reject`。

### B7.2 保存策略：默认另存，覆盖要确认

| 模式 | 落盘 | 是否备份 | 触发条件 |
|---|---|---|---|
| `saveas`（默认） | `<原名>_edited.{pgm,yaml}` | 不备份（目标通常不存在；若已存在则先备份） | 页面默认 |
| `overwrite` | `<原名>.{pgm,yaml}` | **强制备份** | 页面勾选 + `confirm=true` |

**覆盖保存后，运行中的 Nav2 不会自动换图**：`map_server` 的地图是启动时一次性读入的，改完文件必须重启导航才生效。页面在覆盖保存成功后直接显示可一键复制的命令：`~/tools/nav_screen.sh nav <地图名>`（与 A 篇 §7.3 同一口径：后端**不**去 SSH 杀/起 screen 会话）。

### B7.3 体积与类型

| 项 | 上限/规则 | 超限行为 |
|---|---|---|
| `pgm` 解码后 | `MAPS_MAX_PGM_BYTES` = 10 MB | 413 |
| `yaml_text` | 64 KB | 413 |
| 上传体 `Content-Length` | 16 MB | 413 |
| `pgm` 魔数 | 接受 `P5`（二进制）与 `P2`（ASCII），其他魔数拒绝（上游 `parsePGM` 同样只认这两种） | 400 |
| 未知字段 | yaml 只认一期的扁平 `map_saver` schema | 400 |

---

## B八、错误处理与降级

| 情况 | 行为 |
|---|---|
| `MAPS_IO=ssh` 且 paramiko 缺失、ssh 客户端不可用 | `available=False`；查询类接口 `{"ok": True, "status": "unavailable", "reason": "..."}`；写操作 `{"ok": False, "error": "..."}` |
| ssh 连不上 / 超时 | 读：有缓存 → 返回缓存 + `stale: True` + `cached_at`；无缓存 → `unavailable`。写：明确报错（含主机与错误串），**不做静默重试** |
| 板卡 maps 目录为空 | 列表返回空数组 + 页面提示「maps 目录为空」，不报错 |
| yaml 缺失对应 pgm | 列表该条标 `has_pgm: false` 并标红，其余条目照常 |
| yaml 字段非法/缺失 | 该图标 `meta_ok: false`，不参与 A 篇「当前地图识别」 |
| 上传 yaml 与磁盘原值除 `image` 外不一致 | **409 + 差异键列表**，写 `map_edit_reject` |
| 备份目录写不进去（只读挂载/权限） | **拒绝覆盖保存**（备份是覆盖的前置条件，不允许"备份失败但继续"）；另存模式可继续 |
| 写盘失败 | 返回含目标路径的错误字符串；`.tmp` 残留由下次写入覆盖，不做后台清理 |
| 本机没有地图目录却把 `MAPS_IO` 设为 `local`（默认已是 `ssh`，只有手工改回才会遇上） | 列表为空 + 提示「本机没有地图目录，请把 MAPS_IO 设为 ssh 或配置 MAPS_DIR」 |
| 补丁注入失败（上游改版） | 保留上游原生拖拽/下载能力可用，页面顶部红字提示「网络 IO 补丁失效」 |

---

## B九、已知坑清单（动手前先读这一节）

**上游侧（改它之前必须知道）：**

1. **两个 CDN 是硬依赖**：jQuery 与 js-yaml 不通 → 整页白屏。自托管是本加装项的第一件事，不是优化项。
2. **Font Awesome 的 CSS 用相对路径引 webfonts**（`../fonts/fontawesome-webfont.woff2` 等），必须把 `fonts/` 目录一并搬，否则图标全成空框。
3. **补丁必须与编辑器同文档**（IIFE 闭包 + 跨源 iframe 无法覆写 `URL`）。这一条决定了 §B6.1 第 6 处改动不是"可选优化"。
4. **`revokeObjectURL` 必须改成 no-op**：上游点击后**同步** revoke，异步去取 `blob:` URL 会失败。
5. **必须 yaml+pgm 一起注入**，否则两个下载按钮都直接 `alert` 返回。
6. **文件名不能含 `keepout`**，否则被当掩膜，编辑的是另一张画布。
7. **yaml 被上游重新序列化**（键值保留、注释丢失）→ 后端以磁盘原文为本，只改 `image:` 一行（§B5.2 第 7 步）。
8. **上游给出的 yaml 里 `image:` 是 `<名>_edited.pgm`**，与我们的目标名可能不一致 → 以第 4 步决定的目标名为准覆写。
9. **判断上游文件的权威字节数看 git blob**（`git cat-file -s HEAD:editor.html`），不看工作区（CRLF 会多出行数个字节）。

**落盘与现场侧：**

10. **改完地图不等于生效**：`map_server` 启动时一次性加载，必须重启导航（§B7.2）。
11. **未重扫的地图，修图收益有限**：现有三张图未知率 `my_map` 73.3% / `my_map2` 85.1% / `my_map3` 60.7%（A 篇 §十二）。手涂 85% 的未知区不现实 —— 本加装项适合「走廊尽头一小块噪点」「门口漏了一堵墙」这种局部修补。**它替代不了重扫一张图。**
12. **备份目录会污染一期的地图列表**：`maps/.backup/` 必须在 `list()` 里排除（回填项，见 §B十二）。
13. **`saveas` 生成的 `_edited` 图**：标记（`.tags.json`）会**连带复制**，且复制前做 `resolution`/`origin` **指纹比对**（§B5.2 第 6 步）；指纹不一致只给 `warnings[]`、**不复制**，绝不静默错配。⚠️ 旧语义"新图视为空白图"已作废（A 篇 §〇 第 6 条）。
14. **命令注入**：ssh 子进程模式拼接命令行，名字白名单是唯一防线（§B7.1）。

---

## B十、配置项（`conf.py`）

| 键 | 默认 | 含义 |
|---|---|---|
| `MAPS_IO` | `"ssh"` | `ssh`（**默认**：PC 上跑后端，远程读写板卡 —— 用户 2026-09-14 定「地图工作放到 PC 上」）\| `local`（备用：板卡上跑后端） |
| `MAPS_DIR` | `BASE_DIR / "ros2_car" / "maps"` | `local` 模式的根目录（A 篇已规划同一项） |
| `MAPS_SSH_HOST` | `"100.65.82.93"` | 板卡地址（Tailscale） |
| `MAPS_SSH_USER` | `"sunrise"` | 登录用户 |
| `MAPS_SSH_PORT` | `22` | 端口 |
| `MAPS_SSH_ROOT` | `"/home/sunrise/Robot/ros2_car/maps"` | 远程 maps 目录（**路径勘误：不是 `~/ros2/car_ws`**） |
| `MAPS_SSH_KEY` | `""` | 私钥路径（空则用 `~/.ssh/id_*` 默认） |
| `MAPS_SSH_PASSWORD` | `""` | 仅 paramiko 通道使用；**从 `.env` 读，不入 git** |
| `MAPS_SSH_TRANSPORT` | `"auto"` | `auto` \| `paramiko` \| `cli` |
| `MAPS_SSH_TIMEOUT` | `10` | 单次连接/命令超时（秒） |
| `MAPS_MAX_PGM_BYTES` | `10 * 1024 * 1024` | 上传 pgm 上限 |
| `MAPS_BACKUP_KEEP` | `10` | `.backup/` 保留组数 |
| `MAPS_CACHE_DIR` | `DATA_DIR / "mapcache"` | 远程模式本地缓存 |

> 密码走 `.env`（`MAPS_SSH_PASSWORD`），并确认 `.gitignore` 已排除 `.env`（AGENTS 已排除）。

---

## B十一、测试与验收

### B11.1 无网络、无板卡（Windows 主力口径）

> **测试策略（主形态是 ssh 之后尤其重要）：** `MapStore` 的两条实现要跑**同一套契约测试**（CRUD / rename / stat / `list()` 排除备份 / 名字白名单），`SshMapStore` 一侧通过注入指向 `127.0.0.1`（本机 OpenSSH server）或假 transport 的配置来跑，**不让单测依赖板卡**。板卡只出现在 §B11.3 的真机验收里。

1. `LocalMapStore` 对临时目录的 CRUD / rename / stat 正确；`list()` **排除** `.backup/`；
2. 名字白名单：`../etc/passwd`、`a/b`、`a b`、`x"y`、`含keepout名`、超 64 字符 → 全部 400，且**断言没有任何文件被触碰**；
3. yaml 白名单校验：改 `resolution` 的请求 → 409 且列出 `resolution`；只改 `image` → 通过；
4. 保存流程：`saveas` 产出 `_edited.{pgm,yaml}`，`image:` 与文件名一致；`overwrite` 产出备份文件且备份内容 == 保存前的字节；
5. 备份保留：连存 12 次 → `.backup/` 只剩 10 组且删的是最旧的；
6. 体积上限：11 MB 的 pgm → 413；
7. `MAPS_IO=ssh` 但 `paramiko` 未安装 + `MAPS_SSH_TRANSPORT=cli` + 假 `ssh.exe`（一个假脚本）→ 走子进程路径；全不可用 → `available=False` 且**后端照常启动**（AGENTS 红线：可选依赖不得进顶层硬 import）；
8. 缓存降级：先成功读一次填充缓存 → 让 `stat()` 抛异常 → 接口返回缓存 + `stale: True` + `cached_at`；
9. `pixel-netio.js` 的纯函数部分（Blob→目标名映射、`_edited` 后缀处理、白名单前端预校验）用 Vitest 单测。

### B11.2 浏览器手工验收（无需板卡：临时用 `MAPS_IO=local` + `MAPS_DIR` 指向仓库副本，绕开板卡先把整条前端通路验完）

10. 断网（禁用网卡）打开 `/mapeditor/pixel-editor.html?map=my_map` → 页面正常、图标正常、地图自动加载；
11. **上游功能零损失核对**：撤销重做、Un-Scan、线、矩形、填充、keepout、量距各点一遍，确认补丁没破坏它们（对应 §B0.3 左两列）；
12. 画一笔 → 点「Download Map」→ **无浏览器下载框**，页面显示保存成功与备份路径；磁盘上出现 `my_map_edited.pgm`；
13. 用 A 篇 `mapserver.py` 重新渲染该图 → 修改的像素可见；
14. 勾选覆盖原图 → 保存 → 备份目录出现一组备份；页面给出 `nav_screen.sh nav` 命令。
15. **改动量核对**：`git diff --stat` 对上游 `editor.html` 只有 §B6.1 那 6 处、几十行，不出现"整个文件重写"。

### B11.3 板卡真机（需现场安全确认后再动）

16. `MAPS_IO=ssh`，上位机起后端 → `/api/mapeditor/io/test` 返回成功与耗时；
17. 上位机浏览器改一笔 → 保存 → 板卡上 `ls -l /home/sunrise/Robot/ros2_car/maps/` 复核，字节数与上位机一致；
18. 板卡上 `~/tools/nav_screen.sh nav my_map_edited` 起导航 → 车能按改后的图规划（**这一步才是像素修图的意义所在**）；
19. 拔掉 Tailscale/关机 → 页面仍能打开、显示缓存并标注「离线」；恢复后保存成功。

---

## B十二、与既有规格的关系（要做回填）

> **回填进度（2026-09-14 整理）**：A 篇 §2.2、A 篇 §三 已在原文就地标注，其余（A 篇 §5.1 排除 `.backup/`、A 篇 §六 新资源登记、§〇 口径台账）已集中记录在 A 篇新增的 **§〇 口径变更与加装项**；`AGENTS.md` 的「已知坑」与运行位置口径已补。**尚待**：A 篇 §十一 任务表补加装项引用、`ros2_car/建图与导航操作手册.md` 的「改完必须重启导航」一节、`2026-08-27` 规格的 vendor 说明。

| 既有文档 | 回填内容 |
|---|---|
| A 篇 §2.2 | 「像素级修图：本轮明确不做」→ 改为「**已由本文 B 篇实现**」，并链过去 |
| A 篇 §5.1 | `GET /api/map/list` 需排除 `maps/.backup/`；补 `POST /api/map/{name}/save` 与 `/api/mapeditor/io` 三条；**另：地图变"三件套"**（`.pgm`+`.yaml`+`.tags.json`），`list`/`rename`/`copy`/`delete` 都要连带处理标记文件与残缺态（A 篇 §〇 第 7 条） |
| A 篇 §三 | 原文「地图文件真相在板卡…后端在板卡上跑时自然指向正确目录；Windows 端用仓库里的副本开发」→ 改为「**后端主跑在 PC 上**（用户 2026-09-14：「还是把地图工作放到PC上吧，因为板卡性能不是很好」），地图文件真相仍在板卡、经 `MapStore(ssh)` 读写；仓库里的 `ros2_car/maps/` 降级为离线样本，不再当作开发期真相」 |
| A 篇 §六 | 登记 `public/pixel-editor.html`、`public/pixel-netio.js`、`public/vendor/` 三处新资源与 `/mapeditor/pixel-editor.html` 路径 |
| A 篇 §十一（任务表） | 新增 A 篇之外的加装任务（本文 §B十三），并注明「独立于 A 篇其余任务，可先落地」 |
| `2026-08-27-frontend-multi-end-design.md` | 无需改动（`mapeditor` 包已登记）；仅补一句「含 vendor 静态资源，`pnpm build` 直接拷贝 `public/`」 |
| `ros2_car/建图与导航操作手册.md` | 补一节「**改完地图必须重启导航才生效**」+ 在浏览器里改图的入口与备份位置 |
| `AGENTS.md` | ①「已知坑」补一条：地图像素修图在 `/mapeditor/pixel-editor.html`，变更前自动备份到 `maps/.backup/`；②「快速上手」补一句后端运行位置口径：**默认跑在 PC 上**，地图文件在板卡、经 `MAPS_IO=ssh` 读写 |

---

## B十三、任务分解（粗粒度，供实现计划展开）

**阶段一 · `ssh` 主通路（PC 上跑后端 —— 用户 2026-09-14 定的主形态）**

> 任务 1~4 与板卡解耦：`SshMapStore` 先对着 `127.0.0.1` 或假 transport 跑通，**不等板卡**；任务 5~9 是纯前端与本地落盘逻辑。也就是说**除了任务 11~12 的真机验收，整条链路都能在板卡不可达的情况下开发和验证完**。

| # | 任务 | 依赖 |
|---|---|---|
| 1 | `LLM/mapstore.py`：`MapStore` 协议 + `LocalMapStore`（纯 stdlib，先做它当契约基准与备用形态）；若一期未开工，附带 `GET /api/map/list`、`GET /api/map/{name}/download` 两条最小只读路由 | — |
| 2 | `SshMapStore`：paramiko 通道（可选依赖、try/except） | 1 |
| 3 | `SshMapStore`：`ssh.exe/scp.exe` 子进程通道（密钥认证，`MAPS_SSH_TRANSPORT=cli`） | 1 |
| 4 | 本地缓存与新鲜度判定 + 离线降级（§B4.2）；`GET /api/mapeditor/io`、`POST /api/mapeditor/io/test` | 2、3 |
| 5 | 自托管 5 个 CDN 资源到 `public/vendor/`（含 Font Awesome `fonts/` 与上游 MIT LICENSE）；核对离线可用 | — |
| 6 | **复制上游 `editor.html` → `public/pixel-editor.html`（转 LF）**，按 §B6.1 改 6 处 | 5 |
| 7 | `public/pixel-netio.js`：读通道注入 + 写通道覆写 + 状态条（显示 IO 模式 / 离线标注 / 保存目标） | 4、6 |
| 8 | `POST /api/map/{name}/save`：白名单、体积、yaml 白名单校验、备份、原子写、审计，**含标记随行（第 6 步：复制 `.tags.json` + 指纹比对 + 刷缓存）** | 4 |
| 9 | `frontend/packages/mapeditor` 骨架（vite、base、proxy、`dev:mapeditor`、`server.py` 挂载） | — |
| 10 | 测试 §B11.1 的 1~9 + 浏览器验收 §B11.2（用 `MAPS_IO=local` 绕开板卡） | 1~9 |

**阶段二 · 板卡真机与备用形态**

| # | 任务 | 依赖 |
|---|---|---|
| 11 | 板卡真机验收 §B11.3（**板卡可达是前置，当前实测不通**，见 §B十四） | 10 |
| 12 | 备用形态在板卡上跑一次回归（`MAPS_IO=local`），确认与主形态行为一致 | 11 |

**阶段三 · 收尾**

| # | 任务 | 依赖 |
|---|---|---|
| 13 | 文档回填（§B十二） | 全部 |
| 14 | `requirement.txt` 增补 `paramiko`（**可选依赖，注释说明可缺**） | 2 |

---

## B十四、动手前的前置条件（不解决则这次加装收益有限）

1. **上游副本必须在位**：`D:\_project\Robot\ROS-SLAM-Map-Editor`（§B0.2）。若缺失，从上游 clone 到定版 `646104e` 再开工 —— 换版本会让 §B6.1 的 6 处改动对应关系失效。
2. **板卡可达（主形态的第一前置）**：`ssh sunrise@100.65.82.93` 与 Tailscale 必须通。**本文档编写时实测失败** —— `ssh: connect to host 100.65.82.93 port 22: Connection timed out`（板卡未开机或 Tailscale 未登录）。主形态的整个 IO 都压在这条上，**真机验收前必须恢复**；开发期可用 `MAPS_IO=local` + `MAPS_DIR` 指向仓库副本先行推进（任务 1~10 不受影响）。
3. **ssh 认证方式要先定**：`cli` 通道（Windows 自带 `ssh.exe`/`scp.exe`，零依赖）**只支持密钥** —— Windows 没有 `sshpass`，明文密码无法自动化；要用它，需先给 PC 配一次公钥到板卡 `~/.ssh/authorized_keys`。paramiko 通道则可以吃密码（`MAPS_SSH_PASSWORD` 从 `.env` 读，不入 git）。
4. **重扫一张地图**（A 篇同款前置）：现有三张图未知率 60.7%~85.1%，"没扫到"占了绝大多数。像素修图能擦噪点、补漏墙、把**小片** unknown 涂成 free，但没法凭空扫出一栋楼。**先重扫，再修图。**
5. **`ros2_car/maps/` 里有脏文件**（`my_map2`/`my_map3` 未入 git）：本加装项的 `list()` 必须容错，且 `.backup/` 要在列表里排除。

---

## 附录（B 篇）· 上游归属与改动清单

> **上游工程的本地路径、定版 commit、目录清单、四条使用约定与能力对账表见 §B〇。** 本节只留归属、引用要求与改动清单。

- **上游**：GyroPalm/ROS-SLAM-Map-Editor，作者 Dominick Lee（GyroPalm, LLC），**MIT License**，2025。
  - 引用格式（上游 README 要求）：Lee, Dominick. (2025). *ROS SLAM Map Editor* [Computer software]. GyroPalm, LLC. <https://github.com/GyroPalm/ROS-SLAM-Map-Editor>
  - 上游 `LICENSE` 原文必须落到 `public/vendor/ROS-SLAM-Map-Editor.LICENSE`，并在 `pixel-netio.js` 头部注明「本页为改造自上述项目的衍生作品」。
  - **只改 6 处**（§B6.1）：5 条 CDN URL + 1 行补丁 `<script>`。若将来同步上游，按此 6 处重做即可，其余字节保持原样以便逐字节 diff。
- 备选（本轮不用，留档）：[vdovetzi/oge](https://github.com/vdovetzi/oge)（Python 纯 stdlib + Tk 桌面版，MIT，约 26 KB，含单测）—— 若将来想要一个**不依赖浏览器的命令行修图工具**，它的 PGM/YAML 读写（`oge/model.py`）是现成参考；[Tony-tpc/map_edit](https://github.com/Tony-tpc/map_edit)（RViz2 插件，无 license，需要 ROS 环境）。

---

# 实现台账与偏差（2026-09-14 落地）

> **本节性质：** 本文档正文（A 篇 §〇～§十二、B 篇 §B〇～§B十四）是**设计意图**，本节是**实际落地事实**。凡与正文冲突，以本节为准。
> **事实来源：** 本节所有条目均逐条核对过 `LLM/{mapstore,maptags,mapserver,roslink,locator,conf,db,server}.py`、`frontend/packages/mapeditor/**`、`ros2_car` 与 `tests/test_mapeditor.py` 的**实际代码**，不是照抄正文的计划。

## D1. 后端模块（实际文件与职责）

| 文件 | 实际内容 |
|---|---|
| `LLM/mapstore.py`（新，940 行） | `MapStoreError`；`normalize_name()`/`check_name()`（白名单）；`MapCache`（`DATA_DIR/mapcache/`，键 = `sha1(io_mode+root+name+ext)`，值 `{data(base64), mtime, size, at}`）；`MapStore(Protocol)`；`LocalMapStore`（纯 stdlib，`root = conf.MAPS_DIR`）；`SshMapStore`（`_ParamikoTransport` 优先 / `_CliTransport` 退回，`MAPS_SSH_TRANSPORT=auto\|paramiko\|cli`）；`get_store()`（单例，配置签名变了自动重建）/`reset_store()`/`io_status()`/`io_test()`；`EXTS = ("yaml","pgm","tags")` |
| `LLM/maptags.py`（新，651 行） | `<图名>.tags.json` 的 `resolve()`/`save()`（**唯一落盘出口**）/`replace_all()`；`sync_map()`（**单向** 文件→SQLite，`mtime+size+sha1` 幂等）/`_ensure_fresh()`；`fingerprint_check()`；`upsert_destination()`/`delete_destination()`/`upsert_zone()`/`delete_zone()`/`get_destination()`/`get_zone()`/`get_destinations()`/`get_zones()`；`learn_here()`；`record_room_polygon()`（16 边形近似圆，**无 HTTP 入口**）；`reindex()`/`reindex_all()`；`tags_path()`/`file_stat()`/`file_mtime_iso()` |
| `LLM/mapserver.py`（新，581 行） | `parse_yaml_flat()`/`meta_from_yaml()`/`update_yaml_image()`；`PGM` + `parse_pgm()`（P2/P5）+ `encode_pgm()`；`encode_gray_png()`（`zlib`+`struct` 手写，8bit 灰度）；`read_meta()`/`read_pgm()`/`map_info()`/`image_png()`（缓存键 `path+mtime+size`）；`occupancy()`/`classify_counts()`/`classify_pixel()`；`meters_to_pixel()`/`pixel_to_meters()`/`pixel_at()`；`_distance_transform()`/`_clearance_grid()`；`validate_point()` |
| `LLM/roslink.py`（新） | `available()`/`status()`/`subscribe()`/`drain()`/`latest_pose()`/`latest_map()`/`call_service()`/`close()`/`reset_for_test()`；**websocket-client 是可选依赖**，缺失时整层降级 |
| `LLM/locator.py`（新，240 行） | `set_pose_for_test()`/`set_map_for_test()`/`clear_injection()`；`available()`/`get_pose()`/`pose_payload()`；`_fingerprint()`/`_topic_map_meta()`/`current_map()`/`status()` |
| `LLM/db.py`（改） | 新增三张**只读索引缓存**表：`map_tags_manifest`（主键 `map_name`）、`destinations`、`zones`；访问函数 `get_map_tags_manifest()`/`replace_map_tags()`/`drop_map_tags()`/`clear_all_map_tags()`/`list_destinations()`/`list_zones()`/`count_map_tags()`；另有 `_migrate_map_tags_cache()` 把旧库的 `uid` 单列主键迁成 `(map_name, uid)` 复合主键（幂等，每次 `init_db()` 都跑，失败只打 WARN 不拖垮启动） |
| `LLM/conf.py`（改） | `MAPS_IO`（默认 `"ssh"`）/`MAPS_DIR`/`MAPS_CACHE_DIR`/`MAPS_BACKUP_DIRNAME`/`MAPS_SSH_*`（HOST/USER/PORT/ROOT/KEY/PASSWORD/TRANSPORT/TIMEOUT）/`MAPS_MAX_PGM_BYTES`/`MAPS_MAX_YAML_BYTES`/`MAPS_MAX_BODY_BYTES`/`MAPS_BACKUP_KEEP`/`MAP_RE_NAME_RE`/`MAP_RE_NAME_BANNED`/`ROSBRIDGE_*`；`DEFAULT_SETTINGS` 新增 `current_map="my_map"`/`map_boundary_margin_m=0.3`/`map_topic_fingerprint_enabled=True`/`mapeditor_auto_switch_map=False` |
| `LLM/server.py`（改） | 地图编辑器路由全部落在 `# 地图编辑器（第三个前端 /mapeditor）` 注释块之后（`server.py:843` 起）；`lifespan` 只加一行 `audit.log("map_io_change", mode=…, root=…)`，**未新增任何启动步骤**；静态挂载新增 `_MAPEDITOR_DIST` 与 `/mapeditor` |

## D2. 与本文档**不一致**的地方（逐条，按本文档正文顺序）

| # | 本文档写法 | **实际实现** |
|---|---|---|
| **D2-1** | §5.2：`POST /api/map/{name}/tags/reindex-all` | **实际路径是 `POST /api/map/reindex-all/tags`**。刻意避开 `/api/map/{name}/...` 前缀，免得和 `{name}` 参数路由抢匹配（代码 docstring 里留了这句原因）。 |
| **D2-2** | §5.2 未提路由定义顺序 | `POST /api/destinations/validate` 在 `server.py` 里**必须定义在** `POST /api/destinations/{uid}` **之前** —— 否则 FastAPI 会先匹配 `{uid}`，把 `"validate"` 当成一个地点 uid（**实测踩过**：报"地点不存在：validate"）。代码里已就地注释。**改动这些路由顺序时务必保持 validate 在前。** |
| **D2-3** | §5.1：`GET /api/map/{name}/download?file=yaml\|pgm` | **实际允许 `file=yaml\|pgm\|tags`**（默认 `yaml`）。`tags` 给的是 `application/json` + `<名>.tags.json` 文件名。`_MAP_EXTS = ("yaml","pgm","tags")` 同时被 `download` 与 `DELETE /api/map/{name}` 复用。 |
| **D2-4** | §4.2：`destinations.uid TEXT PRIMARY KEY` / `zones.uid TEXT PRIMARY KEY`（单列） | **实际是复合主键 `PRIMARY KEY (map_name, uid)`**（两表都是）。原因：**uid 只在单张图内稳定**——三张图各有自己的 `d1`，单列主键会让 `my_map2` 的 `d1` 覆盖 `my_map` 的 `d1`，刷新缓存必报 `UNIQUE constraint failed: destinations.uid`。`UNIQUE(map_name, name)` 不变。 |
| **D2-5** | §5.1：`POST /api/map/{name}/copy` **不复制地点/区域**（"新图视为空白"） | **已作废，实际「标记随行」**：复制时连 `.tags.json` 一起拷（`MapStore.copy()` 连带），再对**新图**做一次指纹比对 —— 一致才保留并把 `map` 字段改成新名；**不一致就删掉新图的 tags 并写 `warnings[]`**（绝不静默错配）。响应的 `tags_copied` 是布尔值。此与 §〇 第 6/7 条一致；**§5.1 旧表那行文字已作废**。 |
| **D2-6** | §7.4：未知率按 `occupied_thresh`/`free_thresh`/`negate` 三分 | 口径确认：`occupancy(pixel, maxval, negate) = pixel/maxval if negate else 1 - pixel/maxval` —— **默认 `negate=0` 时黑＝占用**（与 ROS `map_server` 反色语义一致）。阈值判据是 `occ > occupied_thresh`（默认 0.65）与 `occ < free_thresh`（默认 0.25），其余计 unknown。 |
| **D2-7** | §5.3 只写 `/api/map/current` 一个识别入口 | 实际**多了一个汇总接口** `GET /api/mapeditor/status` → `{ok, io, locator}`（`locator.status()` 里含 `pose`/`rosbridge`/`current_map`）。供编辑器顶部状态条一次性取齐。 |
| **D2-8** | §5.1：`GET /api/map/{name}/image.png` 只写"按 mtime+size 缓存" | 实际**多了一个响应头**：断连降级吃缓存时返回 **`X-Map-Stale: 1`**（有缓存时间则另带 `X-Map-Cached-At`）；`/download` 同样带这两个头。 |
| **D2-9** | §5.4：假位姿注入只提"`locator` 支持注入" | 实际**有 HTTP 入口**：`POST /api/mapeditor/pose/inject`（体 `{x,y,yaw,width,height,resolution,origin}`）。**只传 `x` 也行**；**两个都不传即清空注入**（返回 `{ok, cleared: true}`）。无 ROS 环境开发/测试全靠它。 |
| **D2-10** | §5.3：靠"人工声明"以外的自动识别；§5.3 未提开关 | 实际有设置项 `map_topic_fingerprint_enabled`（默认 `True`）。关掉时 `current_map()` 直接返回 `source: "unknown"` + 说明文案，**不报错**。另 §4.3 之外还多一个 `mapeditor_auto_switch_map`（默认 `False`，打开编辑器时是否自动跳到识别出的那张图）。 |
| **D2-11** | §B7.1：白名单 `^[A-Za-z0-9_-]{1,64}$` 且不含 `keepout` | 实现一致，但**额外做了名字归一**：`normalize_name()` 会先剥掉 `.tags.json`/`.yaml`/`.yml`/`.pgm` 后缀再校验（所以 `my_map.pgm` 这种输入能被接受并归一为 `my_map`）。`keepout` 检查在归一后的小写名上做。 |
| **D2-12** | §B7.3：`pgm` 上限按"解码后"算 | 实际口径是**回传的 pgm 字节数**：`len(base64.b64decode(pgm_b64)) > MAPS_MAX_PGM_BYTES` → 413。`yaml_text` 同样按 `len(yaml_text.encode("utf-8")) > MAPS_MAX_YAML_BYTES` 判。`MAPS_MAX_BODY_BYTES`（16 MB）**未在 save 路由里显式校验**（依赖 ASGI/服务器层），此项**未核实**是否有中间件兜。 |
| **D2-13** | §B5.2 执行顺序第 1 步"白名单校验"排最前 | 实际 `map_save()` 的**执行顺序是：读名字 → 体积/base64/魔数校验 → 名字与 mode 白名单 → 覆盖须 `confirm` → yaml 白名单比对 → 备份 → 标记随行 → 写 pgm → 写 yaml → 刷缓存 → 审计**。代码注释里写明了"白名单必须最早做"的意图，但实际排在了 base64/体积之后（先解 base64 才知道字节数）。**新增写入路径时别照抄正文的顺序描述。** |
| **D2-14** | §B5.2 第 5 步备份文件名统一 `<name>.<YYYYmmdd-HHMMSS>.{pgm,yaml}` | 实现一致；但**清理只认扩展名为 `pgm`/`yaml` 的成组文件**（`_prune_backups()` 要求 `parts[-1] in ("pgm","yaml")`）——否则 `<name>.notes.txt` 这类旁路文件会被算成一组、**多删一组备份**（代码注释记录"实测踩过"）。 |
| **D2-15** | §B6.3：`server.py` 挂 `/mapeditor`（措辞隐含"dist 必须有"） | 实际**有导出回退**：`_MAPEDITOR_DIST` 存在就挂 dist，**否则回退挂 `packages/mapeditor/public/`** —— 这样 `pixel-editor.html` 不经 Vite 打包、后端直跑时也能打开。`_serve_dist()` 用 `StaticFiles(html=True)`。 |
| **D2-16** | §B4.2：缓存键 `sha1(io_mode + root + name + ext)` | 一致。补充：`stat()` 明确报"远程文件不存在"时**不回退缓存**（否则删掉的图还能读出来）；只有**连接类失败**才降级吃缓存。缓存写失败（只读 FS 等）退化为 no-op，不影响主流程。 |
| **D2-17** | §B3.3 / §B6.1：产物落 `public/` | 一致。实测凭证：`git diff --no-index ROS-SLAM-Map-Editor/editor.html frontend/packages/mapeditor/public/pixel-editor.html` → **7 insertions(+), 5 deletions(-)**，即 6 处改动（5 条 CDN URL + 1 行补丁注释/`<script>`）；**LF 副本 43178 B vs 上游 git blob 43165 B（`git cat-file -s HEAD:editor.html`）**，工作区副本是 CRLF 44280 B（不要看它）。§B0.2 的"只改 6 处、不得整文件重写"**已核对通过**。 |
| **D2-18** | §B6.3：前端接线（`dev:mapeditor`、base、5175、proxy） | 一致（`frontend/package.json` 有 `dev:mapeditor`；`vite.config.ts` 为 `base:"/mapeditor/"`、`server.port: 5175`、`proxy["/api"] = VITE_API_TARGET \|\| "http://127.0.0.1:8000"`）。另：`mapeditor/package.json` 的 `build` 脚本是 **`node scripts/build.mjs`** 而不是裸 `vite build`，见文末「未做/待验」第 1 条。 |

## D3. 实际响应字段（覆盖正文里与实现不符的字段描述）

| 接口 | 实际响应字段 |
|---|---|
| `POST /api/map/{name}/save`（B 篇 §B5.2） | `{ok, wrote[], backup[], target, tags_copied, warnings[], restart_hint, note}` —— **没有 `rejected[]`**。`wrote` 是 `["<目标>.pgm", "<目标>.yaml"]`，标记随行成功才追加 `"<目标>.tags.json"`；`restart_hint` 形如 `~/tools/nav_screen.sh nav <目标名>`；`note` 是"改完必须重启导航才生效"的说明。拒绝路径一律 `4xx` + `{ok:false, error, ...}`（如 `diffs[]`、`need_confirm`、`affects`）。 |
| `POST /api/map/{name}/rename` | `{ok, old, new}`（连带重命名 `.pgm`/`.yaml`/`.tags.json`，改 yaml 的 `image:` 与 tags 的 `map:`）。 |
| `POST /api/map/{name}/copy` | `{ok, src, dst, tags_copied, warnings[]}`。 |
| `POST /api/map/{name}/meta` | `{ok, changed[], backup[], affected, tags_updated, meta}`；未确认且有标记时 `409 + {ok:false, error, need_confirm:true, affects}`。 |
| `DELETE /api/map/{name}?confirm=` | `{ok, removed[], affected}`；有标记未确认时 `409 + need_confirm`。 |
| `GET /api/map/{name}/meta` | `{ok, meta, counts, tags_exists, tags_warnings[], tags_path, fingerprint, tags_mtime}`。 |
| `GET /api/map/{name}/tags` | `{ok, map, exists, tags, warnings[], stale, cached_at, error, fingerprint, path}`。 |
| `GET /api/map/list` | `{ok, maps[], mode, root, current_map}`；每项含 `name/has_pgm/has_yaml/mtime` + `counts{destinations,zones}` + `status`（`ok` 或"残缺：…"）+ `meta_ok/problems[]` + `width/height/resolution/origin/unknown_ratio` + `stale/cached_at` + `current`。Store 不可用时 `{ok:true, status:"unavailable", reason, maps:[], …}`。 |
| `POST /api/destinations` / `POST /api/destinations/{uid}` | `{ok, uid, warnings[], save}`。 |
| `POST /api/destinations/learn` | `{ok, uid, warnings[], pose, save}`；位姿不可用时按"写操作"返回 `{ok:false, error}`（提示可改为在图上点选）。 |
| `POST /api/zones` / `POST /api/zones/{uid}` | `{ok, uid}`；`DELETE /api/zones/{uid}?map=` → `{ok, uid, orphaned[]}`（父区域删掉后列出被孤立的子区域）。 |
| `PUT /api/map/{name}/tags` | `{ok, map, bytes, stat, cache{destinations,zones,warnings[]}, warnings[]}`。 |
| `POST /api/map/{name}/tags/reindex` | `{ok, map, exists, rebuilt, destinations, zones, warnings[], fingerprint}`。 |
| `POST /api/map/reindex-all/tags` | `{ok, results[]}`，单项同 `sync_map()` 的返回（单图失败只记 `{map, error}`，不整体失败）。 |
| `GET /api/mapeditor/io` | `{ok, mode, root, available, reason, transport, stale, cached_at, host}`（带 `?name=` 时另给 `cached_at_text`）。 |
| `POST /api/mapeditor/io/test` | `{ok, available, reason, elapsed_ms, count}`。 |
| `GET /api/robot/pose` | 正常 `{ok:true, status:"ok", source, x, y, yaw, suspect, note}`；降级 `{ok:true, status:"unavailable", reason, source:null, x:null, y:null, yaw:null}`（**`ok` 保持 True**，遵 AGENTS「系统稳健性」）。`suspect=true` 表示位姿恒在原点（AMCL 初始位姿可能未对齐）。 |
| `GET /api/map/current` | `{ok, source:"map_topic"\|"unknown", name, detail}`（多项命中时另带 `ambiguous[]`；可识别时另带 `topic{}`）。 |
| `GET /api/mapeditor/status` | `{ok, io{…}, locator{pose, rosbridge, available, reason, current_map}}`。 |

## D4. 审计事件名（实际落盘的）

- `map_io_change`（lifespan 启动时记一次生效的 IO 模式与 root）
- `map_change`（`action=list / meta_update / rename / copy / delete / destination_delete / destination_learn / zone_delete / tags_reindex / tags_reindex_all / tags_save / tags_replace / meta_fingerprint_confirm / …`）
- `map_edit_save`（像素修图保存成功）
- `map_edit_reject`（拒绝：名字非法 / yaml 字段不一致 / pgm 写失败 / 备份失败 等，带 `stage` 字段）

> 正文 §八 写的"新增事件名 `map_change`/`zone_change`"里 **`zone_change` 实际未采用** —— 区域改动统一记在 `map_change` 下、用 `action` 区分。

## D5. 测试实际状态（2026-09-14 实跑）

- `tests/test_mapeditor.py` **单跑 → 46 passed**（`D:\_project\Robot\.venv\Scripts\python.exe -m pytest tests/test_mapeditor.py -q`）。覆盖：LocalStore 契约、`list()` 排除 `.backup/`、名字白名单"不触碰任何文件"、不可用降级、P5/P2 解析与坏魔数、PNG 结构、未知率口径、yaml `image:` 保留、往返换算与 y 翻转、`validate_point` 警告、tags 空态/CRUD/外部改文件触发重建/缓存可丢弃重建/旧库主键迁移/指纹变更检测/schema 归一化/`learn_here`/`replace_all`/备份保留 10 组、位姿注入降级、指纹识别当前地图、以及全部 HTTP 路由（含 `save` 四类拒绝、标记随行、审计落盘）。
- **全量 `pytest tests -q` → 91 passed / 4 failed**，4 个红态**都不在 `test_mapeditor.py`**、且属既有基线漂移（**未修，非本轮范围**）：`tests/test_modules_status.py::test_modules_status_shape`（模块集合断言）、`tests/test_unlock_switch.py` 3 例（`VoiceWorker.__init__() got an unexpected keyword argument 'chat_fn'`，测试与 `voice/worker.py` 签名漂移）。
- 观察记录（供后人参考）：单独跑 mapeditor 用例与"mapeditor + 其他文件"两种顺序都复现过 **46 passed**；有一次按 pytest 默认目录顺序全量跑时出现过 `test_mapeditor.py::test_old_cache_schema_is_migrated` 报 `IntegrityError`，**该用例单跑与两种顺序组合均通过**，未定性为真实缺陷 —— 若再次遇到，按"用例间 sqlite 全局状态污染"方向查。

## D6. 前端实际文件（`frontend/packages/mapeditor/`）

```
package.json          name=mapeditor，dev=vite，build=node scripts/build.mjs，verify:build
vite.config.ts        base "/mapeditor/"、port 5175、proxy /api、shared alias 到源码
index.html  tsconfig.json
src/main.ts  src/App.vue
src/pages/{MapCanvas,PlacePanel,ZonePanel,MapFiles}.vue
src/lib/{coords,colorize,api,types}.ts
public/pixel-editor.html      ← 上游副本（LF，相对上游 git blob 只改 6 处）43178 B
public/pixel-netio.js         ← 接线层（读注入 + 写截获 + 状态条）
public/vendor/                ← jquery-3.4.1.min.js / js-yaml-4.1.0.min.js /
                                bootstrap-4.4.1.min.css + .bundle.min.js /
                                font-awesome-4.7.0/{css,fonts} / ROS-SLAM-Map-Editor.LICENSE
scripts/build.mjs  scripts/verify-build.mjs  scripts/esbuild-shim.mjs  scripts/child-process-stdio.mjs
dist/                         ← 已产出（index.html + assets/ + pixel-editor.html + pixel-netio.js + vendor/）
```

仓库根另有（**注意不在 `frontend/` 下**）：`scripts/vendor_mapeditor_assets.py`（幂等下载/落盘 5 个资源 + 上游 LICENSE，只用 stdlib）、`scripts/e2e_smoke.mjs`（无头浏览器 CDP 冒烟，见「未做/待验」）。

## D7. 未做 / 待验（**不要当成已完成**）

1. **真实开发机上的 `vite build` 复验 —— 未做。** 本开发沙箱（DSH workspace-write）里 `vite build` 跑不起来：① 浏览器进程无法启动（headless Edge 直接被沙箱杀掉），② Node 子进程不能用管道 stdio（`spawn EPERM`，esbuild 的服务进程起不来）。为此 `packages/mapeditor/scripts/build.mjs` 用**文件句柄 stdio** 起子进程把真正的 `vite build` 输出接出来（正常机器上与 admin 的 `vite build` 完全等价）；撞上 `spawn EPERM` 时**不静默兜底**，而是明确失败并提示改用 `pnpm --filter mapeditor build:sandbox`（= `scripts/verify-build.mjs`，同一个 `vite.config.ts` 的等价构建，实测产物与 `vite build` 逐字节相同）。**`dist/` 里的产物是用 `build:sandbox` 产出的，尚未在真实开发机上用 `vite build` 复验过。** 正常机器上的正确做法：`cd frontend && pnpm install && pnpm --filter mapeditor build`。
2. **真实浏览器联调 —— 未做。** `scripts/e2e_smoke.mjs` 在沙箱里跑不起来（同一条浏览器限制）；**在正常机器上可用**，用法见脚本头注释。
3. **板卡真机验收 —— 未做**（§九 7~10、§B11.3 16~19 全部未跑）。实测 `ssh sunrise@100.65.82.93` **连接超时**，`MAPS_IO=ssh` 的真实读写也**未验**。
4. **§十二 / §B十四 的前置条件依然成立**：① 重扫一张地图（三张图未知率 60.7%~85.1%）；② 对齐 AMCL 初始位姿（实测恒在原点，`pose_payload()` 会用 `suspect=true` 标出来）；③ 板卡可达。
5. **未核实项**（明确标注，勿当事实）：`MAPS_MAX_BODY_BYTES`（16 MB）在 `save` 路径上是否有中间件兜底；`pnpm build` 对 `mapeditor` 在真实 Node/pnpm 版本下的行为（本机只验到脚本与 `dist/` 存在）。
6. **板卡上那份操作手册副本**（`/home/sunrise/Robot/ros2_car/建图与导航操作手册.md`）**未同步** —— 本轮只改了仓库里这份，板卡 ssh 不通，需另行同步。
