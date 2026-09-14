# -*- coding: utf-8 -*-
"""会话/病房/策略路由测试（临时库隔离，直接改 db.DB_PATH，**不 reload**）。"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

K = {"X-Surface": "kiosk"}
A = {"X-Surface": "admin"}


@pytest.fixture()
def c():
    from LLM import db
    from LLM import session
    tmp = tempfile.mkdtemp()
    old = db.DB_PATH
    db.DB_PATH = os.path.join(tmp, "t.db")
    db.init_db()
    session.reset_for_test()
    db.upsert_ward("ward_101", name="101 病房")
    db.upsert_profile("elder_101_1", name="李爷爷")
    db.set_admin_password("111111")
    import LLM.server as server
    server._login_fail.clear()      # 登录冷却计数是模块级状态，逐个用例清零，避免顺序依赖
    client = TestClient(server.app)        # 不进 lifespan（不启动语音/轮询/rosbridge）
    yield client
    server._login_fail.clear()
    db.DB_PATH = old


def test_session_user_rejects_role_field(c):
    r = c.post("/api/session/user", json={"uid": "ward_101", "locked": False, "role": "admin"},
               headers=K)
    assert r.status_code == 400            # R1：前端不许指定角色


def test_login_and_logout_flow_two_surfaces(c):
    assert c.post("/api/session/login", json={"password": "bad"}, headers=A).json()["ok"] is False
    ok = c.post("/api/session/login", json={"password": "111111"}, headers=A).json()
    assert ok["ok"] is True and ok["role"] == "admin"
    assert c.get("/api/session/user", headers=A).json()["role"] == "admin"
    assert c.get("/api/session/user", headers=K).json()["role"] == "ward"   # 双槽隔离
    c.post("/api/session/logout", headers=A)
    assert c.get("/api/session/user", headers=A).json()["role"] == "ward"


def test_session_user_get_has_role_fields(c):
    body = c.get("/api/session/user", headers=K).json()
    for key in ("uid", "role", "locked", "source", "slot", "ward_uid",
                "ttl_remain", "auth_required", "autoswitch"):
        assert key in body


def test_session_user_get_keeps_legacy_shape(c):
    """老接口形状不许少：`ok` 必在、无主体时 `uid` 为 None。

    `frontend/packages/shared/src/api/session.ts`（`ok: boolean`、`uid: string | null`）与
    `tests/test_session_api.py` 都吃这个形状 —— 车前屏靠 `uid === null` 判断"没选人"。
    """
    body = c.get("/api/session/user", headers=K).json()
    assert body["ok"] is True
    assert body["uid"] is None
    assert body["locked"] is False
    # 选定主体后 uid 是真主体（不是 None）
    assert c.post("/api/session/user", json={"uid": "ward_101", "locked": True},
                  headers=K).json()["ok"] is True
    assert c.get("/api/session/user", headers=K).json()["uid"] == "ward_101"


def test_login_lockout_after_three_failures(c):
    for _ in range(3):
        c.post("/api/session/login", json={"password": "bad"}, headers=K)
    r = c.post("/api/session/login", json={"password": "111111"}, headers=K).json()
    assert r["ok"] is False and "10 秒" in r["error"]      # 冷却期内正确口令也先挡住
    assert c.get("/api/session/user", headers=K).json()["role"] == "ward"


def test_wards_crud_and_admin_auth_toggle(c):
    c.post("/api/session/login", json={"password": "111111"}, headers=A)
    body = c.get("/api/wards", headers=A).json()
    assert body["wards"][0]["uid"] == "ward_101"
    r = c.post("/api/wards", json={"uid": "ward_102", "name": "102 病房"}, headers=A)
    assert r.json()["ok"] is True
    r = c.post("/api/session/admin-auth", json={"required": False}, headers=A)
    assert r.json()["required"] is False


def test_non_admin_cannot_manage_wards(c):
    assert c.post("/api/wards", json={"uid": "ward_999", "name": "越权"}, headers=K).status_code == 403
    assert c.post("/api/session/admin-auth", json={"required": False}, headers=K).status_code == 403
    assert c.post("/api/wards/ward_101/zone", headers=K).status_code == 403


def test_password_change_flow(c):
    c.post("/api/session/login", json={"password": "111111"}, headers=A)
    assert c.post("/api/session/password",
                  json={"old": "000000", "new": "654321"}, headers=A).json()["ok"] is False
    assert c.post("/api/session/password",
                  json={"old": "111111", "new": "654321"}, headers=A).json()["ok"] is True
    c.post("/api/session/logout", headers=A)
    assert c.post("/api/session/login", json={"password": "654321"}, headers=A).json()["ok"] is True


def test_policy_roles_visibility(c):
    """策略矩阵是**按角色**下发（规格 §7：`admin` 可见全量，其它角色只拿自己那份摘要）。

    ⚠️ 计划测试修正（1 行）：原测试在 admin 槽**未登录**时断言拿到全量 —— 与计划自己的实现
    `get_principal(_surface(...))["role"]` 互相矛盾（口令门默认开，未登录的槽位角色是派生出来的
    `ward`），只有把 `X-Surface: admin` 当成免登录特权才能通过，那正好是 R1 要堵的洞。
    故按规格补一次登录。
    """
    c.post("/api/session/login", json={"password": "111111"}, headers=A)
    assert set(c.get("/api/policy/roles", headers=A).json()) == {"admin", "ward", "elder"}
    kiosk = c.get("/api/policy/roles", headers=K).json()
    assert set(kiosk) == {"ward"}          # 非管理员只拿到自己那份摘要


def test_profiles_endpoint_lists_elders_only(c):
    """**回归（历史隐患②）**：病房用户不许混进 `GET /api/profiles` 的老人列表。"""
    c.post("/api/session/login", json={"password": "111111"}, headers=A)
    c.post("/api/wards", json={"uid": "ward_102", "name": "102 病房"}, headers=A)
    profiles = c.get("/api/profiles", headers=A).json()["profiles"]
    uids = [p["uid"] for p in profiles]
    assert "elder_101_1" in uids and "ward_101" not in uids and "ward_102" not in uids
    assert [p["uid"] for p in c.get("/api/profiles?kind=ward", headers=A).json()["profiles"]] \
        == ["ward_101", "ward_102"]


def test_profile_ward_assignment(c):
    c.post("/api/session/login", json={"password": "111111"}, headers=A)
    r = c.post("/api/profiles/elder_101_1/ward", json={"ward_id": "ward_101"}, headers=A)
    assert r.json()["ok"] is True
    wards = c.get("/api/wards", headers=A).json()["wards"]
    assert wards[0]["elders"] == ["elder_101_1"]
    assert c.post("/api/profiles/elder_101_1/ward", json={"ward_id": ""}, headers=K).status_code == 403


def test_unknown_surface_is_rejected(c):
    """`X-Surface` 非法 → **显式 400**，不许静默回落 kiosk。

    静默回落会让"管理台的口令登录"落到车前屏上（顺带把车前屏提权），而且审计里的
    slot 名还是那个错名 —— 追溯性一并破坏。
    """
    r = c.get("/api/session/user", headers={"X-Surface": "bogus"})
    assert r.status_code == 400
    assert "X-Surface" in r.json()["detail"]
    assert c.post("/api/session/login", json={"password": "111111"},
                  headers={"X-Surface": "bogus"}).status_code == 400


def test_session_user_set_rejects_admin_uid(c):
    """`uid="admin"` 只能在登录口拿（R1）：边界直接 400。

    不能靠 `session.set_subject` 的拒绝分支 —— 它返回的**是当前主体**（与成功同形），
    前端会误以为"切到管理员成功了"。
    """
    r = c.post("/api/session/user", json={"uid": "admin"}, headers=K)
    assert r.status_code == 400
    assert "login" in r.json()["detail"]
    assert c.get("/api/session/user", headers=K).json()["role"] == "ward"


def test_chat_takes_principal_from_surface(c, monkeypatch):
    """`/api/chat` 必须按请求槽位取 principal（端到端闸门 2），并把它交给对话流。

    管理员说的话是**管理层**的话（不沉淀成老人记忆），不能因为浏览器走的是 kiosk 槽就降级成
    集体层；反之车前屏也不该因为管理台登录了而提权。
    """
    from LLM import chat, server, voice_api
    seen = {}

    def stream(*args, **kwargs):
        seen.update(kwargs)
        yield {"type": "done", "assistant": "好"}

    monkeypatch.setattr(chat, "chat_stream", stream)
    monkeypatch.setattr(voice_api, "begin_text_reply", lambda: None)
    monkeypatch.setattr(voice_api, "feed_text_reply", lambda h, d: None)
    monkeypatch.setattr(voice_api, "end_text_reply", lambda h, flush_tail=True: None)
    monkeypatch.setattr(server._bg, "submit", lambda *a, **kw: None)

    c.post("/api/session/login", json={"password": "111111"}, headers=A)
    assert c.post("/api/chat", json={"uid": "elder_101_1", "message": "你好"},
                  headers=A).status_code == 200
    assert seen["principal"]["role"] == "admin"
    assert c.post("/api/chat", json={"uid": "elder_101_1", "message": "你好"},
                  headers=K).status_code == 200
    assert seen["principal"]["role"] == "ward"      # 双槽隔离：管理台登录不提权车前屏
    assert c.post("/api/chat", json={"uid": "elder_101_1", "message": "你好"},
                  headers={"X-Surface": "bogus"}).status_code == 400
