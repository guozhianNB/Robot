# AGENTS.md

养老陪护巡逻机器人（云-边-端）主仓库。三大子系统 + 文档：

```
LLM/       Python FastAPI 后端 —— 大模型端"大脑与嘴"（最活跃，优先看这里）
frontend/  前端 —— pnpm workspace（Vue3+Vite+TS）：packages/admin 管理端 + packages/kiosk 车载端 + packages/shared 共享层
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
  - **开发**：`cd frontend && pnpm install`；后端 8000 先启，再开 `pnpm dev:admin`（管理端 http://127.0.0.1:5173，vite 代理 `/api`→8000）或 `pnpm dev:kiosk`（车载端 :5174）。
  - **生产**：跑 `scripts/build_frontend.ps1` 构建产物；后端启动时自动把 `admin`/`kiosk` 的 dist 用 StaticFiles 挂到 `/admin`、`/kiosk`（同 8000 端口；未构建则对应接口返回提示）。
- **虚拟环境**：`.venv/`（Windows 用 `Scripts/python.exe`）。核心依赖 openai / fastapi / uvicorn / pydantic / python-dotenv；语音链路额外依赖 numpy / sherpa-onnx / sounddevice / modelscope / torch（见 `requirement.txt`）。**注意：`requirement.txt` 声明 ≠ 环境已装齐，后端必须容忍可选依赖缺失、降级运行（见「系统稳健性」）。**
- **配置**：API key 在根 `.env`（`DEEPSEEK_API_KEY`），`LLM/conf.py` 用 `load_dotenv(BASE_DIR/".env")` 加载。

## 架构与模块（LLM/ 后端）

- `server.py` — FastAPI 入口：CORS 全开，`lifespan` 启动 `db.init_db()` → `_seed_demo()` → `reminder.start()` → `bus.start_drain()` → `voice_api.start_voice()`；全局 OpenAI 客户端（DeepSeek），`_bg` 线程池跑后台任务；末尾挂载 `/admin`、`/kiosk` 前端静态产物。
- `chat.py` — 对话编排：`chat_stream()`（SSE 生成器，工具循环最多 2 轮）、`route_thinking()`（思考路由）、`build_system()/build_messages()`（System Prompt + RAG + 滚动窗口）、`llm_json()`。
- `conf.py` — **集中配置**。路径、`DEFAULT_SETTINGS`、`THINKING_KEYWORDS`、`HISTORY_WINDOW`、`MEMORY_RULES`、`MODEL`、超时等。**改参数先来这里**。
- `db.py` — SQLite 数据层（`LLM/data/brain.db`，WAL + 线程锁）。函数命名 `get_*`/`add_*`/`set_*`/`update_*`/`delete_*`/`upsert_*`，协程侧用 `asyncio.to_thread`。
- `memory.py` — RAG 记忆 + 半自动沉淀：`recall()`、`note_turn()`、`consolidate()`、`suggest_from_chat()`。**红线：`MEDICAL_KEYWORDS` 命中拒绝写入**。（MaiBot 对标增强：核心记忆定稿/保护、回收站、纠错、画像防退化等，见 `docs/log.md` 2026-09 条目）
- `reminder.py` — 独立线程定时调度（15s tick），状态机 `pending→triggered→confirmed/unconfirmed/missed`。
- `bus.py` — SSE 事件总线，`publish()`（任意线程）→ asyncio 扇出订阅者。
- `tools.py` — 工具注册中心 + 分发：本地工具（`LLM/tool/` 下 `@tool` 装饰器注册，自动加载）+ MCP 工具（`conf.MCP_SERVERS` 配置），`run_tool()` 统一分发，per-tool 开关自动生效。
- `mcp_client.py` — MCP 客户端桥（**可选能力**）：后台线程 + 专属事件循环拉起 stdio MCP 服务器子进程，`tools/list` 转 OpenAI function-calling schema 并入工具循环，`mcp_enabled` 总开关控制；依赖缺失/连接失败只降级不崩后端。
- `log.py` — 审计日志（JSONL 落 `LLM/data/audit.jsonl`，线程锁追加）：`log(event, **fields)`。
- `vectors.py` — 零依赖轻量向量检索（字符 n-gram 哈希 + TF + L2 + 余弦）。
- `voice/` + `voice_api.py` — 语音链路（唤醒/识别/播报/声纹，**可选能力**）：外部依赖缺失时整体降级，后端照常启动，见「系统稳健性」。

## API 端点（server.py）

**权威清单 = `server.py` 里的 `@app.*` 装饰器行**，摘要：

- 健康/状态：`GET /api/health` ｜ `GET /api/modules/status` ｜ `GET /api/logs/warnings` ｜ `GET /api/context`
- 对话：`POST /api/chat`（SSE 流式，体 `{uid, message, thinking:"auto|on|off"}`）｜ `GET|DELETE /api/chat/history`
- 档案/会话：`GET|POST /api/profiles` ｜ `GET|POST /api/session/user`（active_uid + 锁定，kiosk 手动选人）
- 提醒：`GET|POST /api/reminders` + `/{rid}/confirm` `/delete`
- 记忆：`/api/memories` 一族 —— `GET|POST`、`/{mid}/confirm|reject|delete`、`/recycle`(+restore/purge)、`/correct`、`/import`、`/portrait`(护士手动画像)、`/suggest`、`/health`；`/api/memories/core`(+confirm/unconfirm/pin/unpin/{mid}delete)、`/rag`(+delete)、`/graph`、`/expressions`(+approve/reject)
- 语音（可选，依赖缺失时降级，见「系统稳健性」）：`GET /api/voice/status` ｜ `POST /api/voice/enroll` ｜ `GET /api/voice/speakers` ｜ `DELETE /api/voice/speakers/{uid}` ｜ `POST /api/voice/record`(+`/{id}/audio`) ｜ `GET /api/face/status`
- 告警/系统：`POST /api/alarm`（kiosk SOS）｜ `POST /api/system/shutdown`
- 工具/设置：`GET /api/tools` ｜ `GET /api/tools/log` ｜ `GET|POST /api/settings`
- 广播：`GET /api/events`（SSE）

## 关键约定（改动前必读）

1. **新增功能三件套**：路由放 `server.py`、配置/常量放 `conf.py`、数据操作放 `db.py`。
2. **审计贯穿**：几乎所有状态变更都要 `log.py::log()` 落审计（事件类型：`chat`/`memory_change`/`reminder`/`tool`/`settings`/`alarm`）。
3. **错误处理**：异常捕获后 `audit.log(...)` + 返回 `{"ok":False,...}` 或 SSE `error` 事件；后台任务异常吞掉防崩溃。
4. **SSE 事件协议两端强耦合**：聊天流事件类型（`meta`/`reasoning`/`content`/`tool_start`/`tool_result`/`done`/`error`）由 `chat.py::chat_stream` 产出；总线事件（`reminder`/`alarm`/`voice_state`/`chat_new`/`user_changed`…）由 `bus.py::publish` 产出。前端消费侧的**唯一事实来源** = `frontend/packages/shared/src/events.ts`（类型枚举 + `parseBusPayload`），新增/改动事件要在后端 publish 点与 events.ts 同步维护。
5. **DeepSeek 流式陷阱**：thinking disabled 时 `delta` 无 `reasoning_content` 属性，必须用 `getattr(delta, "reasoning_content", None)` 安全取值（见 `llm_request.py` 示例）。
6. 包内模块用 `from . import xxx` 相对导入；文件头 `# -*- coding: utf-8 -*-` + r-string docstring。

## 系统稳健性（降级运行，重点）

**原则：可选能力的外部依赖缺失时，系统必须降级运行，而不是拒绝启动。** 部署环境不一（板卡 / 无模型 / 依赖未装齐），`requirement.txt` 只声明依赖、不代表运行环境已装齐，后端必须容忍缺失。

- **红线**：可选依赖（numpy / sherpa-onnx / sounddevice / modelscope / torch 等）绝不允许出现在 `server.py` 及后端导入链的顶层硬 import 中——`LLM.server` 必须能无条件 import、`lifespan` 必须能无条件启动。
- **降级模式（范例 `LLM/voice_api.py`）**：可选依赖在模块顶层逐个 `try/except` 引入，失败时置模块级标志 `_VOICE_AVAILABLE = False`，并把缺失项逐个收集进 `_MISSING_DEPS`（缺多个时别只报第一个）。
- **不可用时的行为**：
  - `start_*` 型启动钩子：`audit.log("voice_degraded", ...)` 记录 + `print("[WARN] ...")` 提示 + 返回 `None` 静默跳过，不影响 lifespan 其余步骤；
  - 查询类 API：返回 `{"ok": True, "status": "unavailable", ..., "reason": "缺少依赖：..."}`（`ok` 保持 True——服务健康 ≠ 功能可用，前端不会误判为后端故障）；
  - 写操作 API：返回 `{"ok": False, "error": "语音模块不可用（缺少依赖：...）"}`。
- **新功能引入可选依赖时遵循此模式**；能用 stdlib 就绝不引入外部依赖（如 `vectors.py` 零依赖向量检索）。

## 前端（frontend/）

- pnpm workspace（Vue3 + Vite + TS），三个包：
  - `packages/admin` — 管理端（PC 浏览器）：页签 = 监控总览/对话/记忆/提醒/工具日志/语音状态/设置；dev :5173。
  - `packages/kiosk` — 车载交互端（老人面前屏幕，无屏也能跑，语音主交互在后端闭环）：状态条/对话区/切换用户(锁定)/提醒/SOS/设置弹层；dev :5174。
  - `packages/shared` — 共享层：REST client（`src/api/`）+ SSE 事件唯一定义 `src/events.ts`（类型枚举 + `parseBusPayload`），admin/kiosk 都从它引入。
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
- 历史档案：`docs/superpowers/specs/2026-08-18-ai-chat-frontend-design.md` 等 8 月旧规格描述的是单文件 `UI/index.html` 时代的实现，仅作过程参考（其 `/api/chat` 请求体已过时，实际为 `{uid, message, thinking}`）。

## 固件（stm32/）

- `stm32/control/` — 主固件，**改动前必读** `stm32/control/AGENTS.md`（target_sources 注册、勿动 CubeMX 文件、10ms 周期约束等）。
- `stm32/led_test/` — 独立小型 CubeMX 测试工程，非活跃业务。

## 已知坑

- `.gitignore` 排除了 `.venv/`、`.env`、`LLM/data/*.db*`、`LLM/data/audit.jsonl`（运行时数据不入库）。
- `requirement.txt` 声明的语音依赖（numpy / sherpa-onnx / sounddevice / modelscope 等）在目标环境可能未装齐：新依赖记得固化进去，且后端必须容忍缺失、降级运行（见「系统稳健性」）。
- 后端 run 用包方式 `LLM.server:app`（`server.py` 里路径基于 `Path(__file__).parent.parent` 定位 `.env`）。
- **前端已在 2026-08-27 从 `UI/` 单文件迁至 `frontend/`**：改前端先看 `docs/superpowers/specs/2026-08-27-frontend-multi-end-design.md` 与 shared 的 events.ts；`UI(old)/`（git 跟踪）为旧实现参考；根目录 `UI/`、`Front/` 是空残留目录，勿当现役前端。
- **后端运行位置（2026-09-14 起）**：LLM 后端**默认跑在 PC 上**（板卡 RDK X5 性能有限，重活不下放板卡）。地图文件的真相仍在板卡 `ros2_car/maps/`，经 `MAPS_IO=ssh` 读写；板卡上跑后端时用 `MAPS_IO=local`。相关规格：`docs/superpowers/specs/2026-09-14-mapeditor-pixel-edit-design.md`。
- **地图像素修图入口**：`/mapeditor/pixel-editor.html`（第三期，改造自 GyroPalm/ROS-SLAM-Map-Editor，MIT）。保存前自动备份到 `maps/.backup/`（故 `GET /api/map/list` 必须排除该目录）；改完地图**必须重启导航才生效**（`~/tools/nav_screen.sh nav <地图名>`）。
