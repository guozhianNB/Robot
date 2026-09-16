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
    """幂等启动：已在跑（managed/external）→ 直接返回现状。

    判活分两步、且**句柄优先于探活**（顺序不能换）：
      1. 先看**自己的句柄**（``proc.poll()``）：它是"我们拉起的孩子还活着吗"的权威答案，
         不该被网络层的一次超时误判掉 —— 刚 spawn、uvicorn 还在 import 的那几百毫秒里
         探活必然是 False，但那时我们是 managed，不是"没起来"；
      2. 再看**探活**：既没句柄又探到 8010 有应答 = 外部实例（孤儿，规格 §3.3 / 孤儿表：
         报 ``source="external"``、**不重复拉起**），这种情况没有句柄可查，只能靠探活。
    两步都不中、且端口**另有非编辑器监听者**时才是"端口被占用"（点明改 MAP_EDITOR_PORT）。
    """
    global _proc, _started_at
    with _lock:
        proc = _proc
        managed = proc is not None and proc.poll() is None
    if managed:
        return status()
    if _probe(2.0):
        return status()                  # 端口上有外部实例（孤儿）：不重复拉起
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


# 三条都用**同步 `def`**：FastAPI 会把同步处理函数丢进线程池跑，而 `status()` 里的 `_probe()`
# 是同步 HTTP（最长 0.5s）；写成 `async def` 会把事件循环卡住（本仓既有约定：会阻塞的活儿
# 不进事件循环，见 server.py 里 `asyncio.to_thread` 的用法）。
@router.get("/api/mapeditor/service")
def map_editor_service(x_surface: str = Header(default="kiosk")):
    """编辑器服务状态（仅管理员）。"""
    _require_admin(x_surface)
    return status()


@router.post("/api/mapeditor/service/start")
def map_editor_service_start(x_surface: str = Header(default="kiosk")):
    """拉起编辑器服务（幂等）。"""
    _require_admin(x_surface)
    return start()


@router.post("/api/mapeditor/service/stop")
def map_editor_service_stop(x_surface: str = Header(default="kiosk")):
    """停掉编辑器服务（幂等）。"""
    _require_admin(x_surface)
    return stop()
