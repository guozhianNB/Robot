# 前端多端重构实现计划（Vue3 双端 + 会话状态 + /api/alarm）

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 将单文件 HTML 前端重构为 Vue3 + Vite + TS 的 pnpm workspace 双端工程（admin 后台数据端 + kiosk 车载交互端 + shared 共享层），后端新增 `/api/alarm` 与 `/api/session/user`（active_uid + 锁定模式）。

**架构：** 交互闭环在后端（语音链路不依赖前端）；前端既是订阅者（SSE 观察状态）也是操作者（REST 发指令）。`shared/` 是 SSE 事件协议与 REST client 的唯一事实来源。会话状态（`active_uid`/锁定）由后端统一维护并广播 `user_changed`。

**技术栈：** Vue 3 + Vite + TypeScript + pnpm workspace（前端）；FastAPI + pytest（后端，仅小改）；vitest（前端单测）。

**规格：** `docs/superpowers/specs/2026-08-27-frontend-multi-end-design.md`

---

## 文件结构（本计划创建/修改）

**后端（修改 4 个文件 + 新增 2 个测试）：**

- 修改：`LLM/voice/identity.py` — `effective_uid()` 增加 `locked_uid` 参数与锁定分支
- 修改：`LLM/voice/worker.py` — 新增 `self.locked_uid` 字段；`_handle_speech` 使用锁定逻辑；识别到锁定外用户时记审计提示
- 修改：`LLM/voice_api.py` — 新增模块级会话状态 + `set_session_uid()` / `get_session_uid()`
- 修改：`LLM/server.py` — 新增 `GET/POST /api/session/user`、`POST /api/alarm` 端点
- 测试：`tests/test_identity_lock.py` — effective_uid 锁定行为矩阵
- 测试：`tests/test_session_api.py` — 会话端点 + alarm 端点（TestClient）

**前端（全部新建，`frontend/` 目录）：**

```
frontend/
  package.json                     # workspace root
  pnpm-workspace.yaml              # packages: shared / admin / kiosk
  tsconfig.base.json
  .gitignore                       # node_modules / dist
  packages/
    shared/
      package.json
      tsconfig.json
      vitest.config.ts
      src/
        index.ts                   # 包出口
        events.ts                  # ★ SSE 事件类型 + parseBusEvent
        api/
          client.ts                # fetch 封装（REST）
          session.ts               # /api/session/user
          alarm.ts                 # /api/alarm
      tests/
        events.test.ts
        client.test.ts
    admin/
      package.json
      tsconfig.json
      vite.config.ts               # dev proxy /api → :8000
      index.html
      src/
        main.ts
        App.vue                    # 页签壳（沿用单页风格，无 router）
        pages/
          OverviewPage.vue         # 监控总览（新增）
          ChatPage.vue             # 对话
          MemoriesPage.vue         # 记忆
          RemindersPage.vue        # 提醒
          ToolLogPage.vue          # 工具日志
          SettingsPage.vue         # 设置
          VoiceStatusPage.vue      # 语音状态（新增）
    kiosk/
      package.json
      tsconfig.json
      vite.config.ts
      index.html
      src/
        main.ts
        App.vue                    # 单页沉浸式
        components/
          VoiceStatusBar.vue       # 状态条 + active_uid + 锁定标记
          ChatArea.vue             # 对话区 + 输入框 + 快捷短语
          UserSwitcher.vue         # 切换用户弹层
          SettingsSheet.vue        # 设置弹层
          SosButton.vue            # 紧急呼叫
          ReminderBanner.vue       # 提醒卡片 + 确认
```

---

## 任务 1：后端 — `effective_uid` 锁定分支（TDD）

**文件：**
- 修改：`LLM/voice/identity.py`
- 测试：`tests/test_identity_lock.py`

- [ ] **步骤 1：编写失败的测试**

创建 `tests/test_identity_lock.py`：

```python
# -*- coding: utf-8 -*-
"""effective_uid 锁定模式行为矩阵（规格 §8.2）。

- 未锁定：声纹识别高置信 → 用识别结果；低置信/未识别 → 沿用当前 uid。
- 锁定：固定返回锁定 uid，声纹识别到其他人也不切换。
"""
from LLM.voice.identity import effective_uid, IdentityVote


def _vote(uid, conf):
    return IdentityVote(candidate_uid=uid, confidence=conf, source="voiceprint")


def test_unlocked_high_confidence_uses_voiceprint():
    assert effective_uid(_vote("elder_002", 0.9), "elder_001") == "elder_002"


def test_unlocked_low_confidence_keeps_current():
    assert effective_uid(_vote(None, 0.1), "elder_001") == "elder_001"


def test_locked_returns_locked_uid_even_if_voiceprint_differs():
    assert effective_uid(_vote("elder_002", 0.9), "elder_001", "elder_003") == "elder_003"


def test_locked_with_none_current():
    assert effective_uid(_vote(None, 0.0), None, "elder_003") == "elder_003"


def test_unlock_recovers_voiceprint_priority():
    # 解锁（locked_uid=None）后恢复声纹优先
    assert effective_uid(_vote("elder_002", 0.9), "elder_001", None) == "elder_002"
```

- [ ] **步骤 2：运行测试验证失败**

运行：`..\.venv\Scripts\python.exe -m pytest tests/test_identity_lock.py -v`
预期：FAIL，`TypeError: effective_uid() got an unexpected keyword argument 'locked_uid'`

- [ ] **步骤 3：编写最少实现**

修改 `LLM/voice/identity.py` 的 `effective_uid`：

```python
def effective_uid(vote: IdentityVote, current_uid: Optional[str],
                  locked_uid: Optional[str] = None) -> Optional[str]:
    """锁定优先：手动锁定时固定返回锁定用户（声纹只提示不切换，规格 D11）；
    未锁定：高置信度用识别结果，低置信度沿用当前 uid（宁问勿猜）。"""
    if locked_uid:
        return locked_uid
    if vote.candidate_uid is not None:
        return vote.candidate_uid
    return current_uid
```

- [ ] **步骤 4：运行测试验证通过**

运行：`..\.venv\Scripts\python.exe -m pytest tests/test_identity_lock.py -v`
预期：PASS，5 passed

- [ ] **步骤 5：Commit**

```bash
git add LLM/voice/identity.py tests/test_identity_lock.py
git commit -m "feat: effective_uid 支持锁定模式（规格 D11）"
```

---

## 任务 2：后端 — worker 锁定字段与审计提示

**文件：**
- 修改：`LLM/voice/worker.py:24,142-146`

- [ ] **步骤 1：给 worker 增加 locked_uid 字段**

在 `LLM/voice/worker.py` 的 `__init__` 中，`self.current_uid = None` 之后加一行：

```python
        self.current_uid = None
        self.locked_uid = None       # 手动锁定用户（None=未锁定，规格 D11）
```

- [ ] **步骤 2：`_handle_speech` 使用锁定逻辑 + 锁定外识别审计提示**

把 `LLM/voice/worker.py:142-146` 改为：

```python
        vote = self.fusion.resolve(seg)
        uid = id_mod.effective_uid(vote, self.current_uid, self.locked_uid)
        # 锁定时识别到锁定外用户：只记审计提示，不切换（规格 §8.2 行为矩阵）
        if self.locked_uid and vote.candidate_uid and vote.candidate_uid != self.locked_uid:
            audit.log("voice_spk", action="locked_ignored", locked=self.locked_uid,
                      detected=vote.candidate_uid, score=round(vote.confidence, 3))
        self.current_uid = uid or self.current_uid
        audit.log("voice_spk", identified=(vote.candidate_uid is not None),
                  uid=vote.candidate_uid, score=round(vote.confidence, 3))
```

- [ ] **步骤 3：运行既有测试确认无回归**

运行：`..\.venv\Scripts\python.exe -m pytest tests/ -q`
预期：PASS（voice 模块无音频依赖的测试不受影响）

- [ ] **步骤 4：Commit**

```bash
git add LLM/voice/worker.py
git commit -m "feat: voice worker 支持锁定模式（locked_uid + 锁定外识别审计提示）"
```

---

## 任务 3：后端 — voice_api 会话访问器 + server 端点（TDD）

**文件：**
- 修改：`LLM/voice_api.py`（新增会话状态 + set/get_session_uid）
- 修改：`LLM/server.py`（新增 3 个端点）
- 测试：`tests/test_session_api.py`

- [ ] **步骤 1：编写失败的测试**

创建 `tests/test_session_api.py`：

```python
# -*- coding: utf-8 -*-
"""会话状态端点（/api/session/user）与紧急呼叫端点（/api/alarm）测试。

用 TestClient 测真实路由；voice worker 不启动（get_status 返回 stopped），
会话状态独立于语音可用性 —— 语音不可用时手动切换用户仍须可用（规格 §8）。
"""
import pytest
from fastapi.testclient import TestClient

from LLM import server, log as audit, voice_api


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """重置会话全局状态 + 隔离审计日志（防测试间污染真实 audit.jsonl）。"""
    voice_api._session_uid = None
    voice_api._session_locked = False
    monkeypatch.setattr(audit, "AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    yield


@pytest.fixture()
def client():
    # 不触发 lifespan（不起 reminder/voice 线程），只测路由层
    return TestClient(server.app)


def test_session_user_default(client):
    r = client.get("/api/session/user")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["uid"] is None
    assert body["locked"] is False


def test_session_user_set_and_get(client):
    r = client.post("/api/session/user", json={"uid": "elder_002", "locked": True})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    got = client.get("/api/session/user").json()
    assert got["uid"] == "elder_002"
    assert got["locked"] is True
    assert got["source"] == "manual"


def test_session_user_unlock(client):
    client.post("/api/session/user", json={"uid": "elder_002", "locked": True})
    r = client.post("/api/session/user", json={"uid": "elder_002", "locked": False})
    assert r.status_code == 200
    got = client.get("/api/session/user").json()
    assert got["locked"] is False


def test_alarm_reports_ok(client):
    r = client.post("/api/alarm", json={"type": "sos", "uid": "elder_001",
                                        "message": "按了紧急呼叫"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
```

- [ ] **步骤 2：运行测试验证失败**

运行：`..\.venv\Scripts\python.exe -m pytest tests/test_session_api.py -v`
预期：FAIL，`404 Not Found`（端点尚未实现）

- [ ] **步骤 3：实现 voice_api 会话访问器**

在 `LLM/voice_api.py` 中，`_worker = None` 附近增加模块级会话状态与访问器：

```python
# 会话状态（独立于语音可用性）：手动选择用户 + 锁定标志（规格 §8）
_session_uid = None
_session_locked = False


def set_session_uid(uid: str, locked: bool) -> dict:
    """手动切换当前会话用户（规格 D11）。
    锁定时写 worker.locked_uid（声纹不再自动切换）；解锁时清空。
    语音不可用时仍返回 ok —— 会话状态独立于语音能力。"""
    global _session_uid, _session_locked
    _session_uid = uid
    _session_locked = bool(locked)
    if _worker is not None:
        try:
            _worker.locked_uid = uid if locked else None
        except Exception:
            pass
    audit.log("session", action="set_uid", uid=uid, locked=bool(locked), by="nurse")
    return {"ok": True, "uid": uid, "locked": bool(locked)}


def get_session_uid() -> dict:
    """当前会话用户：手动选择优先；否则语音链路识别结果；否则 None。"""
    uid = _session_uid
    locked = _session_locked
    source = "manual" if locked else "none"
    if uid is None and _worker is not None:
        uid = getattr(_worker, "current_uid", None)
        source = "voiceprint" if uid else "none"
    return {"ok": True, "uid": uid, "locked": locked, "source": source}
```

- [ ] **步骤 4：实现 server.py 端点**

在 `LLM/server.py` 的数据模型区（`ReminderIn` 之后）新增模型：

```python
class SessionUserIn(BaseModel):
    uid: str
    locked: bool = True


class AlarmIn(BaseModel):
    type: str = "sos"          # sos / fall / health / no_activity ...
    uid: str = ""
    message: str = ""
```

在 `LLM/server.py` 的"语音"路由区（`/api/voice/status` 之前）新增端点：

```python
# ---------------------------------------------------------------- 会话状态
@app.get("/api/session/user")
async def session_user_get():
    """当前会话用户（active_uid + 锁定标志），全端共享。"""
    return voice_api.get_session_uid()


@app.post("/api/session/user")
async def session_user_set(s: SessionUserIn):
    """手动切换当前会话用户并广播 user_changed（kiosk 切用户/admin 同步）。"""
    res = voice_api.set_session_uid(s.uid, s.locked)
    bus.publish("user_changed", uid=s.uid, locked=s.locked, source="manual")
    return res


# ---------------------------------------------------------------- 紧急呼叫
@app.post("/api/alarm")
async def alarm_report(a: AlarmIn):
    """紧急呼叫上报（规格 D6）：审计 + 广播；微信推送留给模块 11。"""
    from . import log as audit
    audit.log("alarm", action="report", type=a.type, uid=a.uid,
              message=a.message[:200], by="nurse")
    # 注意：payload 键用 alarm_type 而非 type —— bus.publish 内部构造
    # {"type": event_type, **payload}，payload 里再用 type 会覆盖事件类型，
    # 导致广播的事件 type 变成 "sos" 而非 "alarm"，前端会丢弃该事件（已修复）
    bus.publish("alarm", level="critical", alarm_type=a.type, uid=a.uid, message=a.message)
    return {"ok": True}
```

- [ ] **步骤 5：运行测试验证通过**

运行：`..\.venv\Scripts\python.exe -m pytest tests/test_session_api.py -v`
预期：PASS，4 passed

- [ ] **步骤 6：全量回归**

运行：`..\.venv\Scripts\python.exe -m pytest tests/ -q`
预期：PASS（既有测试全部通过）

- [ ] **步骤 7：Commit**

```bash
git add LLM/voice_api.py LLM/server.py tests/test_session_api.py
git commit -m "feat: 会话状态端点(active_uid/锁定) + /api/alarm 紧急呼叫（规格 D6/D11）"
```

---

## 任务 4：前端工程搭建（pnpm workspace + Vite + Vue3 + TS）

**文件：** 全部新建于 `frontend/`

- [ ] **步骤 1：创建 workspace 根文件**

创建 `frontend/package.json`：

```json
{
  "name": "robot-frontend",
  "private": true,
  "scripts": {
    "dev:admin": "pnpm --filter admin dev",
    "dev:kiosk": "pnpm --filter kiosk dev",
    "build": "pnpm -r build",
    "test": "pnpm -r test"
  }
}
```

创建 `frontend/pnpm-workspace.yaml`：

```yaml
packages:
  - packages/*
```

创建 `frontend/tsconfig.base.json`：

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "strict": true,
    "jsx": "preserve",
    "esModuleInterop": true,
    "skipLibCheck": true,
    "forceConsistentCasingInFileNames": true,
    "resolveJsonModule": true,
    "types": []
  }
}
```

创建 `frontend/.gitignore`：

```
node_modules/
dist/
*.local
```

- [ ] **步骤 2：创建 shared 包**

创建 `frontend/packages/shared/package.json`：

```json
{
  "name": "shared",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "main": "src/index.ts",
  "types": "src/index.ts",
  "scripts": {
    "test": "vitest run"
  },
  "devDependencies": {
    "typescript": "^5.5.0",
    "vitest": "^2.0.0"
  }
}
```

创建 `frontend/packages/shared/tsconfig.json`：

```json
{
  "extends": "../../tsconfig.base.json",
  "include": ["src", "tests"]
}
```

创建 `frontend/packages/shared/vitest.config.mjs`（**注意：.mjs 纯 JS 配置，不用 .ts**——沙箱环境 vitest 加载 .ts 配置需 esbuild spawn 会 EPERM；.mjs 走 vite 原生 import；测试文件转译用进程内 typescript API，见下）：

```js
import ts from "typescript";
import { defineConfig } from "vitest/config";

// 沙箱环境禁止 fork/pipe 子进程（EPERM）：
//  - pool: "threads" —— worker_threads 同进程线程，绕开 tinypool fork
//  - esbuild: false + 进程内 typescript 转译插件 —— 绕开 esbuild spawn
//  - deps.optimizer 全关 —— 绕开依赖预构建（esbuild）
const tsTranspilePlugin = {
  name: "ts-transpile-in-process",
  enforce: "pre",
  transform(code, id) {
    if (!/\.[cm]?tsx?$/.test(id)) return null;
    const out = ts.transpileModule(code, {
      compilerOptions: {
        target: ts.ScriptTarget.ES2022,
        module: ts.ModuleKind.ESNext,
        jsx: ts.JsxEmit.Preserve,
        esModuleInterop: true,
        sourceMap: false,
      },
      fileName: id,
    });
    return { code: out.outputText, map: null };
  },
};

export default defineConfig({
  esbuild: false,
  plugins: [tsTranspilePlugin],
  test: {
    environment: "node",
    pool: "threads",
    deps: {
      optimizer: { ssr: { enabled: false }, web: { enabled: false } },
    },
  },
});
```

- [ ] **步骤 3：创建 admin 包**

创建 `frontend/packages/admin/package.json`：

```json
{
  "name": "admin",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "shared": "workspace:*",
    "vue": "^3.4.0"
  },
  "devDependencies": {
    "@vitejs/plugin-vue": "^5.0.0",
    "typescript": "^5.5.0",
    "vite": "^5.4.0",
    "vue-tsc": "^2.0.0"
  }
}
```

创建 `frontend/packages/admin/vite.config.ts`：

```ts
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { fileURLToPath, URL } from "node:url";

export default defineConfig({
  plugins: [vue()],
  resolve: {
    // shared 包 main 指向 TS 源码（src/index.ts），必须 alias 到源码路径，
    // 否则 Vite build 无法解析 workspace 包（monorepo 已知坑）
    alias: {
      shared: fileURLToPath(new URL("../shared/src/index.ts", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
```

创建 `frontend/packages/admin/tsconfig.json`：

```json
{
  "extends": "../../tsconfig.base.json",
  "compilerOptions": { "types": ["vite/client"] },
  "include": ["src", "vite.config.ts"]
}
```

创建 `frontend/packages/admin/index.html`：

```html
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>后台数据端</title>
  </head>
  <body>
    <div id="app"></div>
    <script type="module" src="/src/main.ts"></script>
  </body>
</html>
```

- [ ] **步骤 4：创建 kiosk 包**

创建 `frontend/packages/kiosk/package.json`（同 admin，端口 5174）：

```json
{
  "name": "kiosk",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "shared": "workspace:*",
    "vue": "^3.4.0"
  },
  "devDependencies": {
    "@vitejs/plugin-vue": "^5.0.0",
    "typescript": "^5.5.0",
    "vite": "^5.4.0",
    "vue-tsc": "^2.0.0"
  }
}
```

创建 `frontend/packages/kiosk/vite.config.ts`：

```ts
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { fileURLToPath, URL } from "node:url";

export default defineConfig({
  plugins: [vue()],
  resolve: {
    // shared 包 main 指向 TS 源码（src/index.ts），必须 alias 到源码路径，
    // 否则 Vite build 无法解析 workspace 包（monorepo 已知坑）
    alias: {
      shared: fileURLToPath(new URL("../shared/src/index.ts", import.meta.url)),
    },
  },
  server: {
    port: 5174,
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
```

创建 `frontend/packages/kiosk/tsconfig.json`（同 admin）。

创建 `frontend/packages/kiosk/index.html`（标题"车载交互端"，挂载点 `#app`）。

- [ ] **步骤 5：安装依赖并验证 dev server 可启动**

运行：`pnpm install`
预期：成功，生成 `frontend/node_modules` 与 lockfile

运行：`pnpm --filter admin dev`（后台 job，等待输出 `Local: http://localhost:5173/`）
预期：dev server 启动成功
→ 验证后 kill 该 job

- [ ] **步骤 6：Commit**

```bash
git add frontend/
git commit -m "chore: 前端工程搭建（pnpm workspace + Vite + Vue3 + TS，双端骨架）"
```

---

## 任务 5：shared — SSE 事件协议（events.ts）TDD

**文件：**
- 创建：`frontend/packages/shared/src/events.ts`
- 创建：`frontend/packages/shared/src/index.ts`
- 测试：`frontend/packages/shared/tests/events.test.ts`

- [ ] **步骤 1：编写失败的测试**

创建 `frontend/packages/shared/tests/events.test.ts`：

```ts
import { describe, it, expect } from "vitest";
import { parseBusEvent } from "../src/events";

describe("parseBusEvent", () => {
  it("解析 reminder 事件", () => {
    const ev = parseBusEvent(
      'data: {"type":"reminder","id":1,"uid":"elder_001","title":"吃药"}'
    );
    expect(ev?.type).toBe("reminder");
    if (ev?.type === "reminder") {
      expect(ev.uid).toBe("elder_001");
    }
  });

  it("解析 user_changed 事件", () => {
    const ev = parseBusEvent(
      'data: {"type":"user_changed","uid":"elder_002","locked":true,"source":"manual"}'
    );
    expect(ev?.type).toBe("user_changed");
    if (ev?.type === "user_changed") {
      expect(ev.uid).toBe("elder_002");
      expect(ev.locked).toBe(true);
    }
  });

  it("解析 voice_state 事件", () => {
    const ev = parseBusEvent(
      'data: {"type":"voice_state","state":"listening"}'
    );
    expect(ev?.type).toBe("voice_state");
  });

  it("忽略心跳注释行", () => {
    expect(parseBusEvent(": keep-alive")).toBeNull();
  });

  it("未知类型返回 null 不抛异常", () => {
    expect(parseBusEvent('data: {"type":"unknown_event","x":1}')).toBeNull();
  });
});
```

- [ ] **步骤 2：运行测试验证失败**

运行（在 `frontend/`）：`pnpm --filter shared test`
预期：FAIL，`Cannot find module '../src/events'`

- [ ] **步骤 3：实现 events.ts**

创建 `frontend/packages/shared/src/events.ts`：

```ts
// ★ SSE 事件协议唯一事实来源（规格 §4.1）—— 后端 bus.publish 的事件类型与此对照
export interface ReminderEvent {
  type: "reminder";
  id: number;
  uid: string;
  kind: string;
  title: string;
  content: string;
  status: string;
  missed: boolean;
  silent: boolean;
  time: string;
}

export interface ReminderConfirmedEvent {
  type: "reminder_confirmed";
  id: number;       // 与后端 reminder.py:151 publish("reminder_confirmed", id=rid, ...) 一致（同 reminder 事件用 id 键）
  uid?: string;
  title?: string;
}

export interface AlarmEvent {
  type: "alarm";
  level: string;
  alarm_type?: string;  // sos / fall / health / no_activity ...（不能叫 type，会与 bus.publish 的事件类型键冲突）
  uid?: string;
  message?: string;
}

export interface ChatNewEvent {
  type: "chat_new";
  uid: string;
  user: string;
  assistant: string;
}

export interface VoiceStateEvent {
  type: "voice_state";
  state: string;     // idle / wake / listening / recognized / speaking
  uid?: string;
  text?: string;
}

export interface UserChangedEvent {
  type: "user_changed";
  uid: string;
  locked: boolean;
  source: "manual" | "voiceprint";
}

export type BusEvent =
  | ReminderEvent
  | ReminderConfirmedEvent
  | AlarmEvent
  | ChatNewEvent
  | VoiceStateEvent
  | UserChangedEvent;

const KNOWN_TYPES = new Set([
  "reminder",
  "reminder_confirmed",
  "alarm",
  "chat_new",
  "voice_state",
  "user_changed",
]);

/** 解析 SSE 原始帧（"data: {...}" 或心跳注释行）→ BusEvent | null */
export function parseBusEvent(raw: string): BusEvent | null {
  if (!raw.startsWith("data:")) return null;       // 心跳注释行等
  return parseBusPayload(raw.slice(5));
}

/** 解析已剥离 "data:" 前缀的 payload JSON（EventSource 的 msg.data 场景）→ BusEvent | null */
export function parseBusPayload(raw: string): BusEvent | null {
  try {
    const payload = JSON.parse(raw.trim()) as Record<string, unknown>;
    const type = payload["type"];
    if (typeof type !== "string" || !KNOWN_TYPES.has(type)) return null;
    return payload as unknown as BusEvent;
  } catch {
    return null;                                    // 坏帧容错
  }
}

/** 将 SSE 流按 \n\n 切帧并解析（供 fetch 原始流场景复用） */
export function parseSseChunk(chunk: string): BusEvent[] {
  const out: BusEvent[] = [];
  for (const frame of chunk.split("\n\n")) {
    const ev = parseBusEvent(frame.trim());
    if (ev) out.push(ev);
  }
  return out;
}
```

创建 `frontend/packages/shared/src/index.ts`：

```ts
export * from "./events";
export * from "./api/client";
export * from "./api/session";
export * from "./api/alarm";
```

- [ ] **步骤 4：运行测试验证通过**

运行（在 `frontend/`）：`pnpm --filter shared test`
预期：PASS，5 passed

- [ ] **步骤 5：Commit**

```bash
git add frontend/packages/shared/
git commit -m "feat(shared): SSE 事件协议唯一事实来源 + 解析器（规格 §4.1）"
```

---

## 任务 6：shared — REST client（TDD）

**文件：**
- 创建：`frontend/packages/shared/src/api/client.ts`
- 创建：`frontend/packages/shared/src/api/session.ts`
- 创建：`frontend/packages/shared/src/api/alarm.ts`
- 测试：`frontend/packages/shared/tests/client.test.ts`

- [ ] **步骤 1：编写失败的测试**

创建 `frontend/packages/shared/tests/client.test.ts`：

```ts
import { describe, it, expect, vi, afterEach } from "vitest";
import { apiGet, apiPost } from "../src/api/client";

afterEach(() => vi.restoreAllMocks());

describe("REST client", () => {
  it("apiGet 解析 JSON", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true, json: async () => ({ ok: true, uid: "elder_001" }),
    }));
    const res = await apiGet("/api/session/user");
    expect(res.uid).toBe("elder_001");
    expect(fetch).toHaveBeenCalledWith("/api/session/user", expect.any(Object));
  });

  it("apiPost 发送 JSON body", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true, json: async () => ({ ok: true }),
    });
    vi.stubGlobal("fetch", fetchMock);
    await apiPost("/api/session/user", { uid: "elder_002", locked: true });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/session/user");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ uid: "elder_002", locked: true });
  });

  it("非 ok 响应抛错", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false, status: 500, json: async () => ({}),
    }));
    await expect(apiGet("/api/xxx")).rejects.toThrow();
  });
});
```

- [ ] **步骤 2：运行测试验证失败**

运行（在 `frontend/`）：`pnpm --filter shared test`
预期：FAIL，`Cannot find module '../src/api/client'`

- [ ] **步骤 3：实现 client.ts**

创建 `frontend/packages/shared/src/api/client.ts`：

```ts
// 统一 REST client：所有 /api 调用走这里（规格 §4）
async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) throw new Error(`API ${res.status}: ${url}`);
  return (await res.json()) as T;
}

export function apiGet<T = any>(url: string): Promise<T> {
  return request<T>(url, { headers: { Accept: "application/json" } });
}

export function apiPost<T = any>(url: string, body?: unknown): Promise<T> {
  return request<T>(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}
```

创建 `frontend/packages/shared/src/api/session.ts`：

```ts
import { apiGet, apiPost } from "./client";

export interface SessionUser {
  ok: boolean;
  uid: string | null;
  locked: boolean;
  source: "manual" | "voiceprint" | "none";
}

export function getSessionUser(): Promise<SessionUser> {
  return apiGet<SessionUser>("/api/session/user");
}

export function setSessionUser(uid: string, locked: boolean): Promise<SessionUser> {
  return apiPost<SessionUser>("/api/session/user", { uid, locked });
}
```

创建 `frontend/packages/shared/src/api/alarm.ts`：

```ts
import { apiPost } from "./client";

export interface AlarmResult {
  ok: boolean;
}

export function reportAlarm(type: string, uid: string, message: string): Promise<AlarmResult> {
  return apiPost<AlarmResult>("/api/alarm", { type, uid, message });
}
```

- [ ] **步骤 4：运行测试验证通过**

运行（在 `frontend/`）：`pnpm --filter shared test`
预期：PASS，8 passed（events 5 + client 3）

- [ ] **步骤 5：Commit**

```bash
git add frontend/packages/shared/
git commit -m "feat(shared): REST client + session/alarm API（规格 §4）"
```

---

## 任务 7：kiosk — 主界面（状态条/对话区/提醒/SOS）

**文件：** 全部新建于 `frontend/packages/kiosk/src/`

- [ ] **步骤 1：创建 main.ts**

创建 `frontend/packages/kiosk/src/main.ts`：

```ts
import { createApp } from "vue";
import App from "./App.vue";

createApp(App).mount("#app");
```

- [ ] **步骤 2：创建 SSE 订阅 composable**

创建 `frontend/packages/kiosk/src/useBus.ts`：

```ts
// SSE 事件订阅（EventSource 自动重连，规格 §5）
import { onUnmounted, ref } from "vue";
import { parseBusPayload } from "shared";

export function useBus(onEvent: (ev: import("shared").BusEvent) => void) {
  const connected = ref(false);
  let es: EventSource | null = null;
  let closed = false;

  function connect() {
    if (closed) return;
    es = new EventSource("/api/events");
    es.onopen = () => (connected.value = true);
    es.onmessage = (msg: MessageEvent) => {
      // EventSource 的 msg.data 已剥离 "data:" 前缀（WHATWG 标准）——
      // 直接 JSON.parse 按 payload.type 分发；不能用 parseSseChunk（它要求 data: 前缀，会全丢）
      const ev = parseBusPayload(msg.data as string);
      if (ev) onEvent(ev);
    };
    es.onerror = () => {
      connected.value = false;
      es?.close();
      setTimeout(connect, 3000);   // 自动重连
    };
  }

  connect();
  onUnmounted(() => {
    closed = true;
    es?.close();
  });
  return { connected };
}
```

- [ ] **步骤 3：创建 VoiceStatusBar.vue**

创建 `frontend/packages/kiosk/src/components/VoiceStatusBar.vue`：

```vue
<script setup lang="ts">
// 状态条：语音状态机三色 + active_uid + 锁定标记（规格 §5）
import { computed } from "vue";

const props = defineProps<{
  state: string;          // idle / wake / listening / speaking / unavailable
  uid: string | null;
  locked: boolean;
}>();

const emit = defineEmits<{ (e: "open-switcher"): void }>();

const label = computed(() => {
  switch (props.state) {
    case "listening": return "正在听…";
    case "speaking": return "播报中…";
    case "unavailable": return "语音不可用";
    default: return "◉ 待机";
  }
});

const color = computed(() => {
  if (props.state === "listening") return "#3b82f6";
  if (props.state === "speaking") return "#22c55e";
  if (props.state === "unavailable") return "#ef4444";
  return "#9ca3af";
});
</script>

<template>
  <div class="status-bar">
    <span class="dot" :style="{ background: color }"></span>
    <span class="label">{{ label }}</span>
    <button class="user" @click="emit('open-switcher')">
      👤 {{ uid ?? "未选择" }} {{ locked ? "🔒" : "" }}
    </button>
  </div>
</template>

<style scoped>
.status-bar {
  display: flex; align-items: center; gap: 12px;
  padding: 16px 24px; background: #111827; color: #f9fafb;
  font-size: 22px;
}
.dot { width: 14px; height: 14px; border-radius: 50%; }
.user { margin-left: auto; background: none; border: 1px solid #374151;
  color: #f9fafb; padding: 8px 16px; border-radius: 12px; font-size: 20px; }
</style>
```

- [ ] **步骤 4：创建 ChatArea.vue**

创建 `frontend/packages/kiosk/src/components/ChatArea.vue`：

```vue
<script setup lang="ts">
// 对话区：识别文本/回复实时上屏 + 手动输入（规格 §5）
import { ref } from "vue";

export interface Msg { role: "user" | "assistant"; content: string; uid?: string }

defineProps<{ messages: Msg[] }>();

const text = ref("");
const emit = defineEmits<{ (e: "send", text: string): void }>();

const QUICK = ["我要吃药", "请找家人", "今天天气怎么样", "给我讲讲新闻"];

function send() {
  const t = text.value.trim();
  if (!t) return;
  emit("send", t);
  text.value = "";
}
</script>

<template>
  <div class="chat-area">
    <div class="messages">
      <div v-for="(m, i) in messages" :key="i" :class="['msg', m.role]">
        <span v-if="m.role === 'user'">👴 {{ m.uid ? m.uid + "：" : "" }}{{ m.content }}</span>
        <span v-else>🤖 {{ m.content }}</span>
      </div>
    </div>
    <div class="quick">
      <button v-for="q in QUICK" :key="q" @click="emit('send', q)">{{ q }}</button>
    </div>
    <div class="input-row">
      <input v-model="text" placeholder="输入内容…" @keyup.enter="send" />
      <button class="send" @click="send">发送</button>
    </div>
  </div>
</template>

<style scoped>
.chat-area { flex: 1; display: flex; flex-direction: column; min-height: 0; }
.messages { flex: 1; overflow-y: auto; padding: 20px 24px; font-size: 24px; line-height: 1.8; }
.msg { margin-bottom: 16px; }
.msg.user span { background: #1e3a5f; padding: 10px 16px; border-radius: 14px; }
.msg.assistant span { background: #1f2937; padding: 10px 16px; border-radius: 14px; }
.quick { display: flex; gap: 10px; padding: 0 24px 12px; flex-wrap: wrap; }
.quick button { background: #374151; color: #f9fafb; border: none;
  padding: 12px 18px; border-radius: 12px; font-size: 20px; }
.input-row { display: flex; gap: 10px; padding: 12px 24px 20px; }
.input-row input { flex: 1; font-size: 22px; padding: 12px 16px;
  border-radius: 12px; border: 1px solid #374151; background: #1f2937; color: #f9fafb; }
.send { background: #2563eb; color: #fff; border: none; padding: 12px 28px;
  border-radius: 12px; font-size: 22px; }
</style>
```

- [ ] **步骤 5：创建 ReminderBanner.vue**

创建 `frontend/packages/kiosk/src/components/ReminderBanner.vue`：

```vue
<script setup lang="ts">
// 提醒卡片：触发时展示 + 确认（规格 §5）
import type { ReminderEvent } from "shared";

defineProps<{ reminder: ReminderEvent | null }>();
const emit = defineEmits<{ (e: "confirm", rid: number): void }>();
</script>

<template>
  <div v-if="reminder" class="banner">
    <div>
      <div class="title">⏰ {{ reminder.title }}</div>
      <div class="content">{{ reminder.content }}</div>
    </div>
    <button class="ok" @click="emit('confirm', reminder.id)">确认</button>
  </div>
</template>

<style scoped>
.banner { display: flex; align-items: center; gap: 16px;
  background: #b45309; color: #fff; padding: 16px 24px; font-size: 20px; }
.banner .title { font-weight: bold; }
.ok { background: #22c55e; color: #fff; border: none; padding: 12px 28px;
  border-radius: 12px; font-size: 20px; margin-left: auto; }
</style>
```

- [ ] **步骤 6：创建 SosButton.vue**

创建 `frontend/packages/kiosk/src/components/SosButton.vue`：

```vue
<script setup lang="ts">
// 紧急呼叫（规格 D6）：始终一键直达
const emit = defineEmits<{ (e: "sos"): void }>();
</script>

<template>
  <button class="sos" @click="emit('sos')">🆘 紧急呼叫</button>
</template>

<style scoped>
.sos { background: #dc2626; color: #fff; border: none; padding: 20px 32px;
  border-radius: 16px; font-size: 26px; font-weight: bold; }
</style>
```

- [ ] **步骤 7：创建 App.vue 组装**

创建 `frontend/packages/kiosk/src/App.vue`：

```vue
<script setup lang="ts">
// kiosk 主界面：状态条 + 对话区 + 提醒 + SOS（规格 §5）
import { onMounted, ref } from "vue";
import {
  type BusEvent, type ReminderEvent, setSessionUser,
  reportAlarm, getSessionUser,
} from "shared";
import { useBus } from "./useBus";
import VoiceStatusBar from "./components/VoiceStatusBar.vue";
import ChatArea, { type Msg } from "./components/ChatArea.vue";
import ReminderBanner from "./components/ReminderBanner.vue";
import SosButton from "./components/SosButton.vue";

const state = ref("idle");
const uid = ref<string | null>(null);
const locked = ref(false);
const messages = ref<Msg[]>([]);
const reminder = ref<ReminderEvent | null>(null);
const { connected } = useBus(onEvent);

async function loadSession() {
  try {
    const s = await getSessionUser();
    uid.value = s.uid;
    locked.value = s.locked;
  } catch { /* 后端未就绪时忽略 */ }
}

function onEvent(ev: BusEvent) {
  if (ev.type === "voice_state") state.value = ev.state;
  if (ev.type === "chat_new") {
    messages.value.push({ role: "user", content: ev.user, uid: ev.uid });
    messages.value.push({ role: "assistant", content: ev.assistant });
  }
  if (ev.type === "reminder") reminder.value = ev;
  if (ev.type === "user_changed") {
    uid.value = ev.uid;
    locked.value = ev.locked;
  }
}

async function sendText(text: string) {
  messages.value.push({ role: "user", content: text, uid: uid.value ?? undefined });
  messages.value.push({ role: "assistant", content: "" });  // 占位，流式填充
  const last = messages.value[messages.value.length - 1];
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ uid: uid.value ?? "elder_001", message: text, thinking: "auto" }),
    });
    if (!res.ok || !res.body) throw new Error("chat failed");
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    // /api/chat 返回的是 chat_stream 事件（reasoning/content/done），
    // 不是 bus 广播事件 —— 直接按 data: 行解析，不能用 parseSseChunk（只认 bus 类型）
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      for (const line of decoder.decode(value, { stream: true }).split("\n")) {
        if (!line.startsWith("data:")) continue;
        try {
          const ev = JSON.parse(line.slice(5).trim());
          if (ev.type === "content") last.content += ev.content;
          if (ev.type === "done") break;
        } catch { /* 坏帧忽略 */ }
      }
    }
    if (!last.content) last.content = "（无回复）";
  } catch {
    last.content = "（发送失败，请重试）";
  }
}

async function onSos() {
  try {
    await reportAlarm("sos", uid.value ?? "", "老人按下紧急呼叫按钮");
  } catch { /* 广播失败也要提示用户 */ }
  alert("已发送紧急呼叫");
}

async function onSwitchUser(nextUid: string) {
  await setSessionUser(nextUid, true);   // 手动切换即锁定（规格 D11）
  uid.value = nextUid;
  locked.value = true;
}

async function onConfirmReminder(rid: number) {
  try {
    await fetch(`/api/reminders/${rid}/confirm`, { method: "POST" });
    reminder.value = null;
  } catch { /* 忽略 */ }
}

onMounted(loadSession);
</script>

<template>
  <div class="kiosk">
    <VoiceStatusBar :state="state" :uid="uid" :locked="locked" />
    <ReminderBanner v-if="reminder" :reminder="reminder" @confirm="onConfirmReminder" />
    <ChatArea :messages="messages" @send="sendText" />
    <div class="bottom">
      <SosButton @sos="onSos" />
      <span class="conn" :class="{ off: !connected }">{{ connected ? "●" : "○ 重连中" }}</span>
    </div>
  </div>
</template>

<style>
html, body, #app { height: 100%; margin: 0; }
body { background: #0b1220; color: #f9fafb; font-family: system-ui, sans-serif; }
</style>
<style scoped>
.kiosk { height: 100vh; display: flex; flex-direction: column; }
.bottom { display: flex; align-items: center; gap: 20px; padding: 16px 24px; }
.conn { color: #22c55e; font-size: 20px; }
.conn.off { color: #ef4444; }
</style>
```

- [ ] **步骤 8：构建验证**

运行（在 `frontend/`）：`pnpm --filter kiosk build`
预期：成功，产物输出到 `frontend/packages/kiosk/dist/`

- [ ] **步骤 9：Commit**

```bash
git add frontend/packages/kiosk/
git commit -m "feat(kiosk): 主界面（状态条/对话区/提醒/SOS，规格 §5）"
```

---

## 任务 8：kiosk — 切换用户弹层 + 设置弹层

**文件：** 新建于 `frontend/packages/kiosk/src/components/`

- [ ] **步骤 1：创建 UserSwitcher.vue**

创建 `frontend/packages/kiosk/src/components/UserSwitcher.vue`：

```vue
<script setup lang="ts">
// 切换用户弹层：/api/profiles 列表 → 选人即锁定（规格 §5/D11）
import { onMounted, ref } from "vue";

interface Profile { uid: string; name: string; nickname: string; bed: string }

const props = defineProps<{ current: string | null }>();
const emit = defineEmits<{ (e: "pick", uid: string): void; (e: "close"): void }>();

const profiles = ref<Profile[]>([]);
const loading = ref(true);

onMounted(async () => {
  try {
    const res = await fetch("/api/profiles");
    const body = await res.json();
    profiles.value = (body.profiles ?? []) as Profile[];
  } finally {
    loading.value = false;
  }
});
</script>

<template>
  <div class="overlay" @click.self="emit('close')">
    <div class="sheet">
      <h2>选择当前老人</h2>
      <p v-if="loading">加载中…</p>
      <ul v-else>
        <li v-for="p in profiles" :key="p.uid"
            :class="{ active: p.uid === current }" @click="emit('pick', p.uid)">
          {{ p.nickname || p.name || p.uid }}
          <small v-if="p.bed">（{{ p.bed }}床）</small>
          <span v-if="p.uid === current">✓</span>
        </li>
      </ul>
      <button class="close" @click="emit('close')">关闭</button>
    </div>
  </div>
</template>

<style scoped>
.overlay { position: fixed; inset: 0; background: rgba(0,0,0,.6);
  display: flex; align-items: center; justify-content: center; z-index: 50; }
.sheet { background: #1f2937; color: #f9fafb; padding: 28px; border-radius: 20px;
  min-width: 420px; font-size: 22px; }
.sheet h2 { margin-top: 0; }
.sheet li { padding: 14px 16px; border-radius: 12px; cursor: pointer;
  list-style: none; display: flex; gap: 8px; align-items: center; }
.sheet li.active { background: #2563eb; }
.sheet li span { margin-left: auto; color: #22c55e; }
.close { margin-top: 18px; width: 100%; padding: 14px; border-radius: 12px;
  background: #374151; color: #f9fafb; border: none; font-size: 20px; }
</style>
```

- [ ] **步骤 2：创建 SettingsSheet.vue**

创建 `frontend/packages/kiosk/src/components/SettingsSheet.vue`：

```vue
<script setup lang="ts">
// 设置弹层：老人相关项（语音/音量/亮度/唤醒词显示），共享后端设置（规格 §5）
import { onMounted, ref } from "vue";

const emit = defineEmits<{ (e: "close"): void }>();

const settings = ref<Record<string, unknown>>({});
const loaded = ref(false);

onMounted(async () => {
  try {
    const res = await fetch("/api/settings");
    const body = await res.json();
    settings.value = body.settings ?? {};
  } finally {
    loaded.value = true;
  }
});

async function save(key: string, value: unknown) {
  settings.value[key] = value;
  await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ settings: { [key]: value } }),
  });
}
</script>

<template>
  <div class="overlay" @click.self="emit('close')">
    <div class="sheet">
      <h2>设置</h2>
      <template v-if="loaded">
        <label>
          <input type="checkbox" :checked="!!settings.voice_enabled"
                 @change="save('voice_enabled', ($event.target as HTMLInputElement).checked)" />
          语音交互
        </label>
        <label>
          <input type="checkbox" :checked="!!settings.tts_enabled"
                 @change="save('tts_enabled', ($event.target as HTMLInputElement).checked)" />
          语音播报
        </label>
        <label>
          唤醒词：<b>{{ settings.wakeword ?? "小机器人" }}</b>
        </label>
      </template>
      <button class="close" @click="emit('close')">关闭</button>
    </div>
  </div>
</template>

<style scoped>
.overlay { position: fixed; inset: 0; background: rgba(0,0,0,.6);
  display: flex; align-items: center; justify-content: center; z-index: 50; }
.sheet { background: #1f2937; color: #f9fafb; padding: 28px; border-radius: 20px;
  min-width: 380px; font-size: 22px; display: flex; flex-direction: column; gap: 18px; }
.sheet h2 { margin-top: 0; }
.sheet label { display: flex; gap: 12px; align-items: center; }
.sheet input[type="checkbox"] { width: 26px; height: 26px; }
.close { padding: 14px; border-radius: 12px; background: #374151;
  color: #f9fafb; border: none; font-size: 20px; }
</style>
```

- [ ] **步骤 3：App.vue 接入弹层（修改）**

修改 `frontend/packages/kiosk/src/App.vue`：

- import 增加 `ref` 弹层开关与两个组件：
  - `const showSwitcher = ref(false); const showSettings = ref(false);`
  - `<VoiceStatusBar ... @open-switcher="showSwitcher = true" />`
- 模板底部增加：

```vue
    <UserSwitcher v-if="showSwitcher" :current="uid"
                  @pick="onSwitchUser" @close="showSwitcher = false" />
    <SettingsSheet v-if="showSettings" @close="showSettings = false" />
```

- `onSwitchUser` 改为：

```ts
async function onSwitchUser(nextUid: string) {
  await setSessionUser(nextUid, true);
  uid.value = nextUid;
  locked.value = true;
  showSwitcher.value = false;
}
```

- [ ] **步骤 4：构建验证**

运行（在 `frontend/`）：`pnpm --filter kiosk build`
预期：成功

- [ ] **步骤 5：Commit**

```bash
git add frontend/packages/kiosk/
git commit -m "feat(kiosk): 切换用户弹层 + 设置弹层（规格 §5/D11）"
```

---

## 任务 9：admin — 壳与页签路由

**文件：** 新建于 `frontend/packages/admin/src/`

- [ ] **步骤 1：创建 main.ts**

创建 `frontend/packages/admin/src/main.ts`：

```ts
import { createApp } from "vue";
import App from "./App.vue";

createApp(App).mount("#app");
```

- [ ] **步骤 2：创建 App.vue（页签壳 + SSE toast）**

创建 `frontend/packages/admin/src/App.vue`：

```vue
<script setup lang="ts">
// admin 壳：7 页签 + SSE toast（沿用旧前端单页风格，规格 §6）
import { onUnmounted, ref } from "vue";
import { type BusEvent, parseBusPayload } from "shared";
import OverviewPage from "./pages/OverviewPage.vue";
import ChatPage from "./pages/ChatPage.vue";
import MemoriesPage from "./pages/MemoriesPage.vue";
import RemindersPage from "./pages/RemindersPage.vue";
import ToolLogPage from "./pages/ToolLogPage.vue";
import SettingsPage from "./pages/SettingsPage.vue";
import VoiceStatusPage from "./pages/VoiceStatusPage.vue";

const tabs = [
  { id: "overview", label: "监控总览" },
  { id: "chat", label: "对话" },
  { id: "memories", label: "记忆" },
  { id: "reminders", label: "提醒" },
  { id: "tools", label: "工具日志" },
  { id: "voice", label: "语音状态" },
  { id: "settings", label: "设置" },
];
const active = ref("overview");
const toasts = ref<{ id: number; text: string }[]>([]);
let es: EventSource | null = null;

function pushToast(text: string) {
  const id = Date.now();
  toasts.value.push({ id, text });
  // 捕获 id 供过滤（不能用 Date.now()——6 秒后已推进，恒不相等导致 toast 永不消失）
  setTimeout(() => {
    toasts.value = toasts.value.filter((t) => t.id !== id);
  }, 6000);
}

function onEvent(ev: BusEvent) {
  if (ev.type === "reminder") pushToast(`⏰ ${ev.title}：${ev.content}`);
  if (ev.type === "alarm") pushToast(`🚨 ${ev.type}：${ev.message ?? "告警"}`);
  if (ev.type === "user_changed") pushToast(`👤 当前用户切换为 ${ev.uid}`);
}

function connect() {
  es = new EventSource("/api/events");
  es.onmessage = (msg: MessageEvent) => {
    // EventSource 的 msg.data 已剥离 "data:" 前缀——用 parseBusPayload（parseSseChunk 要求前缀会全丢）
    const ev = parseBusPayload(msg.data as string);
    if (ev) onEvent(ev);
  };
  es.onerror = () => { es?.close(); setTimeout(connect, 3000); };
}

connect();
onUnmounted(() => es?.close());
</script>

<template>
  <div class="admin">
    <nav>
      <button v-for="t in tabs" :key="t.id" :class="{ active: active === t.id }"
              @click="active = t.id">{{ t.label }}</button>
    </nav>
    <main>
      <OverviewPage v-if="active === 'overview'" />
      <ChatPage v-else-if="active === 'chat'" />
      <MemoriesPage v-else-if="active === 'memories'" />
      <RemindersPage v-else-if="active === 'reminders'" />
      <ToolLogPage v-else-if="active === 'tools'" />
      <VoiceStatusPage v-else-if="active === 'voice'" />
      <SettingsPage v-else-if="active === 'settings'" />
    </main>
    <div class="toasts">
      <div v-for="t in toasts" :key="t.id" class="toast">{{ t.text }}</div>
    </div>
  </div>
</template>

<style>
html, body, #app { height: 100%; margin: 0; }
body { background: #0f172a; color: #e2e8f0; font-family: system-ui, sans-serif; }
</style>
<style scoped>
.admin { display: flex; flex-direction: column; height: 100vh; }
nav { display: flex; gap: 4px; padding: 10px 16px; background: #111827;
  border-bottom: 1px solid #1f2937; }
nav button { background: none; border: none; color: #94a3b8; padding: 10px 18px;
  border-radius: 10px; font-size: 15px; cursor: pointer; }
nav button.active { background: #1e3a5f; color: #f8fafc; }
main { flex: 1; overflow-y: auto; padding: 20px; }
.toasts { position: fixed; top: 60px; right: 20px; display: flex;
  flex-direction: column; gap: 8px; z-index: 100; }
.toast { background: #1e3a5f; color: #f8fafc; padding: 12px 18px;
  border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,.4); }
</style>
```

- [ ] **步骤 3：构建验证**

运行（在 `frontend/`）：`pnpm --filter admin build`
预期：成功（页面文件在任务 10-13 中补齐，此处先建占位可编译的页面）

> 说明：为让本任务可独立编译，先创建 7 个最小可编译页面（每个仅 `<template><div>待实现</div></template>`），后续任务逐个填充。创建方式：`scripts` 不必写脚本，直接手动创建 7 个文件即可。

- [ ] **步骤 4：Commit**

```bash
git add frontend/packages/admin/
git commit -m "feat(admin): 页签壳 + SSE toast（规格 §6）"
```

---

## 任务 10：admin — 对话页（ChatPage）

**文件：** 修改 `frontend/packages/admin/src/pages/ChatPage.vue`

- [ ] **步骤 1：实现对话页（历史 + 流式输出，对齐旧前端功能）**

```vue
<script setup lang="ts">
// 对话页：历史回读 + SSE 流式打字机（规格 §6，接口契约与旧前端一致）
import { onMounted, ref } from "vue";
import { parseSseChunk } from "shared";

interface Msg { role: "user" | "assistant"; content: string }

const uid = ref("elder_001");
const messages = ref<Msg[]>([]);
const text = ref("");
const sending = ref(false);

async function loadHistory() {
  const res = await fetch(`/api/chat/history?uid=${uid.value}&limit=200`);
  const body = await res.json();
  messages.value = (body.history ?? []).map((h: any) => ({
    role: h.role, content: h.content,
  }));
}

async function send() {
  const t = text.value.trim();
  if (!t || sending.value) return;
  sending.value = true;
  messages.value.push({ role: "user", content: t });
  text.value = "";
  let assistant = "";
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ uid: uid.value, message: t, thinking: "auto" }),
    });
    if (!res.body) throw new Error("no body");
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = decoder.decode(value, { stream: true });
      // 旧协议：chat_stream 事件直接 data: {...}（type: content/reasoning/done）
      for (const line of chunk.split("\n")) {
        if (!line.startsWith("data:")) continue;
        try {
          const ev = JSON.parse(line.slice(5).trim());
          if (ev.type === "content") assistant += ev.content;
          if (ev.type === "done") break;
        } catch { /* 坏帧忽略 */ }
      }
      messages.value[messages.value.length - 1] = { role: "assistant", content: assistant };
    }
  } catch {
    messages.value.push({ role: "assistant", content: "（发送失败）" });
  } finally {
    sending.value = false;
  }
}

async function clearHistory() {
  await fetch(`/api/chat/history?uid=${uid.value}`, { method: "DELETE" });
  messages.value = [];
}

onMounted(loadHistory);
</script>

<template>
  <div class="chat-page">
    <div class="toolbar">
      <input v-model="uid" placeholder="老人 uid" @change="loadHistory" />
      <button @click="loadHistory">刷新</button>
      <button @click="clearHistory">清空</button>
    </div>
    <div class="msgs">
      <div v-for="(m, i) in messages" :key="i" :class="['msg', m.role]">
        <b>{{ m.role === "user" ? "👤" : "🤖" }}</b> {{ m.content }}
      </div>
    </div>
    <div class="input-row">
      <input v-model="text" placeholder="输入消息…" @keyup.enter="send" :disabled="sending" />
      <button @click="send" :disabled="sending">{{ sending ? "发送中…" : "发送" }}</button>
    </div>
  </div>
</template>

<style scoped>
.chat-page { display: flex; flex-direction: column; height: 100%; }
.toolbar { display: flex; gap: 8px; margin-bottom: 12px; }
.toolbar input { flex: 1; padding: 8px 12px; border-radius: 8px;
  border: 1px solid #334155; background: #1e293b; color: #e2e8f0; }
.msgs { flex: 1; overflow-y: auto; margin-bottom: 12px; }
.msg { padding: 10px 14px; margin-bottom: 8px; border-radius: 10px;
  background: #1e293b; white-space: pre-wrap; }
.msg.user { border-left: 3px solid #3b82f6; }
.msg.assistant { border-left: 3px solid #22c55e; }
.input-row { display: flex; gap: 8px; }
.input-row input { flex: 1; padding: 10px 12px; border-radius: 8px;
  border: 1px solid #334155; background: #1e293b; color: #e2e8f0; }
.input-row button { padding: 10px 20px; border-radius: 8px;
  background: #2563eb; color: #fff; border: none; }
</style>
```

- [ ] **步骤 2：构建验证**

运行（在 `frontend/`）：`pnpm --filter admin build`
预期：成功

- [ ] **步骤 3：Commit**

```bash
git add frontend/packages/admin/src/pages/ChatPage.vue
git commit -m "feat(admin): 对话页（历史回读 + 流式输出，规格 §6）"
```

---

## 任务 11：admin — 记忆/提醒/工具日志页

**文件：** 修改 `frontend/packages/admin/src/pages/MemoriesPage.vue`、`RemindersPage.vue`、`ToolLogPage.vue`

- [ ] **步骤 1：实现 MemoriesPage.vue（列表 + 确认/拒绝/删除，对齐旧前端）**

```vue
<script setup lang="ts">
// 记忆页：列表 + 状态过滤 + 确认/拒绝/删除（规格 §6）
import { onMounted, ref } from "vue";

interface Memory { id: number; uid: string; type: string; content: string;
  status: string; created_at?: string }

const items = ref<Memory[]>([]);
const status = ref("");

async function load() {
  const q = status.value ? `?status=${status.value}` : "";
  const res = await fetch(`/api/memories${q}`);
  const body = await res.json();
  items.value = body.memories ?? [];
}

async function act(id: number, action: "confirm" | "reject" | "delete") {
  if (action === "delete") {
    await fetch(`/api/memories/${id}`, { method: "DELETE" });
  } else {
    await fetch(`/api/memories/${id}/${action}`, { method: "POST" });
  }
  await load();
}

onMounted(load);
</script>

<template>
  <div>
    <div class="toolbar">
      <select v-model="status" @change="load">
        <option value="">全部</option>
        <option value="confirmed">已确认</option>
        <option value="pending">待处理</option>
      </select>
      <button @click="load">刷新</button>
    </div>
    <div v-for="m in items" :key="m.id" class="row">
      <div>
        <b>[{{ m.type }}]</b> {{ m.content }}
        <small>（{{ m.uid }} · {{ m.status }}）</small>
      </div>
      <div class="actions">
        <button v-if="m.status !== 'confirmed'" @click="act(m.id, 'confirm')">确认</button>
        <button @click="act(m.id, 'reject')">拒绝</button>
        <button class="danger" @click="act(m.id, 'delete')">删除</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.toolbar { display: flex; gap: 8px; margin-bottom: 12px; }
.row { display: flex; justify-content: space-between; align-items: center;
  padding: 10px 14px; margin-bottom: 8px; background: #1e293b; border-radius: 10px; }
.actions { display: flex; gap: 6px; }
.actions button { padding: 6px 12px; border-radius: 6px; border: none;
  background: #334155; color: #e2e8f0; cursor: pointer; }
.actions .danger { background: #7f1d1d; }
</style>
```

- [ ] **步骤 2：实现 RemindersPage.vue（列表 + 新增 + 确认/删除，对齐旧前端）**

```vue
<script setup lang="ts">
// 提醒页：列表 + 新增（护士建议）+ 确认/删除（规格 §6）
import { onMounted, ref } from "vue";

interface Reminder { id: number; uid: string; kind: string; title: string;
  content: string; status: string; trigger_type: string; trigger_time: string }

const items = ref<Reminder[]>([]);
const form = ref({ uid: "elder_001", content: "", trigger_type: "once",
  trigger_time: "08:00" });

async function load() {
  const res = await fetch("/api/reminders");
  const body = await res.json();
  items.value = body.reminders ?? [];
}

async function add() {
  await fetch("/api/reminders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(form.value),
  });
  form.value.content = "";
  await load();
}

async function confirmRid(id: number) {
  await fetch(`/api/reminders/${id}/confirm`, { method: "POST" });
  await load();
}

async function del(id: number) {
  await fetch(`/api/reminders/${id}`, { method: "DELETE" });
  await load();
}

onMounted(load);
</script>

<template>
  <div>
    <div class="add">
      <input v-model="form.uid" placeholder="uid" />
      <input v-model="form.content" placeholder="提醒内容" />
      <select v-model="form.trigger_type">
        <option value="once">一次</option>
        <option value="daily">每日</option>
      </select>
      <input v-model="form.trigger_time" placeholder="08:00" />
      <button @click="add">新增</button>
    </div>
    <div v-for="r in items" :key="r.id" class="row">
      <div>
        <b>{{ r.title }}</b>：{{ r.content }}
        <small>（{{ r.uid }} · {{ r.status }} · {{ r.trigger_time }}）</small>
      </div>
      <div class="actions">
        <button v-if="r.status === 'triggered' || r.status === 'unconfirmed'"
                @click="confirmRid(r.id)">确认</button>
        <button class="danger" @click="del(r.id)">删除</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.add { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.add input, .add select { padding: 8px 10px; border-radius: 8px;
  border: 1px solid #334155; background: #1e293b; color: #e2e8f0; }
.row { display: flex; justify-content: space-between; align-items: center;
  padding: 10px 14px; margin-bottom: 8px; background: #1e293b; border-radius: 10px; }
.actions { display: flex; gap: 6px; }
.actions button { padding: 6px 12px; border-radius: 6px; border: none;
  background: #334155; color: #e2e8f0; cursor: pointer; }
.actions .danger { background: #7f1d1d; }
</style>
```

- [ ] **步骤 3：实现 ToolLogPage.vue（工具调用日志，对齐旧前端）**

```vue
<script setup lang="ts">
// 工具日志页：工具调用记录（规格 §6）
import { onMounted, ref } from "vue";

interface ToolLog { id: number; uid: string; name: string; args: string;
  result: string; created_at?: string }

const items = ref<ToolLog[]>([]);

async function load() {
  const res = await fetch("/api/tools/log?limit=100");
  const body = await res.json();
  items.value = body.logs ?? [];
}

onMounted(load);
</script>

<template>
  <div>
    <button @click="load">刷新</button>
    <div v-for="l in items" :key="l.id" class="row">
      <div>
        <b>{{ l.name }}</b>
        <pre>{{ l.args }}</pre>
        <pre class="result">{{ l.result }}</pre>
        <small>（{{ l.uid }}）</small>
      </div>
    </div>
  </div>
</template>

<style scoped>
.row { padding: 10px 14px; margin: 8px 0; background: #1e293b; border-radius: 10px; }
pre { white-space: pre-wrap; margin: 4px 0; font-size: 13px; }
.result { color: #86efac; }
</style>
```

- [ ] **步骤 4：构建验证**

运行（在 `frontend/`）：`pnpm --filter admin build`
预期：成功

- [ ] **步骤 5：Commit**

```bash
git add frontend/packages/admin/src/pages/
git commit -m "feat(admin): 记忆/提醒/工具日志页（规格 §6）"
```

---

## 任务 12：admin — 设置页 + 语音状态页 + 监控总览页

**文件：** 修改 `frontend/packages/admin/src/pages/SettingsPage.vue`、`VoiceStatusPage.vue`、`OverviewPage.vue`

- [ ] **步骤 1：实现 SettingsPage.vue（设置开关，对齐旧前端）**

```vue
<script setup lang="ts">
// 设置页：功能开关（一键开关，持久化，规格 §6）
import { onMounted, ref } from "vue";

const settings = ref<Record<string, unknown>>({});

async function load() {
  const res = await fetch("/api/settings");
  const body = await res.json();
  settings.value = body.settings ?? {};
}

async function save(key: string, value: unknown) {
  await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ settings: { [key]: value } }),
  });
  await load();
}

const BOOLS = [
  ["voice_enabled", "语音链路总开关"],
  ["asr_enabled", "语音识别"],
  ["tts_enabled", "语音播报"],
  ["reminder_enabled", "定时提醒"],
  ["web_search_enabled", "联网搜索"],
  ["thinking_router_enabled", "思考路由"],
  ["memory_consolidation_enabled", "记忆整理"],
  ["alarm_enabled", "报警上报"],
] as const;

onMounted(load);
</script>

<template>
  <div>
    <div v-for="[key, label] in BOOLS" :key="key" class="row">
      <span>{{ label }}（{{ key }}）</span>
      <input type="checkbox" :checked="!!settings[key]"
             @change="save(key, ($event.target as HTMLInputElement).checked)" />
    </div>
  </div>
</template>

<style scoped>
.row { display: flex; justify-content: space-between; align-items: center;
  padding: 10px 14px; margin-bottom: 8px; background: #1e293b; border-radius: 10px; }
</style>
```

- [ ] **步骤 2：实现 VoiceStatusPage.vue（语音状态 + 声纹档案）**

```vue
<script setup lang="ts">
// 语音状态页：心跳 + 子模块状态 + 降级原因 + 声纹档案（规格 §6）
import { onMounted, ref } from "vue";

const status = ref<any>(null);
const speakers = ref<string[]>([]);
const details = ref<Record<string, { samples: number }>>({});

async function load() {
  const s = await (await fetch("/api/voice/status")).json();
  status.value = s;
  const sp = await (await fetch("/api/voice/speakers")).json();
  speakers.value = sp.speakers ?? [];
  details.value = sp.details ?? {};
}

onMounted(load);
</script>

<template>
  <div>
    <button @click="load">刷新</button>
    <div v-if="status" class="card">
      <h3>状态：{{ status.status }}</h3>
      <p v-if="status.reason">{{ status.reason }}</p>
      <pre>{{ JSON.stringify(status.modules ?? {}, null, 2) }}</pre>
    </div>
    <div class="card">
      <h3>声纹档案</h3>
      <ul>
        <li v-for="uid in speakers" :key="uid">
          {{ uid }}（样本 {{ details[uid]?.samples ?? 0 }}）
        </li>
        <li v-if="!speakers.length">无档案</li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.card { background: #1e293b; padding: 16px; border-radius: 10px; margin: 12px 0; }
pre { white-space: pre-wrap; font-size: 13px; }
</style>
```

- [ ] **步骤 3：实现 OverviewPage.vue（监控总览）**

```vue
<script setup lang="ts">
// 监控总览：模块状态 + 告警列表（规格 §6）
import { onMounted, ref } from "vue";

const modules = ref<any>(null);
const warnings = ref<any[]>([]);

async function load() {
  modules.value = (await (await fetch("/api/modules/status")).json()).modules ?? {};
  warnings.value = (await (await fetch("/api/logs/warnings?limit=20")).json()).logs ?? [];
}

onMounted(load);
</script>

<template>
  <div>
    <button @click="load">刷新</button>
    <div class="card">
      <h3>模块状态</h3>
      <ul>
        <li v-for="(m, name) in modules" :key="String(name)">
          {{ name }}：{{ (m as any).status }}
          <span v-if="(m as any).reason">（{{ (m as any).reason }}）</span>
        </li>
      </ul>
    </div>
    <div class="card">
      <h3>最近告警/错误</h3>
      <div v-for="(w, i) in warnings" :key="i" class="warn">
        {{ w.ts }} · {{ w.event }} · {{ (w as any).action ?? "" }}
        <span v-if="(w as any).error">：{{ (w as any).error }}</span>
      </div>
      <p v-if="!warnings.length">暂无</p>
    </div>
  </div>
</template>

<style scoped>
.card { background: #1e293b; padding: 16px; border-radius: 10px; margin: 12px 0; }
.warn { padding: 6px 0; border-bottom: 1px solid #334155; font-size: 13px; }
</style>
```

- [ ] **步骤 4：构建验证**

运行（在 `frontend/`）：`pnpm --filter admin build`
预期：成功

- [ ] **步骤 5：Commit**

```bash
git add frontend/packages/admin/src/pages/
git commit -m "feat(admin): 设置/语音状态/监控总览页（规格 §6）"
```

---

## 任务 13：端到端联调验证（开发机）

**文件：** 无（验证任务）

- [ ] **步骤 1：启动后端**

运行（项目根）：`..\.venv\Scripts\python.exe -m uvicorn LLM.server:app --host 0.0.0.0 --port 8000`（后台 job）
预期：启动成功，无语音依赖也照常运行（若语音依赖缺失会降级，属正常）

- [ ] **步骤 2：验证会话端点**

运行：
```
curl http://127.0.0.1:8000/api/session/user
curl -X POST http://127.0.0.1:8000/api/session/user -H "Content-Type: application/json" -d "{\"uid\":\"elder_002\",\"locked\":true}"
curl http://127.0.0.1:8000/api/session/user
```
预期：第一次 `{"ok":true,"uid":null,...}`；POST 后返回 locked=true；第三次 uid=elder_002

- [ ] **步骤 3：验证 alarm 端点**

运行：`curl -X POST http://127.0.0.1:8000/api/alarm -H "Content-Type: application/json" -d "{\"type\":\"sos\",\"uid\":\"elder_001\",\"message\":\"测试\"}"`
预期：`{"ok":true}`；审计日志 `LLM/data/audit.jsonl` 出现 `"event":"alarm","action":"report"`

- [ ] **步骤 4：验证 admin/kiosk dev server**

运行：`pnpm --filter admin dev` 与 `pnpm --filter kiosk dev`（两个后台 job）
浏览器打开 `http://localhost:5173`（admin）与 `http://localhost:5174`（kiosk）
预期：页面加载，SSE toast 正常，kiosk 状态条显示语音状态，SOS 触发 alarm 广播

- [ ] **步骤 5：验证 user_changed 全端同步**

kiosk 点切换用户选 elder_002 → admin 页面 toast 出现"当前用户切换为 elder_002"
预期：两端状态一致

- [ ] **步骤 6：杀 kiosk 进程验证语音不受影响**

kill kiosk dev server job → 后端继续正常运行，`/api/session/user` 照常响应
预期：后端无任何前端依赖，符合规格 §11

- [ ] **步骤 7：Commit（如有联调修复）**

```bash
git add -A
git commit -m "fix: 联调修复（如有）"
```

---

## 任务 14：部署 — FastAPI 静态托管 + 构建产物

**文件：**
- 修改：`LLM/server.py`（静态托管 kiosk/admin 产物）
- 创建：`scripts/build_frontend.ps1`
- 创建：`scripts/kiosk-systemd.service`（板卡部署参考）

- [ ] **步骤 1：server.py 增加静态托管**

在 `LLM/server.py` 末尾（`tools_list` 之后）增加：

```python
# ---------------------------------------------------------------- 前端静态托管
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

_FRONTEND_DIST = BASE_DIR / "frontend" / "packages"
_KIOSK_DIST = _FRONTEND_DIST / "kiosk" / "dist"
_ADMIN_DIST = _FRONTEND_DIST / "admin" / "dist"


def _serve_dist(dist: Path, path: str):
    """挂载单个端构建产物；SPA 回退到 index.html（无 router，实际用不到回退，防御性）。"""
    app.mount(path, StaticFiles(directory=str(dist), html=True), name=path.strip("/"))


if _KIOSK_DIST.exists():
    _serve_dist(_KIOSK_DIST, "/kiosk")
if _ADMIN_DIST.exists():
    _serve_dist(_ADMIN_DIST, "/admin")


@app.get("/")
async def root():
    """默认入口：有 admin 产物则给 admin，否则提示构建。"""
    if _ADMIN_DIST.exists():
        return FileResponse(str(_ADMIN_DIST / "index.html"))
    return {"ok": True, "message": "前端未构建。运行 scripts/build_frontend.ps1 生成产物。"}
```

> 注意：挂载 `/kiosk` 与 `/admin` 必须放在所有 `/api` 路由**之后**定义——FastAPI 按注册顺序匹配，`/api/*` 路由先注册保证优先。

- [ ] **步骤 2：创建构建脚本**

创建 `scripts/build_frontend.ps1`：

```powershell
# 构建前端双端产物（开发机执行；板卡部署时拷贝 dist 即可）
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..\frontend")
pnpm install
pnpm build
Write-Host "构建完成：frontend/packages/{admin,kiosk}/dist"
```

- [ ] **步骤 3：创建 systemd 参考单元**

创建 `scripts/kiosk-systemd.service`：

```ini
[Unit]
Description=Robot kiosk display (Chromium)
After=network.target llm-server.service
Wants=llm-server.service

[Service]
Type=simple
User=sunrise
Environment=DISPLAY=:0
ExecStart=/usr/bin/chromium --kiosk --noerrdialogs --disable-infobars http://127.0.0.1:8000/kiosk/
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

- [ ] **步骤 4：验证静态托管**

运行（项目根，后端已启动）：重新启动后端 job → `curl http://127.0.0.1:8000/kiosk/`
预期：返回 kiosk 的 index.html（需先执行步骤 2 构建）

- [ ] **步骤 5：Commit**

```bash
git add LLM/server.py scripts/
git commit -m "feat: FastAPI 静态托管前端产物 + 构建脚本 + kiosk systemd 参考"
```

---

## 任务 15：验收回归

**文件：** 无

- [ ] **步骤 1：后端全量测试**

运行：`..\.venv\Scripts\python.exe -m pytest tests/ -q`
预期：全部 PASS（含新增 test_identity_lock / test_session_api）

- [ ] **步骤 2：前端全量测试**

运行（在 `frontend/`）：`pnpm test`
预期：shared 8 个用例 PASS

- [ ] **步骤 3：双端构建**

运行（在 `frontend/`）：`pnpm build`
预期：admin 与 kiosk 均构建成功

- [ ] **步骤 4：规格覆盖核对**

逐项核对 `docs/superpowers/specs/2026-08-27-frontend-multi-end-design.md`：
- D1-D11 全部落实（D11 锁定模式见任务 1-3）
- §5 kiosk 交互（状态条/对话区/切用户/设置/提醒确认/SOS/身份确认弹窗——身份确认弹窗本期以 user_changed/voice_state 为基础的最小实现，见任务 7-8）
- §6 admin 7 页签全部实现（任务 9-12）
- §7 /api/alarm（任务 3）
- §8 会话状态（任务 1-3）
- §9 部署（任务 14）
- §11 降级（任务 13 步骤 6 验证）
- 风险表第 4 项"锁定模式被误触"：kiosk 切用户入口为点按状态条用户（非 SOS 直通），老人误触风险由"切换后需再次操作才生效"缓解，SOS 始终直达——实现中确认开关顺序即可

- [ ] **步骤 5：最终 commit（如有遗漏）**

```bash
git add -A
git commit -m "chore: 验收回归修复"
```

---

## 自检记录（计划编写时执行）

**规格覆盖度：**
- §2 D1-D11 → 任务 1-3（D6/D11 后端）、任务 7-8（D5 kiosk）、任务 9-12（admin）
- §4 工程结构 → 任务 4
- §4.1 events.ts → 任务 5
- §5 kiosk → 任务 7-8
- §6 admin → 任务 9-12
- §7 /api/alarm → 任务 3
- §8 会话状态 → 任务 1-3
- §9 部署 → 任务 14
- §11 降级 → 任务 13
- §13 测试 → 任务 1/3/5/6/13/15

**占位符扫描：** 无 TODO/待定；所有步骤含完整代码或精确命令。任务 9 步骤 3 的"最小可编译页面"已注明直接创建方式（非占位符，是编译过渡）。

**类型一致性：**
- `effective_uid(vote, current_uid, locked_uid=None)` 在任务 1 定义、任务 2 调用，签名一致
- `set_session_uid(uid, locked)` / `get_session_uid()` 在任务 3 定义并测试，一致
- `parseBusEvent` / `parseSseChunk` 任务 5 定义、任务 7-9 使用，一致
- `reportAlarm(type, uid, message)` 任务 6 定义、任务 7 调用，一致
- `user_changed` payload `{uid, locked, source}` 后端广播（任务 3）与前端事件类型（任务 5）一致
