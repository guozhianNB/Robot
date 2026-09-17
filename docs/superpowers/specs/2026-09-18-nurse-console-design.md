# 护士台（小白友好告警面板）+ 后端通知中心设计

> 日期：2026-09-18 ｜ 状态：待用户审查 ｜ 范围：后端新增通知中心（1 模块 + 1 表 + 5 路由 + 2 处接线）+ 新增第四个前端包 `packages/nurse`
> 关联：`docs/目标文档及说明/大模型端开发目标.md` 模块 11（报警模块）、`docs/superpowers/specs/2026-08-27-frontend-multi-end-design.md`（D3 端形态 / D6 alarm 端点先行）、`docs/superpowers/specs/2026-09-14-layered-user-roles-design.md`（R1–R5 红线）、`AGENTS.md` 关键约定 1–7

## 1. 目标与背景

**要做的事：** 给养老院护士一个**只看通知**的极简面板——小车回报了什么、哪位老人触发了告警、哪条提醒没人确认；护士看一眼、点一下"处理了"，其它什么都不用做。

**为什么卡点不在前端：** 现状核查（2026-09-18）发现系统里**根本没有"通知"这个实体**：

| 现状 | 位置 | 后果 |
|---|---|---|
| `POST /api/alarm` 只做「写一行审计 + `bus.publish` 广播一次」 | `LLM/server.py:1088` | 页面没开着，这条告警**永久丢失** |
| `reminders` 是给**老人**到点播报的提醒 | `LLM/agent/reminder.py` | 不是护士待办；超时未确认只广播一条 warning |
| 小车（ros2_car）**没有任何向后端回报的通道** | `LLM/maps/roslink.py` 只订阅 `/amcl_pose`、`/map` | "巡房完成/到达 301"这类回报无处可进 |
| 主后端 8000 无 `/api/robot/pose` | 位姿接口在编辑器进程 8010 | 面板拿不到小车位置（本版不做位置） |

**需求文档早已预留这个位置**（模块 11）："独立于对话的『告警汇聚与转发』服务，接收各模块上报的紧急信息，第一时间传达给护士/护工。" 上报体格式（模块 11 原文）：

```json
{ "source": "llm", "level": "critical", "uid": "elder_001",
  "type": "sos", "message": "老人说胸口疼", "timestamp": "2026-08-18T10:00:00" }
```

本设计就是**模块 11 的落地**：后端补"汇聚 + 保存 + 转发"，前端补"护士看的那个页面"。

**需求文档对应条目：** 模块 11「报警模块」🔴 高；总表 516 行 `- [ ] 报警模块：/api/alarm 端口 + 告警列表页 + 微信推送` 中的前两项（微信推送仍不做）。

## 2. 已确认决策记录

| # | 决策点 | 结论 |
|---|---|---|
| D1 | 端形态 | 新增**第四个前端包** `frontend/packages/nurse`，由主后端 8000 挂 `/nurse`；`admin`/`kiosk`/`mapeditor` 一律不动 |
| D2 | 护士身份（**2026-09-18 二次修订，见 D11**） | 初版＝**复用管理台口令**（`X-Surface: admin`）。因管理员会话是 300s 绝对超时、无续期，被动挂着的护士台每 5 分钟被弹回口令页；用户 2026-09-18 拍板「**护士台不再需要登录、也不会被弹**」→ 改为 **D11**。**始终不变**：不新增护士角色、不动 `session.derive_role` / `policy.POLICY_DEFAULTS` / `X-Surface` 取值域（R1–R5 红线区一行不改） |
| D3 | 通知持久化 | 新增 `notifications` 表作**唯一存储**；`/api/alarm` 的既有契约（审计 + 广播）**保持不变**，只增加"落库"这一路 |
| D4 | 上报口权限 | `POST /api/notifications` **不校验身份**（模块 11 原文"任何模块发现异常都往该端口 POST"，且雷达/视觉未必持有口令），但每次上报写审计；`ack` / `ack-all` / `DELETE` / `GET` 必须 admin |
| D5 | 防刷屏 | 同 `(source, type, uid)` 且**尚未处理**的通知，`NOTIFY_DEDUP_S`（默认 60s）内再次上报 → 合并为一条、`count + 1`、刷新 `last_at`，不新增行 |
| D6 | 总线事件 | 新增 `notification`（新通知/合并）与 `notification_ack`（已被处理，多端同步）。**payload 里的一级键用 `kind`，绝不能用 `type`**——`bus.publish` 内部是 `{"type": event_type, **payload}`，payload 里的 `type` 会把事件类型覆盖掉（`/api/alarm` 正是因此才叫 `alarm_type`，见 `LLM/server.py:1096` 注释） |
| D7 | 视觉尺度 | **不做大按钮、不放大字号**。用户原话："护士端就不用大按钮了吧，人家护士眼睛又不瞎"→ 按普通 PC 控制台尺寸（正文 14–15px、按钮 padding 6px 14px），浅色主题、零术语即可 |
| D8 | 监听地址 | **必须监听 0.0.0.0**。用户原话："这个前端一定要放在 0.0.0.0 上，这样局域网内 PC 就能访问"→ 生产由后端 8000 托管（启动即 `--host 0.0.0.0`）；**dev server 必须显式 `server.host: true`**（Vite 默认只绑 localhost，这是局域网打不开的常见原因） |
| D9 | 面板职责 | **只读 + 确认**：拉列表、订阅事件、标记已处理。不发消息、不操作小车、不派车、不改任何设置 |
| D10 | 明确不做 | 地图编辑器、老人注册、记忆、设置、工具日志、身份与权限、对话、微信推送、小车位置地图——一个都不放进护士台（用户原话点名了前两个） |
| D11 | 护士台免登录（2026-09-18 二次修订，取代 D2） | 用户原话：「**护士台不再需要登录、也不会被弹**」。落地＝① 前端**去掉登录门**，打开 `/nurse` 直接是通知列表；② 后端三条读/确认端点 **`GET /api/notifications`、`POST /{nid}/ack`、`POST /ack-all` 改为免鉴权**（与投递口同口径）；③ **`DELETE /api/notifications/{nid}` 仍仅管理员**（删记录是数据损失，不放宽）；④ **管理台 `/admin` 的口令门一点不放宽**（不采用"关口令门"的全局做法——那会让局域网内谁都能进管理台）。**仍不动** `session`/`policy`/`X-Surface` 取值域（不新增 surface、不新增角色） |

## 3. 架构与数据流

```
                 写入方（谁产生通知）
  ┌──────────────┬──────────────────┬───────────────────┐
  │ kiosk SOS    │ 任意模块/小车     │ reminder 超时      │
  │ POST /api/alarm│ POST /api/notifications│ _escalate()  │
  └──────┬───────┴────────┬─────────┴─────────┬─────────┘
         │                │                   │
         └────────────────┴───────────────────┘
                          ▼
              LLM/agent/notify.py :: ingest()
       ┌──────────────────────────────────────────────┐
       │ 1. 归一化（级别兜底 / 截断 / 时间戳）           │
       │ 2. 去重合并（同 key 未处理且 60s 内 → count+1） │
       │ 3. store/db.py 落库 notifications             │
       │ 4. core/log.py 写审计                         │
       │ 5. core/bus.py publish("notification", kind=… )│
       └──────────────────────────────────────────────┘
                          ▼
        bus.publish → GET /api/events (SSE 扇出)
                          ▼
   ┌───────────────────────────────────────────────────┐
   │ packages/nurse（护士台，局域网 PC 浏览器）           │
   │  实时：SSE notification / notification_ack         │
   │  兜底：每 30s（断线时 5s）GET /api/notifications    │
   │  操作：POST /{id}/ack —— 其他屏幕立刻同步           │
   └───────────────────────────────────────────────────┘
```

**两条通道职责分明**（沿用 2026-08-27 规格 §1 的口径）：REST 用于"读列表 + 确认"，SSE 用于"状态变化广播"。面板不依赖 SSE 也能工作（轮询兜底）。

## 4. 后端设计

### 4.1 数据表（`LLM/store/db.py`）

```sql
CREATE TABLE IF NOT EXISTS notifications (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  level      TEXT NOT NULL DEFAULT 'info',   -- info / warning / critical
  source     TEXT NOT NULL DEFAULT '',       -- kiosk / llm / vision / radar / cart / reminder / system
  type       TEXT NOT NULL,                  -- sos / fall / … / task_done / patrol_done / low_battery …
  uid        TEXT DEFAULT '',                -- 相关老人 uid，可空（小车类通知常常无老人）
  title      TEXT DEFAULT '',                -- 短标题（缺省时按 type 生成中文）
  body       TEXT DEFAULT '',                -- 正文，截断 500
  ref        TEXT DEFAULT '',                -- 外部关联键（如 rid:12 / task:7），便于追溯
  count      INTEGER NOT NULL DEFAULT 1,     -- 去重合并计数
  created_at TEXT NOT NULL,                  -- 首次上报时间
  last_at    TEXT NOT NULL,                  -- 最近一次上报/合并时间
  ack_at     TEXT DEFAULT '',                -- 处理时间（空 = 未处理）
  ack_by     TEXT DEFAULT ''                 -- 处理人标识（admin / 口令来源）
);
CREATE INDEX IF NOT EXISTS idx_notifications_created ON notifications(created_at DESC);
```

- 建表写进 `init_db()` 的 `executescript` 列表；后续加列走既有 `_ensure_columns()`。
- **不建索引在 `ack_at` 上**：列表一次只取最近 50 条，量级可控（YAGNI）。
- **保留期**：`notify.prune()` 在 `lifespan` 启动时调**一次**，删 `NOTIFY_KEEP_DAYS`（默认 30 天）之前的**已处理**通知；未处理的一律不删（漏掉未处理的通知比留垃圾更糟）。**不做定时清理任务**（YAGNI：一次启动清一次足够）。

### 4.2 模块 `LLM/agent/notify.py`（新）

分层归属：**agent 层**（需要 `store/db.py` + `core/bus.py` + `core/log.py`），符合 `core → store → agent → server` 单向依赖，不碰 server。

```python
LEVELS = ("info", "warning", "critical")
# type → 缺省级别（救命的事不由上报方自己说了算，服务端兜底）
DEFAULT_LEVEL = {"sos": "critical", "fall": "critical", "help": "critical",
                 "health": "warning", "no_activity": "warning",
                 "reminder_unconfirmed": "warning", "reminder_missed": "warning",
                 "low_battery": "warning", "offline": "warning", "task_failed": "warning"}
TITLES = {…}          # type → 中文标题（sos=紧急呼叫 / fall=疑似跌倒 / task_done=任务完成 / patrol_done=巡房完成 / arrived=已到达 …）
TITLE_FALLBACK = "通知"

def ingest(source: str, type: str, *, level: str = "", uid: str = "", title: str = "",
           body: str = "", ref: str = "", ts: str = "") -> dict
    """唯一写入口：归一化 → 去重合并 → 落库 → 审计 → 广播。返回 {ok, id, deduped, level}。"""

def list_notices(state: str = "all", limit: int = 50, before_id: int = 0) -> list[dict]
def counts() -> dict          # {"unread": n, "critical": n}
def ack(nid: int, by: str = "admin") -> bool
def ack_all(by: str = "admin") -> int
def remove(nid: int) -> bool
def prune(days: int = 0) -> int
```

**归一化规则（纯函数，好测）：**
- `level` 非法或缺失 → 查 `DEFAULT_LEVEL`，再兜底 `info`。
- `type` 缺失/空 → `ValueError`（路由层转 400）。
- `title` 缺省 → `TITLES.get(type, TITLE_FALLBACK)`。
- `body` 截断 500 字符、`title` 截断 80、`ts` 非法或为空 → 服务器当前时间（`db.now_iso()`）。
- `uid` 不校验存在性（上报方可能是视觉/雷达，未必有档案）；列表返回时由路由层补 `uid_name`。

**去重合并：** 查最近 `NOTIFY_DEDUP_S` 秒内、`ack_at=''`、`(source, type, uid)` 相同的行；命中则 `count+1`、`last_at=now`（**不改 `body`**——保留最早那条原文，避免被后续嘈杂文本覆盖），仍然广播一次 `notification`。

### 4.3 路由（`LLM/server.py`）

| 方法 | 路径 | 身份 | 说明 |
|---|---|---|---|
| POST | `/api/notifications` | **免鉴权**（D4） | 通用上报口，小车/视觉/雷达/任何模块。体见 §4.4；返回 `{ok, id, deduped}` |
| GET | `/api/notifications` | **免鉴权**（D11） | `?state=all\|unread&limit=50&before_id=`；返回 `{ok, items, counts}`，`items[].uid_name` 由 `db.get_profile(uid)` 补齐（姓名 + 床号） |
| POST | `/api/notifications/{nid}/ack` | **免鉴权**（D11） | 标记已处理；广播 `notification_ack` |
| POST | `/api/notifications/ack-all` | **免鉴权**（D11） | 一次全标已处理；返回 `{ok, acked}`；广播 `notification_ack`（`all: true`） |
| DELETE | `/api/notifications/{nid}` | admin（**不放宽**） | 删单条（误报清理）——删记录是数据损失，仍要口令 |

**`/api/alarm` 改造（契约不变）：** 保留现有 `audit.log("alarm", …)` 与 `bus.publish("alarm", …)`（kiosk 的 toast 依赖后者），**在其后追加**一次 `notify.ingest(source="kiosk", type=a.type, uid=a.uid, body=a.message)`。`source` 默认 `kiosk`，请求体可带 `source` 覆盖（视觉/雷达将来直接发 `/api/notifications`，不必挤 `/api/alarm`）。

**`reminder._escalate()` 接线：** 在现有 warning 广播之后追加 `notify.ingest(source="reminder", type="reminder_unconfirmed", level="warning", uid=rem["uid"], title=f"提醒未确认：{rem['title']}", ref=f"rid:{rem['id']}")`。⚠️ 该方法原实现里**没有** `settings` 的 `alarm_enabled` 之外的门；接线必须放在 `bus.publish("alarm", …)` **同级**（不要放进 `if settings.get("alarm_enabled")` 里，否则关掉告警广播的部署会连通知一起丢——通知是给护士看的记录，不是告警播报）。

### 4.4 上报体（对齐模块 11）

```json
{ "source": "cart", "level": "info", "type": "task_done", "uid": "",
  "title": "巡房完成", "message": "1–3 层共 12 间，全部正常",
  "timestamp": "2026-09-18T10:00:00", "ref": "task:7" }
```

- **必填**：`type`。其余全可省（省了走兜底/默认）。
- 字段名对齐模块 11 原文（`source/level/uid/type/message/timestamp`），额外两个可选键 `title`、`ref`。
- **字段映射**：请求体的 `message` → 表的 `body` 列（对外沿用模块 11 的命名，对内用 `body`）；`timestamp` 只作为**上报方声称的时间**，落库以服务器时间为准，不采信外部时间（避免时钟错乱排序错）。
- 小车侧将来接线的目标是**一条 curl/POST 就能用**：

```bash
curl -X POST http://<PC-IP>:8000/api/notifications \
  -H "Content-Type: application/json" \
  -d '{"source":"cart","type":"patrol_done","message":"巡房完成：1–3 层 12 间全部正常"}'
```

### 4.5 总线事件（`shared/src/events.ts` 必须同步）

```ts
{ type: "notification",      id, level, source, kind, uid, uid_name, title, body, count, last_at }
{ type: "notification_ack",  id?, all?, by }
```

⚠️ **`kind` 是通知类型（sos/task_done…），不是 `type`**（D6 陷阱）。`uid_name` 一并广播，免得前端再拉一次档案。

**合并（去重命中）时广播的是"本次" `title`/`body`，而库里保留"最早"那条原文**——这是有意的：实时卡片要显示最新描述（例如同一故障的最新细节），留痕要保留原始上报文本。因此实时 toast 与列表正文可能不同；30s 兜底轮询会以后端为准覆盖本地。

### 4.6 审计与配置

- 审计事件：`notify_ingest`（含 source/type/level/uid/id/deduped）、`notify_ack`（id/by/all）、`notify_delete`（id/by）。模块 11 要求告警"强制可追溯"，所以上报与处理**两头都留痕**。
- `LLM/conf.py` 新增：`NOTIFY_DEDUP_S = 60`、`NOTIFY_KEEP_DAYS = 30`、`NOTIFY_LIST_LIMIT = 50`、`NOTIFY_BODY_MAX = 500`。

## 5. 前端设计（`frontend/packages/nurse`）

### 5.1 工程骨架

```
packages/nurse/
  package.json          name=nurse，dev/build（无 test 脚本，沿用 admin/kiosk 现状）
  index.html            <title>护士台</title>
  vite.config.ts        base:"/nurse/"、server.host:true（0.0.0.0）、port 5176、/api 代理 8000、shared alias
  tsconfig.json
  src/main.ts
  src/App.vue                      壳：登录门 + 顶栏 + 筛选 + 列表 + 断线红条
  src/components/NoticeCard.vue    单条通知（色带 / 标题 / 老人 / 正文 / 时间 / 「处理了」）
  src/lib/relativeTime.ts          "3 分钟前"
  src/lib/beep.ts                  WebAudio 合成提示音（critical 用，不放音频文件）
```

### 5.2 页面

```
┌──────────────────────────────────────────────────────────┐
│ 护士台        14:32:07   ● 实时在线   未处理 3            │
├──────────────────────────────────────────────────────────┤
│ [全部] [未处理] [告警] [小车回报]                          │
├──────────────────────────────────────────────────────────┤
│ ▌🔴 张建国 · 301床                              3 分钟前  │
│   紧急呼叫                                                │
│   老人说胸口疼                            [处理了]        │
│ ▌🟠 巡房完成                                    12 分钟前 │
│   1–3 层共 12 间，全部正常                [处理了]        │
│ ░ 提醒未确认：吃降压药 · 103床               25 分钟前     │  ← 已处理：整卡变灰沉底
└──────────────────────────────────────────────────────────┘
```

- **排序**：未处理全部在上——先按级别（critical → warning → info），同级按 `last_at` 倒序；已处理的沉到底部按 `ack_at` 倒序。
- **级别色带**：critical 红 `#dc2626` / warning 橙 `#d97706` / info 蓝灰 `#64748b`；已处理卡片整体 60% 透明。
- **合并计数**：`count > 1` 时标题右侧显示 `×3`（一眼看出"这条刷了 3 次"）。
- **「处理了」按钮**：常规尺寸（D7），点击 → `POST /{id}/ack` → 本地立刻移入已处理 + 广播让其他屏幕同步。
- **筛选**：`全部` / `未处理`（`state=unread`）/ `告警`（level ∈ critical,warning）/ `小车回报`（source=cart）。纯前端过滤，不发新请求。
- **顶栏**：实时时钟（1s tick）、`● 实时在线` / `○ 连接已断`、未处理计数徽章。
- **提示音**：收到 `level=critical` 且未处理的通知 → `beep()` 两声；`document.title` 加前缀 `(3) 护士台`，处理完清零。
- **断线降级**：SSE `onerror` → 3s 自动重连（沿用 admin 的 `connect()` 逻辑），同时切到**5 秒轮询**兜底，顶栏挂红条"实时连接断开，正在自动刷新"；在线时保留 30s 兜底轮询（防 SSE 静默丢帧）。
- **无登录门**（D11，2026-09-18 二次修订）：打开 `/nurse` 直接就是通知列表，**不调用** `login`/`getSessionUser`、**不连会话**、没有"会话过期"概念——因此也不存在被弹回登录页的问题。原设计（复用管理台口令 + 判据 `role !== "admin"`）已作废；`X-Surface` 头在通知三端点上不再有意义（后端不读 principal）。
- **空态**：无数据 → "目前没有通知"；拉取失败 → 大白话提示（不把原始错误串上屏，也不静默）。

### 5.3 shared 层新增

- `shared/src/api/notifications.ts`：`listNotices({state, limit, before_id})` / `ackNotice(id)` / `ackAll()` / `removeNotice(id)` / `ingestNotice(payload)`（后者给将来的小车侧脚本/前端调试用）。
- `shared/src/events.ts`：加 §4.5 两个接口 + `KNOWN_TYPES`（**这是 SSE 协议的唯一定义处，后端 publish 点与它必须同步**）。

## 6. 部署（0.0.0.0 / 局域网可达）

| 场景 | 命令 | 访问地址 |
|---|---|---|
| 生产（推荐） | 项目根 `.venv\Scripts\python.exe -m uvicorn LLM.server:app --host 0.0.0.0 --port 8000` | `http://<本机局域网IP>:8000/nurse/` |
| 开发 | `cd frontend && pnpm dev:nurse` | `http://<本机局域网IP>:5176/nurse/` |

- **`--host 0.0.0.0` 是局域网可达的前提**（只写 `127.0.0.1` 时别的 PC 打不开）；`server.host: true` 是 dev 侧同样的开关。
- 取本机 IP：`ipconfig` 看 IPv4（如 `192.168.1.23`）。**首次访问 Windows 防火墙会弹窗，选"专用网络 允许"**；若已错过弹窗，需手动放行 8000 端口（TCP 入站）。
- 无额外服务：不引 nginx、不引 Node 到现场；多台护士站 PC 同时打开都行（面板无状态，只读 + ack）。
- 板卡/异地访问沿用既有 Tailscale 链路（`100.65.82.93`），本设计不新增网络配置。

## 7. 降级与稳健性

| 故障 | 行为 |
|---|---|
| SSE 断开 | 3s 重连 + 5s 轮询兜底 + 顶栏红条；**通知不丢**（数据在后端，重连后自动补齐列表） |
| 后端完全不可达 | 页面提示"连不上后端"；恢复后自动恢复 |
| 通知表为空 / 超过保留期被清 | 空态文案，不报错 |
| 上报方（小车）掉线 | 不影响面板；恢复后新上报照常 |
| 上报口被局域网内伪造刷屏 | 去重合并 + 每条写审计（**已知限制**：v1 不做限流/签名，见 §9） |
| 通知表膨胀 | `prune()` 删已处理的过期数据；未处理永不删 |

## 8. 测试

**后端 `LLM/tests/test_notify.py`（新增，用 tmp_path 隔离库）：**
1. `ingest` 最小体（只有 `type`）→ 落库 + 缺省级别/标题兜底正确
2. `level` 非法 → 按 `DEFAULT_LEVEL` 兜底；`type` 缺失 → 抛错
3. 去重合并：同 `(source,type,uid)` 60s 内两次 → 1 行、`count=2`、`body` 保持首次原文
4. 已处理后再来同 key → **新增一行**（不得合并进已处理的旧通知）
5. `list_notices(state="unread")` 只回未处理；`counts()` 数值正确
6. `ack` / `ack_all` → `ack_at`/`ack_by` 落库 + 广播 `notification_ack`
7. `prune()` 只删过期的**已处理**行
8. `/api/alarm` 兼容回归：仍返回 `{ok:true}`、**仍广播 `alarm` 事件**（kiosk toast 不退化）、同时多出一条 `source=kiosk` 的消息
9. `reminder._escalate` 触发 → 多出一条 `type=reminder_unconfirmed` 通知
10. 身份：未认证（非 admin 槽）`ack`/`GET`/`DELETE` 被拒；`POST /api/notifications` **免鉴权放行**且写审计（这是 D4 的口径锁定）
11. 广播 payload 断言：`kind` 正确且事件 `type == "notification"`（**防 D6 陷阱回归**）

**前端（沿用现状口径）：** `shared` 的 vitest 补新事件解析用例（`notification` 正常解析、未知 `type` 容错、缺 `kind`/`id` 容错）；`nurse` 页面本身不引测试框架（admin/kiosk 亦无），走人工验收。

**人工验收清单（需用户在浏览器做）：**
1. 局域网内**另一台 PC** 打开 `http://<本机IP>:8000/nurse/` 能看到页面（D8 验收）
2. 命令行 curl 上报一条 `{"type":"sos","message":"测试"}` → 面板 5 秒内出现红卡 + 蜂鸣
3. kiosk 按 SOS → 面板出红卡（且 kiosk 原有 toast 不退化）
4. 在 A 屏幕点「处理了」→ B 屏幕同一张卡同步变灰
5. 停后端 → 页面显示断线红条；重启后端 → 自动恢复且**之前的通知还在**（持久化验收）
6. **把护士台开着 ≥6 分钟不动** → 全程**不被弹回、也不要求登录**（D11 验收）
7. **第二台 PC 同时打开 `/nurse/`** → 同样直接进；同时打开 `/admin/` → **仍然要口令**（验证改动只放宽了护士台、没放宽管理台）

## 9. 风险与已知限制

| 风险 | 对策 / 说明 |
|---|---|
| 上报口免鉴权（D4） | 局域网内可伪造告警。**用户已确认接受**；对策＝去重 + 全量审计（能查到谁在哪台机器上报的）。将来需要收紧时加 `X-Report-Token` 即可，不动现有调用方 |
| 与 admin 共用口令（D2） | 知道口令的护士也能打开完整管理台。**用户已确认接受**；将来要隔离就升级为独立护士角色（需动 R1–R5 红线区，另立规格） |
| 用户可能误以为"面板没更新" | 顶栏常驻"实时在线/连接已断"状态 + 时间戳。**不做**只靠颜色暗示 |
| 与将来「计划表 / 派车」功能冲突 | 上报体已预留 `source`/`ref`，将来小车任务系统只需往 `/api/notifications` POST，**本设计不需要改契约** |
| 通知表长期膨胀 | `prune()` + 30 天保留已处理 |
| 用户 9-18 踩过的"页面还在跑旧 JS" | `_no_store_entry_html` 的入口路径白名单**必须加 `/nurse`**（§10 落点清单第 4 条） |
| **读边界被 SSE 旁路**（2026-09-18 最终审查 I4） | `GET /api/notifications` 要 admin，但同一份 `uid_name`（老人姓名·床号）/`title`/`body` 由 `notification` 事件广播在**完全免鉴权**的 `/api/events` 上，且 CORS `allow_origins=["*"]`（跨源 EventSource 也能读）。**裁定：记入已知限制**——既有 `alarm`/`reminder` 早已同样暴露（本分支只是新增了老人身份字段），给 SSE 加闸门会波及 kiosk/admin 两个既有消费方，属另一份规格的范围。收紧路径：给 `/api/events` 加 `X-Surface` 校验，或把身份字段从广播里摘掉（后者与 §4.5 冲突，需改规格） |
| **~~护士台每 5 分钟被弹回登录门~~（已解决）** | 2026-09-18 最终审查 I5 提出；用户同日拍板 D11「护士台不再需要登录、也不会被弹」→ **前端去掉登录门、三条端点免鉴权**，问题不再存在。**未采用**"给 admin 槽加活动续期"的改法（会削弱"无人看管自动降权"）。管理台 `/admin` 的口令门与 TTL 行为**完全不变** |
| **谁都能标记"已处理"**（D11 的代价） | `ack`/`ack-all` 免鉴权 → 局域网内任何人打开 `/nurse` 都能看通知并清空待办角标（**删单条仍要口令**）。相对本分支之前没有增量式恶化：通知内容本来就经免鉴权 SSE 广播公开（见下一行），且阈值低（LAN、内部系统）。要收紧就加 token 或恢复登录门，属另一份规格 |
| **多台 PC 共享一个进程级 admin 槽**（D2 的直接后果） | 第一台 PC 登录后，第二台 PC 打开 `/nurse/`（或 `/admin/`）**不会**看到登录门——会话是后端进程级的，不是每浏览器一份。局域网内多屏共用一个管理员身份；要按人隔离需独立护士角色（另立规格，见上一行） |
| **角标数可能大于可见卡片数** | 角标是**未处理总数**（全表 COUNT），列表默认上限 50 且面板**不做加载更多**（`before_id` 后端已留、面板不用）→ 未处理 >50 时"未处理 60"但只看到 50 张卡。符合 YAGNI，记此说明 |
| **持续复报长期合并为一行** | 去重窗口锚在 `last_at`：同一 `(source,type,uid)` 每 <60s 复报会一直被合并、`count` 无限增长（一个持续故障 = 一行 + 次数），**ack 之后自然新开一行**。经裁定符合 D5 防刷屏本意 |
| **审计里的"谁处理的"不防伪造** | `AckIn.by` 由客户端自报，`POST /api/notifications` 又免鉴权 → 审计能记"哪台机器在什么时候上报/处理了什么"，但不能作为身份证据。这是 D4 的已知代价 |
| **两个并发上报可能各插一行**（2026-09-18 定向复审确认） | I1 的修复只闭合了"find 命中而 bump 时已被 ack"这一路；**两个并发 ingest 都未命中 find 时仍会各插一行**（`add_notification` 无唯一约束）。影响＝可见重复卡（同一事件两张、角标虚高 1，可 ack/可 prune 收敛），**不是**静默丢失；触发窗口微秒级、修复前即存在。真正闭合需 db 层单临界区 `dedup_or_add` 或部分唯一索引（`(source,type,uid) WHERE ack_at=''`），本版不做 |

## 10. 实施落点清单（7 处接线，一处都不能漏）

| # | 文件 | 改动 |
|---|---|---|
| 1 | `frontend/package.json` | 加 `"dev:nurse": "pnpm --filter nurse dev"` |
| 2 | `frontend/packages/nurse/*` | 新包（§5.1 骨架） |
| 3 | `LLM/server.py` | `_NURSE_DIST` + `_serve_dist(_NURSE_DIST, "/nurse")`（放在 admin 挂载之后） |
| 4 | `LLM/server.py::_no_store_entry_html` | 白名单加 `"/nurse"`（否则护士台会中"页面缓存旧 JS"坑） |
| 5 | `scripts/build_frontend.ps1` | 末尾提示文字改为三端 → 四端产物路径 |
| 6 | `frontend/packages/shared/src/events.ts` | 两个新事件 + `KNOWN_TYPES`（SSE 唯一事实来源） |
| 7 | `AGENTS.md` | 前端"三个包"→ 四个包、`/nurse` 挂载与端口、API 端点清单补 5 条、文档导航加本规格 |

## 11. 任务拆分（SDD 执行用）

| 任务 | 内容 | 主要文件 | 验收 |
|---|---|---|---|
| T1 | 后端通知中心：`notify.py` + `notifications` 表 + 5 条路由 + `conf.py` 常量 | `LLM/agent/notify.py`、`LLM/store/db.py`、`LLM/server.py`、`LLM/conf.py` | `test_notify.py` 1–7、10、11 绿 |
| T2 | 接线与兼容：`/api/alarm` 落库不改契约、`reminder._escalate` 落库 | `LLM/server.py`、`LLM/agent/reminder.py` | `test_notify.py` 8–9 绿 + 既有 reminder/alarm 用例不红 |
| T3 | 前端护士台：新包 + §10 落点 1–7 全部接线（含 `AGENTS.md` 与 build 脚本） | `frontend/packages/nurse/*`、`shared/src/{events.ts,api/notifications.ts}`、`frontend/package.json`、`LLM/server.py`（静态托管与 no-store 白名单）、`scripts/build_frontend.ps1`、`AGENTS.md` | shared 单测绿 + §8 人工验收清单 |

## 12. YAGNI（本版明确不做）

- 派车 / 一键呼叫 / 回复通知（面板只读 + ack）
- 微信 / 企业微信 / 短信推送（模块 11 后续）
- 关键告警"未确认自动升级提醒"（模块 11 完整版功能；v1 靠蜂鸣 + 计数 + 未处理置顶）
- 通知限流 / 签名鉴权 / 护士独立角色
- 通知搜索、导出、地图位置、图片附件
- 面板内的任何设置项（要改设置请去管理台）
- 面板的通知分页/"加载更多"（后端 `before_id` 已留，面板不用；未处理 >50 时看角标即可）
- 给 `/api/events`（SSE）加鉴权闸门（见 §9「读边界被 SSE 旁路」，属另一份规格）
- admin 会话活动续期（见 §9「每 5 分钟弹回登录门」，会动既有安全属性）

---

## 实现台账与偏差

> 落地日期：2026-09-18 ｜ 分支 `feature/nurse-console` ｜ 范围 `408e35e`（规格提交）→ `986cde8`（HEAD）
> 执行方式：任务简报 T1/T2/T3（`.superpowers/sdd/task-*-brief.md`）→ 每个任务全新实现子代理 → 逐任务审查（规格合规 + 代码质量）→ T3 修复轮 → **整分支最终审查**（裁定"修完再合"）→ 一次修复浪潮 → 定向复审（通过）

**实际落点（§10 的 7 处全部落地）**：`LLM/conf.py` 4 常量、`LLM/store/db.py` 建表 + 10 个数据操作、`LLM/agent/notify.py`（新建）、`LLM/server.py` 5 条路由 + `NoticeIn`/`AckIn` + lifespan `prune()` + 静态托管/no-store 白名单、`LLM/agent/reminder.py` 接线、`LLM/tests/test_notify.py`（新建，25 例）、`frontend/packages/nurse/*`（App.vue + NoticeCard.vue + lib + vite/tsconfig/index.html）、`shared/src/{events.ts,api/notifications.ts,index.ts}`、`frontend/package.json`、`scripts/build_frontend.ps1`、`AGENTS.md`。

**测试证据（协调者在 `danger-full-access` 下独立复核）**：
- `pytest LLM/tests -q` → **330 passed / 7 errors / 0 failed**；7 个 error 全在 `LLM/tests/test_speaker_enroll.py`（PermissionError，**基线红态**，非本分支引入）；本批次前基线 305 → +25（`test_notify.py`）
- `pnpm -r build` 四包全过（admin/kiosk/nurse/mapeditor）；`pnpm --filter shared test` 6 文件 31 例全绿
- 产物核对：四端 `dist/index.html` 的 base 依次为 `/admin/`、`/kiosk/`、`/nurse/`、`/mapeditor/`，与后端挂载点一一对应；`/nurse/` 带 `Cache-Control: no-store`

**与规格的偏差（均为有理由的改进或澄清）**：
1. `notify.ingest` **去掉了规格 §4.2 的 `ts` 形参**——依据 §4.4"落库以服务器时间为准、不采信外部时间"，统一 `db.now_iso()`。
2. 前端**不再把原始错误串上屏**（规格 §5.2 曾写「读取通知失败：<原因>」）→ 改人话 + `console.error` 留证。依据 D7「界面文案零术语」。
3. `未处理` 筛选为**纯前端过滤**（规格 §5.2 同段既写"纯前端过滤"又暗示 `state=unread`，属规格自相矛盾；实现取纯前端，取值合理）。
4. `AlarmIn` **新增 `source: str = "kiosk"`** 字段（§4.3 提到可覆盖，实现为可选字段，向后兼容）。
5. `bump_notification` 返回值由 `None` 改为 `int`（rowcount）——最终审查 I1 修复所需；仓内仅 `notify.ingest` 一个调用点。
6. `shared` 新增 `ingestNotice()`（规格 §5.3 有、T3 简报漏列，按规格补齐，UI 未使用）。
7. `AckIn.by` 由客户端自报、`notify.remove` 硬编码 `by="admin"`——规格未约束，记入 §9。
8. 面板另加 `watch(isAdmin)` 兜底（最终审查 M4）与"未登录不连 SSE"的单一清理收口 `stopRealtime()`（T3 修复轮）。

**审查抓出的真问题（值得留档）**：最终整分支审查找出 `find → bump` 竞态会让**新告警被并进已 ack 的行、静默不出卡不响铃**（救命通路，逐任务审查未发现）；以及免鉴权投递口在事件循环上做同步 SQLite 写（可被廉价请求堵死后端）。两者均已修并有测试锁定。

**留作后续（§12 与 §9 已记）**：面板通知分页、`/api/events` 鉴权、通知限流/签名、护士独立角色、微信推送、`(source,type,uid)` 部分唯一索引闭合并发重复窗口、`AlarmIn.source`/`AckIn.by` 长度与取值约束、`notify.py` 去重窗口边界改用 `db.now_iso()`。

### 二次修订（2026-09-18，D11 护士台免登录）

- **起因**：最终审查 I5 指出护士台每 5 分钟被弹回登录门；用户拍板「护士台不再需要登录、也不会被弹」。
- **改动**：前端去掉登录门（`App.vue` 不再调 `login`/`getSessionUser`/`logout`，无"会话过期"路径）；后端 `GET /api/notifications`、`POST /{nid}/ack`、`POST /ack-all` 由「必须 admin」改为**免鉴权**（`DELETE` 保持 admin）；规格 D2 标注作废、新增 D11。
- **未动**：`session.py`/`policy.py`/`X-Surface` 取值域、`/admin` 的口令门与 TTL、`admin`/`kiosk`/`mapeditor` 三个包、通知三端点以外的任何鉴权。
- **测试与验证**：见 `.superpowers/sdd/task-4-report.md`（新增/调整用例：三端点免鉴权、`DELETE` 仍 403、前端无登录门）。


