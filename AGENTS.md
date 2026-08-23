# AGENTS.md

养老陪护巡逻机器人（云-边-端）主仓库。三大子系统 + 文档：

```
LLM/     Python FastAPI 后端 —— 大模型端"大脑与嘴"（最活跃，优先看这里）
UI/      单文件 HTML 前端控制台
stm32/   底盘下位机固件（STM32F103ZETX，C/CMake/HAL）→ 见 stm32/control/AGENTS.md
docs/    需求/教程/接口契约/开发日志
```

## 板卡连接
- 通过ssh连接 `ssh sunrise@100.65.82.93`（Tailscale，异地可用）
- sudo 免密可用，但禁止使用 sudo 安装/改系统，除非用户明确批准。
- sunrise账户密码：`sunrise`

## 快速上手

- **后端启动**（项目根目录）：
  ```
  .venv\Scripts\python.exe -m uvicorn LLM.server:app --host 0.0.0.0 --port 8000
  ```
- **前端**：浏览器直接打开 `UI/index.html`（纯原生 JS 单文件，零构建，后端 CORS 全开）。
- **虚拟环境**：`.venv/`（Windows 用 `Scripts/python.exe`）。核心依赖 openai / fastapi / uvicorn / pydantic / python-dotenv；语音链路额外依赖 numpy / sherpa-onnx / sounddevice / modelscope / torch（见 `requirement.txt`）。**注意：`requirement.txt` 声明 ≠ 环境已装齐，后端必须容忍可选依赖缺失、降级运行（见「系统稳健性」）。**
- **配置**：API key 在根 `.env`（`DEEPSEEK_API_KEY`），`LLM/conf.py` 用 `load_dotenv(BASE_DIR/".env")` 加载。

## 架构与模块（LLM/ 后端）

- `server.py` — FastAPI 入口：CORS 全开，`lifespan` 启动 `db.init_db()` → `_seed_demo()` → `reminder.start()` → `bus.start_drain()` → `voice_api.start_voice()`；全局 OpenAI 客户端（DeepSeek），`_bg` 线程池跑后台任务。
- `chat.py` — 对话编排：`chat_stream()`（SSE 生成器，工具循环最多 2 轮）、`route_thinking()`（思考路由）、`build_system()/build_messages()`（System Prompt + RAG + 滚动窗口）、`llm_json()`。
- `conf.py` — **集中配置**。路径、`DEFAULT_SETTINGS`、`THINKING_KEYWORDS`、`HISTORY_WINDOW`、`MEMORY_RULES`、`MODEL`、超时等。**改参数先来这里**。
- `db.py` — SQLite 数据层（`LLM/data/brain.db`，WAL + 线程锁）。函数命名 `get_*`/`add_*`/`set_*`/`update_*`/`delete_*`/`upsert_*`，协程侧用 `asyncio.to_thread`。
- `memory.py` — RAG 记忆 + 半自动沉淀：`recall()`、`note_turn()`、`consolidate()`、`suggest_from_chat()`。**红线：`MEDICAL_KEYWORDS` 命中拒绝写入**。
- `reminder.py` — 独立线程定时调度（15s tick），状态机 `pending→triggered→confirmed/unconfirmed/missed`。
- `bus.py` — SSE 事件总线，`publish()`（任意线程）→ asyncio 扇出订阅者。
- `tools.py` — 联网工具（OpenAI function-calling 格式）：`web_search` / `get_news`，`run_tool()` 分发，过滤 `BANNED` 惊悚词。
- `log.py` — 审计日志（JSONL 落 `LLM/data/audit.jsonl`，线程锁追加）：`log(event, **fields)`。
- `vectors.py` — 零依赖轻量向量检索（字符 n-gram 哈希 + TF + L2 + 余弦）。
- `voice/` + `voice_api.py` — 语音链路（唤醒/识别/播报/声纹，**可选能力**）：外部依赖缺失时整体降级，后端照常启动，见「系统稳健性」。

## API 端点（server.py）

`GET /api/health` ｜ `POST /api/chat`（SSE 流式，体 `{uid, message, thinking:"auto|on|off"}`）｜ `GET|DELETE /api/chat/history` ｜ `GET|POST /api/profiles` ｜ `GET|POST /api/memories` + `/confirm` `/reject` `/delete` `/suggest` ｜ `GET /api/context` ｜ `GET|POST /api/reminders` + `/confirm` `/delete` ｜ `GET /api/tools/log` ｜ `GET|POST /api/settings` ｜ `GET /api/events`（SSE 广播）｜ `GET /api/tools` ｜ `GET /api/voice/status` ｜ `POST /api/voice/enroll` ｜ `GET /api/voice/speakers`（语音依赖缺失时降级返回，见「系统稳健性」）

## 关键约定（改动前必读）

1. **新增功能三件套**：路由放 `server.py`、配置/常量放 `conf.py`、数据操作放 `db.py`。
2. **审计贯穿**：几乎所有状态变更都要 `log.py::log()` 落审计（事件类型：`chat`/`memory_change`/`reminder`/`tool`/`settings`/`alarm`）。
3. **错误处理**：异常捕获后 `audit.log(...)` + 返回 `{"ok":False,...}` 或 SSE `error` 事件；后台任务异常吞掉防崩溃。
4. **SSE 事件协议两端强耦合**：事件类型（`meta`/`reasoning`/`content`/`tool_start`/`tool_result`/`done`/`error`）在 `chat.py::chat_stream` 与 `UI/index.html::send` 同时维护，改类型要同步。
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

## 前端（UI/）

- `index.html` = 单文件 SPA（5 页签：对话/记忆/提醒/工具日志/设置），纯原生 JS + CDN（marked/highlight.js）。`chat.html` 仅 `<meta refresh>` 跳转旧页。
- 结构：`<style>` 顶部、`<script>` 底部，深色主题，`<section class="page">` + 页签切换。
- SSE：`send()` 用 `fetch` + `ReadableStream` 按 `\n\n` 切事件；`/api/events` 用 `EventSource` 实时 toast（`connectEvents()` 自动重连）。

## 文档导航（docs/，链接勿复制正文）

- `docs/2.pre/大模型端开发目标.md` — **大模型端需求文档**（能力总览/模块/安全红线/决策表），实现 LLM/ 时以此为准。
- `docs/2.pre/USB车控接口.md` — 地瓜派↔STM32 USB CDC 帧协议（v1.0），配套 `docs/2.pre/usb_chassis_demo.py`。
- `docs/2.pre/ROS底盘接口需求.md` — 大模型端↔方向二（ROS2/SLAM）对接契约（draft）。
- `docs/2.pre/log.md` — **开发日志**（按日期追加，记录了各模块实现细节）。
- `docs/1.pre/` — 第一阶段学习教程/硬件规划（PyTorch、ROS2、学习路线、硬件清单）。
- `docs/superpowers/specs/2026-08-18-ai-chat-frontend-design.md` — 前端设计规格（注意：其 `/api/chat` 请求体文档已过时，实际为 `{uid, message, thinking}`）。

## 固件（stm32/）

- `stm32/control/` — 主固件，**改动前必读** `stm32/control/AGENTS.md`（target_sources 注册、勿动 CubeMX 文件、10ms 周期约束等）。
- `stm32/led_test/` — 独立小型 CubeMX 测试工程，非活跃业务。

## 已知坑

- `.gitignore` 排除了 `.venv/`、`.env`、`LLM/data/*.db*`、`LLM/data/audit.jsonl`（运行时数据不入库）。
- `requirement.txt` 声明的语音依赖（numpy / sherpa-onnx / sounddevice / modelscope 等）在目标环境可能未装齐：新依赖记得固化进去，且后端必须容忍缺失、降级运行（见「系统稳健性」）。
- 后端 run 用包方式 `LLM.server:app`（`server.py` 里路径基于 `Path(__file__).parent.parent` 定位 `.env`）。
