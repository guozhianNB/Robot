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
