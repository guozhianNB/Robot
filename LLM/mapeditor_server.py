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
import sys
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
    """延迟退出：先把响应发回去，再退（与 server.py::_delayed_exit 同款做法）。

    **测试进程里绝不真退**：`os._exit(0)` 会让 pytest 以成功码猝死（无 traceback、
    输出被截断），失败不可归因 —— 这是本模块最强的红线之一，所以让它由代码守卫，
    而不是靠"每个调用点都记得注入 `_schedule_exit`"的纪律。
    判据刻意**只**看 `sys.modules`：`PYTEST_CURRENT_TEST` 会被 `subprocess.Popen`
    继承给真实的 uvicorn 子进程，用它会把生产路径也一起拦住。
    """
    if "pytest" in sys.modules:
        raise RuntimeError("拒绝在测试进程里执行 os._exit(0)：请注入 _schedule_exit")
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
    # port 只是**回显**配置常量（编辑器页拿它拼自身地址），不作事实来源：本进程到底绑在哪个
    # 端口，由 uvicorn 启动参数决定。探活方（mapctl._probe）只看这条接口是否 HTTP 200。
    return {"ok": True, "running": True, "pid": os.getpid(), "port": MAP_EDITOR_PORT}


@app.post("/api/mapeditor/service/stop")
async def service_stop():
    """自杀：编辑器页「保存并退出」调它。先回响应，0.5 秒后退出。"""
    _schedule_exit()
    audit.log("map_editor_service", action="self_stop", pid=os.getpid())
    return {"ok": True, "message": "地图编辑器服务正在退出…"}
