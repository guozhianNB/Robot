# -*- coding: utf-8 -*-
"""权限矩阵测试（临时库隔离，直接改 db.DB_PATH，不 reload）。

规格：docs/superpowers/specs/2026-09-26-permission-matrix-design.md
"""
import os
import tempfile

import pytest


@pytest.fixture()
def d():
    from LLM.store import db
    tmp = tempfile.mkdtemp()
    old = db.DB_PATH
    db.DB_PATH = os.path.join(tmp, "t.db")
    db.init_db()
    yield db
    db.DB_PATH = old


def test_grants_start_empty(d):
    assert d.get_role_grants() == {}
    assert d.list_role_grants() == []


def test_set_grant_is_idempotent_and_readable(d):
    d.set_role_grant("elder", "tavily-search", True, by="admin")
    d.set_role_grant("elder", "tavily-search", True, by="admin")
    assert d.get_role_grants() == {("elder", "tavily-search"): True}
    assert len(d.list_role_grants()) == 1


def test_set_grant_overwrites_and_delete_removes(d):
    d.set_role_grant("admin", "robot_move", False, by="admin")
    assert d.get_role_grants()[("admin", "robot_move")] is False
    d.set_role_grant("admin", "robot_move", True, by="admin")
    assert d.get_role_grants()[("admin", "robot_move")] is True
    assert d.delete_role_grant("admin", "robot_move") == 1
    assert d.get_role_grants() == {}
    assert d.delete_role_grant("admin", "robot_move") == 0      # 已无行 → 0
