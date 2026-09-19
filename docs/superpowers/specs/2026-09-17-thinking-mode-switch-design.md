# 思考模式手动切换 + 思维链上屏（2026-09-17）

> 需求（用户原话）：「现在的 llm 对话是默认快速不思考的。我希望你能在前端增加一个选择按钮，手动切换思考模式。
> 但敏感词自动加深思考功能不改。当手选思考强度为不思考时触发敏感问题思考功能，以后者确定的思考深度为准。
> 对话中要展示思维链，注意不要让思维链进入 tts」

## 一、现状（改动前的真实行为）

| 环节 | 现状 |
|------|------|
| 后端思考路由 | `LLM/agent/chat.py::route_thinking()`：关键词（`conf.THINKING_KEYWORDS`）→ 情绪词 → 长句 LLM 预判兜底；命中 `thinking on` |
| `/api/chat` 入参 | `thinking: "auto" \| "on" \| "off"`，但**优先级是手动全量覆盖**：`on`→True、`off`→False，**`off` 会把敏感词命中一起关掉** |
| 前端 | admin `ChatPage.vue` / kiosk `App.vue` 都写死 `thinking: "auto"`，没有 UI；两处都**只渲染 `content`**，`reasoning` 事件被丢弃 |
| 语音播报 | `LLM/server.py::chat_route` 只把 `content` 喂给 `voice_api.feed_text_reply()`；`LLM/voice/worker.py::_consume_events` 也只对 `content` 分句合成 → **思维链本来就不进 TTS**，但没有回归测试锁住 |

## 二、决策

### D1. 手动模式对路由的优先级：安全网优先（`off` 只关"日常问题"的思考）

用户口径两句要一起读：「敏感词自动加深思考功能不改」+「当手选思考强度为不思考时触发敏感问题思考功能，
以后者确定的思考深度为准」→ 安全网是硬约束，手动关闭只压制非敏感问题。

**D1 rev2（2026-09-17 用户实测反馈「强制模式下 llm 还是不思考」后改为五档阶梯）**：
档位从 `auto/on/off` 三档扩成 **`auto` / `none` 不思考 / `low` 轻度 / `high` 中度 / `max` 重度**，
判定返回的是**思考强度 effort**（`None` = 不思考），不再是布尔：

```
mode = 请求体 thinking 合法 → 用它；否则 settings.thinking_mode；否则 auto
routed = route_thinking(text, settings, ...)      # 判定逻辑一个字没改
mode in (low, high, max) → effort = mode                       # 手动深思，method=manual
mode == none             → effort = routed.on ? "high" : None   # 安全网照旧加深
mode == auto             → effort = "high" if routed.on else None
```

`none` 且路由命中时 `method` 保持 `keyword|emotion|llm`（前端据 chip 显示"敏感话题已自动加深"），
`reason` 前缀 `敏感话题已自动加深：`，绝不谎报成 `manual`。旧值 `on`→`high`、`off`→`none` 做别名兼容，
不迁移数据（老前端 bundle / 老设置都能跑）。

### D5. 「强制了也不思考」的根因：`reasoning_effort` 必须是顶层参数（2026-09-17 实测）

原实现把 `{"thinking": {...}, "reasoning_effort": effort}` 整个塞进 `extra_body`。DeepSeek 只会
识读 `extra_body.thinking`，顶层未知字段在 extra_body 里会被**静默忽略**——所以"开思考"生效、
"强度"永远停在默认值，加上当时前端根本不渲染 `reasoning` 事件，表现就是「选了强制也不思考」。
修法：
- `extra_body` 只放 `{"thinking": {"type": "enabled"|"disabled"}}`；
- `reasoning_effort` 走 `client.chat.completions.create(reasoning_effort=...)`（openai 3.3.1 签名里有）。

DeepSeek 官方文档（[思考模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)）：
`reasoning_effort` 取值 `minimal/low/medium/high/xhigh/max/ultra`，映射到实际 effort 为
`low/low/high/high/high/max/max`；思考模式默认开启且 effort 默认 `high`。**实测**（2026-09-17，本机 key）：
`low/high/max` 三档都可用且思维链长度确实不同（同一题：445 / 约 400–900 / 1939 字），
`ultra` 直接 **400**（`Failed to deserialize the JSON body`），故实现只用 `low/high/max`，
不暴露官方那些归并档位。可用模型：`deepseek-flash` / `deepseek-v4-pro`（`conf.MODEL="deepseek-v4-flash"`
实测也通）。

### D6. 「只想不作答」兜底（D5 修复时顺手加的）

思考档位下思维链也消耗 `max_tokens`：敏感问题在 `max` 档出现「reasoning 有内容、`content` 全空」
（实测两次都出现）时，老人那边一个字的回答都听不到。故 `chat_stream` 末尾加一次降级：
本轮没产出正文 → 关思考重答一次（`action="thinking_empty_fallback"`，只降一次防死循环）。

### D2. 模式持久化到 settings（`thinking_mode`）

`conf.DEFAULT_SETTINGS["thinking_mode"] = "auto"`（非特权键 → kiosk 端也能改，与 `asr_provider` 同族）。
理由：kiosk 的语音轮次不过前端（`worker.stream_fn` 直接调 `chat_stream`），要让它也吃到手动选择，
模式必须落在后端；顺带重启后不丢。优先级：请求体显式值 > settings 值。

### D3. 思维链上屏，但绝不入 TTS

- admin：`ChatPage.vue` 助手气泡内加可折叠「💭 思考过程」块，流式累积 `reasoning` 事件。
- kiosk：`ChatArea.vue` 同样展示（浅色小字、默认展开、可点「收起」）；**语音轮次**的 reasoning
  由 worker 新增 `chat_reasoning` 总线事件送到前端（`_publish("chat_reasoning", uid, delta)`）。
- TTS 三处闸门（都不动语义，只加注释 + 回归测试）：
  1. `server.py::chat_route` 只把 `content` 喂 `voice_api.feed_text_reply`（改写成显式 `if`）；
  2. `worker.py::_consume_events` 只对 `content` 调分句/合成；
  3. `worker.py` 文本播报轮 `_TextReplyFeed` 本身只吃 content。
- 思维链**不落库**（`db.append_history` 只写 `full_assistant`）→ 历史回读不会重播思考过程。

### D4. 不做的事

- 不改 `route_thinking` 的判定逻辑（关键词表、情绪词、LLM 预判阈值一律不动）。
- 不暴露 DeepSeek 官方那些会被归并的档位（`minimal/medium/xhigh/ultra` 全归到 low/high/max，
  `ultra` 还直接 400）——阶梯只做 `low/high/max` 三档，前端叫轻/中/重度。
- 思维链不进 RAG/记忆/审计正文（审计只记 `user`/`assistant` 前 200 字，维持现状）。

## 三、接口与事件契约

- `POST /api/chat` body：`{uid, message, thinking: "auto"|"none"|"low"|"high"|"max", speak}`
  （旧值 `on`/`off` 仍被接受＝`high`/`none`）。
- SSE `meta` 事件：`{"type":"meta","router":{"on":bool,"effort":"low|high|max"|null,"reason":str,
  "method":str,"mode":"auto|none|low|high|max","uid":str}}`——`effort` 是本轮真正发给模型的强度，
  降级重答时会再发一条 `meta` 说明。
- 总线新增 `chat_reasoning`：`{type:"chat_reasoning", uid, delta}`——与 `chat_partial` 同族，唯一事实来源
  `frontend/packages/shared/src/events.ts`（`KNOWN_TYPES` 同步加）。

## 四、改动清单

| 文件 | 改动 |
|------|------|
| `LLM/conf.py` | `DEFAULT_SETTINGS["thinking_mode"] = "auto"` + 阶梯注释 |
| `LLM/agent/chat.py` | `THINKING_MODES/FORCED_EFFORTS/LEGACY_MODE_ALIASES`、`_apply_thinking_mode()`（D1 rev2）、`_resolve_thinking_mode()`、`_thinking_extra()`（D5）、空回复兜底（D6） |
| `LLM/server.py` | `chat_route` 里 content 喂 TTS 改显式分支 + 注释 |
| `LLM/voice/worker.py` | `reasoning` → `_publish("chat_reasoning", ...)`；注释锁 TTS 边界 |
| `LLM/voice/voice_api.py` | 语音轮次 `thinking=""`（交给 settings 定档） |
| `frontend/packages/shared/src/thinking.ts`（新） | 五档 `ThinkingMode` / `THINKING_MODE_ORDER` / `normalizeThinkingMode()`（含 on/off 兼容）/ `nextThinkingMode()` |
| `frontend/packages/shared/src/events.ts` | `ChatReasoningEvent` + `KNOWN_TYPES` |
| `frontend/packages/admin/src/pages/ChatPage.vue` | 工具栏下拉（五档）+ 气泡内思考块 |
| `frontend/packages/kiosk/src/App.vue` / `components/ChatArea.vue` / `components/SettingsSheet.vue` | 底部弹出式五档菜单 + 设置弹层单选 + 思考块 |
| `LLM/tests/test_thinking_mode.py`（新） | 五档组合 + 安全网 + 参数形状 + 端到端 + 空回复兜底 |
| `LLM/tests/test_worker_events.py` | 补「reasoning 广播上屏但绝不合成」用例 |

## 五、验收

1. `pytest LLM/tests/test_thinking_mode.py` 全绿（22 例）；`pytest LLM/tests` 失败集合与基线一致。
2. `cd frontend && pnpm -r test` 全绿（含 `shared/tests/thinking.test.ts`）。
3. `pnpm --filter admin build`、`pnpm --filter kiosk build` 通过 → 产物含五档选择器。
4. 真机/浏览器人工：admin 选「重度」问算术题 → 出大段思考过程；选「不思考」问「降压药能减半吗」
   → 仍出思考过程且 `meta.method=keyword`；问「你好」→ 无思考过程、秒回。
   kiosk 说完话 → 屏幕出思考块、音箱**不念**思考内容。
