# 地图编辑器按需启动（独立进程）设计

> **目的：** 让「地图编辑器」（像素修图 + 划线/标点）**默认不跑**：`start_UI.py` 只启陪护用的后端 + 车载对话端(kiosk) + 管理端(admin)；要用编辑器时在 admin 里按一个按钮把它拉起来，在编辑器里按「保存并退出」把它停掉并关窗。
> **状态：** 待实现（本文件 = 设计，实现台账落地后追加到文末）。
> **日期：** 2026-09-15
> **用户原话（2026-09-15）：** 「我想写一个一键启动的脚本 start.py，然后把 start_UI.py 改成纯启动 UI 前端（车载对话 + admin 后台）和其后端的代码。地图编辑器的像素编辑 + 划线一开始不启动，放在后台里面单独放一个按钮，按下后才启动这两个功能，按下保存并退出后关闭。这样以节约资源」
> **本轮范围：** 只做 `start_UI.py` 这条线（含被它启动的后端）。**新 `start.py`（前后端 + ROS 全量一键）本轮不做。**
> **相关文档：** `2026-09-14-map-editor-design.md`（编辑器本体，本文不改它的口径，只改「它在哪跑、什么时候跑」）；`2026-09-14-layered-user-roles-design.md`（admin 口令门 / 病房，本文复用其角色判定）。

---

## 〇、开工前必须先知道的三条事实（2026-09-15 查码核实）

1. **编辑器从来不在启动清单里。** 现在的 `start_UI.py`（由 `start.py` 改名而来）启动的只有：后端 uvicorn(8000) + admin + kiosk。地图编辑器是**后端顺手挂**的一个静态目录 `/mapeditor` 加一组地图路由（`server.py` 约 900 行）。
2. **地图那套后端没有任何后台线程、没有开机就连的常驻连接。** `mapstore` / `maptags` / `mapserver` / `roslink` 全部"被请求时才动"（rosbridge 30 秒内复用连接，闲了就断）。所以**「不启动编辑器」在当前架构下省不下内存或端口**。
3. **唯一一直占资源的是 `session.tick()`**（每秒一次、命中 30 秒缓存的地图指纹识别 + 读 rosbridge 位姿）——那是**病房位置自动切换**，陪护功能本身，**与编辑器无关**。

→ 结论：要做到"真的分开"，只能把编辑器**拆成独立进程**（用户 2026-09-15 选择）。

---

## 一、决策（2026-09-15 用户拍板）

| # | 决策 | 说明 |
|---|---|---|
| **D1** | **形态 = 独立进程**，不是开关位、也不是"只开个新窗口" | 编辑器 = 单独 uvicorn 进程（默认端口 **8010**），由主后端按需拉起、按需杀掉 |
| **D2** | **拆分边界 = B1：只搬编辑器** | 主后端保留「病房位置自动切换」所需的最小依赖（`locator` → 读位姿 / 认当前地图）与「记录当前房间为病房区域」入口（`maptags`）；**编辑器专属**的路由、模型、静态页全部搬进独立进程 |
| **D3** | **退出行为 = 谁按谁退** | 主编辑器与像素修图页**各有一个**「保存并退出」= 保存 → 停服务 → 关窗；主编辑器顶部另给一个「仅关窗（保留服务）」，防"开了两个窗口被误伤" |
| **D4** | **停服务由编辑器自己完成**（不是让页面去调 8000） | 8010 自带 `POST /api/mapeditor/service/stop`（自杀），故编辑器页面**只需要认识自己这个源**，一行 URL 都不用改（前端全部用相对路径） |

### 1.1 诚实说明：这一拆分到底省下了什么

**省不下的：** 主后端仍会经 `locator` / `maptags` 间接 import `mapstore` / `mapserver`（病房自动切换与「记录当前房间为病房区域」要用），这部分内存照旧。

**真正省下的（也是本设计的意义）：**

1. **编辑器进程可以随手杀掉，内存与缓存立即回收**——地图位图/图片缓存（`MapCache`、`/image.png` 编码）主要就是编辑器请求带起来的，过去要回收只能重启**陪护后端**（把对话、语音、提醒一起打断）。
2. **故障与改动隔离**：编辑器 900 行路由崩了不再拖累陪护对话链路；改编辑器代码也不必重启陪护后端。
3. **接口面与端口面缩小**：8000 上不再有 `/mapeditor` 与那 30 多条地图路由。

---

## 二、架构

```
                    ┌────────────────────── 主后端（uvicorn :8000，常驻） ──────────────────────┐
start_UI.py ──────► │ LLM.server:app                                                          │
   ├─ kiosk  :8000  │   /api/chat /api/memories /api/reminders /api/voice/* /api/session/*     │
   ├─ admin  :8000  │   /api/wards*（含「记录当前房间为病房区域」，用 maptags）               │
   └─ 后端   :8000  │   /api/vision/*        ← 陪护链路，一律不经编辑器进程                    │
                    │   ★ 新增 /api/mapeditor/service{,/start,/stop}  ← 仅 admin，管子进程       │
                    └─────────────────────────────────────────────────────────────────────────┘
                                        │ subprocess.Popen / terminate（mapctl.py）
                                        ▼
                    ┌──────────── 地图编辑器服务（uvicorn :8010，按需） ────────────┐
   admin「启动」按钮 │ LLM.mapeditor_server:app                                     │
   → window.open ──► │   /mapeditor/*（静态页 index.html + public/pixel-editor.html）│
                     │   /api/map/* /api/destinations* /api/zones* /api/robot/pose  │
                     │   /api/mapeditor/{status,io,io/test,pose/inject}             │
                     │   ★ 自带 /api/mapeditor/service{,/stop}（自杀）               │
                     └──────────────────────────────────────────────────────────────┘
```

- **端口**：`conf.MAP_EDITOR_PORT = 8010`（`--host 0.0.0.0`，与主后端同口径，便于从别的机器看 admin 时也能打开编辑器）。
- **admin 里拼的 URL**：`http://<location.hostname>:8010/mapeditor/`（**不要写 127.0.0.1**——从另一台机器看 admin 时那会指向它自己）。
- **两个进程共用一个 SQLite**（`LLM/data/brain.db`，WAL + 线程锁）：主后端读 `zones` 索引缓存做病房判定，编辑器写文件后刷缓存。既有 WAL 与锁兜底，不新增同步机制。

---

## 三、后端拆分（精确边界）

### 3.1 `LLM/mapapi.py`（新）——编辑器专属后端

把 `server.py` 里这些**原样搬过来**（含注释与红线说明），包装成 `router = APIRouter()`：

| 来源（`server.py` 行号，2026-09-15 HEAD `f8fd004`） | 内容 |
|---|---|
| `301–379` | pydantic 模型 `MapMetaIn` / `MapNameIn` / `DestinationIn` / `LearnIn` / `ZoneIn` / `ValidateIn` / `MapTagsIn` / `MapSaveIn` / `PoseInjectIn` |
| `1225–2112` | 段头注释、`from . import mapserver, maptags, mapstore, locator`、`_MAP_EXTS`、助手 `_store` / `_store_or_err` / `_err` / `_name_of` / `_tag_counts`，以及全部路由：`/api/map/sources*`、`/api/map/list`、`/api/map/current`、`/api/map/{name}/{meta,rename,copy,download,image.png,tags,tags/reindex,save}`、`/api/map/reindex-all/tags`、`/api/destinations*`、`/api/zones*`、`/api/robot/pose`、`/api/mapeditor/{status,io,io/test,pose/inject}`、`_same_value` |

另外新增（本模块内）：

- `mount_editor(app)`：把 `server.py:2181–2202` 的 `_mount_editor()` 判据**逐字搬来**（`dist/index.html` → `public/index.html` → 退一步只挂 `public/` 让 `pixel-editor.html` 可用，三种情形各自的启动日志也保留）。
- 常量 `MAPEDITOR_DIST` / `MAPEDITOR_PUBLIC` 从 `server.py:2168–2169` 搬到本模块。

**不搬（留在主后端）：** `/api/wards*`（含 `POST /api/wards/{uid}/zone`，它用 `maptags` 写标记）、`/api/profiles/{uid}/ward`、`/api/session/*`、`session.py`（病房自动切换）。

### 3.2 `LLM/mapeditor_server.py`（新）——独立进程入口

```
python -m uvicorn LLM.mapeditor_server:app --host 0.0.0.0 --port 8010
```

内容（薄壳，不含业务）：

1. `app = FastAPI(title="地图编辑器服务")` + CORS 全开（本机/局域网调试用，与主后端同口径）；
2. `app.include_router(mapapi.router)`；
3. `mapapi.mount_editor(app)`；
4. `GET /` → 302 到 `/mapeditor/`；
5. `GET /api/mapeditor/service` → `{"ok": True, "running": True, "pid": os.getpid(), "port": conf.MAP_EDITOR_PORT}`（本服务总由主后端以该端口拉起，此字段只作回显；主后端探活只看 HTTP 200）；
6. `POST /api/mapeditor/service/stop` → 返回 `{"ok": True, "message": "地图编辑器服务正在退出…"}`，随后走模块级 `_schedule_exit()`：它起一个 daemon 线程执行 `_delayed_exit()`（`sleep(0.5)` → `os._exit(0)`，**与 `server.py::_delayed_exit` 同款做法**——先把响应发出去再退，避免前端拿到空响应）。测**只**注入 `_schedule_exit`（断言"被调用了一次"），**绝不**让真实 `os._exit` 在 pytest 里跑起来。

> **降级要求（AGENTS「系统稳健性」）：** 本进程是"可选能力"，起不来不许影响主后端（主后端只用 `Popen` + HTTP 探活，失败就报错给 admin）。

### 3.3 `LLM/mapctl.py`（新）——主后端侧的进程管理

只依赖 `sys` / `subprocess` / `socket` / `urllib` / `os` / `time` + `conf` / `log` / `session` / `bus`。**绝不 import `mapapi`**（否则又回到同一个进程里）。

**状态机**：`none`（没起）→ `managed`（本进程拉起的，有 `Popen` 句柄）→ `external`（端口 8010 有服务但不是本进程拉起的，例如主后端被 `kill -9` 后留下的孤儿）。

**REST（挂在 8000，仅 admin；非 admin → 403）：**

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/api/mapeditor/service` | `{ok, running, source: none\|managed\|external, pid, port, uptime_s}`；`source=managed` 时用 `proc.poll()` 判活 |
| POST | `/api/mapeditor/service/start` | **幂等**：已在跑（managed/external）→ 直接返回现状；否则 `Popen([sys.executable, "-m", "uvicorn", "LLM.mapeditor_server:app", "--host", "0.0.0.0", "--port", str(conf.MAP_EDITOR_PORT)], cwd=BASE_DIR)`，输出透传到主后端终端（排障），随后轮询 8010 的 `GET /api/mapeditor/service` 直到就绪（≤ `conf.MAP_EDITOR_START_TIMEOUT`=20s）。超时/进程提前退出/端口被别的东西占用 → `{ok:false, error}`（附"看上方 uvicorn 日志"；端口占用时点明"改 `conf.MAP_EDITOR_PORT`"）+ 审计 |
| POST | `/api/mapeditor/service/stop` | ①有句柄 → `terminate()` → 等 5s → `kill()`；②无句柄但 8010 活着（external）→ 服务端 `urllib` 调它自己的 `POST /api/mapeditor/service/stop`，再等它消失 ≤5s；③都没起 → `{ok:true}`（幂等）。写审计 |

- 审计事件统一 `map_editor_service`，`action = start|stop|start_failed`，带 `pid` / `port` / `source`。
- **主后端退出收尾**：`lifespan` 的 `yield` 之后与 `POST /api/system/shutdown` 的停止序列里都调 `mapctl.stop()`（避免留下孤儿；`stop()` 自身幂等且吞异常）。
- **脚本侧**：`GET /api/mapeditor/service` 的实现里，判断"端口活但无句柄"用一次 0.5s 超时的 TCP 连接探测（与 `start_UI.py::port_in_use` 同口径）。

### 3.4 `LLM/server.py`（改）

- 删：地图模型段、地图路由段、`/mapeditor` 静态挂载（三处共约 900 行）。
- 加：`from . import mapctl` + `app.include_router(mapctl.router)`（2 行）+ `lifespan`/`shutdown` 里的 `mapctl.stop()`（2 行）。
- **保留**：`from . import locator`、`from . import maptags`（病房那两个入口要用）。即主后端**仍会**间接拿到 `mapstore` / `mapserver`——见 §1.1，别把这说成"完全不碰地图"。

### 3.5 `LLM/conf.py`（改）

```python
MAP_EDITOR_PORT = 8010            # 地图编辑器独立服务端口（按需启动，见 docs/.../2026-09-15-*）
MAP_EDITOR_START_TIMEOUT = 20.0   # 拉起编辑器服务的最长等待秒数
```

---

## 四、前端

### 4.1 admin：新页签「地图编辑器」（`frontend/packages/admin/src/pages/MapEditorPage.vue`，新）

- 位置：页签栏里排在「病房管理」之后（`App.vue` 的 `tabs` 数组插 `{ id: "mapeditor", label: "地图编辑器" }`，`<main>` 里加一条 `v-else-if`，并把文件头注释的「10 页签」改成 11）。
- 内容：
  - **状态卡**：未启动 / 运行中（PID / 端口 / 已运行时长 / `managed|external`）；进页面时拉一次 `GET /api/mapeditor/service`，之后每 5 秒轮询（页面切走即停）。
  - **主按钮「启动地图编辑器」**：`POST .../start` → 成功后 `window.open('http://' + location.hostname + ':' + port + '/mapeditor/', '_blank')`；失败显示后端 `error` 原文（例如"前端未构建：cd frontend && pnpm --filter mapeditor build"）。
  - **次按钮「停止服务」**：`POST .../stop`；用于"窗口被手动关掉，服务还留着"的兜底。
  - 说明文案：像素修图与划线/标点都在这里；在编辑器里点「保存并退出」会自动关服务；改完地图**必须重启导航**才生效（照抄既有提示）。
- `frontend/packages/admin/src/pages/WardsPage.vue`：两处指向 `/mapeditor/` 的 `<a href>` 删掉、换成**切页签**（主后端已不再挂 `/mapeditor`，硬链会 404）。admin 没有路由表（只有 `App.vue` 的 `active` ref），所以做法是 `WardsPage` 抛一个 `goto-mapeditor` 事件，`App.vue` 用 `<WardsPage @goto-mapeditor="active = 'mapeditor'" />` 接住切过去；文案保持"地图编辑器里画多边形/矩形"。

### 4.2 地图编辑器：两个「退出」按钮

- **`frontend/packages/mapeditor/src/App.vue`（顶部工具条）**：
  - `保存并退出`：弹一次确认（"退出后地图编辑器服务会停止，需要再点『启动』才能进来"）→ `POST /api/mapeditor/service/stop`（**同源 8010**）→ 成功后 `window.close()`；若还有未保存的像素改动，提示"像素修图请在其窗口点『保存并退出』"（主编辑器不掌握那个窗口的状态，不假装能替它保存）。
  - `仅关窗（保留服务）`：直接 `window.close()`，不停服务（给"开了两个窗口"兜底）。
  - 新增 `frontend/packages/mapeditor/src/lib/service.ts`：只有两件事——`stopService()`（打同源 `POST /api/mapeditor/service/stop`）与 `closeSelf()`（`window.close()`）。**不另封 `serviceStatus()`**：`App.vue` 的 `statusTick` 本来就在轮询 `/api/mapeditor/status`，`http === 0` 即"服务没了" → 顶部显示红条「地图编辑器服务已停止，请关闭本页」并停掉 2.5 秒轮询（省得刷错误）；再封一个状态函数只会成为没人用的死代码（YAGNI）。
- **`frontend/packages/mapeditor/public/pixel-netio.js`（工具栏第二行）**：
  - 新增 `保存并退出`：复用既有那条链路——挂一个"退出意图"标志，再触发 `manualSave()` → `saveToServer()`，在**既有成功回调 `renderSaveOk(data, mode)` 里**追加分支：只有保存成功才继续 `POST /api/mapeditor/service/stop` + `window.close()`。保存失败（含 409 需 `confirm`）则**清掉退出意图、不停不关**，照旧显示错误与处理按钮。
  - 既有「保存到服务器」按钮保持不变（不带退出意图）。
- **`window.close()` 的降级**：只对 `window.open()` 打开的窗口有效。手动新开标签页时关不掉 → 页面提示「服务已停止，请手动关闭本标签页」。
- `frontend/packages/mapeditor/scripts/test-startup-contract.mjs`：补断言——`App.vue` 里有「保存并退出」且调 `/api/mapeditor/service/stop`；有「仅关窗」；`lib/service.ts` 存在。

### 4.3 `start_UI.py`（改）

- 定位改成「**UI 前端（车载对话 + admin 后台）+ 后端**」，docstring 与启动日志同步改口径；不再提"构建产物由 FastAPI 静态托管 `/admin` `/kiosk` `/mapeditor`"里的 `/mapeditor`。
- 启动完成后打印一行提示：`地图编辑器按需启动：admin →「地图编辑器」页签 → 启动`。
- 不检查 mapeditor 产物（它不属于本启动器的职责）。
- `--dev` 仍然只起 admin / kiosk 两个 dev server（不起 mapeditor 的 :5175）。
- 其余（`.env` 检查、端口检查、健康检查、Ctrl+C 停全部子进程）**原样不动**；Ctrl+C 停掉主后端时，`lifespan` 收尾会把编辑器服务一起带走。

---

## 五、错误处理与降级

| 场景 | 行为 |
|---|---|
| 编辑器服务起不来（缺依赖 / 端口占用 / 产物缺失） | `start` 返回 `{ok:false, error}`，**主后端与陪护功能完全不受影响**；admin 显示红字原文 |
| 主后端被 `kill -9`，编辑器成孤儿 | 下次 `start` 探到 8010 活着 → 报 `source="external"`（**不重复拉起**）；「停止服务」走它自己的自停接口收掉 |
| 编辑器被手动关窗但没停服务 | admin 状态卡仍显示"运行中" + 「停止服务」按钮兜底 |
| `MAPS_IO=ssh` 且板卡不可达 | 与拆分前完全一致：编辑器读图/保存按其既有降级路径报错（本文不改） |
| 编辑器服务中途崩溃 | 编辑器页面 fetch 失败 → 顶部红条提示；admin 5 秒轮询也会反映"未启动" |

---

## 六、验收（可逐条打勾）

1. `python start_UI.py` 起完后：`http://127.0.0.1:8000/api/map/list` **404**；`http://127.0.0.1:8000/mapeditor/` **404**；8010 端口**无人监听**。
2. admin 出现「地图编辑器」页签，初始显示"未启动"；点「启动地图编辑器」→ 8010 起来、自动打开新窗口 `/mapeditor/`，页签显示 PID / 端口 / 运行时长。
3. 编辑器里能正常用：列图、看图（含缓存/断连提示）、标地点、画多边形/矩形区域、像素修图入口 → 与拆分前逐项一致。
4. 在像素修图页改几笔 → 「保存并退出」：保存成功、窗口关闭、8010 消失（`GET /api/mapeditor/service` → `running:false`）。
5. 在编辑器主界面点「保存并退出」：窗口关闭、服务停止；点「仅关窗（保留服务）」：只关窗，服务仍在。
6. 手动关窗（点浏览器 ×）后服务仍在 → admin 点「停止服务」→ 8010 消失。
7. 主后端 Ctrl+C 退出后，8010 也被带走（不留孤儿）。
8. 非 admin（未登录/员工层）调 `POST /api/mapeditor/service/start` → **403**。
9. `pytest LLM/tests tests -q`：失败集合**不超过**既有基线（4 个既有红态 + vision webcam 1 个环境相关）。
10. `pnpm -r build` 三包通过（**沙箱内 esbuild EPERM，需用户侧正常终端**）。

---

## 七、测试

| 文件 | 覆盖 |
|---|---|
| `LLM/tests/test_map_service_split.py`（新） | 主 app 的 `app.routes` 里**没有** `/api/map/list`、`/api/mapeditor/status`，也**没有** `/mapeditor` 挂载；`LLM.mapeditor_server.app` 里**有**；主 app **有** `/api/mapeditor/service` |
| `LLM/tests/test_mapctl.py`（新） | 假 `Popen` + 假端口探测：start 幂等（external 不重复拉起）、就绪超时、进程提前退出、stop 三态（managed→terminate / external→自停接口 / none→幂等 ok）、非 admin 403、审计落盘 |
| `LLM/tests/test_mapeditor_server.py`（新） | `GET /api/mapeditor/service` 形状（pid=os.getpid()）；`POST .../stop` 回包后**确实**安排了退出（注入 `_schedule_exit`，断言"被调用了一次"——真实 `os._exit` 绝不能进 pytest） |
| `frontend/packages/mapeditor/scripts/test-startup-contract.mjs`（改） | 见 §4.2 的三条断言 |
| 手工（用户侧） | §六 的 1–8 条；`pnpm -r build` |

---

## 八、不做（本轮明确排除）

1. **新 `start.py`（前后端 + ROS 全量一键）** —— 用户明确"本轮先不做"。
2. 病房位置自动切换、地图标记真相（`<图名>.tags.json`）口径、`MAPS_IO` 语义 —— 一律不动。
3. 不给编辑器服务加登录（与拆分前 `/mapeditor` 在 8000 上完全无鉴权同口径；它绑在内网/本机）。
4. 不做"编辑器空闲 N 分钟自动退出"（用户要的是显式「保存并退出」；YAGNI）。
5. 不引入任何新依赖（进程管理只用 stdlib `subprocess`）。

---

## 九、风险与权衡

| 风险 | 处置 |
|---|---|
| 主后端被强杀 → 编辑器成孤儿 | `external` 状态可识别、可一键收掉（§3.3）；`stop()` 挂在 `lifespan` 与 `/api/system/shutdown` 两处 |
| 多一个端口（8010）与防火墙/端口占用 | 端口写死 `conf.MAP_EDITOR_PORT` 且可由 `.env`… **注意**：本轮不加 `.env` 覆盖（YAGNI），端口冲突时 `start` 会明确报错并提示改 `conf.py` |
| 编辑器与主后端共享 `brain.db` | 两者都用 WAL + 线程锁，且编辑器写的是"文件→索引缓存"单向路径，与既有并发口径一致 |
| 拆分后"感觉更复杂" | 收益是**可杀、可隔离、接口面缩小**（§1.1）；若哪天嫌麻烦，把 `mapapi.router` 重新 include 回主后端即可回退（不影响前端） |

---

## 十、文件清单

**新增**

- `LLM/mapapi.py`、`LLM/mapeditor_server.py`、`LLM/mapctl.py`
- `LLM/tests/test_map_service_split.py`、`LLM/tests/test_mapctl.py`、`LLM/tests/test_mapeditor_server.py`
- `frontend/packages/admin/src/pages/MapEditorPage.vue`
- `frontend/packages/mapeditor/src/lib/service.ts`

**改动**

- `LLM/server.py`（−约 900 行 / +约 6 行）、`LLM/conf.py`（+2 常量）
- `frontend/packages/admin/src/App.vue`（页签）、`frontend/packages/admin/src/pages/WardsPage.vue`（链接改跳页签）
- `frontend/packages/mapeditor/src/App.vue`（两个按钮）、`frontend/packages/mapeditor/public/pixel-netio.js`（保存并退出）、`frontend/packages/mapeditor/scripts/test-startup-contract.mjs`（断言）
- `start_UI.py`（口径 + 提示）
- `AGENTS.md`（启动方式与端口）、`docs/log.md`、`docs/superpowers/specs/2026-09-14-map-editor-design.md`（追加一条口径变更：编辑器改为独立进程按需启动）

---

## 十一、实现台账与偏差

> **本节在实现落地时填写**（逐任务提交号、与本文的偏差、未验项）；本设计阶段的 §〇～§十 即为定稿口径。
>
> **填写状态（2026-09-15）：任务 1–7 已落地并提交、任务 8（口径 + 文档同步）本轮完成；任务 9（端到端验收）的实跑结果见账本 `.superpowers/sdd/progress.md` 及本节末尾（**新增于其后**）。**

### 11.1 逐任务提交号（批次 BASE `f8fd004`，文档基线 `0015a8d`）

| # | 任务 | 提交 | 前置 | 实测（聚焦 / 全量 `pytest LLM/tests tests -q`） |
|---|---|---|---|---|
| 1 | 后端搬迁：编辑器路由搬进 `LLM/mapapi.py` | `a544046`（计划修正 `d58e2f6`） | `0015a8d` | 54 passed（`test_map_service_split` 4 + `tests/test_mapeditor.py` 50）/ 5 failed · 433 passed · 1 skipped |
| 2 | `conf` 常量 + 独立服务 app `LLM/mapeditor_server.py` | `f6402a2`，修 `5bb8a5a` / `f9193e3` / `50033ee` | `d58e2f6` | `test_mapeditor_server.py` 6 passed / 4 failed · 442 passed · 1 skipped |
| 3 | 主后端侧进程管理 `LLM/mapctl.py` + 接线 | `981841e`，修 `e3f2aa1` | `50033ee` | `test_mapctl.py` 11 + `test_map_service_split.py` 6 passed / 4 failed · 455 passed · 1 skipped |
| 4 | shared 的编辑器服务客户端 + 单测 | `e570236` | `e3f2aa1` | shared vitest 19 passed（基线 16 + 新 3） |
| 5 | admin 新页签「地图编辑器」 | `1d7dd7d`，修 `7e01cd4` | `e570236` | SFC 编译三文件 OK + 4 条静态自查（沙箱 esbuild EPERM，build/tsc 归用户侧） |
| 6 | 编辑器主界面「保存并退出」/「仅关窗」 + 掉线提示 | `e162ef3` | `7e01cd4` | RED（ENOENT `lib/service.ts`）→ GREEN（`mapeditor startup contract: ok`）+ 变异验证 |
| 7 | 像素修图页「保存并退出」 | `2365ac0` | `e162ef3` | DOM-stub 脚手架 12/12 PASS（反向对照 10/12）+ `node --check` OK |
| 8 | `start_UI.py` 口径 + 文档同步 | 见 `git log`（本任务提交） | `2365ac0` | 无新增测试；`Select-String start_UI.py -Pattern 'mapeditor'` 仅剩说明文字 |
| 9 | 端到端验收 | 待落地 | `2365ac0` | **待补：见 11.3** |

**全量测试判据（本任务复跑）**：`4 failed · 455 passed · 1 skipped`，4 个失败 = `test_modules_status.py::test_modules_status_shape` + `test_unlock_switch.py`×3（**既有红态，不许修**）；基线里那条环境相关的 `tests/test_vision.py` 本轮为绿（红的具体是哪一条会飘）。**无新增红态。**

### 11.2 与设计的偏差（逐条：原设计怎么说 / 实际怎么做 / 为什么）

> 以下 12 条是本批次执行中发现的**计划缺陷/偏差**，已同步回填进计划文档 `docs/superpowers/plans/2026-09-15-map-editor-on-demand-service.md` 文末「计划回填与偏差」一节（不改写任务正文的历史文本）。

1. **任务 1 步骤 6 的目标模块尚不存在**：计划让任务 1 直接把 `tests/test_mapeditor.py` 的 fixture 改成 `from LLM.mapeditor_server import app`，但该模块由任务 2 创建 → 照抄会让 17 例接口测试全红。**实际**：任务 1 改用"临时桥"（`try: from LLM.mapeditor_server import app / except ModuleNotFoundError: 临时 FastAPI + `include_router(mapapi.router)`"），任务 2 新增步骤 5.5 收敛为单一 import。**为什么**：搬迁的必然中间态，宁可在任务 1 内保持有测试锚点，也不让 17 例空红。
2. **任务 1 搬迁必须核查"被搬走的模块级 import 还有谁在用"**：搬走模块级 `from fastapi.responses import Response` 连带打断 `/api/vision/snapshot`（相机可用时 `NameError` → 500）。**实际**：`5bb8a5a` 改为函数内 import + 补一条确定性回归测试（monkeypatch `get_jpeg`，不依赖摄像头）；计划原文的文件清单漏了这条传递依赖。
3. **跑既有测试会真停掉本机正在跑的编辑器服务**（任务 3 审查发现）：`tests/test_modules_status.py` 用 `with TestClient(app)` 跑完整 `lifespan` → 收尾 `mapctl.stop()` 走 `external` 分支 → 未打桩的 `port_alive` 探到真 8010 就 `POST` 自停；`mapeditor_server` 的 pytest 护栏只管测试进程自己。**实际**：`e3f2aa1` 在 `tests/conftest.py` 加 autouse 护栏 + 留一条回归用例（RED 实证真打到 `.../service/stop`，GREEN 零调用）。**为什么**：测试不得有外部副作用。
4. **任务 2 的路由内省必须用带 prefix 展开的 fixture**（fastapi 0.141.1 的 `include_router` 惰性）：朴素遍历 `app.routes` 只看得到 9 条。**实际**：新增 `LLM/tests/conftest.py::app_paths` fixture；`f9193e3` 修「展开带 prefix 的 include 时漏前缀」→ 改用 `context.path` + 守卫测试。**为什么**：路由锁若自己数漏了路由，就成了假绿。
5. **任务 2 的自停护栏**：`_delayed_exit` 加"`pytest` in `sys.modules` → 抛错"护栏（把"测试进程绝不真退"从纪律变成代码），`service_stop` 改为先排退出再写审计。**实际**：`50033ee`（RED 实测 pytest 以 0 码猝死 → 加固后 6 passed）。
6. **任务 3 简报的两条用例互斥**：`test_start_spawns_and_reports_managed` 与 `test_start_is_idempotent_for_external` 在同一套桩下要求相反结果（探活 True、无句柄、同一干净态：前者要 managed + `Popen` 被调，后者要 external + `Popen` 不被调），不可同时满足。**实际**：按 §3.3 与 §5 的 probe-first 口径判定**测试错**，把两例的 `_probe` 桩改成时间线（spawn 前 `False`、就绪后 `True`）；断言语义与例数（11）未变，**不改实现**。
7. **任务 4 简报断言 `init.method === "GET"` 永假**：shared 的 `apiGet` 从不设 `method`（fetch 默认即 GET）→ 逐字照抄必红。**实际**：测试内加 `methodOf(init) = init.method ?? "GET"`，断言语义不变。**为什么**：备选方案（给 `apiGet` 显式补 `method`）要动范围外且多窗口共用的 `client.ts`，冲突风险更高。
8. **任务 5 的 `window.open` 位置会被弹窗拦截**：计划骨架把 `window.open` 放在 `await startMapEditor()` **之后**（已离开用户手势同步栈），冷启动慢时会被拦，而那一刻按钮已 disabled、页面无补救入口。**实际**：`7e01cd4` 改「同步栈里先开 `about:blank` 再导航」+ 补「打开编辑器窗口」兜底按钮，顺带 `refresh(force)` 补跑意图 + 状态文案字面化 `managed`/`external`。**为什么不改成 `<a href>`**：编辑器里的 `window.close()` 会失效。
9. **任务 6 的「仅关窗」原本零反馈**（自审发现）：`closeWindowOnly()` 只调 `closeSelf()`；而它恰恰是给"关不掉的标签页"兜底的按钮，被拒时页面毫无变化。**实际**：`e162ef3` 里补 `exitNote` 文案（关得掉时看不见，无副作用）。
10. **任务 7 的像素修图页需要 17 处清位、不是 12 处**：计划只列了 12 处「没保存成功」的出口，遗漏了上游"未加载 yaml/pgm 时只 `alert` 就返回（一次提交都没发生）"以及 `flushDownloads`/`saveToServer` 的 `catch` 路径 → 退出意图会残留、下一次普通保存成功会**误停服务 + 误关窗**。**实际**：`2365ac0` 补齐 5 处（共 17 处），并用 12/12 vs 10/12 的反向对照实验证明这些补丁是承重的。
11. **任务 8 的三处文档漂移（本任务处理）**：①任务简报要求 `start_UI.py` 头 docstring 写成「一键启动程序（**UI 前端 + 后端**）」，而实际文件是「（后端 + 前端双端）」——采用了简报的措辞；②简报让提示里提"全量启动器是另有其脚本（`start.py`）"，但 `start.py` 本批次**不做**（§八 第 1 条），故 prompt 里同时点明"本脚本只管陪护 UI"、不假装它已存在；③`AGENTS.md` 里除简报点名的两段外，**「API 端点（server.py）」一节仍把编辑器路由列在主后端名下**、「前端」节 `packages/mapeditor` 仍写「生产挂 `/mapeditor`」——已一并就地标注真实归属（都只是加限定语，不删原清单）。
12. **`app_paths` / conftest 等测试基础设施是计划外新增**（计划「文件结构」表里没有）：`LLM/tests/conftest.py::app_paths`、`tests/conftest.py` 的 autouse 护栏，均属"为让 TDD 断言有意义"的必要增量。

### 11.3 未验项（**只有用户侧/真机能做**，不算本批次未完成）

1. **浏览器里真正走一遍**：admin →「启动地图编辑器」→ 弹窗打开 → 划线/标点 → 像素修图「保存并退出」；`window.open` → `window.close()` 这条链路**只有真实浏览器能验**（沙箱里无浏览器，任务 6/7 只做了源码级契约断言 + DOM-stub 脚手架）。
2. **`cd frontend && pnpm -r build` 三包构建 + 前端单测**：本沙箱 esbuild EPERM（`spawn EPERM`）；mapeditor 只有 `pnpm build:sandbox` 的等价产物。
3. **真机联动**：`MAPS_IO=ssh` 下编辑器经 SSH 读写板卡 `ros2_car/maps/` 的地图；本文不改地图 IO 口径，仍需板卡可达（`ssh sunrise@100.65.82.93`）。
4. **`remote` MCP / car 工具链**：本批次无关，未动。

### 11.4 任务 9 端到端验收（占位）

**任务 9 端到端验收结果见账本 `.superpowers/sdd/progress.md`（本批次段落）与本规格本节的后续追补**——任务 9 会按本文 §六 的 1–9 条逐条实跑（起 8000、验 404/8010 无人监听、非管理员 403、start/stop 三条路径、主后端退出带走服务、全量回归），并把结果**新增于本节之后**。

