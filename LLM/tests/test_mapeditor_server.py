# -*- coding: utf-8 -*-
r"""独立编辑器服务 app 的外壳行为（规格 §3.2）。

路由内省走 ``app_paths`` fixture（见 conftest.py）—— 本机 fastapi 0.141 的
``include_router()`` 是惰性的，``app.routes`` 里只有 ``_IncludedRouter`` 占位对象。
"""
import os

from fastapi.testclient import TestClient

from LLM import conf, mapeditor_server


def test_editor_app_serves_all_editor_routes(app_paths):
    paths = app_paths(mapeditor_server.app)
    for kept in ("/api/map/list", "/api/destinations", "/api/zones",
                 "/api/robot/pose", "/api/mapeditor/status",
                 "/api/mapeditor/service", "/api/mapeditor/service/stop"):
        assert kept in paths, "独立服务缺路由：{}".format(kept)


def test_editor_app_mounts_mapeditor_page(app_paths):
    assert any(p.startswith("/mapeditor") for p in app_paths(mapeditor_server.app))


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
