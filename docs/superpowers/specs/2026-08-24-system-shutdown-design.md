# 系统退出按钮设计文档

日期：2026-08-24
状态：已批准（用户确认方案 A：后端 shutdown 端点 + 显式资源清理）

## 目标

前端提供「系统退出」按钮：按下后**优雅退出整个后端服务**，停止全部后台任务、
释放所有资源（语音设备 / 调度线程 / 线程池 / SSE 连接 / 数据库），前端显示已退出遮罩。

## 背景：当前资源清单

后端 `LLM.server:app`（uvicorn 启动）运行期持有以下资源：

| 资源 | 现状 | 停止方式 |
|---|---|---|
| 提醒调度线程（`reminder.py`） | daemon 线程，15s tick 死循环 | **无停止钩子**，需新增 `stop()` |
| 语音 worker（`voice_api.py`） | `VoiceWorker` 线程占用音频设备 | 已有 `voice_api.stop_voice()` |
| 事件总线 drain 任务（`bus.py`） | asyncio 后台任务，SSE 扇出 | lifespan 结束时 cancel；需提供 `bus.stop()` |
| 后台任务线程池 `_bg` | `ThreadPoolExecutor(max_workers=4)` | 需 `_bg.shutdown(wait=False)` |
| SQLite（`db.py`） | WAL 模式，每次操作临时连接 | 连接用完即关，无需额外清理 |
| SSE 订阅连接 | 前端 EventSource | 前端主动断开 |

现状 `lifespan` 的 `yield` 之后只调用了 `voice_api.stop_voice()` 和 `drain_task.cancel()`，
没有覆盖提醒线程和线程池，也没有面向「用户主动退出」的入口。

## 方案（用户已确认：方案 A）

- 后端新增 `POST /api/system/shutdown`：**先显式清理全部资源 → 返回响应 → 延迟 1 秒后 `os._exit(0)`**。
  保证前端先收到「退出成功」再真正杀进程；跨平台可靠（不依赖 uvicorn 信号处理）。
- 前端顶栏红色「⏻ 退出」按钮 → 自定义确认弹窗 → 成功后全屏遮罩「系统已退出」+ 断开 EventSource + 停止 ping 轮询。

## 后端改动（LLM/）

### 1. `reminder.py` — 新增停止钩子

```python
import threading
_stop_evt = threading.Event()

def start():
    """启动调度线程（幂等）。"""
    _stop_evt.clear()
    t = threading.Thread(target=_run, name="reminder-scheduler", daemon=True)
    t.start()
    return t

def stop():
    """请求停止：置位事件，_run 循环内检查后退出。"""
    _stop_evt.set()
```

`_run()` 的 `while True` 改为 `while not _stop_evt.is_set()`；`time.sleep(_tick)` 改为
`_stop_evt.wait(_tick)`（置位后立即醒来，退出更及时，无需等整轮 tick）。

### 2. `bus.py` — 新增停止钩子

```python
_stop = False

def stop():
    """停止扇出任务（设置标志，drain 循环退出）。"""
    global _stop
    _stop = True
```

`_drain()` 的 `while True` 改为 `while not _stop`。

### 3. `server.py` — 新增 shutdown 端点

```python
@app.post("/api/system/shutdown")
async def system_shutdown():
    from . import log as audit
    audit.log("system", action="shutdown", by="nurse")
    # 1. 停提醒调度线程（不再触发新提醒）
    reminder.stop()
    # 2. 停语音 worker（释放麦克风/扬声器）
    voice_api.stop_voice()
    # 3. 停事件总线扇出
    bus.stop()
    # 4. 停后台任务线程池（不等待，进程即将退出）
    _bg.shutdown(wait=False)
    # 5. 延迟 1 秒让本响应先送达前端，再真正退出进程
    asyncio.create_task(_delayed_exit())
    return {"ok": True, "message": "系统正在退出…"}

async def _delayed_exit():
    await asyncio.sleep(1.0)
    os._exit(0)
```

要点：
- **先响应用户、后杀进程**：`os._exit(0)` 放在 `create_task` 里延迟 1 秒，前端能收到 200 响应；
- **不依赖 uvicorn 信号**：`os._exit(0)` 直接终止进程，Windows / Linux 均可靠；
- **审计**：退出动作落 `audit.jsonl`（事件类型 `system`，`action=shutdown`）；
- **幂等**：进程已退出后端点自然不可达，前端按离线处理。

## 前端改动（UI/index.html，单文件内完成）

### 顶栏按钮

- 顶栏右侧（健康状态旁）新增红色按钮 **「⏻ 退出」**（id: `exit-btn`）。

### 确认弹窗

- 点击退出按钮 → 复用「模块状态」弹窗同款 modal 样式，展示：
  「确认退出系统？将停止提醒/语音服务并关闭后端。」
  - 按钮：[取消] / [确认退出]（确认按钮红色）。

### 退出流程

```
点击退出 → 确认弹窗 → 确认 → POST /api/system/shutdown
  ├─ 成功（收到响应）→ 显示全屏遮罩「系统已退出」+ 断开 EventSource + clearInterval(ping)
  └─ 失败/离线    → toast「后端已离线，无需退出」+ 同样显示已退出遮罩并停止轮询
```

- 全屏遮罩：覆盖整个页面、隐藏所有功能入口、居中显示「⏻ 系统已退出」+ 提示
  「后端服务已停止，可刷新页面重新连接」。
- 退出按钮在遮罩显示后禁用（幂等防重复点击）。

## 错误处理与边界

- 退出请求失败（后端已离线）→ toast 提示 + 仍显示已退出遮罩、停止轮询（此时后端确实不在）。
- 二次点击 → 按钮已禁用，无副作用。
- 退出后 EventSource 的 `onerror` 重连机制：前端主动 `es.close()` 并停止 `setInterval(ping)`，
  避免遮罩下后台持续重连。

## 测试

- 后端：`curl -X POST http://127.0.0.1:8000/api/system/shutdown` →
  收到 `{"ok":true,...}`，观察审计日志出现 `system/shutdown`，进程约 1 秒后退出；
  SQLite 数据完好（WAL checkpoint 正常落盘）。
- 前端：
  - 正常流程：点击 → 确认 → 遮罩出现 → 后端进程消失；
  - 后端离线时点击：toast 提示 + 遮罩；
  - 重复点击：无副作用。

## 范围（YAGNI）

- 不做：退出鉴权（内网演示环境）、倒计时取消按钮、退出后自动重启、其他端联动。
- 与「模块状态弹窗」共用 modal 基础样式（遮罩/卡片/按钮），两份改动在同一批实现中落地。
