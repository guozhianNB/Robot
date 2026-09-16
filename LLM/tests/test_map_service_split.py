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
