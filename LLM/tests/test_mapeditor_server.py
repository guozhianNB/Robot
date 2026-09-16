# -*- coding: utf-8 -*-
r"""独立编辑器服务 app 的外壳行为（规格 §3.2）。"""
import os

from fastapi.testclient import TestClient

from LLM import conf, mapeditor_server


def _paths(app) -> set:
    """app 的全部路径（含 ``include_router`` 进来的子路由）。

    本机 fastapi 0.141/starlette 1.6 起 ``include_router()`` 是**惰性**的：它往
    ``app.routes`` 里放一个 ``_IncludedRouter`` 占位对象，子路由不在 ``app.routes`` 里
    逐条列出（旧版是当场拷一份）。所以这里遇到占位对象就展开一次；旧版没有该方法时
    行为与 ``{r.path for r in app.routes}`` 完全一致。
    """
    out = set()
    for r in app.routes:
        out.add(getattr(r, "path", "") or "")
        contexts = getattr(r, "effective_route_contexts", None)
        if callable(contexts):
            out |= {getattr(c.original_route, "path", "") or "" for c in contexts()}
    return out


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
