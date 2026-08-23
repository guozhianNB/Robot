# 模块状态弹窗 + 系统退出按钮 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 前端提供两个新能力——①「模块状态」弹窗：打开即展示后端各可选模块（语音/embedding/RAG/知识图谱）的加载状态与缺失依赖原因；②「系统退出」按钮：确认后优雅停止后端全部后台资源（提醒线程/语音/广播/线程池）并退出进程。

**架构：** 后端 `server.py` 新增两个路由：`GET /api/modules/status`（聚合 voice/embed/ragstore/graph 的现有 `status()`）；`POST /api/system/shutdown`（显式调用各模块 stop 钩子 → 返回响应 → 延迟 1s `os._exit(0)`）。`reminder.py` / `bus.py` 各补一个停止钩子。前端 `UI/index.html` 单文件内：顶栏两个按钮 + 一套 modal 基础样式（两功能共用）+ 模块状态列表渲染 + 退出确认流程 + 已退出全屏遮罩。

**技术栈：** Python 3.11+ · FastAPI · pytest + TestClient（httpx）· 原生 JS（零构建单文件）

**规格：**
- `docs/superpowers/specs/2026-08-24-module-status-modal-design.md`
- `docs/superpowers/specs/2026-08-24-system-shutdown-design.md`

---

## 文件结构

**修改：**
- `LLM/reminder.py` — 新增停止钩子 `stop()` + 启动时清标志 + 循环改事件等待
- `LLM/bus.py` — 新增停止标志与 `stop()`，drain 循环退出
- `LLM/server.py` — 新增 `/api/modules/status`、`/api/system/shutdown`、`_delayed_exit()`
- `UI/index.html` — 顶栏按钮、modal 基础样式与容器、模块状态弹窗、退出确认与已退出遮罩、JS 逻辑
- `docs/2.pre/log.md` — 追加开发日志

**新建（测试）：**
- `tests/test_shutdown_hooks.py` — reminder/bus 停止钩子单元测试
- `tests/test_modules_status.py` — 聚合接口结构测试

**注意：** `/api/system/shutdown` 会真实退出进程，**不做自动化测试**（用 TestClient 测会杀掉 pytest），改用手动 curl 验证；stop 钩子与聚合接口走 TDD。

---

## 任务 1：reminder.py 停止钩子（TDD）

**文件：**
- 修改：`LLM/reminder.py`（模块顶部、`_run()`、`start()`）
- 测试：`tests/test_shutdown_hooks.py`

- [ ] **步骤 1：写失败测试**

创建 `tests/test_shutdown_hooks.py`：

```python
# -*- coding: utf-8 -*-
"""系统退出相关停止钩子测试（reminder / bus）。"""
import threading

from LLM import reminder, bus


def test_reminder_start_clears_then_stop_sets_event():
    # 重置为全新事件，避免测试间状态残留
    reminder._stop_evt = threading.Event()
    reminder.start()
    try:
        assert not reminder._stop_evt.is_set()   # start 必须清标志
        reminder.stop()
        assert reminder._stop_evt.is_set()        # stop 必须置位
    finally:
        reminder.stop()                           # 保证线程退出，不泄漏


def test_bus_stop_sets_flag():
    bus._stop = False
    bus.stop()
    assert bus._stop is True
```

- [ ] **步骤 2：运行测试确认失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_shutdown_hooks.py -q`
预期：FAIL——`AttributeError: module 'LLM.reminder' has no attribute '_stop_evt'`、`AttributeError: module 'LLM.bus' has no attribute '_stop'`

- [ ] **步骤 3：reminder.py 加停止钩子**

在 `LLM/reminder.py` 的 `_tick = 15` 附近新增：

```python
_stop_evt = threading.Event()   # 停止信号：stop() 置位后调度线程退出
```

将 `_run()` 的循环与休眠改为事件等待：

```python
def _run():
    while not _stop_evt.is_set():
        try:
            settings = db.get_settings()
            if settings.get("reminder_enabled", True):
                _tick_once(settings)
            # 事件记忆 TTL 清理（不占对话链路）
            expired = db.cleanup_expired_memories()
            if expired:
                audit.log("memory_change", action="expire", count=expired, note="TTL 到期自动清除")
        except Exception as e:
            audit.log("reminder", action="tick_error", error=str(e))
        _stop_evt.wait(_tick)      # 置位后立即醒来退出，无需等整轮 tick
```

将 `start()` 改为清标志后启动，并新增 `stop()`：

```python
def start():
    """启动调度线程（幂等）。"""
    _stop_evt.clear()
    t = threading.Thread(target=_run, name="reminder-scheduler", daemon=True)
    t.start()
    return t


def stop():
    """请求停止：置位事件，_run 循环内检查后退出（不阻塞等待线程）。"""
    _stop_evt.set()
```

- [ ] **步骤 4：bus.py 加停止标志**

在 `LLM/bus.py` 的模块级变量区新增：

```python
_stop = False                   # 停止标志：stop() 置 True 后 drain 循环退出
```

将 `_drain()` 的 `while True:` 改为 `while not _stop:`，并新增：

```python
def stop():
    """停止扇出任务（置位标志，drain 循环退出）。"""
    global _stop
    _stop = True
```

- [ ] **步骤 5：运行测试确认通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_shutdown_hooks.py -q`
预期：`2 passed`

- [ ] **步骤 6：Commit**

```bash
git add LLM/reminder.py LLM/bus.py tests/test_shutdown_hooks.py
git commit -m "feat: reminder/bus 增加停止钩子供系统退出使用"
```

---

## 任务 2：GET /api/modules/status 聚合接口（TDD）

**文件：**
- 修改：`LLM/server.py`（路由区，`/api/health` 之后）
- 测试：`tests/test_modules_status.py`

- [ ] **步骤 1：写失败测试**

创建 `tests/test_modules_status.py`：

```python
# -*- coding: utf-8 -*-
"""GET /api/modules/status 聚合接口测试。"""
import os
from fastapi.testclient import TestClient

os.environ.setdefault("DEEPSEEK_API_KEY", "sk-test")   # 顶层 OpenAI 构造需 key 非空

from LLM.server import app   # noqa: E402


def test_modules_status_shape():
    with TestClient(app) as c:
        r = c.get("/api/modules/status")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        mods = data["modules"]
        assert set(mods.keys()) == {"voice", "embed", "ragstore", "graph"}
        # voice：status 字段存在（running/stopped/unavailable 之一）
        assert mods["voice"]["status"] in ("running", "stopped", "unavailable")
        # 其余三个：available 布尔字段存在
        for k in ("embed", "ragstore", "graph"):
            assert "available" in mods[k]
            assert isinstance(mods[k].get("missing"), list)
```

**说明：** `tests/conftest.py` 的 autouse fixture 会把 `db.DB_PATH` 隔离到临时目录；测试环境无 DASHSCOPE key / 无 kuzu 时，`embed/ragstore/graph` 应降级返回 `available=False` 而非抛错——这正是接口要保证的稳健性。

- [ ] **步骤 2：运行测试确认失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_modules_status.py -q`
预期：FAIL——`AttributeError: 'TestClient' object has no attribute 'get'` 或 404（路由不存在）

- [ ] **步骤 3：server.py 实现聚合接口**

在 `LLM/server.py` 的 `/api/health` 路由之后新增：

```python
@app.get("/api/modules/status")
async def modules_status():
    """可选模块状态聚合：语音 / embedding / RAG 存储 / 知识图谱。
    各模块缺失依赖时自行降级（available=False / status=unavailable），接口照常返回。"""
    from . import embed as e, ragstore, graph as g
    return {"ok": True, "modules": {
        "voice":    voice_api.get_status(),
        "embed":    e.status(),
        "ragstore": ragstore.status(),
        "graph":    g.status(),
    }}
```

- [ ] **步骤 4：运行测试确认通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_modules_status.py -q`
预期：`1 passed`（注意：TestClient 上下文会触发 lifespan，`_seed_demo` 会写临时库、`reminder.start()` 起线程——测试结束后 daemon 线程随进程退出，无影响）

- [ ] **步骤 5：Commit**

```bash
git add LLM/server.py tests/test_modules_status.py
git commit -m "feat: 新增 GET /api/modules/status 聚合可选模块状态"
```

---

## 任务 3：POST /api/system/shutdown 退出端点（手动验证）

**文件：**
- 修改：`LLM/server.py`（路由区末尾附近）

- [ ] **步骤 1：实现 shutdown 端点与延迟退出**

在 `LLM/server.py` 路由区（`/api/events` 之前或之后均可）新增：

```python
@app.post("/api/system/shutdown")
async def system_shutdown():
    """系统退出：停提醒线程 → 停语音（释放音频设备）→ 停广播 → 停线程池，
    返回响应后延迟 1 秒 os._exit(0)，保证前端先收到 200 再杀进程。"""
    from . import log as audit
    audit.log("system", action="shutdown", by="nurse")
    reminder.stop()                      # 1. 提醒调度线程（不再触发新提醒）
    voice_api.stop_voice()               # 2. 语音 worker（释放麦克风/扬声器）
    bus.stop()                           # 3. 事件总线扇出
    _bg.shutdown(wait=False)             # 4. 后台任务线程池（不等待，进程将退出）
    asyncio.create_task(_delayed_exit()) # 5. 1 秒后真正退出
    return {"ok": True, "message": "系统正在退出…"}


async def _delayed_exit():
    """延迟退出：给 uvicorn 留出时间把上面这个响应发回前端。"""
    await asyncio.sleep(1.0)
    os._exit(0)
```

**说明：** `asyncio` / `os` 已在 `server.py` 顶部 import；`reminder` / `voice_api` / `bus` / `_bg` 均已在模块顶部 import 或定义，无需新增导入。

- [ ] **步骤 2：语法与导入自检**

运行：`.venv\Scripts\python.exe -c "from LLM.server import app; print('import ok')"`
预期：输出 `import ok`（不启动服务，只验证模块可导入、路由已注册）

- [ ] **步骤 3：手动验证退出流程**

1. 启动后端（项目根目录，后台）：
   ```
   .venv\Scripts\python.exe -m uvicorn LLM.server:app --host 127.0.0.1 --port 8000
   ```
2. 另一终端调用：
   ```
   curl -X POST http://127.0.0.1:8000/api/system/shutdown
   ```
   预期：立即收到 `{"ok":true,"message":"系统正在退出…"}`；约 1 秒后 uvicorn 进程退出（终端回到 shell）。
3. 验证审计：`LLM/data/audit.jsonl` 末尾新增一条 `{"event":"system","action":"shutdown",...}`。
4. 验证数据完好：重启后端后 `GET /api/health` 正常，档案/提醒数据仍在。
5. **完成后杀掉后台 uvicorn**（若步骤 2 后仍在运行）。

- [ ] **步骤 4：Commit**

```bash
git add LLM/server.py
git commit -m "feat: 新增 POST /api/system/shutdown 优雅退出后端服务"
```

---

## 任务 4：前端 modal 基础样式 + 顶栏按钮

**文件：**
- 修改：`UI/index.html`（`<style>` 区、`<header>`、`<body>` 尾部）

- [ ] **步骤 1：CSS——modal 与退出遮罩样式**

在 `UI/index.html` 的 `<style>` 区（`#toasts` 样式之后）追加：

```css
/* ---------- Modal 弹窗（模块状态 / 退出确认共用） ---------- */
.modal-mask {
  position: fixed; inset: 0; background: rgba(10, 10, 20, .65);
  display: flex; align-items: center; justify-content: center; z-index: 1000;
}
.modal {
  background: #21212f; border: 1px solid #3a3a52; border-radius: 14px;
  width: min(480px, 92vw); max-height: 80vh; display: flex; flex-direction: column;
  box-shadow: 0 10px 40px rgba(0,0,0,.5);
}
.modal-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 13px 16px; border-bottom: 1px solid #32324a; font-size: 14px; font-weight: 600;
}
.modal-close { background: none; border: none; color: #9a9ab4; font-size: 18px; cursor: pointer; line-height: 1; }
.modal-close:hover { color: #fff; }
.modal-body { padding: 14px 16px; overflow-y: auto; font-size: 13px; }
.modal-foot { padding: 11px 16px; border-top: 1px solid #32324a; display: flex; gap: 8px; justify-content: flex-end; }
.mod-row {
  display: flex; align-items: center; gap: 10px; padding: 9px 4px;
  border-bottom: 1px solid #2a2a3e; font-size: 13px;
}
.mod-row:last-child { border-bottom: none; }
.mod-row .st { flex: none; font-size: 14px; }
.mod-row .name { flex: none; width: 110px; font-weight: 600; }
.mod-row .desc { flex: 1; color: #9a9ab4; word-break: break-all; }
.mod-row.warn .desc { color: #fcd34d; }
.mod-row.err .desc { color: #fca5a5; }

/* ---------- 顶栏按钮 ---------- */
#mod-btn { margin-left: 10px; }
#mod-btn.warn { background: #7f1d1d; border-color: #ef4444; color: #fecaca; }
#mod-btn.warn:hover { background: #991b1b; }
#exit-btn { background: #dc2626; color: #fff; border: none; border-radius: 8px; padding: 5px 12px; cursor: pointer; font-size: 12.5px; }
#exit-btn:hover { background: #b91c1c; }
#exit-btn:disabled { background: #555; cursor: not-allowed; }

/* ---------- 已退出全屏遮罩 ---------- */
#exit-overlay {
  position: fixed; inset: 0; background: #101018; z-index: 2000;
  display: flex; align-items: center; justify-content: center; flex-direction: column; gap: 14px;
}
.exit-card { text-align: center; color: #e0e0e0; }
.exit-card h2 { font-size: 22px; margin-bottom: 10px; }
.exit-card p { color: #9a9ab4; font-size: 13px; }
```

- [ ] **步骤 2：HTML——顶栏加两个按钮**

在 `<header>` 的 `<span id="health" class="health">…</span>` 之后追加：

```html
<button id="mod-btn" class="btn gray sm" onclick="openModulesModal()">🔍 模块状态</button>
<button id="exit-btn" onclick="askExit()">⏻ 退出</button>
```

- [ ] **步骤 3：HTML——modal 容器与已退出遮罩**

在 `<div id="toasts"></div>` 之后追加：

```html
<div id="modal-mask" class="modal-mask" style="display:none">
  <div class="modal">
    <div class="modal-head"><span id="modal-title"></span><button class="modal-close" onclick="closeModal()">×</button></div>
    <div id="modal-body" class="modal-body"></div>
    <div id="modal-foot" class="modal-foot"></div>
  </div>
</div>

<div id="exit-overlay" style="display:none">
  <div class="exit-card">
    <h2>⏻ 系统已退出</h2>
    <p>后端服务已停止，可刷新页面重新连接。</p>
  </div>
</div>
```

- [ ] **步骤 4：Commit**

```bash
git add UI/index.html
git commit -m "feat: 前端 modal 基础样式与顶栏模块状态/退出按钮"
```

---

## 任务 5：前端模块状态弹窗逻辑

**文件：**
- 修改：`UI/index.html`（`<script>` 区，健康检查 `ping()` 附近）

- [ ] **步骤 1：实现 modal 通用开关函数**

在 `<script>` 区（`toast()` 函数之后）新增：

```js
// ================================================================
// Modal 弹窗（模块状态 / 退出确认共用）
// ================================================================
function openModal(title, bodyHtml, footHtml = "") {
  document.getElementById("modal-title").textContent = title;
  document.getElementById("modal-body").innerHTML = bodyHtml;
  document.getElementById("modal-foot").innerHTML = footHtml;
  document.getElementById("modal-mask").style.display = "flex";
}
function closeModal() {
  document.getElementById("modal-mask").style.display = "none";
}
document.getElementById("modal-mask").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) closeModal();   // 点遮罩空白处关闭
});
```

- [ ] **步骤 2：实现模块状态加载与渲染**

在 `ping()` 函数之后新增：

```js
// ================================================================
// 模块状态弹窗
// ================================================================
async function openModulesModal() {
  openModal("🔍 模块状态检测", '<div class="hint">检测中…</div>',
    '<button class="btn" onclick="loadModuleStatus()">🔄 重新检测</button><button class="btn gray" onclick="closeModal()">关闭</button>');
  await loadModuleStatus();
}

async function loadModuleStatus() {
  const body = document.getElementById("modal-body");
  const btn = document.getElementById("mod-btn");
  btn.classList.remove("warn");
  try {
    const { modules } = await api("/api/modules/status");
    const rows = [];
    let hasBad = false;

    // 语音链路：running / stopped(设置关闭=停用，非故障) / stopped / unavailable
    const v = modules.voice || {};
    if (v.status === "running") {
      rows.push(`<div class="mod-row"><span class="st">✅</span><span class="name">语音链路</span><span class="desc">运行中</span></div>`);
    } else if (v.status === "stopped" && v.voice_enabled === false) {
      rows.push(`<div class="mod-row warn"><span class="st">⚪</span><span class="name">语音链路</span><span class="desc">已停用（设置中关闭，非故障）</span></div>`);
    } else if (v.status === "stopped") {
      rows.push(`<div class="mod-row warn"><span class="st">⚠️</span><span class="name">语音链路</span><span class="desc">未启动</span></div>`);
      hasBad = true;
    } else {
      rows.push(`<div class="mod-row err"><span class="st">❌</span><span class="name">语音链路</span><span class="desc">${escapeHtml(v.reason || "不可用")}</span></div>`);
      hasBad = true;
    }

    // embed / ragstore / graph：available 布尔
    const simple = [
      ["embed", "Embedding 向量"],
      ["ragstore", "RAG 存储"],
      ["graph", "知识图谱"],
    ];
    for (const [key, name] of simple) {
      const m = modules[key] || {};
      if (m.available) {
        rows.push(`<div class="mod-row"><span class="st">✅</span><span class="name">${name}</span><span class="desc">可用</span></div>`);
      } else {
        const missing = (m.missing || []).join("；") || "未知原因";
        rows.push(`<div class="mod-row err"><span class="st">❌</span><span class="name">${name}</span><span class="desc">缺失依赖：${escapeHtml(missing)}</span></div>`);
        hasBad = true;
      }
    }

    body.innerHTML = rows.join("");
    if (hasBad) btn.classList.add("warn");   // 任一模块异常 → 按钮警示色
  } catch (e) {
    body.innerHTML = '<div class="hint">无法获取模块状态（后端离线）</div>';
    btn.classList.remove("warn");
  }
}
```

- [ ] **步骤 3：页面加载时预检模块状态（刷新按钮警示色）**

在 `init()` 的 `ping()` 调用后追加 `loadModuleStatus();`（此时弹窗未打开，只更新按钮警示色；打开弹窗时 `openModulesModal` 会再次拉取）。将 `init()` 改为：

```js
      loadChatHistory();
      loadMemory(); loadReminders(); loadToolLog(); loadSettings(); ping();
      loadModuleStatus();   // 预检模块状态：有异常时顶栏按钮变红
      connectEvents();
      setInterval(ping, 30000);
```

- [ ] **步骤 4：Commit**

```bash
git add UI/index.html
git commit -m "feat: 前端模块状态弹窗与按钮警示色"
```

---

## 任务 6：前端系统退出流程

**文件：**
- 修改：`UI/index.html`（`<script>` 区，`loadModuleStatus()` 之后；`connectEvents()`）

- [ ] **步骤 1：实现退出确认与执行**

在 `loadModuleStatus()` 之后新增：

```js
// ================================================================
// 系统退出
// ================================================================
function askExit() {
  openModal("⏻ 确认退出系统",
    '<p>确认退出系统？将停止提醒/语音服务并关闭后端。</p>',
    '<button class="btn gray" onclick="closeModal()">取消</button><button class="btn red" onclick="doExit()">确认退出</button>');
}

async function doExit() {
  const body = document.getElementById("modal-body");
  body.innerHTML = '<div class="hint">正在退出…</div>';
  try {
    await api("/api/system/shutdown", { method: "POST" });
  } catch (e) {
    toast("⚠️ 后端已离线", "无需退出", "err");
  }
  showExitOverlay();
}

function showExitOverlay() {
  closeModal();
  document.getElementById("exit-overlay").style.display = "flex";
  document.getElementById("exit-btn").disabled = true;
  document.getElementById("mod-btn").disabled = true;
  if (window._es) window._es.close();          // 断开 SSE，停止自动重连
  if (window._pingTimer) clearInterval(window._pingTimer);   // 停止健康轮询
  document.getElementById("health").textContent = "● 已退出";
  document.getElementById("health").className = "health off";
}
```

- [ ] **步骤 2：把 EventSource 与 ping 定时器改为可停止的模块级引用**

在 `<script>` 区顶部（`let chatMessages = []` 附近）新增：

```js
    let _es = null;            // EventSource 实例（退出时需 close）
    let _pingTimer = null;     // 健康轮询定时器（退出时需 clear）
```

将 `connectEvents()` 中的：

```js
const es = new EventSource(API + "/api/events");
```

改为：

```js
window._es = _es = new EventSource(API + "/api/events");
```

并把该函数内其余所有 `es.addEventListener(` 引用改为 `_es.addEventListener(`（共 3 处：`reminder` / `reminder_confirmed` / `alarm`），`es.onerror` 改为 `_es.onerror`——否则 `const es` 被移除后这些引用会抛 `ReferenceError`。

将 `init()` 中的 `setInterval(ping, 30000);` 改为：

```js
      window._pingTimer = _pingTimer = setInterval(ping, 30000);
```

（脚本顶层 `let` 声明**不会**成为 `window` 属性，所以必须显式写 `window._pingTimer =`，`showExitOverlay` 才能读到并 clear 它；同理 `connectEvents` 里已用 `window._es = _es = new EventSource(...)` 显式挂载。）

（`showExitOverlay` 已通过 `window._es` / `window._pingTimer` 访问，确保两处引用一致。）

- [ ] **步骤 3：语法自检**

在浏览器打开 `UI/index.html`（需先启动后端），控制台应无报错；点「🔍 模块状态」弹窗显示 4 行模块状态。

- [ ] **步骤 4：Commit**

```bash
git add UI/index.html
git commit -m "feat: 前端系统退出确认弹窗与已退出遮罩"
```

---

## 任务 7：集成验证 + 开发日志

**文件：**
- 修改：`docs/2.pre/log.md`

- [ ] **步骤 1：全量测试**

运行：`.venv\Scripts\python.exe -m pytest tests -q`
预期：全部通过（原有 6 个测试文件 + 新增 2 个，无回归）

- [ ] **步骤 2：手动端到端验证**

1. 启动后端：`.venv\Scripts\python.exe -m uvicorn LLM.server:app --host 127.0.0.1 --port 8000`
2. 浏览器打开 `UI/index.html`：
   - 顶栏出现「🔍 模块状态」「⏻ 退出」按钮；
   - 点「模块状态」→ 弹窗显示 4 个模块（语音/Embedding/RAG/知识图谱），全部 ✅ 或降级显示原因；
   - 点「重新检测」→ 列表刷新；
   - 点「退出」→ 确认弹窗 → 取消无副作用；再点 → 确认 → 全屏「系统已退出」遮罩，按钮禁用，SSE 断开；
   - 后端进程约 1 秒后退出；
   - 刷新页面（后端已停）→ 顶栏「后端离线」，点「模块状态」→ 弹窗显示离线提示。
3. 完成后杀掉后台 uvicorn。

- [ ] **步骤 3：追加开发日志**

在 `docs/2.pre/log.md` 末尾按既有格式追加当日记录，内容要点：
- 新增 `GET /api/modules/status` 聚合接口（voice/embed/ragstore/graph）；
- 新增 `POST /api/system/shutdown`：reminder/bus 停止钩子 + 延迟 1s `os._exit(0)`；
- 前端顶栏「模块状态」弹窗（含警示色 + 重新检测）与「退出」按钮（确认弹窗 + 已退出遮罩）；
- 测试：`tests/test_shutdown_hooks.py`、`tests/test_modules_status.py`。

- [ ] **步骤 4：Commit**

```bash
git add docs/2.pre/log.md
git commit -m "docs: 记录模块状态弹窗与系统退出按钮开发日志"
```
