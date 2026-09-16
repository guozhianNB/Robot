# -*- coding: utf-8 -*-
"""测试隔离：把 db 的 SQLite 数据文件指向临时目录，避免污染真实 brain.db。

graph.py / ragstore.py 的路径隔离由各自测试文件内的 fixture 处理
（monkeypatch 其模块级 _DB_PATH / _PATH 后调用 _init()）。
"""
import pytest


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    from LLM import db
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    yield tmp_path


# 与 LLM/mapctl.py::stop() 的"没在跑"返回值同形（/api/mapeditor/service 的状态载荷）。
_IDLE_MAPCTL_STOP = {"ok": True, "running": False, "source": "none", "pid": None,
                     "port": 8010, "uptime_s": None}


@pytest.fixture(autouse=True)
def no_real_mapeditor_stop(monkeypatch):
    r"""红线护栏：进入主 app lifespan 的用例，退出收尾绝不许碰真实世界（2026-09-16）。

    主后端 `LLM/server.py::lifespan` 的 `yield` 之后第一行是 `mapctl.stop()`（带走本进程
    拉起的编辑器独立服务，防孤儿）。测试进程里 `mapctl._proc is None`，这一行不桩就落到
    **external 分支**：用**未打桩的** `port_alive()` 探 127.0.0.1:8010，本机真有编辑器在
    跑（开发时的常态）就真的 `POST /api/mapeditor/service/stop` —— 那台服务真的退出，
    浏览器里未保存的像素修图随之丢失；而且该用例还要白等最多 5 秒。

    `LLM/mapeditor_server.py::_delayed_exit` 的 `"pytest" in sys.modules` 护栏**救不了**
    这个场景：被打到的是主后端拉起的**另一个进程**，它的 `sys.modules` 里没有 pytest。

    放在本目录的 autouse fixture 里（而不是逐个用例桩）是为了**兜住以后新增的用例**：
    谁再写 `with TestClient(server.app)`，收尾也不会打到真实服务。
    `tests/test_modules_status.py::test_lifespan_shutdown_does_not_hit_real_editor_service`
    是这条护栏的回归测试（摘掉本 fixture 它就红）。

    只作用于 `tests/`：`LLM/tests/test_mapctl.py`（另一目录、自带 conftest）测的正是
    `stop()` 的真实分支（managed / external），不受本护栏影响，也不该受影响。
    """
    from LLM import mapctl
    monkeypatch.setattr(mapctl, "stop", lambda: dict(_IDLE_MAPCTL_STOP))
