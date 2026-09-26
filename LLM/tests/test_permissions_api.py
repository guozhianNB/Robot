# -*- coding: utf-8 -*-
"""权限矩阵路由测试（临时库 + TestClient，不进 lifespan）。"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

K = {"X-Surface": "kiosk"}
A = {"X-Surface": "admin"}


@pytest.fixture()
def c():
    from LLM.store import db
    from LLM.agent import session
    tmp = tempfile.mkdtemp()
    old = db.DB_PATH
    db.DB_PATH = os.path.join(tmp, "t.db")
    db.init_db()
    session.reset_for_test()
    db.set_admin_password("111111")
    import LLM.server as server
    server._login_fail.clear()
    client = TestClient(server.app)
    yield client
    server._login_fail.clear()
    db.DB_PATH = old


def _login(c, headers, password="111111"):
    return c.post("/api/session/login", json={"password": password}, headers=headers).json()


def test_matrix_requires_admin_to_see_all(c):
    assert _login(c, A)["ok"] is True
    full = c.get("/api/permissions/matrix", headers=A).json()
    assert full["ok"] is True and full["scope"] == "all"
    assert set(full["roles"]) == {"ward", "elder", "admin"}
    assert "grants" in full

    self_only = c.get("/api/permissions/matrix", headers=K).json()
    assert self_only["scope"] == "self" and self_only["roles"] == ["ward"]
    assert "grants" not in self_only
    # 只回自己那一列：别的角色不在字典里
    assert all(set(t["allowed"]) == {"ward"} for t in self_only["tools"])


def test_matrix_post_rejects_non_admin(c):
    r = c.post("/api/permissions/matrix",
               json={"changes": [{"role": "elder", "tool": "robot_move", "allowed": True}]},
               headers=K)
    assert r.status_code == 403
    from LLM.store import db
    assert db.get_role_grants() == {}


def test_matrix_post_applies_and_shows_as_overridden(c):
    assert _login(c, A)["ok"] is True
    r = c.post("/api/permissions/matrix",
               json={"changes": [{"role": "elder", "tool": "robot_move", "allowed": False}]},
               headers=A)
    assert r.status_code == 200 and r.json()["ok"] is True
    from LLM.store import db
    assert db.get_role_grants() == {("elder", "robot_move"): False}

    row = [t for t in c.get("/api/permissions/matrix", headers=A).json()["tools"]
           if t["name"] == "robot_move"][0]
    assert row["overridden"]["elder"] is True
    assert row["allowed"]["elder"] is False


def test_matrix_post_rejects_locked_but_keeps_others(c):
    """R3 红锁：急停那格被拒，同批次的其它格照常生效（逐格独立提交）。"""
    assert _login(c, A)["ok"] is True
    r = c.post("/api/permissions/matrix", headers=A, json={"changes": [
        {"role": "elder", "tool": "robot_stop", "allowed": False},
        {"role": "elder", "tool": "robot_move", "allowed": False}]})
    assert r.status_code == 403
    body = r.json()
    assert body["ok"] is False
    assert {(x["role"], x["tool"]): x["action"] for x in body["results"]} == {
        ("elder", "robot_stop"): "rejected", ("elder", "robot_move"): "set"}
    from LLM.store import db
    assert db.get_role_grants() == {("elder", "robot_move"): False}


def test_matrix_post_validates_payload(c):
    assert _login(c, A)["ok"] is True
    assert c.post("/api/permissions/matrix", json={"changes": []},
                  headers=A).status_code == 400
    assert c.post("/api/permissions/matrix",
                  json={"changes": [{"role": "x", "tool": "y", "allowed": True}]},
                  headers=A).status_code == 422


def test_permissions_reset(c):
    assert _login(c, A)["ok"] is True
    c.post("/api/permissions/matrix",
           json={"changes": [{"role": "elder", "tool": "robot_move", "allowed": False}]},
           headers=A)
    r = c.post("/api/permissions/reset", json={"role": "elder"}, headers=A)
    assert r.status_code == 200 and r.json() == {"ok": True, "removed": 1}
    from LLM.store import db
    assert db.get_role_grants() == {}
    assert c.post("/api/permissions/reset", json={}, headers=K).status_code == 403
