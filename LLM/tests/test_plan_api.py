# -*- coding: utf-8 -*-
"""Plan HTTP API and nurse page PIN contracts."""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from LLM.store import db
    os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")
    import LLM.server as server

    old = db.DB_PATH
    db.DB_PATH = os.path.join(tempfile.mkdtemp(), "plans.db")
    db.init_db()
    db.set_admin_password("2468")
    server._nurse_pin_fail.clear()
    yield TestClient(server.app)
    server._nurse_pin_fail.clear()
    db.DB_PATH = old


def _manual_plan(title="护士确认"):
    return {"title": title, "steps": [{"type": "manual", "label": "确认"}]}


def test_nurse_unlock_tracks_current_admin_password(client):
    from LLM.store import db

    assert client.post("/api/nurse/page-unlock", json={"pin": "2468"}).json()["ok"] is True
    db.set_admin_password("1357")
    assert client.post("/api/nurse/page-unlock", json={"pin": "2468"}).json()["ok"] is False
    assert client.post("/api/nurse/page-unlock", json={"pin": "1357"}).json()["ok"] is True


def test_nurse_unlock_has_independent_three_failure_cooldown(client):
    import LLM.server as server

    for _ in range(3):
        assert client.post("/api/nurse/page-unlock", json={"pin": "bad"}).json()["ok"] is False
    # Correct credentials are blocked during the page-only cooldown.
    assert client.post("/api/nurse/page-unlock", json={"pin": "2468"}).json()["ok"] is False
    assert server._login_fail == {}


def test_nurse_pin_cooldown_expiry_resets_consecutive_failures(client, monkeypatch):
    import LLM.server as server

    clock = iter([100.0, 100.0, 100.0, 111.0])
    monkeypatch.setattr(server.time, "monotonic", lambda: next(clock))
    for _ in range(3):
        server._verify_nurse_pin("bad")
    result = server._verify_nurse_pin("bad")
    assert result["ok"] is False
    assert server._nurse_pin_fail == {"n": 1, "until": 0.0}


def test_nurse_pin_verification_is_serialized(client, monkeypatch):
    import concurrent.futures
    import time
    import LLM.server as server

    active = 0
    peak = 0

    def verify(_pin):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        time.sleep(0.02)
        active -= 1
        return False

    monkeypatch.setattr(server.db, "verify_admin_password", verify)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(server._verify_nurse_pin, ["bad"] * 4))
    assert peak == 1


def test_plan_api_is_public_and_strict(client):
    created = client.post("/api/plans", json=_manual_plan()).json()
    assert created["ok"] is True
    plan_id = created["plan"]["id"]
    assert client.get(f"/api/plans/{plan_id}").status_code == 200
    assert client.get("/api/plans").json()["ok"] is True
    denied = client.post("/api/plans", json={**_manual_plan(), "creator": "admin"})
    assert denied.status_code == 422


def test_plan_api_conflicts_include_latest_and_missing_is_404(client):
    created = client.post("/api/plans", json=_manual_plan()).json()["plan"]
    plan_id, version = created["id"], created["version"]
    changed = client.post(f"/api/plans/{plan_id}/priority",
                          json={"version": version, "priority": "P1"})
    assert changed.status_code == 200
    stale = client.post(f"/api/plans/{plan_id}/priority",
                         json={"version": version, "priority": "P2"})
    assert stale.status_code == 409
    assert stale.json()["plan"]["version"] == version + 1
    bad = client.post(f"/api/plans/{plan_id}/confirm",
                      json={"version": version + 1, "step_id": 999, "decision": "complete"})
    assert bad.status_code == 409
    assert "plan" in bad.json()
    assert client.get("/api/plans/999999").status_code == 404


def test_plan_requests_cannot_inject_execution_fields(client):
    for field, value in (("status", "succeeded"), ("completion", {"ok": True}),
                         ("creator_uid", "admin")):
        response = client.post("/api/plans", json={**_manual_plan(), field: value})
        assert response.status_code == 422
