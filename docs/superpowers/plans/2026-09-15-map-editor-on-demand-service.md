# 地图编辑器按需启动（独立进程）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 把地图编辑器（像素修图 + 划线/标点）从主后端拆成**独立进程** `LLM.mapeditor_server:app`（:8010），默认不跑；admin 新增「地图编辑器」页签按需拉起，编辑器里「保存并退出」把它停掉并关窗。

**架构：** `server.py` 里编辑器专属的约 900 行（模型 `301–379`、路由与助手 `1225–2112`、`/mapeditor` 静态挂载）搬进 `LLM/mapapi.py`（`APIRouter`）；`LLM/mapeditor_server.py` 是薄壳 app；`LLM/mapctl.py` 在主后端侧用 stdlib `Popen` 管理该进程并暴露 3 条**仅管理员**的 REST。主后端**保留** `locator` / `maptags`（病房位置自动切换、记录病房区域要用）。

**技术栈：** Python 3 / FastAPI / uvicorn / stdlib `subprocess`（无新依赖）；前端 Vue3 + Vite + TS（pnpm monorepo）；测试 pytest + vitest。

**规格：** `docs/superpowers/specs/2026-09-15-map-editor-on-demand-service-design.md`（本计划的唯一依据，冲突以规格为准）

---

## 文件结构（先锁分解）

| 文件 | 动作 | 职责 |
|---|---|---|
| `LLM/mapapi.py` | 创建 | 编辑器专属后端：34 条路由 + 9 个模型 + 5 个助手函数 + `mount_editor(app)`。**不 import `server`**，是独立 app 的业务核心 |
| `LLM/mapeditor_server.py` | 创建 | 独立进程入口：建 app、挂 CORS、`include_router(mapapi.router)`、`mount_editor`、`GET /` 跳转、`GET|POST /api/mapeditor/service{,/stop}`（自停） |
| `LLM/mapctl.py` | 创建 | 主后端侧**进程管理**（stdlib only）：`status()/start()/stop()/reset_for_test()` + 3 条 admin-only 路由。**绝不 import `mapapi`** |
| `LLM/server.py` | 修改 | 删掉编辑器模型/路由/静态挂载；顶层 import 改 `locator, maptags`；接上 `mapctl.router`；lifespan 与 `/api/system/shutdown` 收尾调 `mapctl.stop()` |
| `LLM/conf.py` | 修改 | `MAP_EDITOR_PORT` / `MAP_EDITOR_START_TIMEOUT` |
| `LLM/tests/test_map_service_split.py` | 创建 | 拆分边界（主 app 没有 / 新 app 有 / 主 app 保留病房） |
| `LLM/tests/test_mapeditor_server.py` | 创建 | 独立 app 外壳（路由、自身状态、`/` 跳转、自停被调度） |
| `LLM/tests/test_mapctl.py` | 创建 | 进程管理状态机（假 Popen/假探活）+ 403 鉴权 |
| `tests/test_mapeditor.py` | 修改 | `client` fixture 从 `LLM.server` 改挂 `LLM.mapeditor_server`（1 行） |
| `frontend/packages/shared/src/api/mapService.ts` | 创建 | admin 调 8000 的 3 个客户端函数 |
| `frontend/packages/shared/src/index.ts` | 修改 | 导出 `./api/mapService` |
| `frontend/packages/shared/tests/mapService.test.ts` | 创建 | 3 个函数的 method/URL/X-Surface 断言 |
| `frontend/packages/admin/src/pages/MapEditorPage.vue` | 创建 | 状态卡 + 启动（`window.open`）+ 停止 + 5 秒轮询 |
| `frontend/packages/admin/src/App.vue` | 修改 | 加「地图编辑器」页签（11 个） |
| `frontend/packages/admin/src/pages/WardsPage.vue` | 修改 | 两处 `/mapeditor/` 硬链 → `goto-mapeditor` 事件 |
| `frontend/packages/mapeditor/src/lib/service.ts` | 创建 | 编辑器侧同源自停 + 关窗 + 服务掉线探测 |
| `frontend/packages/mapeditor/src/App.vue` | 修改 | 顶部「保存并退出」+「仅关窗（保留服务）」+ 掉线红条 |
| `frontend/packages/mapeditor/public/pixel-netio.js` | 修改 | 工具栏「保存并退出」（保存成功才停服务+关窗） |
| `frontend/packages/mapeditor/scripts/test-startup-contract.mjs` | 修改 | 3 条新契约断言 |
| `start_UI.py` | 修改 | 口径改为「UI 前端 + 后端」+ 编辑器按需启动提示 |
| `AGENTS.md`、`docs/log.md`、`docs/superpowers/specs/2026-09-14-map-editor-design.md` | 修改 | 文档同步（启动方式/端口/口径变更） |

---

## 任务 1：后端搬迁 —— 编辑器路由搬进 `LLM/mapapi.py`

**文件：**
- 创建：`LLM/mapapi.py`
- 修改：`LLM/server.py`（删 3 块 + 改 1 行 import）
- 修改：`tests/test_mapeditor.py:670-671`（fixture 改挂新 app）
- 测试：`LLM/tests/test_map_service_split.py`（新建）

- [ ] **步骤 1：编写失败的测试**

创建 `LLM/tests/test_map_service_split.py`：

```python
# -*- coding: utf-8 -*-
r"""编辑器后端从主后端拆出去的边界测试（规格 docs/superpowers/specs/2026-09-15-*.md §3）。"""
from LLM.server import app as main_app
from LLM import mapapi


def _paths(app) -> set:
    return {getattr(r, "path", "") for r in app.routes}


def test_main_app_no_longer_serves_map_editor():
    """主后端不该再暴露编辑器路由（它们跑在独立进程里）。"""
    paths = _paths(main_app)
    for gone in ("/api/map/list", "/api/map/current", "/api/map/sources",
                 "/api/destinations", "/api/zones", "/api/robot/pose",
                 "/api/mapeditor/status", "/api/mapeditor/io"):
        assert gone not in paths, "主后端不该还有编辑器路由：{}".format(gone)


def test_main_app_has_no_mapeditor_mount():
    assert not any(p.startswith("/mapeditor") for p in _paths(main_app))


def test_mapapi_router_carries_all_editor_routes():
    paths = {r.path for r in mapapi.router.routes}
    for kept in ("/api/map/list", "/api/map/current", "/api/map/{name}/meta",
                 "/api/map/{name}/save", "/api/destinations", "/api/zones",
                 "/api/robot/pose", "/api/mapeditor/status"):
        assert kept in paths, "编辑器路由丢了：{}".format(kept)


def test_main_app_keeps_wards_and_chat():
    """病房自动切换与「记录当前房间为病房区域」留在主后端（B1 边界）。"""
    paths = _paths(main_app)
    for kept in ("/api/chat", "/api/wards", "/api/wards/{ward_uid}/zone",
                 "/api/session/user", "/api/health"):
        assert kept in paths
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_map_service_split.py -q`
预期：collection ERROR —— `ModuleNotFoundError: No module named 'LLM.mapapi'`

- [ ] **步骤 3：写一次性搬迁脚本**

创建 `temp_split_map.py`（**跑完即删**；已 gitignore 不适用，故命名 `temp_*` 且最后删除）：

```python
# -*- coding: utf-8 -*-
r"""一次性搬迁脚本：把 LLM/server.py 的编辑器三块搬进 LLM/mapapi.py。跑完即删。"""
import re
from pathlib import Path

SRC = Path("LLM/server.py")
DST = Path("LLM/mapapi.py")

text = SRC.read_text(encoding="utf-8")

A_START = "# ---------------------------------------------------------------- 地图编辑器模型"
A_END = "# ---------------------------------------------------------------------------\n# 路由"
B_START = "# ---------------------------------------------------------------------------\n# 地图编辑器（第三个前端 /mapeditor）"
B_END = "# ------------------------------------------------------------------ 摄像头 HTTP 桥"


def cut(t, start, end):
    i = t.index(start)
    j = t.index(end, i + len(start))
    return t[i:j]


models = cut(text, A_START, A_END)
routes = cut(text, B_START, B_END)

HEADER = '''# -*- coding: utf-8 -*-
r"""地图编辑器后端（独立服务）—— 从 ``server.py`` 原样搬来的编辑器专属路由。

为什么单独一个模块（规格 docs/superpowers/specs/2026-09-15-map-editor-on-demand-service-design.md）：
编辑器**默认不跑**，由主后端按需以独立进程拉起（``LLM.mapeditor_server:app``，:8010）；
主后端只保留陪护需要的 ``locator`` / ``maptags``（病房位置自动切换、记录病房区域），
不再背着这 900 行。

红线（规格 §4）：标记的唯一真相是地图文件夹里的 <图名>.tags.json，brain.db 只是只读索引缓存；
             所有写接口都是「改文件 → maptags.sync_map() → 审计」，绝不"只改库不改文件"。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import conf, db
from . import log as audit
from .conf import BASE_DIR

router = APIRouter()


'''

MOUNT = '''

# ---------------------------------------------------------------- 静态托管 /mapeditor
_MAPEDITOR_DIST = BASE_DIR / "frontend" / "packages" / "mapeditor" / "dist"
_MAPEDITOR_PUBLIC = BASE_DIR / "frontend" / "packages" / "mapeditor" / "public"


def _mount(app, dist: Path, path: str) -> None:
    """挂一个端的构建产物；SPA 回退到 index.html（本编辑器无 router，用不到回退，防御性）。"""
    app.mount(path, StaticFiles(directory=str(dist), html=True), name=path.strip("/"))


def mount_editor(app) -> None:
    """挂 `/mapeditor`：判据是**入口页 index.html 存在**，不是目录存在。

    为什么这么判：`frontend/packages/mapeditor/dist/` 会被 dev 构建留下来，但如果它里面没有
    `index.html`（例如只把 `public/` 拷过去、或 dist 只装了 `pixel-editor.html`），
    直接挂 dist 会让 `/mapeditor/` 变 404 —— 此时必须回退到 `public/`。
    两个候选都没有 `index.html` 时，退一步挂 `public/`（至少 `pixel-editor.html` 能打开），
    并在启动日志里说清楚「前端未构建」，别让人对着 404 猜。
    """
    for cand, label in ((_MAPEDITOR_DIST, "dist（构建产物）"),
                        (_MAPEDITOR_PUBLIC, "public（未构建回退）")):
        if (cand / "index.html").exists():
            _mount(app, cand, "/mapeditor")
            print("[mapeditor] 挂载 {}：{}".format(label, cand))
            return
    if _MAPEDITOR_PUBLIC.exists():
        _mount(app, _MAPEDITOR_PUBLIC, "/mapeditor")
        print("[WARN] [mapeditor] 没有 index.html —— 只挂了 public/ 使 pixel-editor.html 可用："
              "{}\\n        要打开地图编辑器主界面请先构建："
              "cd frontend && pnpm --filter mapeditor build".format(_MAPEDITOR_PUBLIC))
'''

body = (models + routes).replace("@app.", "@router.")
assert body.count("@router.") == 34, "预期搬 34 条路由，实得 {}".format(body.count("@router."))
DST.write_text(HEADER + body + MOUNT, encoding="utf-8")

# ---- 从 server.py 删掉这三块 ----
new = text
for start, end in ((A_START, A_END), (B_START, B_END)):
    new = new.replace(cut(new, start, end), "", 1)

new = re.sub(r"^_MAPEDITOR_DIST = .*\n_MAPEDITOR_PUBLIC = .*\n", "", new, flags=re.M)
new = re.sub(r"def _mount_editor\(\):.*?\n_mount_editor\(\)\n", "", new, flags=re.S)

assert "_mount_editor" not in new and "_MAPEDITOR_DIST" not in new
SRC.write_text(new, encoding="utf-8")
print("ok: mapapi.py 已生成；server.py 已瘦身（{} -> {} 行）".format(
    text.count("\n"), new.count("\n")))
```

- [ ] **步骤 4：运行脚本并核对**

运行：`.venv\Scripts\python.exe temp_split_map.py`
预期：`ok: mapapi.py 已生成；server.py 已瘦身（2210 -> 约 1310 行）`（脚本自带 `assert`，路由数不是 34 会直接报错）

逐一核对（缺一不可）：

```powershell
git diff --stat -- LLM/server.py
Select-String -Path LLM/mapapi.py -Pattern '@router\.' | Measure-Object   # 期望 34
Select-String -Path LLM/mapapi.py -Pattern '^router = APIRouter\(\)'      # 期望 1 条
Select-String -Path LLM/server.py -Pattern '_mount_editor|_MAPEDITOR'     # 期望 0 条
Select-String -Path LLM/server.py -Pattern '^def _serve_dist'             # 期望 1 条（kiosk/admin 仍用）
```

- [ ] **步骤 5：修 `server.py` 的顶层 import**

搬迁块原本顺手 import 了 `locator` / `maptags`，删掉后主后端就缺名字了。在 `LLM/server.py` 第 33–38 行的 import 组里加一行（放在 `from . import session` 之后）：

```python
from . import locator, maptags   # 病房位置自动切换 / 记录病房区域要用（编辑器路由已搬走）
```

验证：

```powershell
Select-String -Path LLM/server.py -Pattern 'locator\.|maptags\.' | ForEach-Object { $_.Line.Trim() }
# 期望只剩：locator.available() / locator.get_pose() / maptags.record_room_polygon() / maptags.get_zone()
```

- [ ] **步骤 6：把接口层测试改挂新 app**

`tests/test_mapeditor.py` 的 `client` fixture（第 662–672 行）里，把：

```python
    from LLM.server import app
```

改成：

```python
    from LLM.mapeditor_server import app   # 编辑器路由已拆到独立 app（规格 2026-09-15）
```

- [ ] **步骤 7：跑新测试 + 接口层测试**

```powershell
.venv\Scripts\python.exe -c "from LLM.server import app; from LLM import mapapi; print('routes', len(app.routes), len(mapapi.router.routes))"
.venv\Scripts\python.exe -m pytest LLM/tests/test_map_service_split.py tests/test_mapeditor.py -q
```

预期：`test_map_service_split.py` 4 passed；`tests/test_mapeditor.py` 全 passed（原 30 例一条不少）

- [ ] **步骤 8：全量回归（判据 = 失败集合不变）**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests tests -q`
预期：**5 failed / 429 passed / 1 skipped**（失败集合 = `test_modules_status` + `test_unlock_switch`×3 + `test_vision.py::test_end_to_end_webcam_source_serves_decodable_jpeg`，与本次改动无关）

- [ ] **步骤 9：Commit**

```powershell
Remove-Item temp_split_map.py
git add -- LLM/mapapi.py LLM/server.py LLM/tests/test_map_service_split.py tests/test_mapeditor.py
git commit -m "refactor(llm): 编辑器后端路由搬进 LLM/mapapi.py（主后端不再承载 /mapeditor）" -- LLM/mapapi.py LLM/server.py LLM/tests/test_map_service_split.py tests/test_mapeditor.py
```

---

## 任务 2：`conf` 常量 + 独立服务 app `LLM/mapeditor_server.py`

**文件：**
- 修改：`LLM/conf.py`（`DEFAULT_SETTINGS` 之后加两个常量）
- 创建：`LLM/mapeditor_server.py`
- 测试：`LLM/tests/test_mapeditor_server.py`（新建）

- [ ] **步骤 1：编写失败的测试**

创建 `LLM/tests/test_mapeditor_server.py`：

```python
# -*- coding: utf-8 -*-
r"""独立编辑器服务 app 的外壳行为（规格 §3.2）。"""
import os

from fastapi.testclient import TestClient

from LLM import conf, mapeditor_server


def _paths(app) -> set:
    return {getattr(r, "path", "") for r in app.routes}


def test_editor_app_serves_all_editor_routes():
    paths = _paths(mapeditor_server.app)
    for kept in ("/api/map/list", "/api/destinations", "/api/zones",
                 "/api/robot/pose", "/api/mapeditor/status",
                 "/api/mapeditor/service", "/api/mapeditor/service/stop"):
        assert kept in paths, "独立服务缺路由：{}".format(kept)


def test_editor_app_mounts_mapeditor_page():
    assert any(p.startswith("/mapeditor") for p in _paths(mapeditor_server.app))


def test_service_status_reports_self():
    c = TestClient(mapeditor_server.app)
    d = c.get("/api/mapeditor/service").json()
    assert d["ok"] is True and d["running"] is True
    assert d["pid"] == os.getpid()
    assert d["port"] == conf.MAP_EDITOR_PORT


def test_root_redirects_to_editor():
    c = TestClient(mapeditor_server.app)
    r = c.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/mapeditor/"


def test_self_stop_schedules_exit(monkeypatch):
    """自停必须"先回响应、再排退出"，且退出是**可注入**的（测试绝不能真 os._exit）。"""
    calls = []
    monkeypatch.setattr(mapeditor_server, "_schedule_exit", lambda: calls.append(1))
    c = TestClient(mapeditor_server.app)
    d = c.post("/api/mapeditor/service/stop").json()
    assert d["ok"] is True
    assert calls == [1]
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_mapeditor_server.py -q`
预期：ERROR —— `ModuleNotFoundError: No module named 'LLM.mapeditor_server'`

- [ ] **步骤 3：加配置常量**

`LLM/conf.py`，紧跟 `DEFAULT_SETTINGS = {...}` 字典之后（文件里 `MODEL` 等常量附近）加：

```python
# ---- 地图编辑器独立服务（按需启动，见 docs/superpowers/specs/2026-09-15-map-editor-on-demand-service-design.md）----
MAP_EDITOR_PORT = 8010            # 编辑器服务端口（主后端 mapctl 用它拉起/探活/停止）
MAP_EDITOR_START_TIMEOUT = 20.0   # 拉起编辑器服务的最长等待秒数
```

- [ ] **步骤 4：写最小实现**

创建 `LLM/mapeditor_server.py`：

```python
# -*- coding: utf-8 -*-
r"""地图编辑器独立服务（FastAPI app）—— 按需启动。

入口（由主后端 ``LLM.mapctl`` 拉起）：
    python -m uvicorn LLM.mapeditor_server:app --host 0.0.0.0 --port 8010

它的全部业务 = ``mapapi.router``（地图文件 / 标记 / 区域 / 位姿）+ ``/mapeditor`` 静态页，
外加两条"服务自身"的接口：状态与**自停**（编辑器页的「保存并退出」调的就是自停）。

为什么自停由本服务提供：编辑器页面因此**只需要认识自己这个源**（:8010），
前端一行 URL 都不用改；主后端只靠 ``proc.poll()`` / HTTP 探活看它活没活。
"""
from __future__ import annotations

import os
import threading
import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from . import log as audit
from . import mapapi
from .conf import MAP_EDITOR_PORT

app = FastAPI(title="地图编辑器服务")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)
app.include_router(mapapi.router)
mapapi.mount_editor(app)


def _delayed_exit(delay: float = 0.5) -> None:
    """延迟退出：先把响应发回去，再退（与 server.py::_delayed_exit 同款做法）。"""
    time.sleep(delay)
    os._exit(0)


def _schedule_exit() -> threading.Thread:
    """排一次延迟退出（独立成函数 = 测试可注入，绝不真退）。"""
    t = threading.Thread(target=_delayed_exit, daemon=True)
    t.start()
    return t


@app.get("/")
async def root():
    """直接进编辑器主界面。"""
    return RedirectResponse(url="/mapeditor/")


@app.get("/api/mapeditor/service")
async def service_status():
    """本服务自身状态（主后端用它探活；编辑器页也可用它判断"服务还在不在"）。"""
    return {"ok": True, "running": True, "pid": os.getpid(), "port": MAP_EDITOR_PORT}


@app.post("/api/mapeditor/service/stop")
async def service_stop():
    """自杀：编辑器页「保存并退出」调它。先回响应，0.5 秒后退出。"""
    audit.log("map_editor_service", action="self_stop", pid=os.getpid())
    _schedule_exit()
    return {"ok": True, "message": "地图编辑器服务正在退出…"}
```

- [ ] **步骤 5：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_mapeditor_server.py -q`
预期：5 passed

- [ ] **步骤 6：Commit**

```powershell
git commit -m "feat(llm): 地图编辑器独立服务 app（:8010）+ 自停接口 + conf 常量" -- LLM/conf.py LLM/mapeditor_server.py LLM/tests/test_mapeditor_server.py
```
（先 `git add -- LLM/conf.py LLM/mapeditor_server.py LLM/tests/test_mapeditor_server.py`）

---

## 任务 3：主后端侧进程管理 `LLM/mapctl.py` + 接线

**文件：**
- 创建：`LLM/mapctl.py`
- 修改：`LLM/server.py`（顶层 import 1 行、`include_router` 1 行、lifespan 收尾 1 行、`/api/system/shutdown` 序列 1 行）
- 测试：`LLM/tests/test_mapctl.py`（新建）、`LLM/tests/test_map_service_split.py`（追加 1 例）

- [ ] **步骤 1：编写失败的测试**

创建 `LLM/tests/test_mapctl.py`：

```python
# -*- coding: utf-8 -*-
r"""编辑器服务的进程管理（规格 §3.3）—— 全程假 Popen / 假探活，不真起进程。"""
import pytest
from fastapi.testclient import TestClient

from LLM import mapctl, session
from LLM.server import app


class FakeProc:
    """够用的 Popen 替身。"""

    def __init__(self, pid=4242, alive=True):
        self.pid = pid
        self.returncode = None if alive else 1
        self._alive = alive
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self._alive else self.returncode

    def terminate(self):
        self.terminated = True
        self._alive = False

    def kill(self):
        self.killed = True
        self._alive = False

    def wait(self, timeout=None):
        return 0


@pytest.fixture()
def clean_state():
    mapctl.reset_for_test()
    yield
    mapctl.reset_for_test()


def test_status_none_when_nothing_running(clean_state, monkeypatch):
    monkeypatch.setattr(mapctl, "_probe", lambda timeout=2.0: False)
    st = mapctl.status()
    assert st["running"] is False and st["source"] == "none" and st["pid"] is None


def test_start_spawns_and_reports_managed(clean_state, monkeypatch):
    spawned = []
    monkeypatch.setattr(mapctl, "_probe", lambda timeout=2.0: True)
    monkeypatch.setattr(mapctl, "port_alive", lambda port=None, timeout=0.5: False)
    monkeypatch.setattr(mapctl.subprocess, "Popen",
                        lambda cmd, cwd=None: spawned.append(cmd) or FakeProc())
    st = mapctl.start()
    assert st["ok"] is True and st["running"] is True and st["source"] == "managed"
    assert st["pid"] == 4242 and st["port"] == mapctl._port()
    assert spawned and spawned[0][1:3] == ["-m", "uvicorn"]
    assert "LLM.mapeditor_server:app" in spawned[0]


def test_start_is_idempotent_for_external(clean_state, monkeypatch):
    """8010 上有外部实例（孤儿）→ 不重复拉起。"""
    called = []
    monkeypatch.setattr(mapctl, "_probe", lambda timeout=2.0: True)
    monkeypatch.setattr(mapctl.subprocess, "Popen", lambda *a, **k: called.append(1))
    st = mapctl.start()
    assert st["source"] == "external" and st["running"] is True
    assert called == []


def test_start_reports_failure_when_process_exits(clean_state, monkeypatch):
    monkeypatch.setattr(mapctl, "_probe", lambda timeout=2.0: False)
    monkeypatch.setattr(mapctl, "port_alive", lambda port=None, timeout=0.5: False)
    monkeypatch.setattr(mapctl.subprocess, "Popen", lambda cmd, cwd=None: FakeProc(alive=False))
    st = mapctl.start()
    assert st["ok"] is False and "启动即退出" in st["error"]


def test_start_reports_failure_on_timeout(clean_state, monkeypatch):
    monkeypatch.setattr(mapctl.conf, "MAP_EDITOR_START_TIMEOUT", 0.2)
    monkeypatch.setattr(mapctl, "_probe", lambda timeout=2.0: False)
    monkeypatch.setattr(mapctl, "port_alive", lambda port=None, timeout=0.5: False)
    monkeypatch.setattr(mapctl.subprocess, "Popen", lambda cmd, cwd=None: FakeProc())
    st = mapctl.start()
    assert st["ok"] is False and "未就绪" in st["error"]


def test_start_refuses_foreign_port_occupier(clean_state, monkeypatch):
    monkeypatch.setattr(mapctl, "_probe", lambda timeout=2.0: False)
    monkeypatch.setattr(mapctl, "port_alive", lambda port=None, timeout=0.5: True)
    monkeypatch.setattr(mapctl.subprocess, "Popen", lambda *a, **k: pytest.fail("不该拉起"))
    st = mapctl.start()
    assert st["ok"] is False and "被占用" in st["error"]
    assert "MAP_EDITOR_PORT" in st["error"]


def test_stop_terminates_managed(clean_state, monkeypatch):
    proc = FakeProc()
    monkeypatch.setattr(mapctl, "_probe", lambda timeout=2.0: True)
    monkeypatch.setattr(mapctl, "port_alive", lambda port=None, timeout=0.5: False)
    monkeypatch.setattr(mapctl.subprocess, "Popen", lambda cmd, cwd=None: proc)
    mapctl.start()
    st = mapctl.stop()
    assert proc.terminated is True and st["ok"] is True and st["running"] is False


def test_stop_uses_self_stop_for_external(clean_state, monkeypatch):
    """无句柄但端口活 → 走它自己的 /stop；随后探活转 False 即算停掉。"""
    probed = {"n": 0}
    posted = []

    class FakeResp:
        status = 200

        def read(self):
            return b'{"ok":true}'

    def fake_probe(timeout=2.0):
        probed["n"] += 1
        return probed["n"] == 1          # 第一次（stop 前）活着，之后判"已停"

    monkeypatch.setattr(mapctl, "_probe", fake_probe)
    monkeypatch.setattr(mapctl, "port_alive", lambda port=None, timeout=0.5: True)
    monkeypatch.setattr(mapctl.urllib.request, "urlopen",
                        lambda req, timeout=None: posted.append(req.full_url) or FakeResp())
    st = mapctl.stop()
    assert st["ok"] is True and st["running"] is False
    assert posted and posted[0].endswith("/api/mapeditor/service/stop")


def test_stop_is_idempotent_when_none(clean_state, monkeypatch):
    monkeypatch.setattr(mapctl, "_probe", lambda timeout=2.0: False)
    monkeypatch.setattr(mapctl, "port_alive", lambda port=None, timeout=0.5: False)
    st = mapctl.stop()
    assert st["ok"] is True and st["running"] is False


def test_service_routes_require_admin(clean_state):
    c = TestClient(app)
    assert c.get("/api/mapeditor/service", headers={"X-Surface": "kiosk"}).status_code == 403
    assert c.post("/api/mapeditor/service/start", headers={"X-Surface": "kiosk"}).status_code == 403
    assert c.post("/api/mapeditor/service/stop", headers={"X-Surface": "kiosk"}).status_code == 403
    assert c.get("/api/mapeditor/service", headers={"X-Surface": "nope"}).status_code == 400


def test_service_start_route_returns_payload_for_admin(clean_state, monkeypatch):
    monkeypatch.setattr(session, "get_principal",
                        lambda slot: {"uid": "admin", "role": "admin", "slot": slot})
    monkeypatch.setattr(mapctl, "start", lambda: {"ok": True, "running": True,
                                                  "source": "managed", "pid": 1,
                                                  "port": 8010, "uptime_s": 0.1})
    c = TestClient(app)
    d = c.post("/api/mapeditor/service/start", headers={"X-Surface": "admin"}).json()
    assert d["ok"] is True and d["source"] == "managed"
```

并在 `LLM/tests/test_map_service_split.py` 末尾追加：

```python
def test_main_app_mounted_map_editor_service_control():
    """主后端新增的 3 条服务控制路由（编辑器本身跑在别的进程）。"""
    paths = _paths(main_app)
    assert "/api/mapeditor/service" in paths
    assert "/api/mapeditor/service/start" in paths
    assert "/api/mapeditor/service/stop" in paths
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_mapctl.py LLM/tests/test_map_service_split.py -q`
预期：`ModuleNotFoundError: No module named 'LLM.mapctl'`（collection error）

- [ ] **步骤 3：写实现**

创建 `LLM/mapctl.py`：

```python
# -*- coding: utf-8 -*-
r"""地图编辑器独立服务的进程管理（主后端侧，规格 §3.3）。

主后端**不 import 编辑器业务**（那是 ``LLM.mapapi``，且跑在另一个进程里）：本模块只用 stdlib
拉起 / 探活 / 停止它，对外暴露 3 条**仅管理员**可用接口：

    GET  /api/mapeditor/service          状态（none | managed | external）
    POST /api/mapeditor/service/start    幂等启动（等就绪 ≤ MAP_EDITOR_START_TIMEOUT）
    POST /api/mapeditor/service/stop     幂等停止（有句柄→terminate；无句柄但端口活→让它自停）

``external`` = 端口上有服务，但不是本进程拉起的（例如主后端被 kill -9 后留下的孤儿）：
这种情况**不重复拉起**；停止时调它自己的 ``POST /api/mapeditor/service/stop``
（比"按端口找 PID 再 kill"更安全、且跨平台）。
"""
from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
import urllib.request

from fastapi import APIRouter, Header, HTTPException

from . import conf
from . import log as audit
from . import session
from .conf import BASE_DIR

router = APIRouter()

_lock = threading.RLock()
_proc: "subprocess.Popen | None" = None
_started_at = 0.0


# --------------------------------------------------------------------------- 基础
def _port() -> int:
    return int(conf.MAP_EDITOR_PORT)


def port_alive(port: int | None = None, timeout: float = 0.5) -> bool:
    """端口上是否有东西在监听（与 start_UI.py::port_in_use 同口径）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", int(port or _port()))) == 0


def _probe(timeout: float = 2.0) -> bool:
    """探活编辑器服务的 ``GET /api/mapeditor/service``（HTTP 200 即就绪）。"""
    url = "http://127.0.0.1:{}/api/mapeditor/service".format(_port())
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:      # noqa: BLE001  连不上/超时都算没就绪（降级：服务健康 ≠ 后端故障）
        return False


def reset_for_test() -> None:
    """清掉模块级状态（测试用；不动真实进程）。"""
    global _proc, _started_at
    with _lock:
        _proc = None
        _started_at = 0.0


# --------------------------------------------------------------------------- 三态
def status() -> dict:
    """当前状态（不抛异常）。"""
    global _proc, _started_at
    with _lock:
        proc = _proc
        managed = proc is not None and proc.poll() is None
        if proc is not None and not managed:
            _proc = None            # 进程已退出：清掉句柄，下次重新拉
            _started_at = 0.0
    running = managed or _probe(0.5)
    return {"ok": True,
            "running": bool(running),
            "source": "managed" if managed else ("external" if running else "none"),
            "pid": proc.pid if managed else None,
            "port": _port(),
            "uptime_s": round(time.time() - _started_at, 1) if managed and _started_at else None}


def start() -> dict:
    """幂等启动：已在跑（managed/external）→ 直接返回现状。"""
    global _proc, _started_at
    st = status()
    if st["running"]:
        return st
    if port_alive():
        audit.log("map_editor_service", action="start_failed",
                  reason="port_in_use", port=_port())
        return {"ok": False, "running": False, "source": "none", "pid": None,
                "port": _port(), "uptime_s": None,
                "error": "端口 {} 被占用且不是地图编辑器服务：请释放该端口，"
                         "或改 conf.MAP_EDITOR_PORT".format(_port())}

    cmd = [sys.executable, "-m", "uvicorn", "LLM.mapeditor_server:app",
           "--host", "0.0.0.0", "--port", str(_port())]
    with _lock:
        _proc = subprocess.Popen(cmd, cwd=str(BASE_DIR))     # 输出透传，便于排障
        _started_at = time.time()

    deadline = time.monotonic() + float(conf.MAP_EDITOR_START_TIMEOUT)
    while time.monotonic() < deadline:
        if _proc.poll() is not None:
            rc = _proc.returncode
            reset_for_test()
            audit.log("map_editor_service", action="start_failed",
                      reason="exited", code=rc, port=_port())
            return {"ok": False, "running": False, "source": "none", "pid": None,
                    "port": _port(), "uptime_s": None,
                    "error": "地图编辑器服务启动即退出（退出码 {}）："
                             "请看主后端终端里的 uvicorn 日志".format(rc)}
        if _probe(1.0):
            audit.log("map_editor_service", action="start",
                      pid=_proc.pid, port=_port(), source="managed")
            return status()
        time.sleep(0.4)

    stop()                       # 半死状态（占端口又不可用）不留着
    audit.log("map_editor_service", action="start_failed", reason="timeout", port=_port())
    return {"ok": False, "running": False, "source": "none", "pid": None,
            "port": _port(), "uptime_s": None,
            "error": "地图编辑器服务 {} 秒内未就绪（已停止）：请看主后端终端里的 "
                     "uvicorn 日志".format(int(conf.MAP_EDITOR_START_TIMEOUT))}


def stop() -> dict:
    """幂等停止：有句柄→terminate（5s 后 kill）；无句柄但端口活→调它自己的 stop。"""
    global _proc, _started_at
    with _lock:
        proc = _proc
        _proc = None
        _started_at = 0.0

    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        audit.log("map_editor_service", action="stop",
                  pid=proc.pid, port=_port(), source="managed")
        return {"ok": True, "running": False, "source": "none", "pid": None,
                "port": _port(), "uptime_s": None}

    if port_alive():
        url = "http://127.0.0.1:{}/api/mapeditor/service/stop".format(_port())
        try:
            req = urllib.request.Request(
                url, method="POST", data=b"{}",
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=3).read()
        except Exception as e:      # noqa: BLE001  外部实例不归我们管：失败要说清怎么收
            audit.log("map_editor_service", action="stop_failed",
                      source="external", error=str(e))
            return {"ok": False, "running": True, "source": "external", "pid": None,
                    "port": _port(), "uptime_s": None,
                    "error": "该实例不是本后端拉起的，且自停接口调用失败：{}；"
                             "请到它的终端按 Ctrl+C".format(e)}
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not _probe(0.5):
                audit.log("map_editor_service", action="stop",
                          source="external", port=_port())
                return {"ok": True, "running": False, "source": "none", "pid": None,
                        "port": _port(), "uptime_s": None}
            time.sleep(0.3)
        return {"ok": False, "running": True, "source": "external", "pid": None,
                "port": _port(), "uptime_s": None,
                "error": "已请求外部实例退出，但 5 秒内仍在监听"}

    return {"ok": True, "running": False, "source": "none", "pid": None,
            "port": _port(), "uptime_s": None}


# --------------------------------------------------------------------------- 路由
def _require_admin(x_surface: str) -> None:
    """仅管理员可启停编辑器服务；X-Surface 非法值 → 400（与 server._surface 同口径）。"""
    from .server import _surface        # 延迟导入：server 反过来 import 本模块，顶层导入会成环
    if session.get_principal(_surface(x_surface))["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可启停地图编辑器服务")


@router.get("/api/mapeditor/service")
async def map_editor_service(x_surface: str = Header(default="kiosk")):
    """编辑器服务状态（仅管理员）。"""
    _require_admin(x_surface)
    return status()


@router.post("/api/mapeditor/service/start")
async def map_editor_service_start(x_surface: str = Header(default="kiosk")):
    """拉起编辑器服务（幂等）。"""
    _require_admin(x_surface)
    return start()


@router.post("/api/mapeditor/service/stop")
async def map_editor_service_stop(x_surface: str = Header(default="kiosk")):
    """停掉编辑器服务（幂等）。"""
    _require_admin(x_surface)
    return stop()
```

- [ ] **步骤 4：给 `server.py` 接线**

四处小改（全部带精确锚点）：

1. 顶层 import 组（与任务 1 加的 `locator, maptags` 同一组）追加：

```python
from . import mapctl         # 地图编辑器服务（独立进程）的启停管理
```

2. `app` 与 CORS 中间件定义之后（`app.add_middleware(...)` 那一整块之后）追加：

```python
app.include_router(mapctl.router)   # /api/mapeditor/service{,/start,/stop}（仅管理员）
```

3. `lifespan` 里 `yield` 之后的收尾段**第一行**加：

```python
    mapctl.stop()             # 编辑器服务是本进程拉起的：主后端退出不该留孤儿
```

（放第一位是为了给它留足退出预算——后面跟着 `mcp_client.stop()` / `voice_api.stop_voice()` 等。）

4. `POST /api/system/shutdown` 的停止序列里**第一步**（`voice_api.stop_voice()` 之前）加：

```python
        mapctl.stop()                        # 编辑器服务（独立进程）一起带走
```

> ⚠️ 该端点最终会 `os._exit(0)`，`stop()` 的 terminate 等待最长 5 秒、而延迟退出只有 1 秒：极端情况下（子进程 1 秒内没退）会留下孤儿 —— 这正是 `external` 状态存在的意义（admin 页「停止服务」能收掉它，见规格 §5）。所以这里放第一位、并接受这个残余风险，不做更复杂的联动。

- [ ] **步骤 5：运行测试验证通过**

```powershell
.venv\Scripts\python.exe -m pytest LLM/tests/test_mapctl.py LLM/tests/test_map_service_split.py -q
```
预期：`test_mapctl.py` 11 passed；`test_map_service_split.py` 5 passed

- [ ] **步骤 6：全量回归**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests tests -q`
预期：仍 **5 failed / 429 passed / 1 skipped**（失败集合不变）

- [ ] **步骤 7：Commit**

```powershell
git add -- LLM/mapctl.py LLM/server.py LLM/tests/test_mapctl.py LLM/tests/test_map_service_split.py
git commit -m "feat(llm): mapctl 管理编辑器服务进程（3 条 admin-only 接口 + 退出收尾）" -- LLM/mapctl.py LLM/server.py LLM/tests/test_mapctl.py LLM/tests/test_map_service_split.py
```

---

## 任务 4：shared 的地图编辑器服务客户端 + 单测

**文件：**
- 创建：`frontend/packages/shared/src/api/mapService.ts`
- 修改：`frontend/packages/shared/src/index.ts`
- 测试：`frontend/packages/shared/tests/mapService.test.ts`（新建）

- [ ] **步骤 1：编写失败的测试**

创建 `frontend/packages/shared/tests/mapService.test.ts`：

```ts
import { afterEach, describe, expect, it, vi } from "vitest";
import { getMapEditorService, startMapEditor, stopMapEditor } from "../src/api/mapService";

function stub(body: any = { ok: true, running: true, source: "managed", pid: 7, port: 8010 }) {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => body });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => vi.restoreAllMocks());

describe("地图编辑器服务客户端", () => {
  it("状态查询走 GET 且带 X-Surface", async () => {
    const f = stub();
    await getMapEditorService("admin");
    const [url, init] = f.mock.calls[0];
    expect(url).toBe("/api/mapeditor/service");
    expect(init.method).toBe("GET");
    expect(init.headers["X-Surface"]).toBe("admin");
  });

  it("启动走 POST /start", async () => {
    const f = stub();
    await startMapEditor("admin");
    const [url, init] = f.mock.calls[0];
    expect(url).toBe("/api/mapeditor/service/start");
    expect(init.method).toBe("POST");
    expect(init.headers["X-Surface"]).toBe("admin");
  });

  it("停止走 POST /stop", async () => {
    const f = stub();
    await stopMapEditor("admin");
    const [url, init] = f.mock.calls[0];
    expect(url).toBe("/api/mapeditor/service/stop");
    expect(init.method).toBe("POST");
  });
});
```

- [ ] **步骤 2：运行测试验证失败**

运行（**沙箱内**；正常终端直接用 `pnpm --filter shared test`）：
`cd frontend; node --import ./temp-esbuild-register.mjs ./packages/shared/node_modules/vitest/vitest.mjs run --root packages/shared`
预期：FAIL —— `Failed to resolve import "../src/api/mapService"`

> 若 `frontend/temp-esbuild-register.mjs` 已不存在：它只是把 `packages/mapeditor/scripts/esbuild-shim.mjs` + `child-process-stdio.mjs` 注册进当前 Node 进程（沙箱禁子进程管道 stdio，esbuild 起不来）。内容见本计划附录 A。

- [ ] **步骤 3：写最小实现**

创建 `frontend/packages/shared/src/api/mapService.ts`：

```ts
// 地图编辑器「独立服务」的启停（规格 docs/superpowers/specs/2026-09-15-map-editor-on-demand-service-design.md）。
// 这三条打的是 **主后端 8000**：编辑器本身跑在 :8010，由主后端拉起/停掉。
import { apiGet, apiPost, type Surface } from "./client";

export interface MapEditorService {
  ok: boolean;
  running: boolean;
  source: "none" | "managed" | "external";
  pid: number | null;
  port: number;
  uptime_s: number | null;
  error?: string;
}

/** 编辑器服务状态（仅管理员）。 */
export function getMapEditorService(surface: Surface): Promise<MapEditorService> {
  return apiGet<MapEditorService>("/api/mapeditor/service", surface);
}

/** 拉起编辑器服务（幂等；已在跑则原样返回现状）。 */
export function startMapEditor(surface: Surface): Promise<MapEditorService> {
  return apiPost<MapEditorService>("/api/mapeditor/service/start", {}, surface);
}

/** 停掉编辑器服务（幂等；编辑器里的「保存并退出」调的是它自己的同源接口）。 */
export function stopMapEditor(surface: Surface): Promise<MapEditorService> {
  return apiPost<MapEditorService>("/api/mapeditor/service/stop", {}, surface);
}
```

`frontend/packages/shared/src/index.ts` 追加一行：

```ts
export * from "./api/mapService";
```

- [ ] **步骤 4：运行测试验证通过**

同步骤 2 的命令。预期：`mapService.test.ts` 3 passed，shared 全量 **19 passed**

- [ ] **步骤 5：Commit**

```powershell
git add -- frontend/packages/shared/src/api/mapService.ts frontend/packages/shared/src/index.ts frontend/packages/shared/tests/mapService.test.ts
git commit -m "feat(shared): 地图编辑器服务启停客户端 + 单测" -- frontend/packages/shared/src/api/mapService.ts frontend/packages/shared/src/index.ts frontend/packages/shared/tests/mapService.test.ts
```

---

## 任务 5：admin 新页签「地图编辑器」

**文件：**
- 创建：`frontend/packages/admin/src/pages/MapEditorPage.vue`
- 修改：`frontend/packages/admin/src/App.vue`（tabs + `<main>` + 注释页签数）
- 修改：`frontend/packages/admin/src/pages/WardsPage.vue`（两处硬链 → 事件）

- [ ] **步骤 1：写页面**

创建 `frontend/packages/admin/src/pages/MapEditorPage.vue`：

```vue
<script setup lang="ts">
// 地图编辑器（独立进程 :8010）的按需启停入口。
// 规格：docs/superpowers/specs/2026-09-15-map-editor-on-demand-service-design.md §4.1
// 为什么要有这一页：编辑器（像素修图 + 划线/标点）默认不跑，跑起来是另一个进程；
// 这里只做「启动 / 停止 / 看状态」，编辑器本身在它自己的窗口里。
import { onMounted, onUnmounted, ref } from "vue";
import { getMapEditorService, startMapEditor, stopMapEditor, type MapEditorService } from "shared";

const st = ref<MapEditorService | null>(null);
const busy = ref(false);
const note = ref("");
const err = ref("");
let timer: number | null = null;
let pending = false;

async function refresh() {
  if (pending) return;
  pending = true;
  try {
    const r = await getMapEditorService("admin");
    if (!r.ok) { err.value = r.error ?? "读取服务状态失败"; return; }
    st.value = r;
    err.value = "";
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e);
  } finally {
    pending = false;
  }
}

/** 编辑器 URL：用 location.hostname（不是 127.0.0.1）——从另一台机器看 admin 时也能开对。 */
function editorUrl(port: number) {
  return `http://${window.location.hostname}:${port}/mapeditor/`;
}

async function start() {
  busy.value = true; note.value = ""; err.value = "";
  try {
    const r = await startMapEditor("admin");
    st.value = r;
    if (!r.ok) { err.value = r.error ?? "启动失败（详情见后端终端）"; return; }
    note.value = r.source === "external"
      ? "已有外部实例在跑，直接打开。"
      : `已启动（PID ${r.pid ?? "?"}）。`;
    window.open(editorUrl(r.port), "_blank");   // 必须是脚本打开，编辑器里的 window.close() 才生效
    await refresh();
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e);
  } finally {
    busy.value = false;
  }
}

async function stop() {
  if (!window.confirm("确定停止地图编辑器服务？（编辑器窗口会连不上，需要重新点启动）")) return;
  busy.value = true; note.value = ""; err.value = "";
  try {
    const r = await stopMapEditor("admin");
    st.value = r;
    if (!r.ok) { err.value = r.error ?? "停止失败"; return; }
    note.value = "已停止。";
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e);
  } finally {
    busy.value = false;
  }
}

function label(s: MapEditorService | null) {
  if (!s) return "读取中…";
  if (!s.running) return "未启动";
  const who = s.source === "managed" ? `PID ${s.pid ?? "?"}` : "外部实例";
  const up = s.uptime_s != null ? `，已运行 ${Math.round(s.uptime_s)} 秒` : "";
  return `运行中（${who}，端口 ${s.port}${up}）`;
}

onMounted(() => {
  void refresh();
  timer = window.setInterval(() => { void refresh(); }, 5000);
});
onUnmounted(() => {
  if (timer !== null) window.clearInterval(timer);
  timer = null;
});
</script>

<template>
  <div class="page">
    <h2>🗺 地图编辑器</h2>
    <p class="hint">
      像素修图与划线/标点都在这里按需启动 —— 编辑器是**独立进程**（默认端口 8010），
      不用它的时候不占资源；在编辑器里点「保存并退出」会自动把它停掉。
    </p>

    <div class="card">
      <div class="row">
        <span class="state" :class="{ on: st?.running }">{{ label(st) }}</span>
        <button :disabled="busy || st?.running === true" @click="start">
          {{ busy ? "处理中…" : "启动地图编辑器" }}
        </button>
        <button class="danger" :disabled="busy || !st?.running" @click="stop">停止服务</button>
        <button :disabled="busy" @click="refresh">刷新状态</button>
      </div>
      <p v-if="note" class="ok">{{ note }}</p>
      <p v-if="err" class="bad">{{ err }}</p>
    </div>

    <div class="card">
      <h3>用法</h3>
      <ol>
        <li>点「启动地图编辑器」→ 自动打开编辑器窗口（新标签页）。</li>
        <li>在编辑器里列图 / 标地点 / 画区域；像素修图点地图文件面板里的「像素修图」。</li>
        <li>干完点「保存并退出」→ 保存 + 停服务 + 关窗。<b>只关窗</b>（点浏览器 ×）不会停服务，
            回本页点「停止服务」即可。</li>
        <li>⚠️ 改完地图<b>必须重启导航</b>才生效：<code>~/tools/nav_screen.sh nav &lt;地图名&gt;</code>。</li>
      </ol>
    </div>
  </div>
</template>

<style scoped>
.page { max-width: 900px; }
h2 { margin: 0 0 8px; font-size: 20px; }
h3 { margin: 0 0 8px; font-size: 15px; color: #cbd5e1; }
.hint { color: #94a3b8; font-size: 13px; line-height: 1.7; }
.card { background: #111827; border: 1px solid #1f2937; border-radius: 10px;
  padding: 14px 16px; margin-bottom: 16px; }
.row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.state { margin-right: auto; color: #94a3b8; font-size: 14px; }
.state.on { color: #4ade80; }
button { padding: 8px 16px; border-radius: 8px; border: none; background: #1e3a5f;
  color: #e2e8f0; cursor: pointer; font-size: 14px; }
button.danger { background: #7f1d1d; }
button:disabled { opacity: 0.55; cursor: not-allowed; }
.ok { color: #4ade80; font-size: 13px; margin: 10px 0 0; }
.bad { color: #f87171; font-size: 13px; margin: 10px 0 0; }
ol { margin: 0; padding-left: 20px; color: #cbd5e1; font-size: 13px; line-height: 1.9; }
code { background: #1e293b; padding: 1px 5px; border-radius: 4px; }
</style>
```

- [ ] **步骤 2：接进 admin 壳**

`frontend/packages/admin/src/App.vue`：

1. 文件头注释 `// admin 壳：登录门 + 10 页签 + ...` → `// admin 壳：登录门 + 11 页签 + ...`
2. `import RolesPage from "./pages/RolesPage.vue";` 之后加：

```ts
import MapEditorPage from "./pages/MapEditorPage.vue";
```

3. `tabs` 数组里 `{ id: "wards", label: "病房管理" },` 之后加：

```ts
  { id: "mapeditor", label: "地图编辑器" },
```

4. `<main>` 里 `<WardsPage v-else-if="active === 'wards'" />` 改成：

```vue
      <WardsPage v-else-if="active === 'wards'" @goto-mapeditor="active = 'mapeditor'" />
```

5. `<RolesPage v-else-if="active === 'roles'" />` 之后加：

```vue
      <MapEditorPage v-else-if="active === 'mapeditor'" />
```

- [ ] **步骤 3：WardsPage 的两处硬链改事件**

`frontend/packages/admin/src/pages/WardsPage.vue`：

1. `<script setup>` 里加事件声明（紧跟 `import { ... } from "shared";` 之后）：

```ts
const emit = defineEmits<{ (e: "goto-mapeditor"): void }>();
```

2. 第 180 行附近的散文链接：

```vue
      <a href="/mapeditor/" target="_blank" rel="noopener">地图编辑器</a> 画多边形/矩形（类型选「ward 病区」），
```

改成：

```vue
      <button class="link" @click="emit('goto-mapeditor')">地图编辑器</button> 画多边形/矩形（类型选「ward 病区」），
```

3. 第 224 行附近的说明文字同样把 `/mapeditor/` 硬链改成按钮：

```vue
        先在下面填「地图名 + 区域 uid」（区域在<button class="link" @click="emit('goto-mapeditor')">地图编辑器</button>里画好后从它的列表里抄 uid），
```

4. 样式里加一个链接样式的按钮（`.acts .danger { ... }` 那一段之后）：

```css
button.link { background: none; border: none; color: #7dd3fc; padding: 0; font-size: inherit;
  text-decoration: underline; cursor: pointer; }
```

5. 全文件自查：`Select-String -Path frontend/packages/admin/src/pages/WardsPage.vue -Pattern '/mapeditor/'` → 期望 0 条

- [ ] **步骤 4：SFC 自检（沙箱可行的确定性检查）**

用 `.pnpm` 里的 `@vue/compiler-sfc` 真编译一遍改动过的 `.vue`（解析 + `<script setup>` + 模板三关），比"人工看模板"扎实，且沙箱里能跑：

```powershell
cd D:\_project\Robot
$sfc = (Get-ChildItem frontend\node_modules\.pnpm -Directory -Filter "@vue+compiler-sfc@*" | Select-Object -First 1).FullName + "\node_modules\@vue\compiler-sfc\dist\compiler-sfc.cjs.js"
node -e "const sfc=require(process.argv[1]);const fs=require('fs');for(const f of process.argv.slice(2)){const r=sfc.parse(fs.readFileSync(f,'utf8'),{filename:f});if(r.errors.length){console.error('PARSE ERR',f,r.errors);process.exit(1)};const c=sfc.compileScript(r.descriptor,{id:'x'});sfc.compileTemplate({source:r.descriptor.template?r.descriptor.template.content:'',filename:f,id:'x'});console.log('SFC OK',f,c.content.length)}" $sfc frontend/packages/admin/src/App.vue frontend/packages/admin/src/pages/MapEditorPage.vue frontend/packages/admin/src/pages/WardsPage.vue
```

预期：三行 `SFC OK ...`（命令已在 2026-09-15 实测通过；`vue-tsc` / `pnpm --filter admin build` 在沙箱里受限，归任务 9 步骤 8 的用户侧验收）

- [ ] **步骤 5：Commit**

```powershell
git add -- frontend/packages/admin/src/pages/MapEditorPage.vue frontend/packages/admin/src/App.vue frontend/packages/admin/src/pages/WardsPage.vue
git commit -m "feat(admin): 新增「地图编辑器」页签（按需启停独立服务）" -- frontend/packages/admin/src/pages/MapEditorPage.vue frontend/packages/admin/src/App.vue frontend/packages/admin/src/pages/WardsPage.vue
```

---

## 任务 6：编辑器主界面的「保存并退出」/「仅关窗」+ 掉线提示

**文件：**
- 创建：`frontend/packages/mapeditor/src/lib/service.ts`
- 修改：`frontend/packages/mapeditor/src/App.vue`
- 修改：`frontend/packages/mapeditor/scripts/test-startup-contract.mjs`

- [ ] **步骤 1：先改契约测试（红）**

`frontend/packages/mapeditor/scripts/test-startup-contract.mjs`，在末尾 `console.log` 之前加：

```js
const service = fs.readFileSync(path.join(root, "src", "lib", "service.ts"), "utf8");
assert.match(app, /保存并退出/, "编辑器顶部必须有「保存并退出」");
assert.match(app, /仅关窗/, "编辑器顶部必须有「仅关窗（保留服务）」");
assert.match(app, /stopService\(\)/, "「保存并退出」必须调用 stopService()");
assert.match(service, /\/api\/mapeditor\/service\/stop/, "service.ts 必须打同源自停接口");
assert.match(service, /window\.close\(\)/, "service.ts 必须能关窗");
```

- [ ] **步骤 2：运行验证失败**

```powershell
cd frontend\packages\mapeditor; pnpm test:startup
```
预期：FAIL —— `ENOENT ... lib/service.ts`（注意 `app` 变量已在文件顶部由 `read("App.vue")` 读好）

- [ ] **步骤 3：写 `lib/service.ts`**

创建 `frontend/packages/mapeditor/src/lib/service.ts`：

```ts
// 编辑器「服务自身」的两件事（规格 §4.2）：
//  1) 自停 —— 打**同源** :8010 的 /api/mapeditor/service/stop（前端因此只认识自己这个源）；
//  2) 关窗 —— window.close() 只对 window.open 打开的窗口有效，手动开的标签页关不掉，由页面提示兜底。
// 不做 serviceStatus()："服务还在不在"由 App.vue 的 statusTick 判（fetch 连不上 = http 0 = 服务没了），
// 再封一个状态函数只会变成没人用的死代码（YAGNI）。

/** 停服务（服务会先回响应、再退出）。失败返回 false，调用方负责提示"服务可能还在跑"。 */
export async function stopService(): Promise<boolean> {
  try {
    const res = await fetch("/api/mapeditor/service/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    return res.ok;
  } catch {
    return false;
  }
}

/** 关本窗口；关不掉（非脚本打开的标签页）时静默失败，由页面文字兜底。 */
export function closeSelf(): void {
  try {
    window.close();
  } catch {
    /* 忽略：见文件头第 2 条 */
  }
}
```

- [ ] **步骤 4：改 `App.vue`**

1. script 区 `import { enc, getJson, getSource, setSource, sourcesUrl } from "./lib/api";` 之后加：

```ts
import { closeSelf, stopService } from "./lib/service";
```

2. `const canvasStale = ref(false);` 之后加：

```ts
const serviceDown = ref(false);      // 服务已停（或主编辑器被单独打开）→ 顶部红条 + 停轮询
const exiting = ref(false);
```

3. `statusTick()` 函数体改成（新增"服务掉了"的识别）：

```ts
async function statusTick() {
  if (statusPending) return;
  statusPending = true;
  try {
    const r = await getJson<EditorStatusResp>("/api/mapeditor/status");
    if (r.ok) {
      pose.value = r.data.locator.pose;
      currentMap.value = r.data.locator.current_map;
      io.value = r.data.io;
      serviceDown.value = false;
    } else if (r.http === 0) {
      // 连不上自己这个源 = 服务已被停掉/崩了：别再每 2.5 秒刷错误
      serviceDown.value = true;
      if (timer !== null) { window.clearInterval(timer); timer = null; }
    }
  } finally {
    statusPending = false;
  }
}
```

4. 新增两个动作（放在 `function onSelectMap(...)` 之前）：

```ts
/** 保存并退出：区域/地点是即时保存的，所以这里 = 停服务 + 关窗。
 *  像素修图的改动手动保存要在它自己的窗口点「保存并退出」（本页不掌握那个窗口的状态）。 */
async function saveAndExit() {
  if (exiting.value) return;
  if (!window.confirm(
    "退出后地图编辑器服务会停止，需要再到管理台点「启动地图编辑器」才能进来。\n" +
    "（像素修图的改动要在它自己的窗口点「保存并退出」）\n\n确定退出吗？")) return;
  exiting.value = true;
  try {
    const ok = await stopService();
    if (!ok) {
      exitNote.value = "停止编辑器服务失败（服务可能仍在运行）：请看后端日志，或回管理台点「停止服务」";
      return;
    }
    closeSelf();
    exitNote.value = "地图编辑器服务已停止，请手动关闭本标签页";
    serviceDown.value = true;
  } finally {
    exiting.value = false;
  }
}

/** 只关窗、保留服务：给"开了两个窗口"兜底。 */
function closeWindowOnly() {
  closeSelf();
}
```

5. `const loadNote = ref("");` 之后加（与上面两个 ref 放一起）：

```ts
const exitNote = ref("");
```

6. 顶部工具条里，「刷新」按钮之后、「spacer」之前插入：

```vue
      <button class="mini" :disabled="exiting" @click="saveAndExit">保存并退出</button>
      <button class="mini" @click="closeWindowOnly">仅关窗（保留服务）</button>
```

7. `.sub` 状态条里，`<span v-if="canvasStale" ...>` 一行之后插入：

```vue
      <span v-if="serviceDown" class="bad">· 地图编辑器服务已停止，请关闭本页</span>
      <span v-if="exitNote" class="bad">· {{ exitNote }}</span>
```

- [ ] **步骤 5：运行契约测试验证通过**

```powershell
cd frontend\packages\mapeditor; pnpm test:startup
```
预期：`mapeditor startup contract: ok`

- [ ] **步骤 6：Commit**

```powershell
git add -- frontend/packages/mapeditor/src/lib/service.ts frontend/packages/mapeditor/src/App.vue frontend/packages/mapeditor/scripts/test-startup-contract.mjs
git commit -m "feat(mapeditor): 主界面加「保存并退出」/「仅关窗」+ 服务掉线提示" -- frontend/packages/mapeditor/src/lib/service.ts frontend/packages/mapeditor/src/App.vue frontend/packages/mapeditor/scripts/test-startup-contract.mjs
```

---

## 任务 7：像素修图页的「保存并退出」

**文件：**
- 修改：`frontend/packages/mapeditor/public/pixel-netio.js`

- [ ] **步骤 1：加"退出意图"标志与按钮**

`pixel-netio.js`：

1. `var busy = false;` 那一行（第 81 行附近）下面加：

```js
  var exitAfterSave = false;   // 「保存并退出」按下后的意图：保存**成功**才停服务 + 关窗
```

2. 第 197–202 行，`save` 按钮定义之后加：

```js
    var saveExit = el('button', ST_BTN, '保存并退出');
    saveExit.type = 'button';
    saveExit.title = '保存到服务器，成功后关闭地图编辑器服务并关本窗口（失败则不停不关）';
```

并把 `row2.appendChild(save);` 之后改成：

```js
    row2.appendChild(save);
    row2.appendChild(saveExit);
```

3. `setBusy` 里的按钮列表（第 138 行）加上 `'saveExit'`：

```js
    ['reload', 'test', 'save', 'saveExit'].forEach(function (k) {
```

4. `ui = { ... }` 对象里 `save: save,` 之后加：

```js
      saveExit: saveExit,
```

5. `save.addEventListener('click', function () { manualSave(); });` 之后加：

```js
    saveExit.addEventListener('click', function () { exitAfterSave = true; manualSave(); });
```

- [ ] **步骤 2：让"失败/空提交"清掉意图**

1. `manualSave()` 的两个提前 return（没有 mapName、找不到 #btnDownloadMap）里各补一行：

```js
    if (!mapName) { msg('error', '未指定地图（URL 缺 ?map=<地图名>），无法保存。'); exitAfterSave = false; return; }
```
```js
    if (!btn) { msg('error', '找不到 #btnDownloadMap（上游页面结构可能已变）。'); exitAfterSave = false; return; }
```

2. `flushDownloads()` 里两处提前 return 前补 `exitAfterSave = false;`：

```js
    if (!pgmBlob) {
      if (yamlBlob) msg('warn', '只截获到 yaml，没有 pgm 像素数据，本次不提交。');
      exitAfterSave = false;
      return;
    }
    if (busy) {
      msg('warn', '上一次保存尚未结束，本次改动未提交，请稍后重试。');
      exitAfterSave = false;
      return;
    }
```

3. `saveToServer()` 里前端预校验失败的两个 `return Promise.resolve();`（新图名不合法 / 含 keepout）之前各补 `exitAfterSave = false;`。

- [ ] **步骤 3：保存成功后停服务 + 关窗**

1. 在 `renderSaveErr(status, data)` 函数体第一行加：

```js
    exitAfterSave = false;             // 保存失败：清掉退出意图，不停不关
```

2. 在 `renderSaveOk(data, mode)` 函数体**末尾**（warnings 追加之后）加：

```js
    if (exitAfterSave) {
      exitAfterSave = false;
      stopServiceAndClose();
    }
```

3. 在 `renderSaveOk` 之后新增函数：

```js
  /** 「保存并退出」的收尾：停编辑器服务（同源）→ 关窗。失败只提示，不假装成功。 */
  function stopServiceAndClose() {
    setBusy(true, '已保存。正在停止地图编辑器服务…');
    fetch('/api/mapeditor/service/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}'
    }).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      window.close();
      msg('good', '已保存，地图编辑器服务已停止。若本页未自动关闭，请手动关闭。');
    }).catch(function (e) {
      msg('warn', '保存成功，但停止服务失败（' + errText(e) + '）：请回地图编辑器主页点「保存并退出」。');
    }).then(function () {
      setBusy(false);
    });
  }
```

- [ ] **步骤 4：重建产物（**必须**：`/mapeditor` 挂在 `dist/` 上，改的是 `public/`）**

```powershell
cd frontend\packages\mapeditor; pnpm build:sandbox
```
预期：`[build:sandbox] ✅ 产物自检通过`，且 `dist/pixel-netio.js` 里出现 `保存并退出`：

```powershell
Select-String -Path dist\pixel-netio.js -Pattern '保存并退出|service/stop'
```

（正常开发机上等价命令是 `pnpm build`；沙箱里只能用 `build:sandbox` —— 规格 §六 第 10 条的 `pnpm -r build` 留给用户侧复验）

- [ ] **步骤 5：语法自检**

```powershell
node --check frontend\packages\mapeditor\public\pixel-netio.js
```
预期：无输出（语法 OK）

- [ ] **步骤 6：Commit**

```powershell
git add -- frontend/packages/mapeditor/public/pixel-netio.js
git commit -m "feat(mapeditor): 像素修图页加「保存并退出」（保存成功才停服务并关窗）" -- frontend/packages/mapeditor/public/pixel-netio.js
```

---

## 任务 8：`start_UI.py` 口径 + 文档同步

**文件：**
- 修改：`start_UI.py`
- 修改：`AGENTS.md`、`docs/log.md`、`docs/superpowers/specs/2026-09-14-map-editor-design.md`、`docs/superpowers/specs/2026-09-15-map-editor-on-demand-service-design.md`

- [ ] **步骤 1：改 `start_UI.py` 文档串**

把文件头 docstring（第 2–30 行）里的两处口径改掉：

```
养老陪护机器人 —— 一键启动程序（**UI 前端 + 后端**）。
```
```
流程（生产模式）：
  1. 检查根目录 .env 是否配置 DEEPSEEK_API_KEY
  2. 检查 8000 端口占用（占用则询问是否结束旧进程重启）
  3. 检查前端构建产物（frontend/packages/{admin,kiosk}/dist），缺失则提示构建
  4. 用虚拟环境解释器启动 uvicorn（LLM.server:app，静态托管 /admin /kiosk）
  5. 轮询 /api/health 确认后端就绪
  6. 浏览器打开 http://127.0.0.1:8000/（admin 默认入口）与 /kiosk/
  7. 常驻前台，Ctrl+C 优雅停止

**地图编辑器（像素修图 + 划线/标点）不在本脚本的启动清单里**：它由后端按需以
独立进程（:8010）拉起，入口是 admin →「地图编辑器」页签 →「启动地图编辑器」。
要连前后端一起带 ROS 的全量启动器是另有其脚本（`start.py`），本脚本只管陪护 UI。
```

- [ ] **步骤 2：启动完成后打印提示**

`main()` 末尾「常驻前台」那一段（`info("服务运行中。按 Ctrl+C 停止全部进程……")` 之前）加：

```python
    info("地图编辑器按需启动：admin →「地图编辑器」页签 →「启动地图编辑器」（独立进程 :8010）")
```

- [ ] **步骤 3：核对本文件没有 mapeditor 残留职责**

```powershell
Select-String -Path start_UI.py -Pattern 'mapeditor'
```
预期：只剩步骤 1/2 新增的两处**说明文字**，没有任何构建检查或启动动作。

- [ ] **步骤 4：同步文档**

1. `AGENTS.md`「快速上手 → 前端」段：
   - 生产模式说明里 `/mapeditor` 的挂载改口径：**不再由主后端挂载**；编辑器由 `LLM.mapeditor_server:app`（:8010）按需提供，入口在 admin →「地图编辑器」页签。
   - `dev:mapeditor`（:5175）保持不变（开发时仍可单独起 Vite）。
   - 「后端运行位置」那段追加：主后端保留 `locator`/`maptags`（病房自动切换与记录病房区域），编辑器路由已搬到 `LLM/mapapi.py`。
2. `docs/log.md` 追加一条 2026-09-15 条目：三块新文件、搬迁行数、3 条服务接口、前端改动、测试数字（429 passed 基线）、未验项（真机/浏览器 `window.open`→`window.close` 链路、`pnpm -r build`）。
3. `docs/superpowers/specs/2026-09-14-map-editor-design.md` 顶部「口径变更与加装项」表追加一行：**第 8 条 —— 编辑器改为独立进程按需启动（2026-09-15）**，指向本规格。
4. 本规格 `docs/superpowers/specs/2026-09-15-map-editor-on-demand-service-design.md` 的「§十一 实现台账与偏差」填写：逐任务提交号、与设计的偏差、未验项。

- [ ] **步骤 5：Commit**

```powershell
git add -- start_UI.py AGENTS.md docs/log.md docs/superpowers/specs/2026-09-14-map-editor-design.md docs/superpowers/specs/2026-09-15-map-editor-on-demand-service-design.md
git commit -m "docs+chore(start): start_UI.py 定位为纯 UI 启动器；AGENTS/日志/规格同步编辑器按需启动口径" -- start_UI.py AGENTS.md docs/log.md docs/superpowers/specs/2026-09-14-map-editor-design.md docs/superpowers/specs/2026-09-15-map-editor-on-demand-service-design.md
```

---

## 任务 9：端到端验收

**文件：** 无（只跑命令 + 记录结果到规格 §十一）

- [ ] **步骤 1：起主后端（后台作业）**

```powershell
.venv\Scripts\python.exe -m uvicorn LLM.server:app --host 0.0.0.0 --port 8000
```

- [ ] **步骤 2：验收 §六 第 1 条 —— 主后端没有编辑器**

```powershell
(Invoke-WebRequest -SkipHttpErrorCheck http://127.0.0.1:8000/api/map/list).StatusCode   # 期望 404
(Invoke-WebRequest -SkipHttpErrorCheck http://127.0.0.1:8000/mapeditor/).StatusCode    # 期望 404
(Test-NetConnection 127.0.0.1 -Port 8010).TcpTestSucceeded                             # 期望 False
```

- [ ] **步骤 3：验收 §六 第 8 条 —— 非管理员 403**

```powershell
(Invoke-WebRequest -SkipHttpErrorCheck -Method POST -Headers @{'X-Surface'='kiosk'} http://127.0.0.1:8000/api/mapeditor/service/start).StatusCode   # 期望 403
```

- [ ] **步骤 4：手动启动服务（等价于 admin 点按钮）**

```powershell
# 4a 口令门开着时先登管理员（把 admin 槽位提上来；口令见主后端启动日志或你在 admin 页设的那个）
(Invoke-WebRequest -Method POST -ContentType 'application/json' -Body '{"password":"<管理员口令>"}' http://127.0.0.1:8000/api/session/login).Content
# 4b 启动（等价于 admin 点「启动地图编辑器」）
(Invoke-WebRequest -Method POST -Headers @{'X-Surface'='admin'} -ContentType 'application/json' -Body '{}' http://127.0.0.1:8000/api/mapeditor/service/start).Content
# 期望 {"ok":true,"running":true,"source":"managed","pid":...,"port":8010,...}
(Invoke-WebRequest http://127.0.0.1:8010/api/mapeditor/service).Content
(Invoke-WebRequest -SkipHttpErrorCheck http://127.0.0.1:8010/mapeditor/).StatusCode      # 期望 200
(Invoke-WebRequest -SkipHttpErrorCheck http://127.0.0.1:8010/mapeditor/pixel-editor.html).StatusCode   # 期望 200
```

- [ ] **步骤 5：验收 §六 第 4/5/6 条 —— 停止（三条路径）**

```powershell
# 5a 自停（等价于编辑器里点「保存并退出」）
(Invoke-WebRequest -Method POST -ContentType 'application/json' -Body '{}' http://127.0.0.1:8010/api/mapeditor/service/stop).Content
Start-Sleep -Seconds 2
(Test-NetConnection 127.0.0.1 -Port 8010).TcpTestSucceeded           # 期望 False

# 5b 主后端 stop（等价于 admin 点「停止服务」）
(Invoke-WebRequest -Method POST -Headers @{'X-Surface'='admin'} -ContentType 'application/json' -Body '{}' http://127.0.0.1:8000/api/mapeditor/service/start) | Out-Null
(Invoke-WebRequest -Method POST -Headers @{'X-Surface'='admin'} -ContentType 'application/json' -Body '{}' http://127.0.0.1:8000/api/mapeditor/service/stop).Content
(Test-NetConnection 127.0.0.1 -Port 8010).TcpTestSucceeded           # 期望 False

# 5c 幂等：再 stop 一次仍 ok
(Invoke-WebRequest -Method POST -Headers @{'X-Surface'='admin'} -ContentType 'application/json' -Body '{}' http://127.0.0.1:8000/api/mapeditor/service/stop).Content
```

- [ ] **步骤 6：验收 §六 第 7 条 —— 主后端退出带走服务**

```powershell
(Invoke-WebRequest -Method POST -Headers @{'X-Surface'='admin'} -ContentType 'application/json' -Body '{}' http://127.0.0.1:8000/api/mapeditor/service/start) | Out-Null
(Invoke-WebRequest -Method POST http://127.0.0.1:8000/api/system/shutdown).Content
Start-Sleep -Seconds 3
(Test-NetConnection 127.0.0.1 -Port 8010).TcpTestSucceeded           # 期望 False
```

- [ ] **步骤 7：全量回归 + 前端测试**

```powershell
.venv\Scripts\python.exe -m pytest LLM/tests tests -q        # 期望 5 failed / 429+ / 1 skipped（失败集合不变）
cd frontend; node --import ./temp-esbuild-register.mjs ./packages/shared/node_modules/vitest/vitest.mjs run --root packages/shared
cd frontend\packages\mapeditor; pnpm test:startup
```

- [ ] **步骤 8：把结果写进规格 §十一 + 用户侧待验清单**

必须写清**只有用户能做**的验收项：

1. 浏览器里真正走一遍：admin →「启动地图编辑器」→ 弹窗打开 → 划线/标点 → 像素修图「保存并退出」（`window.open` → `window.close()` 链路只有真实浏览器能验）。
2. `cd frontend && pnpm -r build` 三包构建（沙箱 esbuild EPERM）。
3. 真机联动（`MAPS_IO=ssh` 下编辑器读写板卡地图；本文不改地图 IO 口径）。

- [ ] **步骤 9：Commit（若步骤 8 改动了规格文件）**

```powershell
git add -- docs/superpowers/specs/2026-09-15-map-editor-on-demand-service-design.md
git commit -m "docs(spec): 地图编辑器按需启动 —— 回填实现台账与验收结果" -- docs/superpowers/specs/2026-09-15-map-editor-on-demand-service-design.md
```

---

## 附录 A：沙箱里跑 shared 单测的预加载文件

`frontend/temp-esbuild-register.mjs`（本地未入库；缺失时按此内容重建）：

```js
// 受限沙箱里跑 vitest 的模块预加载：esbuild 垫片 + 子进程 stdio 适配。
import { register } from "node:module";
import { pathToFileURL } from "node:url";

import "./packages/mapeditor/scripts/child-process-stdio.mjs";

register("./packages/mapeditor/scripts/esbuild-shim.mjs", pathToFileURL(import.meta.filename));
```

运行：`cd frontend; node --import ./temp-esbuild-register.mjs ./packages/shared/node_modules/vitest/vitest.mjs run --root packages/shared`

---

## 附录 B：本仓提交铁律（每个任务的 commit 都适用）

- **必须带 pathspec**：`git commit -m "..." -- <exact files>`。本仓多个窗口在同一工作区并行开发，裸 `git commit` 会把别人暂存/在途的改动一起提交（有"误含 49 文件"前科）。
- 提交前跑一次全量 pytest，确认**失败集合**没有新增（passed 数会随并行窗口变动，不作为判据）。
- 基线（2026-09-15 实测）：`pytest LLM/tests tests -q` → **5 failed / 429 passed / 1 skipped**；5 个失败 = `test_modules_status`、`test_unlock_switch`×3、`tests/test_vision.py::test_end_to_end_webcam_source_serves_decodable_jpeg`，**均不许修**。
