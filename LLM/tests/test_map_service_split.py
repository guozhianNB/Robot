# -*- coding: utf-8 -*-
r"""编辑器后端从主后端拆出去的边界测试（规格 docs/superpowers/specs/2026-09-15-*.md §3）。

路由内省一律走 ``app_paths`` fixture（见 conftest.py）：fastapi 0.141 +
starlette 1.6 的 ``include_router()`` 是惰性的，直接遍历 ``app.routes`` 会漏掉
全部子路由 —— 本文件的否定断言曾经因此**空过**。
"""
from LLM.server import app as main_app
from LLM import mapapi


def test_main_app_no_longer_serves_map_editor(app_paths):
    """主后端不该再暴露编辑器路由（它们跑在独立进程里）。"""
    paths = app_paths(main_app)
    for gone in ("/api/map/list", "/api/map/current", "/api/map/sources",
                 "/api/destinations", "/api/zones", "/api/robot/pose",
                 "/api/mapeditor/status", "/api/mapeditor/io"):
        assert gone not in paths, "主后端不该还有编辑器路由：{}".format(gone)


def test_main_app_has_no_mapeditor_mount(app_paths):
    assert not any(p.startswith("/mapeditor") for p in app_paths(main_app))


def test_mapapi_router_carries_all_editor_routes(app_paths):
    paths = {r.path for r in mapapi.router.routes}
    for kept in ("/api/map/list", "/api/map/current", "/api/map/{name}/meta",
                 "/api/map/{name}/save", "/api/destinations", "/api/zones",
                 "/api/robot/pose", "/api/mapeditor/status",
                 "/api/map/sources", "/api/map/sources/{sid}/test"):
        assert kept in paths, "编辑器路由丢了：{}".format(kept)
    api_routes = [r.path for r in mapapi.router.routes
                  if getattr(r, "path", "").startswith("/api/")]
    assert len(api_routes) == 34, "编辑器路由应恰好 34 条，实得 {}".format(len(api_routes))

    # 这 5 条 /api/map/sources* 路由此前在全仓没有任何测试，至少把存在性锁住。
    # 注意 `/api/map/sources` 本身就是两条（GET 列表 + POST 新建），故按 route 条目计 5 条。
    sources = [r.path for r in mapapi.router.routes
               if getattr(r, "path", "").startswith("/api/map/sources")]
    assert len(sources) == 5, "/api/map/sources* 应恰好 5 条，实得 {}".format(sources)
    for kept in ("/api/map/sources", "/api/map/sources/{sid}",
                 "/api/map/sources/{sid}/default", "/api/map/sources/{sid}/test"):
        assert kept in sources, "地图来源路由丢了：{}".format(kept)


def test_main_app_keeps_wards_and_chat(app_paths):
    """病房自动切换与「记录当前房间为病房区域」留在主后端（B1 边界）。"""
    paths = app_paths(main_app)
    for kept in ("/api/chat", "/api/wards", "/api/wards/{ward_uid}/zone",
                 "/api/session/user", "/api/health"):
        assert kept in paths
