# -*- coding: utf-8 -*-
r"""编辑器服务的进程管理（规格 §3.3）—— 全程假 Popen / 假探活，不真起进程。"""
import threading

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
    """拉起成功 → managed + 真实 pid。

    ``_probe`` 的桩必须**按时间线**给值，不能恒 True：``start()`` 在拉起之前先探活
    （规格 §3.3：探到 8010 活着 = 外部实例，**不重复拉起**），恒 True 会让"拉起前"那一次
    探活就命中，函数直接返回 ``external`` 而根本不 spawn —— 那样这例断言的是"拉起成功后
    managed"，前提却是"8010 上已经有服务在应答"，自相矛盾。真实世界里探活在拉起前必然
    是 False（没人监听），就绪之后才转 True，本桩即按此建模。
    """
    spawned = []
    ready = {"spawned": False}

    def fake_probe(timeout=2.0):
        return ready["spawned"]          # 拉起前 False；Popen 返回后 True（已就绪）

    def fake_popen(cmd, cwd=None):
        spawned.append(cmd)
        ready["spawned"] = True          # 从此探活转 True（服务就绪）
        return FakeProc()

    monkeypatch.setattr(mapctl, "_probe", fake_probe)
    monkeypatch.setattr(mapctl, "port_alive", lambda port=None, timeout=0.5: False)
    monkeypatch.setattr(mapctl.subprocess, "Popen", fake_popen)
    st = mapctl.start()
    assert st["ok"] is True and st["running"] is True and st["source"] == "managed"
    assert st["pid"] == 4242 and st["port"] == mapctl._port()
    assert spawned and spawned[0][1:3] == ["-m", "uvicorn"]
    assert "LLM.mapeditor_server:app" in spawned[0]


def test_start_is_idempotent_for_external(clean_state, monkeypatch):
    """8010 上有外部实例（孤儿）→ 不重复拉起。"""
    called = []
    monkeypatch.setattr(mapctl, "_probe", lambda timeout=2.0: True)
    # `port_alive` 也必须桩掉：本机真跑着编辑器（开发时的常态）时它会去连真 8010，
    # 让"不重复拉起"这条断言依赖环境（探到真端口 → 走的是另一条分支/甚至真 POST）。
    monkeypatch.setattr(mapctl, "port_alive", lambda port=None, timeout=0.5: False)
    monkeypatch.setattr(mapctl.subprocess, "Popen", lambda *a, **k: called.append(1))
    st = mapctl.start()
    assert st["source"] == "external" and st["running"] is True
    assert called == []


def test_start_judges_and_spawns_under_one_lock(clean_state, monkeypatch):
    """「判活 + spawn」必须在同一段 `_lock` 临界区里。

    判据取得很直接：把 `_lock` 换成**非重入**锁，于是在 `_probe()` / `port_alive()` /
    `Popen()` 里用 `lock.locked()` 就能看见"此刻是否有人握着这把锁"。为什么不用多线程复现
    竞态：GIL 下两个线程的探活极容易错开（第二个线程常在第一段临界区之后才进来），那样的
    用例时红时绿；而"spawn 与判活是否同锁"是那条竞态唯一的根因，且完全确定。
    """
    lock = threading.Lock()              # 非重入：只要有人在临界区里，locked() 就说真话
    seen = []
    spawned = []
    monkeypatch.setattr(mapctl, "_lock", lock)

    def fake_probe(timeout=2.0):
        seen.append(("probe", lock.locked()))
        return bool(spawned)

    def fake_port_alive(port=None, timeout=0.5):
        seen.append(("port_alive", lock.locked()))
        return False

    def fake_popen(cmd, cwd=None):
        seen.append(("spawn", lock.locked()))
        spawned.append(cmd)
        return FakeProc()

    monkeypatch.setattr(mapctl, "_probe", fake_probe)
    monkeypatch.setattr(mapctl, "port_alive", fake_port_alive)
    monkeypatch.setattr(mapctl.subprocess, "Popen", fake_popen)
    st = mapctl.start()
    assert st["source"] == "managed" and st["running"] is True
    assert seen[:3] == [("probe", True), ("port_alive", True), ("spawn", True)], seen


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
    """有句柄 → terminate（与上一例同款时间线桩：拉起前探活 False，否则 start 不会 spawn）。"""
    proc = FakeProc()
    ready = {"spawned": False}

    def fake_probe(timeout=2.0):
        return ready["spawned"]

    def fake_popen(cmd, cwd=None):
        ready["spawned"] = True
        return proc

    monkeypatch.setattr(mapctl, "_probe", fake_probe)
    monkeypatch.setattr(mapctl, "port_alive", lambda port=None, timeout=0.5: False)
    monkeypatch.setattr(mapctl.subprocess, "Popen", fake_popen)
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


def test_stop_hard_kills_without_grace(clean_state, monkeypatch):
    """`hard=True` → 直接 kill（不走 5 秒优雅期）。

    这条形态对应 `/api/system/shutdown`：那条路径只留 1 秒就要 `os._exit(0)`，
    POSIX 上 `terminate()` 只是发 SIGTERM、uvicorn 还要排干连接，等不到就把编辑器
    留成孤儿（reparent 后只能靠 external 状态事后收）。
    """
    proc = FakeProc()
    ready = {"spawned": False}

    def fake_probe(timeout=2.0):
        return ready["spawned"]

    def fake_popen(cmd, cwd=None):
        ready["spawned"] = True
        return proc

    monkeypatch.setattr(mapctl, "_probe", fake_probe)
    monkeypatch.setattr(mapctl, "port_alive", lambda port=None, timeout=0.5: False)
    monkeypatch.setattr(mapctl.subprocess, "Popen", fake_popen)
    mapctl.start()
    st = mapctl.stop(hard=True)
    assert proc.killed is True and proc.terminated is False
    assert st["ok"] is True and st["running"] is False


def test_stop_reports_foreign_port_occupier(clean_state, monkeypatch):
    """无句柄 + 端口活 + 探活假 = **陌生监听者**：不发 POST，直接说清怎么收敛。

    为什么不能发 POST：那个 POST 是编辑器自己的自停接口，陌生服务看不懂、半死的编辑器
    也不会应答；失败后给"请到它的终端按 Ctrl+C"这种指引对陌生服务毫无意义。
    """
    posted = []
    monkeypatch.setattr(mapctl, "_probe", lambda timeout=2.0: False)
    monkeypatch.setattr(mapctl, "port_alive", lambda port=None, timeout=0.5: True)
    monkeypatch.setattr(mapctl.urllib.request, "urlopen",
                        lambda *a, **k: posted.append(a))
    st = mapctl.stop()
    assert st["ok"] is False and st["running"] is True and st["source"] == "external"
    assert "MAP_EDITOR_PORT" in st["error"]
    assert posted == [], "对陌生监听者不该发 /api/mapeditor/service/stop"


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
