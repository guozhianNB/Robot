# -*- coding: utf-8 -*-
r"""独立编辑器服务 app 的外壳行为（规格 §3.2）。

路由内省走 ``app_paths`` fixture（见 conftest.py）—— 本机 fastapi 0.141 的
``include_router()`` 是惰性的，``app.routes`` 里只有 ``_IncludedRouter`` 占位对象。
"""
import os

import pytest
from fastapi.testclient import TestClient

from LLM import conf, mapeditor_server
from LLM.maps import mapapi


def test_editor_app_serves_all_editor_routes(app_paths):
    paths = app_paths(mapeditor_server.app)
    for kept in ("/api/map/list", "/api/destinations", "/api/zones",
                 "/api/robot/pose", "/api/mapeditor/status",
                 "/api/mapeditor/service", "/api/mapeditor/service/stop"):
        assert kept in paths, "独立服务缺路由：{}".format(kept)


def test_editor_app_mounts_mapeditor_page(app_paths):
    """/mapeditor 挂上了**且真能取到页面**。

    只断言"路径前缀存在"是空断言：`StaticFiles` 挂错目录（例如挂到没有 index.html 的
    目录）时路径照样在，页面却 404。`dist/` 不入库（.gitignore），所以"两个候选目录都没有
    index.html"的裸检出上，`/mapeditor/` 本来就该 404（mount_editor 的既有降级口径：
    至少让原生页 `pixel-editor.html` 可达）—— 那种情形下改为断言这条降级仍然成立，
    绝不让断言静默空过。
    """
    assert any(p.startswith("/mapeditor") for p in app_paths(mapeditor_server.app))
    c = TestClient(mapeditor_server.app)
    has_index = any((d / "index.html").exists()
                    for d in (mapapi._MAPEDITOR_DIST, mapapi._MAPEDITOR_PUBLIC))
    if has_index:
        r = c.get("/mapeditor/")
        assert r.status_code == 200, r.text
    else:
        r = c.get("/mapeditor/pixel-editor.html")
        assert r.status_code == 200, r.text


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


def test_delayed_exit_refuses_to_kill_pytest():
    """护栏本身要有测试：在 pytest 进程里调 `_delayed_exit` 必须抛，而不是 os._exit。

    为什么值得一条测试：这个函数的误用形态是"pytest 以 0 码猝死"，任何断言都跑不到，
    只能靠护栏自己红/抛来暴露。
    """
    with pytest.raises(RuntimeError, match="拒绝在测试进程里"):
        mapeditor_server._delayed_exit(delay=0)
