# 语音前端实时联动 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 语音对话发生时前端实时联动：①对话区实时显示新语音对话轮次（不用刷新）；②对话页顶部实时显示语音链路状态（唤醒→聆听→播报）；③语音对话 uid 与当前选中老人不一致时自动切换到对应老人对话页。

**架构：** 复用后端现成 SSE 事件总线（`bus.publish()` 线程安全 + `/api/events` 扇出，前端 `EventSource` 已在监听）。后端仅改 `LLM/voice/worker.py`：在唤醒命中 / ASR 出文本 / TTS 开始 / 播报结束 / 对话落库后各 `_publish` 一个事件（`voice_state` / `chat_new`，全部 try/except 兜底，不影响语音主循环）。前端仅改 `UI/index.html`：对话页加状态指示条、`connectEvents()` 增加两个事件监听、抽 `switchTab()` 公共函数。**文本聊天路径（`/api/chat` SSE 流式）完全不碰，无回归面。**

**技术栈：** Python 3.11+ · FastAPI · pytest · 原生 JS（零构建单文件）

**规格：** `docs/superpowers/specs/2026-08-24-voice-realtime-frontend-design.md`

---

## 文件结构

**修改：**
- `LLM/voice/worker.py` — 新增 `_publish()` 兜底助手 + 4 处事件推送（wake / recognized+chat_new / speaking / idle）
- `UI/index.html` — 对话页状态指示条（CSS+HTML）、`switchTab()` 重构、`handleVoiceState()` / `appendVoiceTurn()` / `ensureChatFor()`、`connectEvents()` 增加监听
- `docs/log.md` — 追加开发日志

**新建（测试）：**
- `LLM/tests/test_worker_events.py` — VoiceWorker 事件广播单元测试（5 个用例）

**注意：**
- 语音全链路（麦克风/模型）在开发机不可行，事件推送用**单元测试 + 前端浏览器模拟事件**验证；另提供 `bus.publish` 手动注入的集成验证（Task 3）。
- 测试运行命令（Windows）：`& '.venv\Scripts\python.exe' -m pytest LLM/tests/test_worker_events.py -v`（工作目录 `D:\_project\Robot`）。

---

## 任务 1：worker.py 事件推送（TDD）

**文件：**
- 修改：`LLM/voice/worker.py`
- 测试：`LLM/tests/test_worker_events.py`（新建）

- [ ] **步骤 1：写失败测试**

创建 `LLM/tests/test_worker_events.py`：

```python
# -*- coding: utf-8 -*-
"""VoiceWorker 事件广播测试：wake / recognized / chat_new / speaking / idle。"""
from LLM.voice import worker as worker_mod
from LLM.voice import session as session_mod


def _make_worker(chat_fn=None, post_turn_fn=None):
    events = []

    def pub(ev, **payload):
        events.append((ev, payload))

    w = worker_mod.VoiceWorker(
        chat_fn=chat_fn or (lambda uid, text: "回复：" + text),
        post_turn_fn=post_turn_fn or (lambda uid, user, assistant: None),
        publish_fn=pub,
    )
    return w, events


def _silence_audit(monkeypatch):
    # 测试不写运行时审计日志
    monkeypatch.setattr(worker_mod.audit, "log", lambda **kw: None)


def test_wake_publish(monkeypatch):
    _silence_audit(monkeypatch)
    w, events = _make_worker()
    w.session = session_mod.Session()
    w.src = type("Src", (), {"read": lambda self: b"\x00" * 320})()
    w.vad = type("Vad", (), {"accept": lambda self, c: None})()
    w.kws = type("Kws", (), {"accept": lambda self, c: "小机器人"})()
    w._step({})
    assert ("voice_state", {"state": "wake"}) in events


def test_speech_publishes_recognized_and_chat_new(monkeypatch):
    _silence_audit(monkeypatch)
    calls = []

    def chat_fn(uid, text):
        calls.append((uid, text))
        return "好的，我记住了"

    w, events = _make_worker(chat_fn=chat_fn)
    w.session = session_mod.Session()
    w.asr = type("Asr", (), {"transcribe": lambda self, seg: "我今天有点头晕"})()
    w.fusion = type(
        "Fusion", (),
        {"resolve": lambda self, seg: type("Vote", (), {"candidate_uid": "elder_002", "confidence": 0.9})()},
    )()
    w._handle_speech("seg", {"asr_enabled": True, "tts_enabled": False})
    assert events[0] == ("voice_state",
                         {"state": "recognized", "uid": "elder_002", "text": "我今天有点头晕"})
    assert events[1] == ("chat_new",
                         {"uid": "elder_002", "user": "我今天有点头晕", "assistant": "好的，我记住了"})
    assert calls == [("elder_002", "我今天有点头晕")]


def test_speak_publishes_speaking(monkeypatch):
    _silence_audit(monkeypatch)
    w, events = _make_worker()
    w.session = session_mod.Session()
    w.session.wake()
    w.tts = type("Tts", (), {"synthesize": lambda self, t: (b"\x00\x00", 16000)})()
    w.sink = type("Sink", (), {"play": lambda self, s, sr: None})()
    w._speak("你好呀")
    assert ("voice_state", {"state": "speaking", "text": "你好呀"}) in events


def test_speaking_done_publishes_idle(monkeypatch):
    _silence_audit(monkeypatch)
    w, events = _make_worker()
    w.session = session_mod.Session()
    w.session.wake()
    w.session.start_speaking()
    w.src = type("Src", (), {"read": lambda self: b"\x00" * 320})()
    w.vad = type("Vad", (), {"accept": lambda self, c: None,
                             "is_speech_now": lambda self: False})()
    w.sink = type("Sink", (), {"is_done": lambda self: True})()
    w._step({})
    assert ("voice_state", {"state": "idle"}) in events


def test_publish_failure_is_silent(monkeypatch):
    """publish_fn 抛异常必须被吞掉，不影响语音主循环。"""
    _silence_audit(monkeypatch)
    w = worker_mod.VoiceWorker(
        chat_fn=lambda uid, text: "x",
        post_turn_fn=lambda uid, u, a: None,
        publish_fn=lambda ev, **kw: (_ for _ in ()).throw(RuntimeError("bus down")),
    )
    w.session = session_mod.Session()
    w.asr = type("Asr", (), {"transcribe": lambda self, seg: "测试"})()
    w.fusion = type("Fusion", (),
                    {"resolve": lambda self, seg: type("Vote", (), {"candidate_uid": None, "confidence": 0.1})()})()
    w._handle_speech("seg", {"asr_enabled": True, "tts_enabled": False})  # 不应抛异常
```

- [ ] **步骤 2：运行测试确认失败**

运行：`& '.venv\Scripts\python.exe' -m pytest LLM/tests/test_worker_events.py -v`（工作目录 `D:\_project\Robot`）
预期：5 个用例 FAIL —— `AssertionError`（事件列表为空，因为 worker 还没有发布逻辑）。

- [ ] **步骤 3：实现 worker.py 事件推送**

修改 `LLM/voice/worker.py`：

3a. 在 `_report` 方法之后新增兜底助手（第 40 行 `_report` 结束后、第 43 行 `_build_runtime` 前）：

```python
    def _publish(self, event_type: str, **kw):
        """事件广播兜底：publish 失败不影响语音主循环。"""
        if self.publish_fn:
            try:
                self.publish_fn(event_type, **kw)
            except Exception:
                pass
```

3b. `_step()` 的 IDLE 分支（唤醒词命中处，原第 100-104 行）——补 `voice_state: wake`（此时声纹未跑，**不带 uid**）：

```python
        if self.session.state == session_mod.State.IDLE:
            hit = self.kws.accept(chunk)
            if hit:
                self.session.wake()
                audit.log("voice_wake", keyword=hit)
                self._publish("voice_state", state="wake")
```

3c. `_step()` 的 SPEAKING 分支（播报结束处，原第 119-121 行）——补 `voice_state: idle`：

```python
            if self.sink.is_done():
                self.session.finish_speaking()
                self._speak_started = None
                self._publish("voice_state", state="idle")
```

3d. `_handle_speech()`（原第 138-146 行）——uid 确定后发 `voice_state: recognized`（带 uid 与识别文本）；`post_turn_fn` 之后发 `chat_new`（此时 `chat_stream` 生成器已耗尽、历史已落库）：

```python
        chat_uid = self.current_uid or "elder_001"
        self._publish("voice_state", state="recognized", uid=chat_uid, text=text)
        reply = self.chat_fn(chat_uid, text)
        if self.post_turn_fn:
            try:
                self.post_turn_fn(chat_uid, text, reply)
            except Exception:
                pass
        self._publish("chat_new", uid=chat_uid, user=text, assistant=reply)
```

3e. `_speak()`（原第 148-153 行）——末尾补 `voice_state: speaking`：

```python
    def _speak(self, text):
        samples, sr = self.tts.synthesize(text)
        self._speak_started = time.monotonic()
        self.session.start_speaking()
        self.sink.play(samples, sr)
        audit.log("voice_tts", text=text[:100], ms=len(samples) * 1000 // sr)
        self._publish("voice_state", state="speaking", text=text)
```

- [ ] **步骤 4：运行测试确认通过 + 全量回归**

运行：`& '.venv\Scripts\python.exe' -m pytest LLM/tests/test_worker_events.py -v`
预期：5 个用例 PASS。

运行：`& '.venv\Scripts\python.exe' -m pytest LLM/tests -q`
预期：全部 PASS（原 20 个 + 新 5 个 = 25 个）。

- [ ] **步骤 5：Commit**

```bash
git add LLM/voice/worker.py LLM/tests/test_worker_events.py
git commit -m "feat(voice): worker 推送 voice_state/chat_new 事件，前端可实时联动"
```

---

## 任务 2：前端实时联动（UI/index.html）

**文件：**
- 修改：`UI/index.html`

- [ ] **步骤 1：加语音状态指示条（CSS + HTML）**

1a. 在 `<style>` 内、`#messages` 规则（第 54 行）附近加：

```css
    #voice-indicator {
      display: none; margin: 8px auto 0; padding: 6px 16px;
      font-size: 13px; color: #86efac;
      background: rgba(34, 197, 94, .12); border: 1px solid rgba(34, 197, 94, .4);
      border-radius: 999px; width: fit-content; max-width: 90%;
    }
    #voice-indicator.on { display: block; }
```

1b. 在 `<section id="page-chat" class="page active">` 内、`<div id="messages"></div>`（第 238 行）之前加：

```html
      <div id="voice-indicator"></div>
```

- [ ] **步骤 2：抽 `switchTab()` 公共函数**

将第 445-456 行的 tab 点击监听整体替换为：

```js
    document.getElementById("tabs").addEventListener("click", (e) => {
      const btn = e.target.closest(".tab");
      if (btn) switchTab(btn.dataset.page);
    });
```

并在该监听器上方新增（`function` 声明会提升，位置不影响调用）：

```js
    function switchTab(page) {
      document.querySelectorAll(".tab").forEach(t =>
        t.classList.toggle("active", t.dataset.page === page));
      document.querySelectorAll(".page").forEach(p =>
        p.classList.toggle("active", p.id === "page-" + page));
      if (page === "memory") loadMemory();
      if (page === "reminder") loadReminders();
      if (page === "tools") { loadToolSwitches(); loadToolLog(); }
      if (page === "settings") loadSettings();
    }
```

- [ ] **步骤 3：新增三个 JS 函数**

在 `clearChatHistory()` 函数（第 671-678 行）之后新增：

```js
    // ================================================================
    // 语音实时联动：状态指示条 / 实时对话轮 / 自动切换老人
    // ================================================================
    function handleVoiceState(d) {
      const el = document.getElementById("voice-indicator");
      if (!el) return;
      const label = {
        wake: "🎤 检测到唤醒词…",
        recognized: "🎤 正在聆听：" + (d.text || ""),
        speaking: "🔊 正在播报…",
      }[d.state];
      if (label) { el.textContent = label; el.classList.add("on"); }
      else { el.classList.remove("on"); el.textContent = ""; }
    }

    function appendVoiceTurn(userText, assistantText) {
      const box = document.getElementById("messages");
      // 防重复：最后一条助手气泡内容相同（如切换重绘后同轮事件二次到达）则跳过
      const last = box.querySelector(".msg.assistant:last-of-type .answer");
      if (last && last.textContent === assistantText) return;
      appendUser(userText);
      const { answer } = appendAssistant(false);
      answer.textContent = assistantText;
      renderMarkdown(answer);
      chatMessages.push({ role: "user", content: userText });
      chatMessages.push({ role: "assistant", content: assistantText });
    }

    function ensureChatFor(uid) {
      const sel = document.getElementById("uid-select");
      if (!sel || ![...sel.options].some(o => o.value === uid)) {
        toast("🎤 新的语音对话", `${uid} 不在当前档案列表中`, "warn");
        return;
      }
      sel.value = uid;
      updateUid();          // 内部会置 currentUid + loadChatHistory() 重绘
      switchTab("chat");
    }
```

- [ ] **步骤 4：`connectEvents()` 增加两个事件监听**

在第 1035 行 `_es.onerror` 之前（`alarm` 监听之后）插入：

```js
      _es.addEventListener("voice_state", (ev) => {
        handleVoiceState(JSON.parse(ev.data));
      });
      _es.addEventListener("chat_new", (ev) => {
        const d = JSON.parse(ev.data);
        if (d.uid && d.uid !== currentUid) {
          ensureChatFor(d.uid);          // 自动切换到对应老人对话页
          return;
        }
        const chatPage = document.querySelector('[data-page="chat"]');
        if (chatPage && chatPage.classList.contains("active")) {
          appendVoiceTurn(d.user || "", d.assistant || "");
        } else {
          toast("🎤 新的语音对话", (d.user || "").slice(0, 40));
        }
      });
```

- [ ] **步骤 5：浏览器手动验证**

前置：启动后端（项目根目录 `.venv\Scripts\python.exe -m uvicorn LLM.server:app --host 0.0.0.0 --port 8000`），浏览器打开 `UI/index.html`。

5a. **状态指示条**：F12 控制台依次执行：

```js
window._es.dispatchEvent(new MessageEvent("voice_state", { data: JSON.stringify({ state: "wake" }) }));
window._es.dispatchEvent(new MessageEvent("voice_state", { data: JSON.stringify({ state: "recognized", uid: currentUid, text: "今天天气怎么样" }) }));
window._es.dispatchEvent(new MessageEvent("voice_state", { data: JSON.stringify({ state: "speaking", text: "今天晴" }) }));
window._es.dispatchEvent(new MessageEvent("voice_state", { data: JSON.stringify({ state: "idle" }) }));
```

预期：对话区顶部依次出现"🎤 检测到唤醒词…"→"🎤 正在聆听：今天天气怎么样"→"🔊 正在播报…"→ 隐藏。

5b. **同老人实时对话**（对话页激活时）：

```js
window._es.dispatchEvent(new MessageEvent("chat_new", { data: JSON.stringify({ uid: currentUid, user: "今天天气怎么样", assistant: "**今天晴**，适合散步" }) }));
```

预期：对话区**原位追加**用户气泡 + 助手气泡（Markdown 加粗生效），不整页重绘、滚动位置不跳。

5c. **防重复**：再执行一次 5b 的命令，预期：不追加（最后一条助手文本相同则跳过）。

5d. **自动切换**（另一位老人，需先有 `elder_002` 档案；如无则先在记忆页新建）：

```js
window._es.dispatchEvent(new MessageEvent("chat_new", { data: JSON.stringify({ uid: "elder_002", user: "我想孙子了", assistant: "那我陪您聊聊" }) }));
```

预期：uid 下拉框切到 elder_002 → 页签切到"对话" → 历史整页重绘包含该轮。

5e. **非对话页 toast**：切到"记忆"页签，再执行 5b 命令，预期：右上角 toast"🎤 新的语音对话"，对话区不重绘。

5f. **文本聊天回归**：对话页输入文字点发送，预期：仍走 `/api/chat` 流式打字机效果，无异常。

- [ ] **步骤 6：Commit**

```bash
git add UI/index.html
git commit -m "feat(ui): 语音实时联动——状态指示条 + chat_new 实时渲染 + 自动切换老人"
```

---

## 任务 3：开发日志 + 最终验证

**文件：**
- 修改：`docs/log.md`

- [ ] **步骤 1：追加开发日志**

在 `docs/log.md` 末尾追加（沿用"按日期追加"惯例）：

```markdown
---

## 2026-08-24 · 语音前端实时联动（SSE 事件推送）

**做了什么：**
- 后端 `LLM/voice/worker.py`：新增 `_publish()` 兜底助手，在唤醒命中 / ASR 出文本 / TTS 开始 / 播报结束 / 对话落库后推送 `voice_state`（wake/recognized/speaking/idle）与 `chat_new`（uid/user/assistant）事件，全部 try/except 兜底，不影响语音主循环。
- 前端 `UI/index.html`：对话页顶部新增语音状态指示条；`connectEvents()` 监听 `voice_state`（状态条）与 `chat_new`（同老人且对话页激活 → 原位追加对话轮并防重复；uid 不一致 → 自动切换到对应老人对话页；其他页签 → toast 提示）；抽 `switchTab()` 公共函数。
- 测试 `LLM/tests/test_worker_events.py`：5 个事件广播单元测试。

**有什么用：** 语音对话（唤醒→识别→回复→播报）全程在前端实时可见，护士不再需要刷新页面才能看到老人和机器人的对话；另一位老人说话时页面自动切过去。
```

- [ ] **步骤 2：最终验证**

2a. 全量测试：`& '.venv\Scripts\python.exe' -m pytest LLM/tests -q`，预期全部 PASS。

2b. 后端启动 + 事件流集成验证（语音 worker 在开发机可能因无麦克风/模型降级，但 `bus.publish` 可直接注入验证）：

```bash
# 终端 A：启动后端
.venv\Scripts\python.exe -m uvicorn LLM.server:app --host 0.0.0.0 --port 8000
# 终端 B：观察事件流
curl -N http://127.0.0.1:8000/api/events
# 终端 C：注入一条模拟事件
.venv\Scripts\python.exe -c "from LLM import bus; bus.publish('chat_new', uid='elder_001', user='测试', assistant='收到'); bus.publish('voice_state', state='speaking', text='测试')"
```

预期：终端 B 的 SSE 流中实时出现这两条 `data:` 行；浏览器 `connectEvents()` 收到后按 Task 2 步骤 5 的逻辑渲染（同老人对话页 → 原位追加）。

- [ ] **步骤 3：Commit**

```bash
git add docs/log.md
git commit -m "docs: 语音前端实时联动开发日志"
```
