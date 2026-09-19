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
    from LLM.store import db
    from LLM.agent import session
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


def test_delete_ward_detaches_elders_and_clears_current_session(c):
    """删病房只删档案/关联，不留下老人归属与当前会话悬空引用。"""
    from LLM.store import db

    c.post("/api/session/login", json={"password": "111111"}, headers=A)
    assert c.post("/api/profiles/elder_101_1/ward",
                  json={"ward_id": "ward_101"}, headers=A).json()["ok"] is True
    assert c.post("/api/session/ward", json={"ward_uid": "ward_101"}, headers=K).status_code == 200

    r = c.delete("/api/wards/ward_101", headers=A)

    assert r.status_code == 200 and r.json() == {"ok": True, "uid": "ward_101"}
    assert db.get_profile("ward_101") is None
    assert db.get_profile("elder_101_1")["ward_id"] == ""
    assert c.get("/api/session/user", headers=K).json()["ward_uid"] == ""
    assert c.delete("/api/wards/ward_101", headers=A).json()["ok"] is False


def test_non_admin_cannot_delete_ward(c):
    assert c.delete("/api/wards/ward_101", headers=K).status_code == 403
    assert c.get("/api/wards", headers=K).json()["wards"][0]["uid"] == "ward_101"


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


def test_factory_password_restore_requires_admin_and_logs_out(c, monkeypatch):
    """恢复接口只允许管理员；成功后当前槽立即降权并可用出厂口令重新登录。"""
    from LLM import conf
    monkeypatch.setattr(conf, "FACTORY_PASSWORD", "factory-123")
    assert c.post("/api/session/password/restore-factory", headers=K).status_code == 403

    c.post("/api/session/login", json={"password": "111111"}, headers=A)
    r = c.post("/api/session/password/restore-factory", headers=A)

    assert r.status_code == 200 and r.json() == {"ok": True}
    assert c.get("/api/session/user", headers=A).json()["role"] == "ward"
    assert c.post("/api/session/login", json={"password": "factory-123"}, headers=A).json()["ok"] is True


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
    from LLM.agent import chat
    from LLM import server
    from LLM.voice import voice_api
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


def test_non_admin_cannot_disable_password_gate(c):
    """**回归（关键）**：未认证/非管理员不许经 /api/settings 关掉口令门。

    `admin_auth_required` 是 `DEFAULT_SETTINGS` 白名单键，`db.set_settings` 照单全收 ——
    没有这道守卫时，一个未认证的 POST 就能关掉口令门，随后 `POST /api/session/login {}`
    拿到 `source=auth_disabled` 的 admin，整套角色保护归零。
    """
    r = c.post("/api/settings", json={"admin_auth_required": False}, headers=K)
    assert r.status_code == 403
    assert c.get("/api/session/admin-auth", headers=K).json()["required"] is True
    assert c.get("/api/settings").json()["settings"]["admin_auth_required"] is True
    # 未认证的 kiosk 槽也不能把自己提权（login 仍要口令）
    assert c.post("/api/session/login", json={}, headers=K).json()["ok"] is False
    assert c.get("/api/session/user", headers=K).json()["role"] == "ward"
    # 管理员可以改
    c.post("/api/session/login", json={"password": "111111"}, headers=A)
    assert c.post("/api/settings", json={"admin_auth_required": False},
                  headers=A).status_code == 200


def test_privileged_settings_are_all_guarded(c):
    """**回归（关键）**：特权键不止口令门一个 —— TTL 与病房自动切换同样只给管理员。"""
    for key, val in (("admin_session_ttl_s", 99999), ("ward_autoswitch_enabled", False),
                     ("mcp_enabled", True), ("admin_auth_required", False)):
        assert c.post("/api/settings", json={"settings": {key: val}},
                      headers=K).status_code == 403
    s = c.get("/api/settings").json()["settings"]
    assert s["admin_session_ttl_s"] == 300 and s["ward_autoswitch_enabled"] is True


def test_kiosk_can_still_write_engine_switches(c):
    """**回归（关键 1 的反面）**：非特权键保持现状 —— kiosk 必须还能切识别/合成引擎。"""
    r = c.post("/api/settings", json={"settings": {"asr_provider": "local"}}, headers=K)
    assert r.status_code == 200 and r.json()["settings"]["asr_provider"] == "local"
    r = c.post("/api/settings", json={"tts_provider": "local"}, headers=K)   # 裸体（无 settings 包）
    assert r.status_code == 200 and r.json()["settings"]["tts_provider"] == "local"


def test_ward_turn_does_not_touch_core_memory(c, monkeypatch):
    """**回归（关键）**：集体层话语不许纠错/改写老人的核心记忆。"""
    from LLM import server
    calls = []
    monkeypatch.setattr(server.rag, "note_turn", lambda *a, **k: calls.append("note_turn"))
    monkeypatch.setattr(server.rag, "correct_instant", lambda *a, **k: calls.append("correct"))
    monkeypatch.setattr(server.rag, "consolidate", lambda *a, **k: None)
    monkeypatch.setattr(server.chat, "summarize_old", lambda *a, **k: None)
    # 病房里的"不对/错了"这类话：既不沉淀、也不纠错、也不写摘要
    server._post_chat_jobs("elder_101_1", "不对，我不住101", "好的", "ward")
    assert calls == []
    # 对照组：老人本人的轮次仍然走完整管线（否则"整条早退"会被误判成通过）
    server._post_chat_jobs("elder_101_1", "我今天有点闷", "我陪您说说话", "elder")
    assert calls == ["note_turn", "correct"]


def test_chat_history_requires_matching_principal(c):
    """**回归**：非管理员不许用别人的 uid 读/删历史（CORS 全开的隐私口子）。"""
    c.post("/api/session/login", json={"password": "111111"}, headers=A)
    c.post("/api/session/user", json={"uid": "elder_101_1", "locked": True}, headers=K)
    assert c.get("/api/chat/history?uid=elder_999", headers=K).status_code == 400
    assert c.delete("/api/chat/history?uid=elder_999", headers=K).status_code == 400
    assert c.get("/api/chat/history", headers=K).status_code == 200   # 不带 uid → 读自己
    c.post("/api/session/login", json={"password": "111111"}, headers=A)
    assert c.get("/api/chat/history?uid=elder_999", headers=A).status_code == 200  # admin 可查


def test_chat_history_without_principal_does_not_fall_back_to_an_elder(c):
    """未选主体时返回空结果，不能沿用旧默认值去读删 elder_001。"""
    from LLM.store import db
    db.append_history("elder_001", "user", "private")

    assert c.get("/api/chat/history", headers=K).json()["history"] == []
    assert c.delete("/api/chat/history", headers=K).json()["cleared"] == 0
    assert db.history_count("elder_001") == 1


def test_session_ward_manual_switch(c):
    """**D18 的 HTTP 入口**：手动切病房只切集体层背景变量（不锁主体），并带覆盖窗口。

    `manual_set_ward` 内部已 publish `ward_changed`（action=manual），路由层不许再发一次。
    """
    r = c.post("/api/session/ward", json={"ward_uid": "ward_101"}, headers=K)
    assert r.status_code == 200
    body = c.get("/api/session/user", headers=K).json()
    assert body["ward_uid"] == "ward_101"
    assert body["locked"] is False           # 与"锁定主体"不同：手动切病房不锁
    assert c.post("/api/session/ward", json={"ward_uid": "ward_101"},
                  headers={"X-Surface": "bogus"}).status_code == 400


def test_session_ward_rejects_non_ward_uid(c):
    """ward_uid 必须指向真实病房，不能把老人或不存在主体写进病房上下文。"""
    assert c.post("/api/session/ward", json={"ward_uid": "elder_101_1"}, headers=K).status_code == 400
    assert c.post("/api/session/ward", json={"ward_uid": "ward_missing"}, headers=K).status_code == 400
    assert c.get("/api/session/user", headers=K).json()["ward_uid"] == ""


def test_ward_assign_unknown_uid_is_not_ok(c):
    """**回归**：uid 不存在时 0 行更新 → 不许报 ok（原先静默 no-op 却返回 ok:True）。"""
    c.post("/api/session/login", json={"password": "111111"}, headers=A)
    r = c.post("/api/profiles/elder_999/ward", json={"ward_id": "ward_101"}, headers=A)
    assert r.status_code == 200              # 与同族其它业务失败分支一致：HTTP 200 + ok:false
    assert r.json()["ok"] is False and "elder_999" in r.json()["error"]
    assert c.post("/api/profiles/elder_101_1/ward",
                  json={"ward_id": "ward_101"}, headers=A).json()["ok"] is True


def test_record_zone_checks_ward_before_writing_map(c, monkeypatch):
    """未知病房必须在地图写入前失败，避免产生无人引用的 tags 区域。"""
    from LLM.maps import locator, maptags
    from LLM.agent import session
    writes = []
    c.post("/api/session/login", json={"password": "111111"}, headers=A)
    monkeypatch.setattr(locator, "get_pose", lambda: {"x": 1.0, "y": 2.0})
    monkeypatch.setattr(session, "running_map_name", lambda: ("demo", "setting"))
    monkeypatch.setattr(maptags, "record_room_polygon",
                        lambda *args, **kwargs: writes.append((args, kwargs)) or {"uid": "z1"})

    r = c.post("/api/wards/ward_missing/zone", headers=A)

    assert r.status_code == 200 and r.json()["ok"] is False
    assert writes == []
