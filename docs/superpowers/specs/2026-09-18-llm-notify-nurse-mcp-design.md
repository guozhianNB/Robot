# 小车 LLM → 护士后台「传达信息」MCP 工具设计

> 日期：2026-09-18 ｜ 状态：设计已获用户批准（方案 A），待实现
> 范围：新增 MCP 服务器 `LLM/notice_mcp/`（1 个工具 `notify_nurse`）+ 接线 `conf.py` / `policy.py` / 审计脱敏 2 行 + 测试
> 关联：`docs/目标文档及说明/大模型端开发目标.md` 模块 11（报警模块）、`docs/superpowers/specs/2026-09-18-nurse-console-design.md`（通知中心 D4/D5/D6/D11）、`AGENTS.md`「系统稳健性」「关键约定 7」、`LLM/vision_mcp/`（MCP 样板）

## 1. 目标与背景

**要做的事：** 给跟老人说话的「小车」大模型一个**真的能把话传给护士**的工具。现状是它只会"说到"，做不到：

| 现状 | 位置 | 后果 |
|---|---|---|
| 人设红线写着「危险信号 → 明确说出『我这就去通知护士』」 | `LLM/agent/prompt/base.md:36` | **模型说了却没工具可调**，护士永远收不到 |
| 角色片段写着「危险信号先安抚再叫护士」 | `LLM/agent/prompt/elder.md:4` | 同上 |
| 需求文档早已要求「我帮您问护士，并把问题转达」 | `大模型端开发目标.md:382` | 转达通道缺失 |
| 通知只能来自 kiosk SOS / 提醒超时 / 外部 POST | `LLM/server.py:1192`、`LLM/agent/reminder.py` | 对话链路一条都进不去 |
| 护士台已有「小车」筛选页签（`source === "cart"`） | `frontend/packages/nurse/src/App.vue:68` | 位置本就给小车留着 |

**归属：** 需求文档模块 11 ——「独立于对话的『告警汇聚与转发』服务，接收各模块上报的紧急信息，第一时间传达给护士/护工」。本设计**不新建通知通道**，只把已有的免鉴权投递口 `POST /api/notifications`（规格 D4）包成模型可调用的工具。

## 2. 已确认决策记录

| # | 决策点 | 结论 |
|---|---|---|
| D1 | 实现形态 | **MCP 子进程 + HTTP 投递口**（方案 A，用户拍板）。新建 `LLM/notice_mcp/`：`notice_client.py`（纯 stdlib HTTP 业务，可脱离 MCP 单测）+ `notice_server.py`（`@server.tool` 注册）。写入仍走 `notify.ingest()` **唯一写入口** ⇒ 级别兜底 / 60s 去重合并 / 审计 / **SSE 实时广播**全部生效，护士台 5 秒内弹卡 |
| D2 | 为何不直调 / 不直写 | 本地工具（方案 B）不是 MCP、只在后端进程内可用；子进程直写 SQLite（方案 C）绕过 `notify` 的去重与广播 ⇒ 护士台没有实时卡（只能等 30s 轮询），且违反"notify 是唯一写入口"。**均否决** |
| D3 | 「子进程回调后端」自环 | **接受**，并说明与 `vision_client.py` 既有评注的区别：识图有本地可用路径（`vision` 包）故绕后端纯属自找麻烦；通知的真相只能由后端进程内的 `notify.ingest` 产出（SSE 广播是**进程内**总线），走 HTTP 反而是唯一不丢实时性的路。安全边界：① 请求只发生在**工具调用瞬间**，那时后端必然在监听；② 对话流是 sync 生成器交 Starlette 线程池（`LLM/server.py::chat_route`）跑的，**不占事件循环**，不会与自身请求死锁；③ 后端重启期间拉起的子进程**不做任何启动探测**，只在调用时返回 `ok:false` |
| D4 | 工具形状 | 单个工具 `notify_nurse(message, level="info", uid="")`，返回 **JSON 字符串**（`mcp_client.call_tool` 只读 `content[*].text`，返回 dict 上游会看到"无文本返回"）。`source` 固定 `"cart"`（对上护士台「小车」页签）、`type` 固定 `"message"`（标题缺省「通知」） |
| D5 | 权限 | elder / ward / admin **三层都给**。理由：老人说不舒服必须能转达；声纹识别失败会 fail-closed 落到 ward 层，那里堵死等于"老人求助喊不出来"（R3 精神）。代价＝未识别的说话人也能刷护士台（60s 去重兜底）；不想给就删 `policy.py` 里 ward 那一行 |
| D6 | 隐私 | `message` 按既有口径进工具日志与审计；但**闸门拒绝时参数必须脱敏**——现在 `LLM/agent/tools.py::_audit_args` 只对 `see_what` 做了这件事，本设计补一条同款，免得"被拒的私聊原文"进审计 |
| D7 | 明确不做 | 不给护士台加对话入口、不做微信推送、不做"护士已处理"回读工具、不改 `session`/`policy` 角色定义（R1–R5 红线区一行不改）、不新增任何第三方依赖 |

## 3. 架构与数据流

```
老人/现场                LLM 后端进程                            护士台
  │                        │                                      │
  │  说话（kiosk 语音/文字）│                                      │
  ├───────────────────────►│ chat_stream 工具循环                  │
  │                        │   └─ run_tool("notify_nurse", …)      │
  │                        │        └─ mcp_client（stdio 子进程）    │
  │                        │             │  LLM/notice_mcp/notice_server.py
  │                        │             │  └─ notice_client.push()（纯 stdlib urllib）
  │                        │             ▼
  │                        │  POST http://127.0.0.1:8000/api/notifications（免鉴权，D4）
  │                        │             ▼
  │                        │      notify.ingest()  ← 唯一写入口
  │                        │        ├─ 归一化/级别兜底/截断
  │                        │        ├─ 60s 去重合并（count+1）
  │                        │        ├─ db 落库 + 审计 notify_ingest
  │                        │        └─ bus.publish("notification", kind=…)
  │                        │             ▼
  └────────────────────────┴──── SSE /api/events ──────────────► 实时弹卡（+ critical 蜂鸣）
```

**跨机部署：** 子进程连的后端地址由 `NOTICE_BACKEND_URL` 决定（默认 `http://127.0.0.1:8000`）。将来 LLM 或小车侧进程不跟后端同机时，只改这一个环境变量即可，代码零改动。

## 4. 详细设计

### 4.1 `LLM/notice_mcp/notice_client.py`（纯 stdlib 业务层）

职责：把"推一条通知"做成一个**永不抛异常**的函数，不碰 MCP、不碰 stdio。

```python
BACKEND_URL = (os.environ.get("NOTICE_BACKEND_URL") or "http://127.0.0.1:8000").strip().rstrip("/")
TIMEOUT = float(os.environ.get("NOTICE_TIMEOUT_S") or 5.0)   # < MCP_TOOL_TIMEOUT(30)

def push(message, level="info", uid="", title="", source="cart", type="message") -> dict
```

- 用 `urllib.request`（标准库，AGENTS.md：能用 stdlib 就绝不引外部依赖）POST JSON 到 `{BACKEND_URL}/api/notifications`，体字段与 `NoticeIn` 对齐：`{source, type, level, uid, title, message}`。
- **归一化在客户端做一遍**（服务端仍会再兜一次，两层不冲突）：`message` 去空白，空则直接返回 `{"ok": False, "error": "要传达的内容不能为空"}` 不发请求；`level` 不在 `info/warning/critical` → `info`；`source`/`type` 空 → 兜底 `cart`/`message`。
- 返回：成功原样透传后端的 `{ok, id, deduped, level}`（并补 `detail` 人话摘要）；失败一律 `{"ok": False, "error": "人话原因"}`，覆盖：连不上（`URLError`）、超时（`socket.timeout`/`TimeoutError`）、HTTP 非 2xx（含 400 的 `detail`）、响应不是 JSON、其它任何异常。**绝不抛给上层**。
- `backend_url()` / `available()` 供调试与自检（不联网）。

### 4.2 `LLM/notice_mcp/notice_server.py`（MCP 注册层）

照抄 `LLM/vision_mcp/see_server.py` 的结构与降级口径：

```python
try:
    from mcp.server.mcpserver import MCPServer
except Exception as e:
    MCPServer = None; _INIT_ERR = str(e)

@server.tool(name="notify_nurse")
def notify_nurse(message: str, level: str = "info", uid: str = "") -> str
```

- docstring 就是模型看到的工具描述，必须写清 **何时用**（老人说不舒服/要找护士/问到用药你答不了要转达；巡房或任务异常要回报）、**参数语义**（`level` 三档，跌倒·呼救·胸痛用 `critical`；`uid` 不确定就留空、把老人的称呼写进 `message`）、**限制**（一条通知只说一件事；同一件事短时间重复报会自动合并；不要用它代替急停）。
- 工具函数**必须 return 字符串**（`json.dumps(..., ensure_ascii=False)`）。
- 日志写文件 + **stderr**，stdout 一个字都不许有（MCP 走 stdio）。
- `main()` 降级：`MCPServer is None` → `_log` + stderr + `sys.exit(2)`（`mcp_client` 记 `connect_error`，后端照常启动，只是没这个工具）。**后端不可达不算启动失败**（后端可能稍后才起），留到每次调用返回 `ok:false`。

### 4.3 接线（3 处既有文件，最小插行）

| 文件 | 改动 |
|---|---|
| `LLM/conf.py` | 新增一个常量 `NOTICE_BACKEND_URL`（env `NOTICE_BACKEND_URL` 可覆盖，默认 `http://127.0.0.1:8000`，去掉尾部 `/`）+ `MCP_SERVERS["notice"]` 一条：`{"command": _sys.executable, "args": [notice_server.py], "env": {"NOTICE_BACKEND_URL": NOTICE_BACKEND_URL}, "enabled": True, "roles": ["elder","ward","admin"]}` |
| `LLM/agent/policy.py` | `allowed_tools` 三层各加 `"notify_nurse"`（**白名单是天花板**：不加，服务器声明了 `roles` 模型也看不见） |
| `LLM/agent/tools.py` | `_audit_args()` 增加 `notify_nurse` 分支：拒绝时只留 `{"level": …, "has_uid": bool}`，**丢掉 `message` 原文** |
| `LLM/agent/chat.py` | 同族 `_tool_log_fields()` 保持记录 `message`（它本来就在对话历史里），**不改**；仅在文档里写明口径 |

> `env` 里给**具体值**而不是 `vision` 那种空串继承：`NOTICE_BACKEND_URL` 不是密钥，且 `conf` 导入时就已 `load_dotenv`，`.env` 里设的值在 conf 层就吃到了 —— 值只有一处真相，不必再走"运行时继承"。

### 4.4 闸门（两道，与既有工具同一把尺子）

1. **服务器级** `MCP_SERVERS["notice"]["roles"] = ["elder","ward","admin"]`；
2. **角色白名单** `policy.POLICY_DEFAULTS[role]["allowed_tools"]` 含 `notify_nurse`；
3. 外加全局总开关 `settings.mcp_enabled`（MCP 工具没有 per-tool 开关，见既有口径）。

任一道不过：`run_tool` 返回 `{"ok": False, "error": "当前身份不允许调用工具 notify_nurse"}` + 审计 `policy_deny`（参数走 4.3 的脱敏分支）。

## 5. 降级矩阵（AGENTS.md「系统稳健性」）

| 情况 | 行为 |
|---|---|
| 没装 `mcp` | 子进程 stderr + exit 2；后端照常起；`/api/tools` 里没有该工具、审计 `mcp_degraded` |
| `mcp_enabled=false` | 不起子进程（既有机制） |
| 后端没起 / 已重启中 | 子进程照常起（**启动不探网**），每次调用返回 `{"ok": false, "error": "连不上护士后台…"}`，模型据实回话，不编造"已通知" |
| 后端 400（`type` 空等） | 客户端不会发出去（本地归一兜底），真发生则透出 `detail` |
| 网络超时 | 5s 超时 → `ok:false`，不阻塞对话（MCP 调用总超时 30s） |

## 6. 测试（`LLM/tests/test_notice_mcp.py`）

- **client**：桩用 **127.0.0.1 随机端口**的 stdlib `http.server`（不碰真 8000，遵守测试红线）；覆盖 正常投递字段、空 `message` 不发请求、`level` 非法归一、URL 尾部斜杠拼接、后端 500、连接被拒、响应非 JSON、超时——**全部 `ok:false` 且不抛异常**。
- **server**：`build_server()` 注册的工具名与 schema（`mcp` 不可用时 skip）；工具函数返回的是 `str`。
- **conf**：`MCP_SERVERS["notice"]` 的 command（== `sys.executable`）/args/env/enabled/roles 断言（照 `test_vision_mcp_configuration`）。
- **闸门**：三层可见性（elder/ward/admin 均可见）；`mcp_enabled=false` 时 `policy_deny` 且审计参数**不含 message 原文**（照 `test_vision_policy_deny_audit_redacts_args_when_mcp_disabled`）。
- 全部用例**不得**有进程级外部副作用（不起真后端、不连板卡）。

## 7. 验收（需用户/真机）

1. 后端起（`uvicorn LLM.server:app --host 0.0.0.0 --port 8000`），管理端「设置」打开 `mcp_enabled`，**重启后端**（MCP 子进程在启动时拉起）。
2. `GET /api/tools` 里能看到 `notify_nurse`（`server: notice`）。
3. 等价 curl（不依赖模型，先验通道）：
   ```
   curl -X POST http://127.0.0.1:8000/api/notifications -H "Content-Type: application/json" \
        -d "{\"source\":\"cart\",\"type\":\"message\",\"level\":\"critical\",\"message\":\"张爷爷说胸口疼\"}"
   ```
   护士台 5 秒内出红卡 + 蜂鸣。
4. 对小车说"我胸口疼" → 护士台出卡；`LLM/notice_mcp/notice_mcp.log` 有调用记录；`LLM/data/audit.jsonl` 有 `notify_ingest`（`source=cart`）。
5. 停掉后端再让模型调用 → 返回 `ok:false`，模型如实说"暂时联系不上护士"，不编造已通知。

## 8. 已知限制

1. **`uid` 通常为空**：模型不知道 uid（System Prompt 不注入 uid 字面量），护士卡上的「姓名 · 床号」多半是空的 —— v1 用"把老人称呼写进 message"缓解。P1 改进路径：由后端在 `run_tool` 阶段把 `principal["uid"]` 注入 MCP 调用参数（要改 `mcp_client` 契约，属另一份规格）。
2. **未识别说话人也能上报**（ward 层，D5 有意为之），只有 60s 去重兜底，没有限流/签名（与通知中心规格 §9 同族）。
3. 通知内容经**免鉴权 SSE** 广播给局域网（既有暴露面，非本设计新增）。
4. 本工具只"传达"，**不代替急停**（`robot_stop`）与 kiosk SOS 按钮。
5. `message` 原文在**允许**路径下会进审计 `tool` 事件与工具日志（与既有口径一致：那句话本来就在对话历史里）；**被闸门拒绝**时才脱敏（§4.3）。与 `see_what` 的"prompt 一律不入日志"口径相反，是否统一留待后续权衡。

## 9. 独立审查与修复轮（2026-09-18）

独立审查子代理结论：**需修复后通过**。落地如下：

| 编号 | 发现 | 处置 |
|---|---|---|
| **M1** | **合并语义会静默吞掉"同分钟内第二条不同的事"**（安全相关）：去重键只有 `(source,type,uid)`，而本工具的 `uid` 通常为空 ⇒ **所有小车消息**在 60s 内塌成一条，且合并只 `count+1`、保留最早那条的**正文与级别**；实时广播却用新正文 ⇒ 护士台 ≤30s 后被列表轮询覆盖回最早那条，critical 计数不涨、重开页面不再蜂鸣 | **已修根因**（3 处后端）：`db.find_unacked_notification` 的合并键加 `body`；`db.bump_notification` 支持**只升不降**地提级别（`notify._higher_level` 算 max，词表留在 agent 层）；`notify.ingest` 合并时**广播与库里那一行对齐**。补 3 条用例（不同正文不合并 / info→critical 升红卡 / 广播与库一致），并保留"同正文仍合并"的防刷屏口径 |
| **M2** | 规格 §7 验收第 4 步与 README 承诺的"调用日志"没实现（`_notify_nurse` 全程无日志、异常也静默） | 已补：每次调用一行 `notify_nurse level=… has_uid=… ok=… deduped=… id=…`（**不记 `message` 原文**），异常分支也留痕（带异常文本） |
| S1 | `level` 大小写/近义词静默降级（模型写 `Critical`/`high` 反而落到最低档、不置顶不蜂鸣） | 已补 `notice_client.normalize_level()`：大小写/空白不敏感 + 常见近义词**往上归**（high/severe/urgent→critical，medium→warning） |
| S2 | 成功判定 `ok is True or "id" in result` 与"只认明确成功"的注释矛盾（`{"ok":false,"id":7}` 会被当成功 ⇒ 模型会对老人撒谎） | 已收紧为 `result.get("ok") is True`，补 3 种畸形响应用例 |
| S3 | `NOTICE_TIMEOUT_S` / `NOTICE_MCP_LOG` 在正常拉起路径不可达（conf 只传了 URL） | 已补 `conf._notice_mcp_env()`（照 `_vision_mcp_env()` 惯例，只在环境变量真设过时传），补用例 |
| S4 | 缺两条防退化用例 | 已补：`notify_nurse` 不在本地工具注册表（否则 MCP 版会被静默遮蔽）、`MESSAGE_MAX == conf.NOTIFY_BODY_MAX` |
| S5 | 闸门拒绝用例只断言子串"不允许"，与总开关分支文案撞车（会因错误原因变绿） | 已收紧为精确文案 + 审计 `reason == "tool_roles_mismatch"` |
| S6 | `_post` 兜底只留异常类名，叠加 M2 后线上无从排查 | 已带 `str(e)[:120]` |
| 死代码 | `main()` 里 `if server is None: sys.exit(2)` 与其它降级分支不一致 | 已改为记日志 + 写 stderr 再退 |
| 复审后残余清理 | 二轮复审（定向）裁定**通过**，另列 8 条非阻断残余 | 已清：`notify.py`/`db.py`/护士台规格两处旧口径注释、`docs/log.md` 的 `urgent→info` 笔误；**工具回话的 `level` 改为回显后端生效级别**（合并升级时不再自说自话）+ 用例；`_higher_level` 先把未知级别归一到词表；`NOTICE_MCP_LOG=""` 不再让文件日志静默失效；补"竞态回落的新行不许继承旧行级别"用例（钉住 `else: eff = lvl`）。**未采纳**：`normalize_level` 收录 `alert/error` 等词（规格口径是"未知兜底 info"，加词属猜测，留给后续按实测补） |

## 10. 实现台账与偏差

**状态：已实现**（2026-09-18，分支 `feature/nurse-console`，BASE `1bad0c2`）。

| 规格条目 | 产物 |
|---|---|
| §4.1 业务层 | `LLM/notice_mcp/notice_client.py`（`push()` / `_post()` / `_resolve_backend()` / `backend_url()` / `endpoint()`） |
| §4.2 MCP 层 | `LLM/notice_mcp/notice_server.py`（`build_server()` 注册 `notify_nurse`） |
| §4.3 接线 | `LLM/conf.py`（`NOTICE_BACKEND_URL` + `_notice_mcp_env()` + `MCP_SERVERS["notice"]`）、`LLM/agent/policy.py`（ward/elder 白名单；admin 是 `None` 无需改）、`LLM/agent/tools.py::_audit_args` |
| §8 M1 根因修复（跨批次） | `LLM/store/db.py`（`find_unacked_notification` 合并键加 `body`、`bump_notification` 支持提级别）、`LLM/agent/notify.py`（`_higher_level` + 广播与库对齐）、`LLM/tests/test_notify.py`（+2 例净增）、护士台规格 D5/§4.2 修订 |
| §6 测试 | `LLM/tests/test_notice_mcp.py`（31 例）+ `LLM/tests/test_policy_roles.py`（断言随策略演进，抽 `WARD_TOOLS` 常量） |
| 文档 | `LLM/notice_mcp/README.md`、`AGENTS.md`（树/notify 条目/文档导航）、`docs/log.md` 2026-09-18 条目、`.gitignore`（`/LLM/notice_mcp/*.log`） |

**验证（本机，2026-09-18，含复审后的残余清理）：** `pytest LLM/tests` = **4 failed / 233 passed / 135 errors** vs 基线（`git worktree` 于 `1bad0c2` 独立重跑）**4 failed / 199 passed / 135 errors**：失败集合逐条一致（`test_policy_tools`×2、`test_settings_roles::test_new_float_setting_roundtrips_as_float`、`test_worker_events::test_speech_publishes_recognized`，均为既有的跨文件 `db.DB_PATH` 顺序污染/沙箱环境红态），净增 34 例全绿（新文件 31 例 + `test_notify.py` 净增 3 例）。

**端到端实测（临时后端 8099 + 独立库 `.tmp/e2e_notice.db`，不碰用户的 8000 与真 `brain.db`）：**
- `notice_client.push("张爷爷说胸口疼，想找护士", level="critical", uid="elder_001")` → `{"ok": true, "id": 1, "level": "critical", "deduped": false}`；库里落成 `source="cart" / type="message" / title="通知" / uid_name="张建国 · 3-12"`（姓名·床号按 `notify.uid_name()` 解析）。
- 60 秒内重复投递同一条 → `deduped=true`、**仍是同一行、`count` 自增**（护士台不会刷屏）。
- `push("   ")` → 本地就返回 `ok:false`，**没有发出请求**（库里没有多出空行）；`level="bogus"` → 归一成 `info`（`"Critical"/"high"` 之类按 §9 S1 往上归）。
- `GET /api/notifications` 返回 `{unread: 2, critical: 1}`，与投递一致。
- 订阅 `GET /api/events` 收到 `data: {"type": "notification", "source": "cart", "kind": "message", …}` —— **护士台实时弹卡依赖的广播确实发出**（`kind` 而非 `type`，符合规格 D6）。
- 子进程冒烟（`Start-Process` 起 `notice_server.py`，stdout/stderr 落文件）：**stdout 一个字节都没有**（不污染 MCP stdio），stderr/日志有 `robot-notice MCP server 就绪（护士后台 http://127.0.0.1:8000，超时 5s）`。
- **未能在此沙箱内做的**：真 MCP stdio 握手（用 `mcp` SDK 的 `stdio_client` 拉起子进程）在本机沙箱被拒（`WinError 5`，沙箱不允许子进程管道）；该路径由 `LLM/tests/test_notice_mcp.py::test_tool_call_through_mcp_layer_returns_json_text`（进程内 `MCPServer.call_tool`）与 `vision_mcp` 同款样板兜住，真机验收时一并确认。

**偏差（2 条，均已在上文修正或说明）：**
1. §4.1 提到的 `available()` **未实现**：本能力没有"本地依赖缺失"可判（只剩 stdlib），而启动又不许探网，故只留 `backend_url()` / `endpoint()` 两个调试入口。
2. §4.3 原写 `env` 传空串走"运行时继承"，落地改为**传 `conf.NOTICE_BACKEND_URL` 的具体值**（该值非密钥，`conf` 导入时已 `load_dotenv`，值只需一处真相）。

**未验（需用户真机）：** 管理端设置页打开 `mcp_enabled` → 重启后端 → `GET /api/tools` 出现 `notify_nurse`；对小车说"我胸口疼" → 护士台出 critical 卡；后端停掉时调用返回 `ok:false`。
