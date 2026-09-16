# -*- coding: utf-8 -*-
r"""地图编辑器独立服务的进程管理（主后端侧，规格 §3.3）。

主后端**不 import 编辑器业务**（那是 ``LLM.mapapi``，且跑在另一个进程里）：本模块只用 stdlib
拉起 / 探活 / 停止它，对外暴露 3 条**仅管理员**可用接口：

    GET  /api/mapeditor/service          状态（none | managed | external）
    POST /api/mapeditor/service/start    幂等启动（等就绪 ≤ MAP_EDITOR_START_TIMEOUT）
    POST /api/mapeditor/service/stop     幂等停止（有句柄→terminate；无句柄但端口活→让它自停）

``external`` = 端口上有服务，但不是本进程拉起的（例如主后端被 kill -9 后留下的孤儿）：
这种情况**不重复拉起**；停止时调它自己的 ``POST /api/mapeditor/service/stop``
（比"按端口找 PID 再 kill"更安全、且跨平台）。端口上若是**陌生监听者**（端口活但探活不通），
不发那个 POST —— 它看不懂，属于"只能人工释放端口/换端口"的情形，见 ``stop()``。
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
        # pid / started_at 与 managed 一起在锁内快照：锁外再读 `_proc` 可能已被
        # 并发的 stop() 置成 None（→AttributeError）或换了新句柄（→pid 张冠李戴）
        pid = proc.pid if managed else None
        started_at = _started_at if managed else 0.0
    running = managed or _probe(0.5)
    return {"ok": True,
            "running": bool(running),
            "source": "managed" if managed else ("external" if running else "none"),
            "pid": pid,
            "port": _port(),
            "uptime_s": round(time.time() - started_at, 1) if started_at else None}


def start() -> dict:
    """幂等启动：已在跑（managed/external）→ 直接返回现状。

    判活分两步、且**句柄优先于探活**（顺序不能换）：
      1. 先看**自己的句柄**（``proc.poll()``）：它是"我们拉起的孩子还活着吗"的权威答案，
         不该被网络层的一次超时误判掉 —— 刚 spawn、uvicorn 还在 import 的那几百毫秒里
         探活必然是 False，但那时我们是 managed，不是"没起来"；
      2. 再看**探活**：既没句柄又探到 8010 有应答 = 外部实例（孤儿，规格 §3.3 / 孤儿表：
         报 ``source="external"``、**不重复拉起**），这种情况没有句柄可查，只能靠探活。
    两步都不中、且端口**另有非编辑器监听者**时才是"端口被占用"（点明改 MAP_EDITOR_PORT）。

    **「判活 + spawn」整段在同一把 `_lock` 里**：否则两个并发的 admin `POST /start`
    （两个页签 / 连点）会各自探活一次、都得出"没起"，然后各 Popen 一个 uvicorn 去争同一个
    8010。就绪轮询则**放在锁外**——那段最长要等 `MAP_EDITOR_START_TIMEOUT`，含在锁里会
    连 `stop()` 一起堵住（stop 也要这把锁）。
    """
    global _proc, _started_at
    with _lock:
        proc = _proc
        managed = proc is not None and proc.poll() is None
        if managed:
            action = "already"           # 我们拉起的孩子还活着（判据优先于探活，见上）
        elif _probe(2.0):
            action = "already"           # 端口上有外部实例（孤儿）：不重复拉起
        elif port_alive():
            action = "port_in_use"
        else:
            action = "spawn"
            cmd = [sys.executable, "-m", "uvicorn", "LLM.mapeditor_server:app",
                   "--host", "0.0.0.0", "--port", str(_port())]
            child = subprocess.Popen(cmd, cwd=str(BASE_DIR))     # 输出透传，便于排障
            _proc = child
            _started_at = time.time()

    if action == "already":
        return status()
    if action == "port_in_use":
        audit.log("map_editor_service", action="start_failed",
                  reason="port_in_use", port=_port())
        return {"ok": False, "running": False, "source": "none", "pid": None,
                "port": _port(), "uptime_s": None,
                "error": "端口 {} 被占用且不是地图编辑器服务：请释放该端口，"
                         "或改 conf.MAP_EDITOR_PORT".format(_port())}

    # ↓ 就绪轮询在锁外；`child` 是本地句柄（不读模块级 `_proc`：并发 stop() 会把它置 None）
    deadline = time.monotonic() + float(conf.MAP_EDITOR_START_TIMEOUT)
    while time.monotonic() < deadline:
        if child.poll() is not None:
            rc = child.returncode
            reset_for_test()
            audit.log("map_editor_service", action="start_failed",
                      reason="exited", code=rc, port=_port())
            return {"ok": False, "running": False, "source": "none", "pid": None,
                    "port": _port(), "uptime_s": None,
                    "error": "地图编辑器服务启动即退出（退出码 {}）："
                             "请看主后端终端里的 uvicorn 日志".format(rc)}
        if _probe(1.0):
            audit.log("map_editor_service", action="start",
                      pid=child.pid, port=_port(), source="managed")
            return status()
        time.sleep(0.4)

    stop()                       # 半死状态（占端口又不可用）不留着
    audit.log("map_editor_service", action="start_failed", reason="timeout", port=_port())
    return {"ok": False, "running": False, "source": "none", "pid": None,
            "port": _port(), "uptime_s": None,
            "error": "地图编辑器服务 {} 秒内未就绪（已停止）：请看主后端终端里的 "
                     "uvicorn 日志".format(int(conf.MAP_EDITOR_START_TIMEOUT))}


def stop(hard: bool = False) -> dict:
    """幂等停止：有句柄→terminate（5s 后 kill）；无句柄但端口活→调它自己的 stop。

    ``hard=True``：不去等那句优雅期，对 managed 句柄**直接 ``kill()``**（随后仍 ``wait(3)``
    兜一下）。给 ``/api/system/shutdown`` 用：那条路径只留 1 秒就要 ``os._exit(0)``，而
    POSIX 上 ``terminate()`` 只是发 SIGTERM、uvicorn 还要排干连接，等不到就会把编辑器
    reparent 成孤儿（只能靠 ``external`` 状态事后收）。Windows 上 terminate 本就是立即的，
    这条形参把两个平台拉齐。``lifespan`` 收尾仍走优雅路径（那里没有 1 秒死线）。
    """
    global _proc, _started_at
    with _lock:
        proc = _proc
        _proc = None
        _started_at = 0.0

    if proc is not None and proc.poll() is None:
        if hard:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        else:
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
                  pid=proc.pid, port=_port(), source="managed", hard=hard)
        return {"ok": True, "running": False, "source": "none", "pid": None,
                "port": _port(), "uptime_s": None}

    if port_alive():
        # 端口活但**探活不通** = 陌生服务或半死的编辑器：它不认识 `/api/mapeditor/service/stop`，
        # POST 过去只会拿到一个看不懂的响应，再报"请到它的终端按 Ctrl+C"对陌生服务毫无意义。
        # 这种情况**不发 POST**，直接说清唯一可执行的收敛方式（释放端口 / 换端口）。
        if not _probe(2.0):
            audit.log("map_editor_service", action="stop_failed",
                      reason="port_foreign", source="external", port=_port())
            return {"ok": False, "running": True, "source": "external", "pid": None,
                    "port": _port(), "uptime_s": None,
                    "error": "{} 上的监听者不是本后端拉起的编辑器，也不是可自停的编辑器实例："
                             "请释放该端口，或改 conf.MAP_EDITOR_PORT".format(_port())}
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
