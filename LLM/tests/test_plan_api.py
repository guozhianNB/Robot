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


def test_navigation_plan_without_readable_map_is_created_as_waiting(client, monkeypatch):
    """回归：导航没跑时创建带 goto 步骤的 Plan 不再 422（设计 §4.2）。

    旧行为把「当前地图未知」当成请求错误返回 422，前端只看到 `API 422: /api/plans`
    而没有任何原因；现在该步骤进入 waiting，保留原动作等待调度器重解析。
    """
    from LLM.agent import plan as plan_ops

    class Unavailable:
        def resolve_place(self, _place):
            return {"ok": False, "status": "rejected", "error": "当前地图未知",
                    "hint": "请确认导航正在运行并重试"}

    monkeypatch.setattr(plan_ops, "_resolver_or_default", lambda _resolver: Unavailable())
    created = client.post("/api/plans", json={
        "title": "去护士站", "priority": "P1",
        "steps": [{"type": "action", "action": "robot_goto_place",
                   "args": {"place": "护士站"}, "label": "去护士站"}],
        "report": {"notify": True, "speak_if_present": False},
    })

    assert created.status_code == 200
    body = created.json()
    assert body["ok"] is True
    assert body["plan"]["status"] == "waiting"
    step = body["plan"]["steps"][0]
    assert step["status"] == "waiting"
    assert step["action"] == "robot_goto_place"
    assert step["target_json"] is None
    assert "当前地图未知" in step["last_error"]

    # 参数本身不合法仍然是请求错误，不许降级成 waiting。
    malformed = client.post("/api/plans", json={
        "title": "缺参数",
        "steps": [{"type": "action", "action": "robot_goto_place", "args": {}}],
    })
    assert malformed.status_code == 422
