# 前端多端重构设计（Vue3 + 双端 + 无屏优先）

> 日期：2026-08-27 ｜ 状态：待用户审查 ｜ 范围：前端重构 + `/api/alarm` 端点
> 关联：`docs/superpowers/specs/2026-08-18-ai-chat-frontend-design.md`（旧单文件前端）、
> `docs/superpowers/specs/2026-08-22-voice-pipeline-design.md`（语音链路，本期不动）、
> `docs/目标文档及说明/大模型端开发目标.md` 模块 11（报警模块，预留）

## 1. 目标与背景

现有前端是单文件 HTML（`UI/index.html`，约 86KB，5 页签：对话/记忆/提醒/工具日志/设置），纯原生 JS。本次重构解决三个问题：

1. **多端化**：拆分为后台数据端（PC 浏览器）与车载交互端（老人面前的屏幕）
2. **无屏优先**：屏在但老人不用屏、只靠语音交互；语音链路是主交互，屏幕是"跟随显示器"
3. **工程化**：消除单文件带来的维护痛点（尤其 SSE 事件协议两端强耦合）

**关键架构原则（已确认）：交互闭环在后端，前端只是视图。** 语音链路（唤醒→ASR→LLM→TTS）已在后端 `voice/` 模块闭环，不依赖任何前端；前端只通过 SSE 事件总线观察状态。因此屏幕进程崩溃、屏没装、老人不用屏——语音链路照常工作。

## 2. 已确认决策记录

| # | 决策点 | 结论 |
|---|--------|------|
| D1 | 无屏场景 | 屏在，老人不用屏，语音是主交互；屏幕做辅助确认和状态展示 |
| D2 | 前端框架 | **Vue 3 + Vite + TypeScript**，pnpm workspace 单仓库 |
| D3 | 端形态 | `admin/`（后台数据端，PC 浏览器）+ `kiosk/`（车载交互端）+ `shared/`（共享层） |
| D4 | 语音交互 | 后端闭环不变；前端只订阅 SSE，屏幕可随时缺席，不参与交互回路 |
| D5 | kiosk 触摸深度 | **纯展示 + 紧急呼叫按钮**，不做操作入口（贴合"老人不用屏"） |
| D6 | 紧急呼叫 | 本期只做 `POST /api/alarm` 端点 + 审计 + 事件广播；微信推送留给模块 11 |
| D7 | 旧前端处置 | 保留在 `UI(old)/` 不删，作为参考实现；admin 功能齐平后仍保留 |
| D8 | 文档范围 | 只覆盖前端重构 + `/api/alarm` 端点，语音后端链路本期不动 |
| D9 | 部署形态 | 构建产物由 FastAPI StaticFiles 托管（复用 8000 端口），板卡 Chromium kiosk + systemd |
| D10 | 迁移策略 | 双轨运行，按功能逐个重写（不搬代码），接口契约不变 |

## 3. 架构总览

```
┌─────────────────────────── 后端 LLM/server.py（不动，除新增 /api/alarm） ───────────┐
│                                                                                   │
│  ├─ chat.py     对话编排（SSE 流式）                                              │
│  ├─ voice/      语音闭环：唤醒→ASR→LLM→TTS（状态机 IDLE/LISTENING/SPEAKING）       │
│  ├─ reminder.py 提醒调度（bus.publish("alarm", ...) 已存在）                       │
│  ├─ bus.py      SSE 事件总线 ◀──── 所有状态在此广播                                │
│  └─ + POST /api/alarm  （本期新增，审计 + 广播，微信推送预留）                     │
│                                                                                   │
└──────────────┬──────────────────────────────────────┬─────────────────────────────┘
               │ 主交互通道：语音（不经过屏幕）           │ SSE 订阅（屏幕只是观察者）
               ▼                                        ▼
        麦克风/扬声器                             ┌──────────────────┐
        唤醒→对话→播报，全闭环                     │ kiosk 车载屏端    │
        屏幕挂了/关了完全不影响                    │ 状态条/识别文本/  │
                                                 │ 回复/提醒卡片     │
                                                 │ + SOS 紧急呼叫    │
                                                 └──────────────────┘
                                                 ┌──────────────────┐
                                                 │ admin 后台数据端  │
                                                 │ 对话/记忆/提醒/   │
                                                 │ 日志/设置/语音状态│
                                                 └──────────────────┘
```

**核心推论**：

1. 屏幕端是"订阅者"不是"参与者"——消费 `/api/events` 可视化语音会话状态，不参与交互回路 → 屏幕崩溃不影响语音
2. 屏幕的独特价值 = 低置信度身份确认（声纹置信度低时语音反问 + 屏幕弹候选身份卡片，护工/家属一键确认）——`identity.py` 融合层"宁问勿猜"的视觉出口
3. 多端共享 `shared/` 的 API client 与事件解析，SSE 协议定义只写一份

## 4. 前端工程结构

```
frontend/                          # 新目录（pnpm workspace 单仓库）
  package.json                     # workspace root
  pnpm-workspace.yaml              # packages: admin / kiosk / shared
  packages/
    shared/
      src/
        api/          # 统一 REST client（/api/chat、/api/reminders、/api/alarm……）
        events.ts     # ★ SSE 事件类型唯一定义：type 枚举 + payload 类型 + 解析器
        types/        # 领域类型（Reminder、Memory、Speaker、VoiceStatus……）
        components/   # 跨端复用组件（提醒卡片、状态条、Toast、SOS 按钮）
    admin/            # 后台数据端入口（Vite + Vue3）
      src/pages/      # 对话 / 记忆 / 提醒 / 工具日志 / 设置 / 语音状态 / 监控总览
    kiosk/            # 车载交互端入口（Vite + Vue3）
      src/App.vue     # 单页沉浸式：状态条 + 对话区 + 提醒轮播 + SOS
```

### 4.1 shared/events.ts（SSE 协议唯一事实来源，草案）

```ts
export type BusEvent =
  | { type: "reminder"; payload: Reminder }
  | { type: "reminder_confirmed"; payload: { rid: string } }
  | { type: "alarm"; payload: { level: string; type: string; uid?: string } }
  | { type: "chat_new"; payload: { uid: string; role: string; content: string } }
  | { type: "voice_state"; payload: VoiceStatus }
  // 后续新增事件只改这一处；两端共用同一解析器

export function parseBusEvent(raw: string): BusEvent | null { /* 按 data.type 分发 */ }
```

消除 AGENTS.md 记录的"SSE 事件协议两端强耦合、改类型要同步"痛点——类型定义在 TS 编译期即校验。

## 5. 车载交互端（kiosk）设计

**定位**：语音会话的实时可视化 + 触摸兜底（仅 SOS）。单页沉浸式，无多页签。

```
┌─────────────────────────────────────────┐
│  状态条：◉ 待机 | 正在听… | 播报中…      │  ← 映射 /api/voice/status（三色）
├─────────────────────────────────────────┤
│                                         │
│   对话区（自动滚动，大字大行距）          │
│   👴 张爷爷：今天吃药了吗？（识别文本）    │
│   🤖 机器人：吃了，刚吃完降糖药……（回复） │
│                                         │
├─────────────────────────────────────────┤
│  底部常驻：提醒卡片轮播 + 时间/电量       │
│  [SOS 紧急呼叫]                         │  ← 唯一触摸操作
└─────────────────────────────────────────┘
```

- **状态条**：直接映射后端语音状态机（IDLE/LISTENING/SPEAKING），三状态三色
- **对话区**：SSE 订阅 `chat_new`，识别文本与回复实时上屏——护工/家属一眼看到机器人正与谁说什么
- **身份确认弹窗**：`voice_state` 携带低置信度标记时弹出候选身份卡片 `[确认] [不是]`
- **显示规范**：大字体、深色主题、防误触、自动亮屏
- **SSE 断线**：EventSource 自动重连（迁移现有 `connectEvents` 逻辑到 shared）

## 6. 后台数据端（admin）设计

沿用现有 5 页签（对话/记忆/提醒/工具日志/设置）+ 新增 2 页：

- **语音状态页**：`/api/voice/status` 心跳 + 子模块状态（ASR/TTS/VAD/KWS/声纹）、降级原因展示、声纹档案列表（`/api/voice/speakers`）+ 注册入口
- **监控总览页**：今日对话数、提醒命中率、语音异常事件（`audit.jsonl` 的 `voice_*` 事件可视化）、告警列表

## 7. 紧急呼叫（POST /api/alarm）

```
kiosk 触摸 [SOS] ──POST /api/alarm {type:"sos", uid}──▶ 后端（新增端点）
   ├─ audit.log("alarm", type="sos", ...)      # 落审计（log.py 已支持）
   ├─ bus.publish("alarm", type="sos", ...)    # 复用现有通道（reminder.py 已这么干）
   │    ├─▶ admin toast + 告警列表标红
   │    └─▶ 语音播报（TTS 优先级高于普通播报，P0 打断）
   └─ 微信推送 → 留给模块 11（本期不做，端点先行）
```

**范围控制**：本期只实现"端点 + 审计 + 广播"，不实现微信推送。请求体对齐需求文档模块 11 的字段草案（`type: "sos"` 等）。

## 8. 后端改动清单（最小化）

| 改动 | 说明 |
|---|---|
| `server.py` 新增 `POST /api/alarm` | 校验 body → `audit.log` → `bus.publish` → 返回 `{ok: true}` |
| `bus.py` 确认 `alarm` 事件类型 | 已存在（reminder.py 在用），无需改动 |
| 其余后端 | 一律不动（语音链路、对话编排、数据库均保持现状） |

## 9. 部署形态

```
开发机：pnpm dev（Vite dev server，热更新，代理 /api → 8000）
板卡：  pnpm build → 产物由 FastAPI StaticFiles 托管（复用 8000 端口，无需 CORS）
kiosk： Chromium --kiosk http://127.0.0.1:8000/kiosk/ 开机自启
       进程崩溃 → systemd 自动拉起（屏幕端可牺牲，语音不受影响）
```

零额外服务：不引 nginx、不引 Node 运行时到板卡（构建产物为纯静态文件）。

## 10. 降级与稳健性（延续既有哲学）

| 故障 | 行为 |
|---|---|
| 语音模块挂 | kiosk 状态条显示"语音不可用"，文字/触摸仍可用（沿用现有降级矩阵） |
| SSE 断线 | EventSource 自动重连 |
| kiosk 进程崩溃 | systemd 拉起；期间语音链路完全不受影响（后端无前端依赖） |
| 板卡无屏/屏坏 | 无需处理——语音闭环本就不经过前端 |

## 11. 迁移策略

- **重写不搬**：不把 86KB 单文件代码搬进 Vue（避免带入混乱），按功能逐个重写
- **双轨运行**：重构期间 `UI(old)/index.html` 保留，接口契约不变，后端 REST 不动
- **验收口径**：admin 端每个页签重写后与旧前端功能对拍验收，齐平后旧文件仍保留（D7）

## 12. 测试

- **shared/events.ts**：单元测试——SSE 原始帧解析、未知事件类型容错、payload 校验
- **kiosk 端**：状态条三态渲染、SSE 断线重连、SOS 按钮触发 `/api/alarm`、身份确认弹窗
- **admin 端**：各页签与旧前端功能对拍、语音状态页降级展示
- **集成**：`POST /api/alarm` → audit 落盘 + SSE 广播 → admin toast + kiosk 提示
- **稳健性回归**：杀 kiosk 进程 → 语音链路不受影响（沿用 M4 验收）

## 13. 里程碑

| 阶段 | 内容 | 验收 |
|------|------|------|
| M1 | 工程搭建：pnpm workspace + Vite + Vue3 + shared/ 骨架 | 双端 dev server 可启动，shared 可导入 |
| M2 | shared/events.ts + REST client + 单测 | SSE 解析单测通过 |
| M3 | admin 端：现有 5 页签重写 + 语音状态页 + 监控总览 | 与旧前端功能对拍通过 |
| M4 | kiosk 端：状态条/对话区/提醒轮播/SOS/身份确认 | 语音状态实时上屏，SOS 全链路通 |
| M5 | 后端 `POST /api/alarm` + 集成 + 稳健性回归 | 审计/广播/降级验证通过 |
| M6 | 板卡部署：StaticFiles 托管 + Chromium kiosk + systemd | 板卡跑通，屏崩溃语音不受影响 |

## 14. 风险与对策

| 风险 | 对策 |
|---|---|
| 重构周期长，旧功能对拍遗漏 | 双轨运行 + 逐页签验收口径（M3） |
| SSE 事件类型在重构中改坏 | 类型集中在 shared/events.ts，TS 编译期校验 + 单测（M2） |
| kiosk 端 Chromium 性能（RDK X5） | 页面轻量（纯展示），无重渲染；若不足再评估降级为 WebView 方案 |
| `/api/alarm` 与模块 11 将来冲突 | 请求体对齐需求文档字段草案，端点先行 |
