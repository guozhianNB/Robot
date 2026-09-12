# Kiosk 输入框回复逐句自动播报实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** kiosk 输入框聊天在保持 SSE 文字实时显示的同时，将回复增量交给后端按句使用当前 TTS 引擎播报，admin 与旧客户端保持静音。

**架构：** `/api/chat` 通过可选 `speak` 字段决定是否创建文本播报会话。线程安全队列把 HTTP 生成线程产生的 `content` 增量交给 `VoiceWorker` 后台线程，并复用现有分句、TTS 回退、播放队列、轮次取代和打断逻辑；SSE 不等待合成。`voice_api` 提供降级安全门面，kiosk 是唯一发送 `speak: true` 的前端。

**技术栈：** Python 3、FastAPI、Pydantic、`queue.Queue`、pytest、Vue 3、TypeScript、Vite、pnpm

---

## 文件结构

- 修改 `LLM/voice/worker.py`：线程安全文本事件流、文本回复播报入口、复用事件消费逻辑。
- 修改 `LLM/tests/test_worker_events.py`：逐句、尾句、轮次取代和事件隔离测试。
- 修改 `LLM/voice_api.py`：begin/feed/end 三个降级安全门面。
- 修改 `LLM/tests/test_voice_api_enroll.py`：门面可用和降级场景测试。
- 修改 `LLM/server.py`：扩展 `ChatRequest` 并桥接 SSE 增量。
- 创建 `LLM/tests/test_chat_text_tts.py`：API 默认静音、启用播报和异常关闭测试。
- 修改 `frontend/packages/kiosk/src/App.vue`：仅 kiosk 发送 `speak: true`。
- 修改 `docs/log.md`：记录实现和验证结果。

### 任务 1：VoiceWorker 增量文本播报会话

**文件：**
- 修改：`LLM/voice/worker.py:30-470`
- 测试：`LLM/tests/test_worker_events.py`

- [ ] **步骤 1：编写失败测试**

新增 `test_text_reply_stream_speaks_sentences_without_chat_events`：构造 stub TTS 记录文本，依次 `feed("第一句。第二")`、`feed("句！尾句")`、`finish(flush_tail=True)`，断言合成顺序为 `第一句。`、`第二句！`、`尾句`，不发布 `chat_partial/chat_new`，只调用一次 `end_of_stream`。新增 `test_new_text_reply_supersedes_old_text_reply`：第二轮开始后第一轮后续文字不得合成或入队。

- [ ] **步骤 2：验证红灯**

运行 `.venv\Scripts\python.exe -m pytest LLM\tests\test_worker_events.py::test_text_reply_stream_speaks_sentences_without_chat_events -q`。

预期：FAIL，`VoiceWorker` 没有 `begin_text_reply`。

- [ ] **步骤 3：实现线程安全 feed**

在 `worker.py` 引入 `queue` 并新增 `_TextReplyFeed`。其接口固定为：

`_TextReplyFeed.feed(delta: str) -> None` 投递非空 content 事件；`finish(flush_tail: bool = True) -> None` 投递一次 done 事件；`__iter__()` 阻塞读取队列、逐个 yield 事件并在 done 后返回。

内部使用 `queue.Queue` 传递 `{"type": "content", "content": delta}` 和 `{"type": "done", "flush_tail": bool}`；锁保护 `_closed`，重复 `finish` 和关闭后的 `feed` 均为幂等空操作。

- [ ] **步骤 4：抽取事件消费器**

将 `_consume_reply` 主体抽为 `_consume_events(events, settings, turn, publish_text=True, wake_if_idle=False)`。保留既有 `SentenceBuffer`、清洗、云端失败回退、入队及审计；`publish_text=False` 不发 `chat_partial`；`wake_if_idle=True` 时首句播放前执行 `session.wake()` 再 `start_speaking()`；收到 `done.flush_tail=False` 时不 flush 半句。原 `_consume_reply` 只创建 `self.stream_fn(uid, user_text)` 并委托该方法。

- [ ] **步骤 5：实现文本播报入口**

新增 `begin_text_reply(settings)`：TTS 或 sink 不可用时返回 `None`；否则先置 abort、停止旧 sink、执行 `session.barge_in()`，再递增 `_turn`、清 abort、设置 `_answering=True`，创建 feed 和名为 `voice-answer` 的后台线程。线程调用 `_consume_events(feed, settings, turn, publish_text=False, wake_if_idle=True)`；最新轮收尾时复位 `_answering`、清 abort、调用一次 `sink.end_of_stream()`，被取代轮只审计 `answer_superseded`。

- [ ] **步骤 6：验证绿灯并提交**

运行 `.venv\Scripts\python.exe -m pytest LLM\tests\test_worker_events.py -q`，预期全部 PASS 且线程正常退出。

提交命令：`git add -- LLM/voice/worker.py LLM/tests/test_worker_events.py`，然后 `git commit -m "feat(voice): 支持增量文本回复逐句播报"`。

### 任务 2：语音 API 降级安全门面

**文件：**
- 修改：`LLM/voice_api.py:40-155`
- 测试：`LLM/tests/test_voice_api_enroll.py`

- [ ] **步骤 1：编写失败测试**

新增测试：运行中的 fake worker 收到 `begin_text_reply(settings)` 并返回 handle；`tts_enabled=False` 或 `_worker=None` 时返回 `None`；`feed_text_reply(None, "忽略")` 与 `end_text_reply(None)` 不抛错；有效 handle 的 `feed`、`finish(flush_tail=False)` 各调用一次。

- [ ] **步骤 2：验证红灯**

运行 `.venv\Scripts\python.exe -m pytest LLM\tests\test_voice_api_enroll.py -q`。

预期：FAIL，`voice_api.begin_text_reply` 不存在。

- [ ] **步骤 3：实现三个门面**

```python
def begin_text_reply():
    if not _VOICE_AVAILABLE or _worker is None:
        return None
    settings = db.get_settings()
    if not settings.get("voice_enabled", True) or not settings.get("tts_enabled", True):
        return None
    try:
        return _worker.begin_text_reply(settings)
    except Exception as e:
        audit.log("voice_error", action="text_tts_begin", error=str(e)[:200])
        return None
```

`feed_text_reply(handle, delta)` 在 handle 和 delta 有效时调用 `handle.feed(delta)`；`end_text_reply(handle, flush_tail=True)` 调用 `handle.finish(flush_tail=flush_tail)`。两者捕获所有异常并分别审计 `text_tts_feed`、`text_tts_end`，不得向聊天请求抛出。

- [ ] **步骤 4：验证绿灯并提交**

运行 `.venv\Scripts\python.exe -m pytest LLM\tests\test_voice_api_enroll.py -q`，预期全部 PASS。

提交 `LLM/voice_api.py` 与 `LLM/tests/test_voice_api_enroll.py`，消息为 `feat(voice): 添加文本播报降级门面`。

### 任务 3：聊天 SSE 接入可选播报

**文件：**
- 修改：`LLM/server.py:139-260`
- 创建：`LLM/tests/test_chat_text_tts.py`

- [ ] **步骤 1：编写失败测试**

创建 `test_chat_text_tts.py`。第一个测试不传 `speak`，mock `voice_api.begin_text_reply` 为一旦调用就失败，断言 `/api/chat` 仍返回 content SSE。第二个测试传 `speak=true`，让聊天流依次产生 `第一句。`、`尾句`、`done`，断言调用顺序如下：

```python
assert calls == [
    ("begin",),
    ("feed", handle, "第一句。"),
    ("feed", handle, "尾句"),
    ("end", handle, True),
]
```

第三个测试让生成器在一个 content 后抛出异常，断言 finally 调用 `end_text_reply(handle, flush_tail=False)`，同时保留 StreamingResponse 既有异常传播语义。

- [ ] **步骤 2：验证红灯**

运行 `.venv\Scripts\python.exe -m pytest LLM\tests\test_chat_text_tts.py -q`。

预期：`speak=true` 测试 FAIL，因为请求模型和路由尚未桥接播报。

- [ ] **步骤 3：扩展请求模型**

```python
class ChatRequest(BaseModel):
    uid: str = "elder_001"
    message: str
    thinking: str = "auto"
    speak: bool = False
```

- [ ] **步骤 4：桥接 SSE 与播报 feed**

在 `gen()` 开头仅当 `req.speak` 时调用 `voice_api.begin_text_reply()`。每个 content 在 yield 前调用 `feed_text_reply`；收到 done 后保存 assistant 并设置 `completed=True`；用 `finally` 调用 `end_text_reply(speech, flush_tail=completed)`。finally 之后保留原 `_bg.submit(_post_chat_jobs, req.uid, req.message, assistant)`，确保历史和记忆只处理一次。

核心结构：

```python
speech = voice_api.begin_text_reply() if req.speak else None
completed = False
try:
    for ev in chat.chat_stream(client, MODEL, req.uid, req.message,
                               req.thinking, settings):
        if ev["type"] == "content":
            voice_api.feed_text_reply(speech, ev.get("content") or "")
        elif ev["type"] == "done":
            assistant = ev.get("assistant", "")
            completed = True
        yield _sse(ev)
finally:
    voice_api.end_text_reply(speech, flush_tail=completed)
```

- [ ] **步骤 5：验证绿灯并提交**

运行 `.venv\Scripts\python.exe -m pytest LLM\tests\test_chat_text_tts.py LLM\tests\test_server_voice_routes.py -q`，预期全部 PASS。

提交 `LLM/server.py` 与 `LLM/tests/test_chat_text_tts.py`，消息为 `feat(chat): 支持 kiosk 回复增量 TTS`。

### 任务 4：仅为 kiosk 启用并完成自动验证

**文件：**
- 修改：`frontend/packages/kiosk/src/App.vue:123-133`
- 修改：`docs/log.md`

- [ ] **步骤 1：建立失败检查**

运行 `rg -n 'speak: true' frontend/packages/kiosk/src/App.vue`。

预期：退出码 1，没有匹配。

- [ ] **步骤 2：只修改 kiosk 请求体**

```typescript
body: JSON.stringify({
  uid: uid.value ?? "elder_001",
  message: text,
  thinking: "auto",
  speak: true,
}),
```

不修改 `frontend/packages/admin/src/pages/ChatPage.vue`，其请求继续使用后端默认的 `speak=false`。

- [ ] **步骤 3：验证前端范围**

运行 `rg -n 'speak: true' frontend/packages/kiosk/src/App.vue`，预期一个匹配。运行 `rg -n 'speak: true' frontend/packages/admin/src/pages/ChatPage.vue`，预期退出码 1、无匹配。

- [ ] **步骤 4：更新开发日志**

在 `docs/log.md` 追加 `2026-09-13 · kiosk 输入框回复逐句自动播报`，记录兼容的 `speak=false` 字段、kiosk 独占启用、增量队列、降级行为和最终验证结果。

- [ ] **步骤 5：运行完整验证**

依次运行：

1. `.venv\Scripts\python.exe -m pytest LLM\tests -q`
2. `pnpm --dir frontend --filter kiosk exec vue-tsc --noEmit`
3. `pnpm --dir frontend --filter kiosk build`
4. `pnpm --dir frontend --filter admin exec vue-tsc --noEmit`
5. `.venv\Scripts\python.exe -m py_compile LLM\server.py LLM\voice_api.py LLM\voice\worker.py`
6. `git diff --check`

预期：所有命令退出码 0；后端无失败测试；kiosk 构建成功；admin 类型检查成功；差异检查无输出。

- [ ] **步骤 6：提交**

提交 `frontend/packages/kiosk/src/App.vue` 与 `docs/log.md`，消息为 `feat(kiosk): 输入框回复启用逐句播报`。

### 任务 5：人工本地验收

**文件：** 无

- [ ] **步骤 1：启动后端**

运行 `.venv\Scripts\python.exe -m uvicorn LLM.server:app --host 0.0.0.0 --port 8000`。

预期：`GET http://127.0.0.1:8000/api/voice/status` 返回 `status=running` 且 `modules.tts=cloud`。

- [ ] **步骤 2：启动 kiosk 并验证播报**

运行 `pnpm --dir frontend dev:kiosk`，打开 `http://127.0.0.1:5174/kiosk/`，输入要求生成至少两句回复的消息。

预期：文字持续增长，每个完整句生成后按顺序从默认扬声器播报。

- [ ] **步骤 3：确认 admin 静音**

运行 `pnpm --dir frontend dev:admin`，打开 `http://127.0.0.1:5173/admin/` 并发送消息。

预期：admin 只显示文字，不触发扬声器播报。
