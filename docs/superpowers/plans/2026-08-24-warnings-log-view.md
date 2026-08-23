# 模块状态弹窗内「警告/错误日志」实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 在模块状态弹窗内加「📋 警告/错误日志」按钮，按下后在同一弹窗内展示后端 `audit.jsonl` 中的警告/错误类日志（服务端过滤），便于排查系统运行中出过的问题。

**架构：** 后端 `log.py` 新增 `read_warnings(limit)` 读 `audit.jsonl` 末尾 N 条并按规则过滤警告/错误类；`server.py` 新增 `GET /api/logs/warnings` 路由。前端 `openModulesModal()` 的 footer 加「📋 警告/错误日志」按钮，点击后 `loadWarningsLog()` 拉取并渲染到弹窗 body（模块状态 ⇄ 日志列表两视图切换）。

**技术栈：** Python 3.11+ · FastAPI · pytest + TestClient（httpx）· 原生 JS（零构建单文件）

**规格：** `docs/superpowers/specs/2026-08-24-module-status-modal-design.md`（含补充设计章节）

---

## 文件结构

**修改：**
- `LLM/log.py` — 新增 `read_warnings(limit)` 读取 + 过滤函数
- `LLM/server.py` — 新增 `GET /api/logs/warnings` 路由
- `UI/index.html` — 弹窗 footer 加按钮、`loadWarningsLog()` 函数、日志渲染样式
- `docs/log.md` — 追加开发日志

**新建（测试）：**
- `tests/test_log_warnings.py` — `read_warnings` 过滤逻辑单元测试

**注意：** `read_warnings` 的测试需要隔离 `audit.jsonl` 路径——用 monkeypatch `log.AUDIT_LOG` 指向 tmp 文件。

---

## 任务 1：log.py 的 read_warnings + 路由（TDD）

**文件：**
- 修改：`LLM/log.py`、`LLM/server.py`
- 测试：`tests/test_log_warnings.py`

- [ ] **步骤 1：写失败测试**

创建 `tests/test_log_warnings.py`：

```python
# -*- coding: utf-8 -*-
"""log.read_warnings 过滤逻辑测试。"""
import json

from LLM import log


def _write(tmp_path, records):
    p = tmp_path / "audit.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return str(p)


def test_filters_errors_and_alarms(tmp_path, monkeypatch):
    records = [
        {"ts": "t1", "event": "voice_state", "status": "running"},          # 正常，应过滤
        {"ts": "t2", "event": "voice_error", "error": "设备故障"},           # error 事件 → 保留
        {"ts": "t3", "event": "reminder", "action": "tick_error", "error": "x"},  # action 含 error → 保留
        {"ts": "t4", "event": "alarm", "level": "warning"},                  # alarm → 保留
        {"ts": "t5", "event": "voice_degraded"},                             # voice_degraded → 保留
        {"ts": "t6", "event": "chat", "action": "turn"},                     # 正常 → 过滤
        {"ts": "t7", "event": "system", "action": "shutdown"},               # 正常 → 过滤
    ]
    monkeypatch.setattr(log, "AUDIT_LOG", _write(tmp_path, records))
    out = log.read_warnings(limit=50)
    assert [r["ts"] for r in out] == ["t2", "t3", "t4", "t5"]   # 按写入顺序保留


def test_limit_and_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(log, "AUDIT_LOG", str(tmp_path / "nonexistent.jsonl"))
    assert log.read_warnings(limit=10) == []                     # 文件不存在 → 空

    records = [{"ts": f"t{i}", "event": "alarm", "level": "warn"} for i in range(5)]
    monkeypatch.setattr(log, "AUDIT_LOG", _write(tmp_path, records))
    out = log.read_warnings(limit=3)                             # limit 生效
    assert [r["ts"] for r in out] == ["t2", "t3", "t4"]


def test_bad_json_skipped(tmp_path, monkeypatch):
    p = tmp_path / "audit.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"ts": "t1", "event": "alarm", "level": "warn"}\n')
        f.write("not-json\n")                                     # 坏行 → 跳过
        f.write('{"ts": "t2", "event": "voice_error"}\n')
        f.write('123\n')                                          # 合法 JSON 但非对象 → 跳过
    monkeypatch.setattr(log, "AUDIT_LOG", str(p))
    out = log.read_warnings(limit=50)
    assert [r["ts"] for r in out] == ["t1", "t2"]
```

- [ ] **步骤 2：运行测试确认失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_log_warnings.py -q`
预期：FAIL——`AttributeError: module 'LLM.log' has no attribute 'read_warnings'`

- [ ] **步骤 3：log.py 实现 read_warnings**

在 `LLM/log.py` 末尾追加：

```python
def read_warnings(limit: int = 50) -> list[dict]:
    """读 audit.jsonl 末尾 limit 条，过滤警告/错误类事件（*_error / alarm / voice_degraded / *warn* / level 含 warn / 含 error 字段）。

    命中任一条规则即视为警告/错误：action 或 event 含 "error"；event == "alarm"；
    level 含 "warn"；event == "voice_degraded"；存在 error 字段。
    文件不存在 → 空列表；单条 JSON 解析失败 → 跳过。
    """
    out = []
    try:
        with open(AUDIT_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if not isinstance(rec, dict):
                        continue   # 合法 JSON 但非对象（数组/数字/字符串/null）→ 跳过
                except Exception:
                    continue   # 坏行跳过
                if _is_warning(rec):
                    out.append(rec)
    except FileNotFoundError:
        return []
    return out[-limit:] if limit > 0 else out


def _is_warning(rec: dict) -> bool:
    action = str(rec.get("action") or "")
    event = str(rec.get("event") or "")
    level = str(rec.get("level") or "")
    if "error" in action or "error" in event:
        return True
    if event == "alarm" or "warn" in level:
        return True
    if event == "voice_degraded":
        return True
    if "error" in rec:
        return True
    return False
```

- [ ] **步骤 4：server.py 新增路由**

在 `LLM/server.py` 路由区（`/api/modules/status` 之后）新增：

```python
@app.get("/api/logs/warnings")
async def logs_warnings(limit: int = Query(50)):
    """最近警告/错误审计日志（服务端过滤，供前端排查用）。"""
    from . import log as audit
    return {"ok": True, "logs": audit.read_warnings(limit=limit)}
```

- [ ] **步骤 5：运行测试确认通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_log_warnings.py -q`
预期：`3 passed`

- [ ] **步骤 6：手动验证接口**

启动后端 → `curl.exe http://127.0.0.1:8000/api/logs/warnings` → 返回 `{"ok":true,"logs":[...]}`；
`curl.exe "http://127.0.0.1:8000/api/logs/warnings?limit=3"` → 最多 3 条。
（启动/清理方式：Start-Process + Stop-Process，参考前批次任务 3；验证完杀掉进程。）

- [ ] **步骤 7：Commit**

```bash
git add LLM/log.py LLM/server.py tests/test_log_warnings.py
git commit -m "feat: 新增 GET /api/logs/warnings 读取警告/错误审计日志"
```

---

## 任务 2：前端弹窗内警告/错误日志视图

**文件：**
- 修改：`UI/index.html`（`<style>` 区、`loadModuleStatus()` 区域）

- [ ] **步骤 1：CSS——日志行样式**

在 `<style>` 区（`.mod-row` 相关样式之后）追加：

```css
/* ---------- 警告/错误日志行 ---------- */
.log-row {
  display: flex; align-items: flex-start; gap: 10px; padding: 8px 4px;
  border-bottom: 1px solid #2a2a3e; font-size: 12.5px;
}
.log-row:last-child { border-bottom: none; }
.log-row .lt { flex: none; font-size: 13px; }
.log-row .lb { flex: 1; min-width: 0; }
.log-row .ltime { color: #6b6b84; font-size: 11.5px; }
.log-row .ltext { color: #e0e0e0; word-break: break-all; margin-top: 2px; }
.log-row.err .ltext { color: #fca5a5; }
.log-row.warn .ltext { color: #fcd34d; }
.log-view-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; font-size: 13px; }
.log-view-head a { color: #7dd3fc; cursor: pointer; font-size: 12.5px; }
```

- [ ] **步骤 2：JS——openModulesModal 加按钮 + loadWarningsLog**

将 `openModulesModal()` 的 footer 改为带日志按钮（在 `loadModuleStatus()` 之后、`askExit()` 之前）：

```js
async function openModulesModal() {
  openModal("🔍 模块状态检测", '<div class="hint">检测中…</div>',
    '<button class="btn" onclick="loadModuleStatus()">🔄 重新检测</button>' +
    '<button class="btn gray" onclick="loadWarningsLog()">📋 警告/错误日志</button>' +
    '<button class="btn gray" onclick="closeModal()">关闭</button>');
  _lastModuleHtml = "";   // 每次打开弹窗重置快照，避免视图切换回滚过期数据
  await loadModuleStatus();
}
```

在 `loadModuleStatus()` 之后新增：

```js
// ================================================================
// 警告/错误日志（同一弹窗内视图切换）
// ================================================================
let _lastModuleHtml = "";   // 记住模块列表 HTML，便于切回

async function loadWarningsLog() {
  const body = document.getElementById("modal-body");
  if (!_lastModuleHtml) _lastModuleHtml = body.innerHTML;   // 首次进入时记住模块视图
  body.innerHTML = '<div class="hint">加载日志中…</div>';
  try {
    const { logs } = await api("/api/logs/warnings");
    if (!logs || !logs.length) {
      body.innerHTML = '<div class="hint">✅ 暂无警告/错误记录</div>';
    } else {
      const rows = logs.map(l => {
        const isErr = !!(l.error) || /error/i.test(l.event || "") || /error/i.test(l.action || "");
        const isWarn = !isErr && ((l.event === "alarm") || /warn/i.test(l.level || ""));
        const cls = isErr ? "err" : isWarn ? "warn" : "";
        const icon = isErr ? "❌" : isWarn ? "⚠️" : "ℹ️";
        const ev = l.event || "unknown";
        const act = l.action ? ` · ${l.action}` : "";
        const detail = l.error ? `<br><span class="ltext">${escapeHtml(l.error)}</span>` : "";
        return `<div class="log-row ${cls}"><span class="lt">${icon}</span><span class="lb">
          <span class="ltime">${escapeHtml(l.ts || "")} · ${escapeHtml(ev)}${escapeHtml(act)}</span>${detail}</span></div>`;
      }).join("");
      body.innerHTML = `<div class="log-view-head"><span>最近 ${logs.length} 条警告/错误</span><a onclick="backToModules()">← 返回模块状态</a></div>${rows}`;
    }
  } catch (e) {
    body.innerHTML = '<div class="hint">无法获取日志（后端离线）</div>';
  }
}

function backToModules() {
  const body = document.getElementById("modal-body");
  body.innerHTML = _lastModuleHtml || '<div class="hint">检测中…</div>';
}
```

- [ ] **步骤 3：Commit**

```bash
git add UI/index.html
git commit -m "feat: 模块状态弹窗内查看警告/错误日志视图"
```

---

## 任务 3：集成验证 + 开发日志

**文件：**
- 修改：`docs/log.md`

- [ ] **步骤 1：全量测试**

运行：`.venv\Scripts\python.exe -m pytest tests -q`
预期：全部通过（原 35 + 新增 3 = 38 个测试），无回归。

- [ ] **步骤 2：手动端到端验证**

1. 启动后端 → `curl.exe http://127.0.0.1:8000/api/logs/warnings`；
2. 造一条错误审计（或等待系统自然产生）：`.venv\Scripts\python.exe -c "from LLM import log; log.log('voice_error', error='测试' if False else '手动注入测试错误')"` —— 注意这会写入真实 audit.jsonl，验证后如需清理可接受（审计日志本就是追加的）；
3. 再次 curl → 应包含刚注入的条目；
4. 浏览器打开 `UI/index.html` → 点「🔍 模块状态」→ 弹窗 footer 有「📋 警告/错误日志」→ 点击 → 显示日志列表（红色错误/橙色告警）→ 「← 返回模块状态」切回正常；
5. 验证完杀掉后台 uvicorn。

- [ ] **步骤 3：追加开发日志**

在 `docs/log.md` 末尾按既有格式追加当日记录，内容要点：
- 新增 `GET /api/logs/warnings`：`log.read_warnings()` 读 audit.jsonl 末尾 N 条、服务端过滤警告/错误类（*_error / alarm / voice_degraded / warn）；
- 前端模块状态弹窗内「📋 警告/错误日志」按钮 + 视图切换（模块状态 ⇄ 日志列表）；
- 测试：`tests/test_log_warnings.py`（过滤规则 / limit / 文件缺失 / 坏行跳过）。

- [ ] **步骤 4：Commit**

```bash
git add docs/log.md
git commit -m "docs: 记录模块状态弹窗警告/错误日志功能开发日志"
```
