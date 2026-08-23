# 语音前端实时联动 设计文档

日期：2026-08-24
状态：已批准（用户确认方案 A：事件推送；范围 = 完整对话轮次 + 语音状态实时指示；uid 不匹配时自动切换对应老人对话页）

## 目标

语音对话（唤醒 → 识别 → LLM → TTS）发生时，前端控制台实时联动：

1. 对话区**实时显示**新完成的语音对话轮次（用户说 + 机器人答），不再需要刷新页面；
2. 对话页顶部**实时显示语音链路状态**（检测到唤醒词 → 正在聆听 → 正在播报）；
3. 语音对话发生在另一位老人身上（uid 与前端当前选中不一致）时，**自动切换**到对应老人的对话页并刷新显示。

## 现状分析

- 语音链路：`LLM/voice/worker.py::VoiceWorker` 采集 → VAD → KWS 唤醒 → ASR → 声纹 → `chat_fn`（内部走 `chat.chat_stream`，生成器结束处 `db.append_history` 落库）→ `post_turn_fn`（记忆整理）→ TTS 播报。
- 后端已有 SSE 事件总线：`bus.publish()`（线程安全，任意线程可调）→ `/api/events` 扇出给所有前端连接。
- worker 已有 `publish_fn`（= `bus.publish`），目前只推 `voice_status`（状态迁移：running / degraded / disabled），不推对话内容。
- 前端 `connectEvents()` 用 `EventSource` 监听 `/api/events`，目前只处理 `reminder` / `reminder_confirmed` / `alarm`。
- 前端对话区渲染只有两条路径：`send()`（网页文本输入，`/api/chat` SSE 流式即时显示）和 `loadChatHistory()`（刷新 / 切换老人时整页重绘）。**语音对话落库后无任何推送 → 前端只能刷新才能看到。**

## 技术选型（方案 A：事件推送）

| 方案 | 描述 | 结论 |
|---|---|---|
| **A 事件推送（采用）** | 语音链路关键节点 `bus.publish()`，前端 EventSource 监听 | 复用现有总线，零轮询，文本聊天路径零影响，改动集中在 2 个文件 |
| B 前端轮询历史 | 每 N 秒拉 `/api/chat/history` | 有延迟、浪费请求、打断滚动位置 |
| C 在 `chat_stream` 统一发事件 | 文本聊天也会触发，前端需去重防双渲染 | 复杂化，有回归风险 |

## 后端改动（仅 `LLM/voice/worker.py`）

新增 2 个事件推送，**全部 try/except 包裹 `publish_fn`，失败不影响语音主循环**（与现有 `post_turn_fn` 处理一致）：

| 事件 | payload | 触发点 |
|---|---|---|
| `voice_state` | `{state: "wake"\|"recognized"\|"speaking"\|"idle", uid?, text?}` | ①KWS 唤醒词命中 ②ASR 出文本（uid 已由声纹确定）③开始 TTS 播报 ④播报结束 |
| `chat_new` | `{uid, user, assistant}` | 一轮对话落库后（`_handle_speech` 中 `post_turn_fn` 之后） |

触发点细节：

- `wake`：`_step()` 的 IDLE 分支，`self.kws.accept(chunk)` 命中后发。此时声纹未跑、uid 不可靠 → **不带 uid**，前端只在当前页显示"检测到唤醒词"，不切换。
- `recognized`：`_handle_speech()` 中 `self.asr.transcribe(seg)` 出文本、`effective_uid` 确定后发，带 `uid` 与 `text`。
- `speaking`：`_speak()` 开始播报时发（带 reply 文本）。
- `idle`：`_step()` 的 SPEAKING 分支 `self.sink.is_done()` 时发，收尾隐藏指示条。
- `chat_new`：`_handle_speech()` 中 `post_turn_fn` 之后发，带 `chat_uid / text / reply`。此时历史已落库（`chat_stream` 生成器已耗尽）。

## 前端改动（仅 `UI/index.html`）

1. **对话页顶部加语音状态指示条** `#voice-indicator`（默认隐藏）：
   - `wake` → `🎤 检测到唤醒词…`
   - `recognized` → `🎤 正在聆听：{text}`
   - `speaking` → `🔊 正在播报…`
   - `idle` → 隐藏
2. **`connectEvents()` 增加 `chat_new` 监听**：
   - `d.uid !== currentUid` → **自动切换**：切 uid 下拉框 → 切到对话页签 → `loadChatHistory()` 重绘（此时不重复 append）；
   - `d.uid === currentUid` 且对话页激活 → **直接 append** 用户气泡 + 助手气泡（保留滚动位置），用"最后一条助手文本比对"防重复（防切换重绘后同轮事件二次渲染）；
   - 对话页未激活 → toast 提示。
3. **抽 `switchTab(page)` 公共函数**：tab 点击监听与自动切换共用。
4. 新增 `handleVoiceState()` / `appendVoiceTurn()` / `ensureChatFor()` 三个小函数。

## 边界与错误处理

- 事件推送全部 try/except 包裹，publish 失败不影响语音主循环。
- 语音依赖缺失降级时 worker 不启动、不发事件，前端无任何影响。
- 事件协议（`chat_new` / `voice_state`）按 AGENTS.md 约定在 `worker.py` 与 `index.html` 两端同步维护。
- 文本聊天路径完全不动，无回归面。

## 验证

- 后端：启动 uvicorn，`curl -N http://127.0.0.1:8000/api/events` 观察事件流；语音链路本机缺依赖会降级、无法全链路实测，但 worker 改动逻辑简单、与现有 publish 模式一致。
- 前端：浏览器验证文本聊天无回归（仍走 `/api/chat` 流式）；用模拟事件验证 `chat_new` / `voice_state` 渲染与自动切换逻辑。
