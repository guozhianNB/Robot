# -*- coding: utf-8 -*-
"""会话状态端点（/api/session/user）与紧急呼叫端点（/api/alarm）测试。

用 TestClient 测真实路由；voice worker 不启动（get_status 返回 stopped），
会话状态独立于语音可用性 —— 语音不可用时手动切换用户仍须可用（规格 §8）。
"""
import pytest
from fastapi.testclient import TestClient

from LLM import server, log as audit, voice_api


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """重置会话全局状态 + 隔离审计日志（防测试间污染真实 audit.jsonl）。"""
    voice_api._session_uid = None
    voice_api._session_locked = False
    monkeypatch.setattr(audit, "AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    yield


@pytest.fixture()
def client():
    # 不触发 lifespan（不起 reminder/voice 线程），只测路由层
    return TestClient(server.app)


def test_session_user_default(client):
    r = client.get("/api/session/user")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["uid"] is None
    assert body["locked"] is False


def test_session_user_set_and_get(client):
    r = client.post("/api/session/user", json={"uid": "elder_002", "locked": True})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    got = client.get("/api/session/user").json()
    assert got["uid"] == "elder_002"
    assert got["locked"] is True
    assert got["source"] == "manual"


def test_session_user_unlock(client):
    client.post("/api/session/user", json={"uid": "elder_002", "locked": True})
    r = client.post("/api/session/user", json={"uid": "elder_002", "locked": False})
    assert r.status_code == 200
    got = client.get("/api/session/user").json()
    assert got["locked"] is False


def test_alarm_reports_ok(client):
    r = client.post("/api/alarm", json={"type": "sos", "uid": "elder_001",
                                        "message": "按了紧急呼叫"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
