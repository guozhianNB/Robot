# AGENTS.md

养老陪护巡逻机器人（云-边-端）主仓库。三大子系统 + 文档：

```
LLM/       Python FastAPI 后端 —— 大模型端"大脑与嘴"（最活跃，优先看这里）
frontend/  前端 —— pnpm workspace（Vue3+Vite+TS）：packages/admin 管理端 + packages/kiosk 车载端 + packages/nurse 护士台 + packages/mapeditor 地图编辑器 + packages/shared 共享层
UI(old)/   旧单文件 HTML 前端（历史参考，勿改；工作区根目录 UI/、Front/ 为空残留，勿当现役）
stm32/     底盘下位机固件（STM32F103ZETX，C/CMake/HAL）→ 见 stm32/control/AGENTS.md
docs/      需求/教程/接口契约/开发日志
```
## 注意，先查看你当前的位置。如果你在ubuntu系统上，你就在开发板卡上，不用ssh连接！
## 板卡连接
- 通过ssh连接 `ssh sunrise@100.65.82.93`（Tailscale，异地可用）
- sudo 免密可用，但禁止使用 sudo 安装/改系统，除非用户明确批准。
- sunrise账户密码：`sunrise`

## 快速上手

- **后端启动**（项目根目录）：
  ```
  .venv\Scripts\python.exe -m uvicorn LLM.server:app --host 0.0.0.0 --port 8000
  ```
- **前端**（`frontend/`，pnpm monorepo，Vue3+Vite+TS，后端 CORS 全开）：
  - **开发**：`cd frontend && pnpm install`；后端 8000 先启，再开 `pnpm dev:admin`（管理端 http://127.0.0.1:5173，vite 代理 `/api`→8000）、`pnpm dev:kiosk`（车载端 :5174）、`pnpm dev:nurse`（护士台 :5176，`server.host: true` 监听 0.0.0.0，局域网 PC 可直接访问）或 `pnpm dev:mapeditor`（地图编辑器 :5175）。
  - **生产**：跑 `scripts/build_frontend.ps1` 构建产物；后端启动时自动把 `admin`/`kiosk`/`nurse` 的 dist 用 StaticFiles 挂到 `/admin`、`/kiosk`、`/nurse`（同 8000 端口）。**`/mapeditor` 不再由主后端挂载**（2026-09-15 起）：地图编辑器是**独立进程** `LLM.mapeditor_server:app`（默认端口 `conf.MAP_EDITOR_PORT`=8010），按需由 admin →「地图编辑器」页签拉起，编辑器 dist 由它自己静态托管（未构建时同样回退到 `packages/mapeditor/public/`，这样原生静态页 `pixel-editor.html` 不经 Vite 打包也能直接打开）。
- **虚拟环境**：`.venv/`（Windows 用 `Scripts/python.exe`）。核心依赖 openai / fastapi / uvicorn / pydantic / python-dotenv；语音链路额外依赖 numpy / sherpa-onnx / sounddevice / modelscope / torch（见 `requirement.txt`）。**注意：`requirement.txt` 声明 ≠ 环境已装齐，后端必须容忍可选依赖缺失、降级运行（见「系统稳健性」）。**
- **配置**：API key 在根 `.env`（`DEEPSEEK_API_KEY`），`LLM/conf.py` 用 `load_dotenv(BASE_DIR/".env")` 加载。

## 架构与模块（LLM/ 后端）

**2026-09-17 按功能重新分层**：依赖单向 `core → store → agent → server`，`LLM/` 根目录只留两个入口 + `conf.py`。

```
LLM/
  server.py            入口①  uvicorn LLM.server:app（:8000，挂 /admin /kiosk /nurse）
  mapeditor_server.py  入口②  uvicorn LLM.mapeditor_server:app（:8010，按需拉起）
  conf.py              集中配置（全包共享的顶层契约；**位置不可动**：BASE_DIR 靠它定位 .env）
  core/                log(审计) bus(SSE) vectors(轻量向量) zonegeo(几何)     零业务、零外部依赖
  store/               db(SQLite) ragstore(Chroma) graph(Kuzu) embed migrate  持久化
  agent/               chat memory tools mcp_client reminder session policy   智能体
                       prompt/  base.md(共用人设+红线) + ward/elder/admin.md(角色片段)
  maps/                mapstore mapsources maptags mapserver locator roslink  地图域
                       mapapi(编辑器路由，仅 :8010 进程 import) mapctl(编辑器进程管理)
  voice/               worker asr tts kws vad speaker + voice_api(挂载逻辑)
  tool/ car_mcp/ vision_mcp/   工具实现与外部 MCP 子进程（不在后端导入链顶层）
```

分层铁律：**下层绝不 import 上层**（`core` 不 import `store`/`agent`；`store` 不 import `agent`）。
唯一例外出口是 `store/db.py` 对 `agent.tools.TOOL_DEFAULTS` 的**函数内延迟导入**（勿提到模块顶层）。
跨层导入写法：子包内 `from ..store import db`，包根 `from .store import db`。

- `server.py` — FastAPI 入口：CORS 全开，`lifespan` 启动 `db.init_db()` → `_seed_demo()` → `notify.prune()` → `reminder.start()` → `bus.start_drain()` → `voice_api.start_voice()`；全局 OpenAI 客户端（DeepSeek），`_bg` 线程池跑后台任务；末尾挂载 `/admin`、`/kiosk`、`/nurse` 前端静态产物。
- `agent/chat.py` — 对话编排：`chat_stream()`（SSE 生成器，工具循环最多 2 轮）、`route_thinking()`（思考路由）、`build_system()/build_messages()`（System Prompt + RAG + 滚动窗口）、`llm_json()`。
- `conf.py` — **集中配置**。路径、`DEFAULT_SETTINGS`、`THINKING_KEYWORDS`、`HISTORY_WINDOW`、`MEMORY_RULES`、`MODEL`、超时等。**改参数先来这里**。
- `store/db.py` — SQLite 数据层（`LLM/data/brain.db`，WAL + 线程锁）。函数命名 `get_*`/`add_*`/`set_*`/`update_*`/`delete_*`/`upsert_*`，协程侧用 `asyncio.to_thread`。
- `agent/session.py` — **分层用户体系的会话层**（双槽会话主体 + 角色推导 + 口令 + 病房自动切换）：`derive_role()`（uid→role 的唯一权威，按 `profiles.kind`，**不靠 uid 前缀**）、`get_principal(slot)`、`set_subject()`、`login_admin()/logout()`、`change_admin_password()/set_admin_auth()/ensure_admin_password()`、`current_ward()/manual_set_ward()/running_map_name()/autoswitch_state()`、`tick()`（每秒：admin TTL 降权 + 病房位置判定）。**角色只在这里推导，业务代码一律读 `get_principal()`（R1）**。
- `agent/policy.py` — **三角色策略包**：`POLICY_DEFAULTS`（ward/elder/admin 三层的 `prompt_file`/`allowed_tools`/`data_scope`/`ward_context`）+ `role_policy()`（未知角色 fail-closed 落集体层，返回浅拷贝防全局白名单被污染）。**纯数据 + 纯函数、不做任何 IO**；P1 的 `check_action()` 动作分级不在此留空壳。
- `core/zonegeo.py` — **点是否在区域内**（纯几何、纯 stdlib、约 30 行）：`point_in_polygon()`（射线法）+ `zone_hit()`（吃缓存行，`shape='rect'` 用外接矩形，点数 <3 一律 `False`，**坏入参绝不抛异常**）。口径与前端 `packages/mapeditor/src/lib/coords.ts` 一致。
- `agent/prompt/` — **角色提示词片段**：`ward.md`/`elder.md`/`admin.md` 三层各一份，由 `chat._load_role_prompt()` 按 `role_policy(role)["prompt_file"]` 装载、叠加在 `base.md` 之上（`base.md` **是共用 base**，管"怎么说"；角色片段管"现在跟谁说话"）；缺文件 → 空串 + 审计 `prompt_role_missing`，不阻断对话。
- `agent/memory.py` — RAG 记忆 + 半自动沉淀：`recall()`、`note_turn()`、`consolidate()`、`suggest_from_chat()`。**红线：`MEDICAL_KEYWORDS` 命中拒绝写入**。（MaiBot 对标增强：核心记忆定稿/保护、回收站、纠错、画像防退化等，见 `docs/log.md` 2026-09 条目）
- `agent/reminder.py` — 独立线程定时调度（15s tick），状态机 `pending→triggered→confirmed/unconfirmed/missed`。
- `agent/notify.py` — **通知中心（模块 11）的唯一写入口**：`ingest()`（归一化 → 去重合并 → 落库 → 审计 → 广播）/ `list_notices()` / `counts()` / `ack()` / `ack_all()` / `remove()` / `prune()`；护士台数据面，规格 `docs/superpowers/specs/2026-09-18-nurse-console-design.md`。
- `core/bus.py` — SSE 事件总线，`publish()`（任意线程）→ asyncio 扇出订阅者。
- `agent/tools.py` — 工具注册中心 + 分发：本地工具（`LLM/tool/` 下 `@tool` 装饰器注册，自动加载）+ MCP 工具（`conf.MCP_SERVERS` 配置），`run_tool()` 统一分发，per-tool 开关自动生效。
- `agent/mcp_client.py` — MCP 客户端桥（**可选能力**）：后台线程 + 专属事件循环拉起 stdio MCP 服务器子进程，`tools/list` 转 OpenAI function-calling schema 并入工具循环，`mcp_enabled` 总开关控制；依赖缺失/连接失败只降级不崩后端。
- `core/log.py` — 审计日志（JSONL 落 `LLM/data/audit.jsonl`，线程锁追加）：`log(event, **fields)`。
- `core/vectors.py` — 零依赖轻量向量检索（字符 n-gram 哈希 + TF + L2 + 余弦）。
- `maps/mapstore.py` — **地图文件在哪、怎么读写**（不含 HTTP 与业务校验）：`MapStore` 协议 + `LocalMapStore`（纯 stdlib）/ `SshMapStore`（主形态：paramiko 优先、退回 `ssh.exe`/`scp.exe` 子进程）+ `MapCache` 离线缓存 + `get_store()/reset_store()/io_status()/io_test()` + 名字白名单 `check_name()` + `backup()/_prune_backups()`。
- `maps/maptags.py` — **地图标记的唯一读写入口**：`<图名>.tags.json` 的读（`resolve`）/原子写（`save`）/整份替换（`replace_all`）、单向刷索引缓存 `sync_map()`、指纹比对 `fingerprint_check()`、地点与区域增删改、`learn_here()`、`record_room_polygon()`、`reindex()/reindex_all()`。
- `maps/mapserver.py` — 地图渲染与校验（**纯 stdlib**）：PGM(P2/P5) 解析 + 灰度 PNG 编码 + 未知率三分（occ/free/unknown）、`meters_to_pixel()/pixel_to_meters()`、`validate_point()`（越界/障碍/未知/余量）、`map_info()`。
- `maps/roslink.py` — rosbridge(websocket) 连接层：`available()/status()`、订阅 `/amcl_pose` 与 `/map`、`call_service()`、连不上只降级不崩、假数据注入。
- `maps/locator.py` — 位姿与当前地图：`pose_payload()`（降级保持 `ok: True`）、`set_pose_for_test()/clear_injection()`、`current_map()`（用 `/map` 元数据指纹反查在跑哪张 yaml）、`status()`。
- `voice/`（含 `voice_api.py`，2026-09-17 并入本包） — 语音链路（唤醒/识别/播报/声纹，**可选能力**）：外部依赖缺失时整体降级，后端照常启动，见「系统稳健性」。
- `../vision/` — **摄像头共享服务**（`protocol.py` 协议 / `camera_server.py` 裸 TCP 守护进程 / `camera_client.py` 客户端 / `webcam.py` Windows·USB 后端 / `webbridge.py` HTTP 桥）：摄像头同一时刻只能被一个进程独占，故由守护进程持有并按通道向多客户端分发最新帧。**三种来源**：`--source auto`（默认）——板卡优先 `mipi`（`hobot_vio`，采集在子进程）、PC 优先 `webcam`（OpenCV，**可选依赖**，惰性导入，进程内），另有 `mock` 合成帧；`--list-cameras` 逐个试读确认设备号。**所有后端统一产出 NV12**，故 client/webbridge 零改动。`webbridge` 把裸 TCP 桥成 `/api/vision/*`，让 **PC 浏览器直接看画面**（硬件 JPEG 不可用时退回纯 stdlib 基线编码器）；摄像头服务地址由 `conf.VISION_HOST/VISION_PORT` 决定（默认本机；摄像头在板卡时指向板卡）。协议/API 见 `vision/README.md`，测试 `tests/test_vision.py`。

## API 端点（server.py）

**权威清单 = `server.py` 里的 `@app.*` 装饰器行**，摘要：

- 健康/状态：`GET /api/health` ｜ `GET /api/modules/status` ｜ `GET /api/logs/warnings` ｜ `GET /api/context`
- 对话：`POST /api/chat`（SSE 流式，体 `{uid, message, thinking:"auto|none|low|high|max", speak}`）｜ `GET|DELETE /api/chat/history`
  - **思考档位（2026-09-17 规格 `docs/superpowers/specs/2026-09-17-thinking-mode-switch-design.md`）**：五档 `auto`（照路由）/ `none` 不思考 / `low` 轻度 / `high` 中度 / `max` 重度——**`none` 也关不掉敏感词安全网**（`THINKING_KEYWORDS`/情绪词/LLM 预判命中的问题照旧加深，`method` 如实上报 `keyword|emotion|llm` 而不是 `manual`）；旧值 `on`/`off` 别名到 `high`/`none`。**空串 / 缺省**=读 `settings.thinking_mode`（非特权键，kiosk 也能改——语音轮次不过前端，只有落库的设置才能让语音吃到手动档位，`voice_api._stream_fn` 传的就是空串）。`meta` 事件带 `router.mode` + `router.effort` 上报实际生效强度。
  - ⚠️ **`reasoning_effort` 必须是顶层参数**（`client.chat.completions.create(reasoning_effort=...)`，openai 3.3.1 签名里有）：塞进 `extra_body` 会被 DeepSeek 当未知字段**静默忽略**，表现就是「选了强制/中度也不思考」——2026-09-17 用户实测踩过。`extra_body` 只放 `{"thinking": {"type": "enabled"|"disabled"}}`；`low/high/max` 是实测可用档（官方 `minimal/medium/xhigh` 会被归并、`ultra` 直接 400）。思考档位下思维链也吃 `max_tokens`，故 `chat_stream` 有「只有思考没正文 → 关思考重答一次」的兜底（`action="thinking_empty_fallback"`）。
  - **思维链**：SSE `reasoning` 事件（kiosk 语音轮次额外广播总线 `chat_reasoning`）**只上屏、绝不进 TTS、不落库**（历史回读不重播思考过程）——`server.py::chat_route` 只把 `content` 喂 `voice_api.feed_text_reply`，`voice/worker.py::_consume_events` 只对 `content` 分句合成；改这两处等于把思维链念出来，`LLM/tests/test_thinking_mode.py`、`test_worker_events.py::test_reasoning_is_shown_but_never_synthesized` 锁死该不变量。
- 档案/会话：`GET|POST /api/profiles` ｜ `GET|POST /api/session/user`（active_uid + 锁定，kiosk 手动选人）
- 提醒：`GET|POST /api/reminders` + `/{rid}/confirm` `/delete`
- 记忆：`/api/memories` 一族 —— `GET|POST`、`/{mid}/confirm|reject|delete`、`/recycle`(+restore/purge)、`/correct`、`/import`、`/portrait`(护士手动画像)、`/suggest`、`/health`；`/api/memories/core`(+confirm/unconfirm/pin/unpin/{mid}delete)、`/rag`(+delete)、`/graph`、`/expressions`(+approve/reject)
- 语音（可选，依赖缺失时降级，见「系统稳健性」）：`GET /api/voice/status` ｜ `POST /api/voice/enroll` ｜ `GET /api/voice/speakers` ｜ `DELETE /api/voice/speakers/{uid}` ｜ `POST /api/voice/record`(+`/{id}/audio`) ｜ `GET /api/face/status`
- 告警/系统：`POST /api/alarm`（kiosk SOS）｜ `POST /api/system/shutdown`
- 通知中心（模块 11，护士台数据面，规格 `docs/superpowers/specs/2026-09-18-nurse-console-design.md`）：`POST /api/notifications`（**免鉴权**投递口，任何模块/小车都能上报）｜ `GET /api/notifications`（`?state=all|unread&limit&before_id`，返回 `{items, counts}`）｜ `POST /api/notifications/{nid}/ack` ｜ `POST /api/notifications/ack-all` ｜ `DELETE /api/notifications/{nid}`（**后四条仅管理员**，走 `X-Surface: admin`）；广播总线事件 `notification`（新通知/去重合并，**通知类型在 `kind` 键里**）与 `notification_ack`
- 工具/设置：`GET /api/tools` ｜ `GET /api/tools/log` ｜ `GET|POST /api/settings`
- 广播：`GET /api/events`（SSE）
- **地图编辑器**（第三个前端 `/mapeditor`，规格见 `docs/superpowers/specs/2026-09-14-map-editor-design.md`）—— ⚠️ **以下路由 2026-09-15 起已搬到 `LLM/maps/mapapi.py`，只在独立进程 `LLM.mapeditor_server`（`conf.MAP_EDITOR_PORT`=8010）上挂载，主后端（8000）不再暴露它们**（主后端只留 3 条仅管理员的 `GET|POST /api/mapeditor/service{,/start,/stop}` 做进程启停）：
  - `/api/map/*`：`GET list` ｜ `GET current`｜ `GET|POST {name}/meta` ｜ `POST {name}/rename` ｜ `POST {name}/copy` ｜ `DELETE {name}` ｜ `GET {name}/download`（`file=yaml|pgm|tags`，默认 `yaml`）｜ `GET {name}/image.png`（灰度 PNG，断连时带 `X-Map-Stale` 头）｜ `POST {name}/save`（像素修图落盘）｜ `GET|PUT {name}/tags` ｜ `POST {name}/tags/reindex` ｜ `POST /api/map/reindex-all/tags`
  - `/api/destinations*`：`GET`（`?map=`）｜ `POST`｜ `POST /validate` ｜ `POST {uid}` ｜ `DELETE {uid}`（`?map=`）｜ `POST /learn`
  - `/api/zones*`：`GET`（`?map=`）｜ `POST`｜ `POST {uid}` ｜ `DELETE {uid}`（`?map=`）
  - `/api/robot/pose`（位姿，降级 `ok: True`）
  - `/api/mapeditor/*`：`GET /status` ｜ `GET /io` ｜ `POST /io/test` ｜ `POST /pose/inject`（假位姿/假地图注入，无 ROS 开发用）
- **摄像头**（`vision/`，摄像头共享服务 + HTTP 桥；来源 `auto`/`mipi`/`webcam`/`mock`）：
  - `/api/vision/status`（状态，含 `target` 回显连的地址；不可用时 `ok:True` + `status:"unavailable"`）｜ `GET /snapshot?channel=&quality=`（单帧 JPEG）｜ `GET /stream?channel=&fps=`（MJPEG，`<img src>` 直接可看）
  - 裸 TCP 服务：`python3 -m vision.camera_server`（默认 `127.0.0.1:9540`；板卡走 mipi、PC 走 webcam，`--mock` 合成帧，`--list-cameras` 查设备号）；客户端 `vision.CameraClient`。摄像头服务地址由 `conf.VISION_HOST/VISION_PORT` 指定。详见 `vision/README.md`。

## 关键约定（改动前必读）

1. **新增功能三件套**：路由放 `server.py`、配置/常量放 `conf.py`、数据操作放 `store/db.py`；模块按功能就近放进 `core/`/`store/`/`agent/`/`maps/`/`voice/` 子包，**别再平铺到 LLM 包根**。
2. **审计贯穿**：几乎所有状态变更都要 `core/log.py::log()` 落审计（事件类型：`chat`/`memory_change`/`reminder`/`tool`/`settings`/`alarm`）。
3. **错误处理**：异常捕获后 `audit.log(...)` + 返回 `{"ok":False,...}` 或 SSE `error` 事件；后台任务异常吞掉防崩溃。
4. **SSE 事件协议两端强耦合**：聊天流事件类型（`meta`/`reasoning`/`content`/`tool_start`/`tool_result`/`done`/`error`）由 `agent/chat.py::chat_stream` 产出；总线事件（`reminder`/`alarm`/`voice_state`/`chat_new`/`user_changed`…）由 `core/bus.py::publish` 产出。前端消费侧的**唯一事实来源** = `frontend/packages/shared/src/events.ts`（类型枚举 + `parseBusPayload`），新增/改动事件要在后端 publish 点与 events.ts 同步维护。
5. **DeepSeek 流式陷阱**：thinking disabled 时 `delta` 无 `reasoning_content` 属性，必须用 `getattr(delta, "reasoning_content", None)` 安全取值（见 `llm_request.py` 示例）。
6. 包内模块用相对导入 —— **子包内跨层写 `from ..store import db`、同包写 `from . import xxx`**（`from LLM.xxx` 绝对导入只用于 `tests/`）；文件头 `# -*- coding: utf-8 -*-` + r-string docstring。
7. **角色闸门与红线（2026-09-14 分层用户体系 P0）**：前端传上来的 `role` 一律不可信（R1）——uid→role
   只由 `session.derive_role()` 按 `profiles.kind` 推导（不靠 uid 前缀）；未知 ≈ 集体层最小能力（R2）；
   急停/呼救永远放行（R3）；医疗写入红线与角色无关（R4）；层级上下文单向（R5）。业务接口按请求头
   `X-Surface: kiosk|admin` 取 principal（非法值 400）。**拿 admin 只能走 `/api/session/login`（口令）**，
   `POST /api/session/user` 带 `uid="admin"` 或 `role` 一律 400。**病房区域几何的唯一真相是地图文件夹的
   `<图名>.tags.json`**（`brain.db.zones` 只是只读缓存），`profiles` 只记 `ward_map`+`ward_zone`。
   详细规格：`docs/superpowers/specs/2026-09-14-layered-user-roles-design.md`（§14 是复审结论）。

## 系统稳健性（降级运行，重点）

**原则：可选能力的外部依赖缺失时，系统必须降级运行，而不是拒绝启动。** 部署环境不一（板卡 / 无模型 / 依赖未装齐），`requirement.txt` 只声明依赖、不代表运行环境已装齐，后端必须容忍缺失。

- **红线**：可选依赖（numpy / sherpa-onnx / sounddevice / modelscope / torch 等）绝不允许出现在 `server.py` 及后端导入链的顶层硬 import 中——`LLM.server` 必须能无条件 import、`lifespan` 必须能无条件启动。
- **降级模式（范例 `LLM/voice/voice_api.py`）**：可选依赖在模块顶层逐个 `try/except` 引入，失败时置模块级标志 `_VOICE_AVAILABLE = False`，并把缺失项逐个收集进 `_MISSING_DEPS`（缺多个时别只报第一个）。
- **不可用时的行为**：
  - `start_*` 型启动钩子：`audit.log("voice_degraded", ...)` 记录 + `print("[WARN] ...")` 提示 + 返回 `None` 静默跳过，不影响 lifespan 其余步骤；
  - 查询类 API：返回 `{"ok": True, "status": "unavailable", ..., "reason": "缺少依赖：..."}`（`ok` 保持 True——服务健康 ≠ 功能可用，前端不会误判为后端故障）；
  - 写操作 API：返回 `{"ok": False, "error": "语音模块不可用（缺少依赖：...）"}`。
- **新功能引入可选依赖时遵循此模式**；能用 stdlib 就绝不引入外部依赖（如 `core/vectors.py` 零依赖向量检索）。

## 前端（frontend/）

- pnpm workspace（Vue3 + Vite + TS），四个端包 + `shared` 共享层：
  - `packages/admin` — 管理端（PC 浏览器）：页签 = 监控总览/对话/记忆/提醒/工具日志/语音状态/设置；dev :5173。
  - `packages/kiosk` — 车载交互端（老人面前屏幕，无屏也能跑，语音主交互在后端闭环）：状态条/对话区/切换用户(锁定)/提醒/SOS/设置弹层；dev :5174。
  - `packages/nurse` — **护士台**（PC 浏览器，模块 11 告警面板）：只做三件事 = 看通知 / 点「处理了」/ 一眼看清未处理条数（不做对话、设置、记忆、地图、身份权限）；dev `:5176`（`server.host: true`）、生产由主后端挂 `/nurse`。规格 `docs/superpowers/specs/2026-09-18-nurse-console-design.md`。
  - `packages/shared` — 共享层：REST client（`src/api/`）+ SSE 事件唯一定义 `src/events.ts`（类型枚举 + `parseBusPayload`）+ 思考档位工具 `src/thinking.ts`（五档 `ThinkingMode` / `THINKING_MODE_ORDER` / `normalizeThinkingMode`（含 on/off 兼容）/ `thinkingModeLabel`），admin/kiosk 都从它引入。
  - `packages/mapeditor` — **地图编辑器**（PC 浏览器，看图 / 标地点 / 划区域 / 管地图文件）：dev `:5175`、生产由编辑器自己的进程挂 `/mapeditor`（主后端 8000 不再挂载，见「快速上手 → 前端」）；`src/` 是 Vue3 应用（App.vue + pages/{MapCanvas,PlacePanel,ZonePanel,MapFiles}.vue + lib/{coords,colorize,api,types}.ts），**另含不经打包的原生静态页** `public/pixel-editor.html`（上游 `ROS-SLAM-Map-Editor/editor.html` 的 LF 副本，相对上游 git blob 只改 6 处）、接线层 `public/pixel-netio.js` 与自托管资源 `public/vendor/`（5 个原 CDN 资源 + 上游 MIT LICENSE）。
- 工程细节：vite `base` 与后端挂载路径一致（admin→`/admin/`、kiosk→`/kiosk/`，见 vite.config 注释 C-1）；`shared` 包 alias 到 TS 源码（monorepo 已知坑）。
- **功能齐平是渐进迁移**：个别旧功能尚未搬入 Vue admin——典型如**老人注册向导**（旧入口在 `UI(old)/index.html`「➕ 注册老人」4 步向导，规格 `docs/superpowers/specs/2026-08-24-elder-registration-flow-design.md`，后端 `/api/profiles` + `/api/voice/enroll` 一直可用）。要动此类功能先看旧实现 + 规格，别从零重造。

## 文档导航（docs/，链接勿复制正文）

- `docs/目标文档及说明/大模型端开发目标.md` — **大模型端需求文档**（能力总览/模块/安全红线/决策表），实现 LLM/ 时以此为准。
- `docs/目标文档及说明/USB车控接口.md` — 地瓜派↔STM32 USB CDC 帧协议（v1.0），配套 demo 在 `docs/2.pre/usb_chassis_demo.py`。
- `docs/目标文档及说明/ROS底盘接口需求.md` — 大模型端↔方向二（ROS2/SLAM）对接契约（draft）。
- `docs/log.md` — **开发日志**（按日期追加，记录了各模块实现细节）。
- `docs/1.pre/` — 第一阶段学习教程/硬件规划（PyTorch、ROS2、学习路线、硬件清单）。
- `docs/superpowers/specs/2026-08-27-frontend-multi-end-design.md` — **前端多端重构设计**（现行 frontend/ 架构依据：D1-D11 决策、admin/kiosk 分工、部署形态）。
- `docs/superpowers/specs/2026-08-24-elder-registration-flow-design.md` — 老人注册向导设计（尚未迁入 Vue admin，见「前端（frontend/）」节）。
- `docs/superpowers/specs/2026-09-14-map-editor-design.md` — **地图编辑器设计（A 篇：看图/标地点/划区域/管地图文件；B 篇：像素修图）**，**2026-09-14 已落地**（实现台账与偏差见文末「实现台账与偏差（2026-09-14 落地）」一节）。改地图相关代码前必读。
- `docs/superpowers/specs/2026-09-17-thinking-mode-switch-design.md` — **思考档位手动切换（五档：自动/不思考/轻/中/重度）+ 思维链上屏**（2026-09-17 落地，含 D5「`reasoning_effort` 必须是顶层参数、塞 extra_body 会被静默忽略」这条实测坑）：`none` 关不掉敏感词安全网、档位落 `settings.thinking_mode`、思维链只上屏绝不进 TTS。改 `chat_stream`/`voice/worker.py` 前必读。
- `docs/superpowers/specs/2026-09-18-nurse-console-design.md` — **护士台 + 后端通知中心设计**（模块 11 落地：`notifications` 表 + 5 条路由 + 总线 `notification`/`notification_ack` + 第四个前端包 `packages/nurse`）：含 D6「payload 里通知类型用 `kind`、绝不能用 `type`」与 D7 视觉尺度（不做大按钮/大字号）、D8（`server.host: true` + `--host 0.0.0.0` 保证局域网可达）。改通知链路前必读。
- 历史档案：`docs/superpowers/specs/2026-08-18-ai-chat-frontend-design.md` 等 8 月旧规格描述的是单文件 `UI/index.html` 时代的实现，仅作过程参考（其 `/api/chat` 请求体已过时，实际为 `{uid, message, thinking}`）。

## 固件（stm32/）

- `stm32/control/` — 主固件，**改动前必读** `stm32/control/AGENTS.md`（target_sources 注册、勿动 CubeMX 文件、10ms 周期约束等）。
- `stm32/led_test/` — 独立小型 CubeMX 测试工程，非活跃业务。

## 已知坑

- `.gitignore` 排除了 `.venv/`、`.env`、`LLM/data/*.db*`、`LLM/data/audit.jsonl`（运行时数据不入库）。
- `requirement.txt` 声明的语音依赖（numpy / sherpa-onnx / sounddevice / modelscope 等）在目标环境可能未装齐：新依赖记得固化进去，且后端必须容忍缺失、降级运行（见「系统稳健性」）。
- 后端 run 用包方式 `LLM.server:app`（`LLM/conf.py` 里 `BASE_DIR = Path(__file__).resolve().parent.parent` 定位 `.env`）。**分包时 `conf.py` 位置不可动**（2026-09-17 整理时特意留在包根），动它要同步改 `BASE_DIR`/`DATA_DIR`/`PROMPT_FILE` 三处 `__file__` 推导。
- **前端已在 2026-08-27 从 `UI/` 单文件迁至 `frontend/`**：改前端先看 `docs/superpowers/specs/2026-08-27-frontend-multi-end-design.md` 与 shared 的 events.ts；`UI(old)/`（git 跟踪）为旧实现参考；根目录 `UI/`、`Front/` 是空残留目录，勿当现役前端。
- **后端运行位置（2026-09-14 起）**：LLM 后端**默认跑在 PC 上**（板卡 RDK X5 性能有限，重活不下放板卡）。**地图编辑器已落地**：`MAPS_IO` 默认 `ssh`，地图文件的真相仍在板卡 `ros2_car/maps/`，经 SSH 读写（`GET /api/map/list` 已排除备份目录 `maps/.backup/`）；板卡上跑后端时用 `MAPS_IO=local`。主后端**只保留** `locator`（病房位置自动切换）与 `maptags`（`POST /api/wards/{uid}/zone` 的「记录当前房间为病房区域」）；**编辑器自己的路由（`/api/map/*`、`/api/destinations*`、`/api/zones*`、`/api/robot/pose`、`/api/mapeditor/{status,io,io/test,pose/inject}`）已搬到 `LLM/maps/mapapi.py`**，只在独立进程 `LLM.mapeditor_server`（:8010）里挂载，主后端不再暴露。相关规格：`docs/superpowers/specs/2026-09-14-map-editor-design.md` 的 **B 篇**（§B四～§B十二）、`docs/superpowers/specs/2026-09-15-map-editor-on-demand-service-design.md`（按需启动）。
- **地图标记存在地图文件夹里（2026-09-14 起）**：地点与区域（哪间是 101、护士办公室在哪）的**唯一真相是 `<地图名>.tags.json`**（与 `.pgm`/`.yaml` 同级同前缀，含 `resolution`/`origin` 指纹）；`brain.db` 的 `destinations`/`zones` 两表**只是索引缓存**，单向（文件→库）、可丢弃可重建。**不要把它们当真相去写** —— 违反就重演"两套真相"事故。规格：`docs/superpowers/specs/2026-09-14-map-editor-design.md` §4。
- **地图像素修图入口**：`/mapeditor/pixel-editor.html`（由编辑器独立进程 :8010 提供，主后端 8000 上不可达）—— 它是**在开源工程 `ROS-SLAM-Map-Editor/`（GyroPalm/ROS-SLAM-Map-Editor，MIT，定版 `646104e`，已 gitignore）上改进**的产物，规格见 `docs/superpowers/specs/2026-09-14-map-editor-design.md` **§B〇**（上游位置/目录清单/四条使用约定）与 **§B6.1**（相对上游原文件**只改 6 处**）。保存前自动备份到 `maps/.backup/`（故 `GET /api/map/list` 必须排除该目录）；改完地图**必须重启导航才生效**（`~/tools/nav_screen.sh nav <地图名>`）。
