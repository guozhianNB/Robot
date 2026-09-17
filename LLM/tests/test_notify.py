# -*- coding: utf-8 -*-
r"""通知中心（模块 11）测试：notify 模块 + 5 条路由 + 广播形态。

库隔离照抄 `tests/test_server_roles_routes.py`（临时库 + 直接改 db.DB_PATH，**不 reload**）。
"""
from fastapi.testclient import TestClient
import os
import sqlite3
import tempfile
import time

import pytest

from LLM.agent import notify
from LLM.conf import BASE_DIR
from LLM.store import db

K = {"X-Surface": "kiosk"}
A = {"X-Surface": "admin"}

_TMP_DB = ""          # 本次用例的隔离库路径（供 fixture 清理）


def _init_tmp_db() -> str:
    """隔离库（照抄 `test_server_roles_routes.py`：临时目录 + 直接改 db.DB_PATH，**不 reload**）。

    临时目录优先用系统 `tempfile.mkdtemp()`；**打不开就退回仓库内** `LLM/data/_t1_notify_tmp/`
    —— 某些沙箱下 spawn 出的进程对 `%TEMP%` 下新建目录不可读写，会让每个用例直接
    `sqlite3.OperationalError: unable to open database file`，退路保证用例在受限环境也能跑。
    实际路径记进 `_TMP_DB`，供 fixture 跑完清理。
    """
    global _TMP_DB
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "t.db")
    try:
        sqlite3.connect(path).close()
    except sqlite3.OperationalError:
        tmp = str(BASE_DIR / "LLM" / "data" / "_t1_notify_tmp")
        os.makedirs(tmp, exist_ok=True)
        path = os.path.join(tmp, f"t{os.getpid()}_{time.time_ns()}.db")
    _TMP_DB = path
    old = db.DB_PATH
    db.DB_PATH = path
    db.init_db()
    return old


def _cleanup_tmp_db() -> None:
    """删掉本次用例的库文件（.db / .db-wal / .db-shm）。"""
    if not _TMP_DB:
        return
    root, base = os.path.split(_TMP_DB)
    for f in os.listdir(root):
        if f.startswith(base):
            try:
                os.remove(os.path.join(root, f))
            except OSError:
                pass


@pytest.fixture()
def c():
    """隔离库 + 已设出厂口令的 TestClient（不进 lifespan，避免语音/轮询副作用）。"""
    from LLM.agent import session
    old = _init_tmp_db()
    session.reset_for_test()
    db.set_admin_password("111111")
    import LLM.server as server
    server._login_fail.clear()
    client = TestClient(server.app)
    yield client
    server._login_fail.clear()
    db.DB_PATH = old
    _cleanup_tmp_db()          # 退路目录在仓库里：每个用例跑完清掉自己的库文件


def _client(c):
    """未登录的 admin 槽：口令门默认开 → 角色是派生的 ward。"""
    assert c.get("/api/session/user", headers=A).json()["role"] == "ward"
# ------------------------------------------------------------------ 1 最小体
def test_ingest_minimal_body_falls_back_to_defaults():
    old = _init_tmp_db()
    try:
        r = notify.ingest("", "task_done")          # 只给 type；source 空
        assert r["ok"] is True and r["deduped"] is False

        row = db.get_notification(r["id"])
        assert row["level"] == "info"               # task_done 不在 DEFAULT_LEVEL → info
        assert row["title"] == "任务完成"
        assert row["source"] == "system"            # source 空 → system
        assert row["count"] == 1
        assert row["ack_at"] == "" and row["ack_by"] == ""
    finally:
        _cleanup_tmp_db()
        db.DB_PATH = old


# ------------------------------------------------------------------ 2 兜底与红线
def test_ingest_level_fallback_and_empty_type_raises():
    old = _init_tmp_db()
    try:
        r = notify.ingest("car", "sos", level="bogus")     # 非法 level → 按类型兜底
        assert r["level"] == "critical"
        assert db.get_notification(r["id"])["level"] == "critical"

        r2 = notify.ingest("car", "offline", level="")     # 空 level 同样兜底
        assert r2["level"] == "warning"

        for bad in ("", "   "):
            try:
                notify.ingest("car", bad)
            except ValueError as e:
                assert "type" in str(e)
            else:
                raise AssertionError(f"type={bad!r} 必须抛 ValueError")
    finally:
        _cleanup_tmp_db()
        db.DB_PATH = old


# ------------------------------------------------------------------ 3 去重合并
def test_ingest_dedups_within_window_and_keeps_earliest_body():
    old = _init_tmp_db()
    try:
        a = notify.ingest("car", "offline", uid="elder_1", body="第一次失联")
        b = notify.ingest("car", "offline", uid="elder_1", body="第二次失联", title="改写标题")

        assert b["deduped"] is True and b["id"] == a["id"]
        rows = db.list_notifications()
        assert len(rows) == 1                        # 仍是 1 行
        assert rows[0]["count"] == 2
        assert rows[0]["body"] == "第一次失联"        # 保留最早原文
        assert rows[0]["title"] == "小车失联"         # title 也不被后来者改写
    finally:
        _cleanup_tmp_db()
        db.DB_PATH = old


# ------------------------------------------------------------------ 4 已 ack 的不许并入
def test_acked_notification_is_not_merged_again():
    old = _init_tmp_db()
    try:
        a = notify.ingest("car", "offline", uid="elder_1", body="失联")
        assert notify.ack(a["id"]) is True

        b = notify.ingest("car", "offline", uid="elder_1", body="又失联")

        assert b["deduped"] is False and b["id"] != a["id"]
        assert len(db.list_notifications(state="all")) == 2
        assert len(db.list_notifications(state="unread")) == 1
        assert db.get_notification(a["id"])["count"] == 1     # 旧行不再被 bump
    finally:
        _cleanup_tmp_db()
        db.DB_PATH = old


# ------------------------------------------------------------------ 5 列表与计数
def test_unread_filter_and_counts():
    old = _init_tmp_db()
    try:
        p = notify.ingest("car", "task_done")                 # info
        s = notify.ingest("car", "sos")                       # critical
        notify.ack(p["id"])

        assert [n["id"] for n in notify.list_notices(state="unread")] == [s["id"]]
        assert len(notify.list_notices(state="all")) == 2

        cnt = notify.counts()
        assert cnt == {"unread": 1, "critical": 1}
        notify.ack(s["id"])
        assert notify.counts() == {"unread": 0, "critical": 0}
    finally:
        _cleanup_tmp_db()
        db.DB_PATH = old


# ------------------------------------------------------------------ 6 ack / ack_all
def test_ack_and_ack_all():
    old = _init_tmp_db()
    try:
        a = notify.ingest("car", "offline", uid="elder_1", body="失联")

        assert notify.ack(a["id"], by="nurse") is True
        row = db.get_notification(a["id"])
        assert row["ack_at"] != "" and row["ack_by"] == "nurse"
        assert notify.ack(a["id"]) is False                   # 已确认 → 0 行 → False
        assert notify.ack(999999) is False                    # 不存在 → False

        for t in ("fall", "help", "health"):
            notify.ingest("car", t)
        n = notify.ack_all(by="nurse")
        assert n == 3
        assert notify.counts() == {"unread": 0, "critical": 0}
        assert notify.ack_all() == 0                          # 幂等
    finally:
        _cleanup_tmp_db()
        db.DB_PATH = old


# ------------------------------------------------------------------ 7 prune 只删已处理的
def test_prune_keeps_unacked_and_recent_rows():
    old = _init_tmp_db()
    try:
        old_ts = "2020-01-01 00:00:00"
        i_unacked_old = db.add_notification("info", "car", "offline", title="老未处理",
                                            body="", ts=old_ts)
        i_acked_old = db.add_notification("info", "car", "offline", title="老已处理",
                                          body="", ts=old_ts)
        i_acked_new = notify.ingest("car", "task_done")["id"]
        db.ack_notification(i_acked_old)
        db.ack_notification(i_acked_new)

        assert notify.prune(days=1) == 1                      # 只有"已处理 + last_at 过期"该删
        assert db.get_notification(i_acked_old) is None
        assert db.get_notification(i_unacked_old) is not None  # 未处理的老行必须留着
        assert db.get_notification(i_acked_new) is not None    # 刚处理的不删
    finally:
        _cleanup_tmp_db()
        db.DB_PATH = old


# ------------------------------------------------------------------ 8 投递免鉴权
def test_post_notifications_is_anonymous_but_type_is_required(c):
    # 缺 type → 400（契约：类型问题一律 400，不让 Pydantic 抛 422 给机器投递方）
    r = c.post("/api/notifications", json={"source": "car", "message": "x"}, headers=K)
    assert r.status_code == 400
    r = c.post("/api/notifications", json={"source": "car", "type": "  "}, headers=K)
    assert r.status_code == 400
    # **有意放行**：投递口不带任何会话也必须成功 —— 告警源（小车/巡检/语音）没有口令，
    # 需求文档模块 11 明确"任何模块发现异常都往该端口 POST"；读/确认/删除才是管理员专属。
    r = c.post("/api/notifications", json={"type": "offline", "source": "car"}, headers=K)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert db.list_notifications()[0]["source"] == "car"


# ------------------------------------------------------------------ 9 管理口只给 admin
def test_notification_admin_routes_reject_non_admin(c):
    nid = notify.ingest("car", "offline")["id"]

    assert c.get("/api/notifications", headers=K).status_code == 403
    assert c.post(f"/api/notifications/{nid}/ack", headers=K).status_code == 403
    assert c.post("/api/notifications/ack-all", headers=K).status_code == 403
    assert c.delete(f"/api/notifications/{nid}", headers=K).status_code == 403

    assert db.get_notification(nid) is not None
    assert db.get_notification(nid)["ack_at"] == ""
    assert c.get("/api/notifications", headers=K).json()["detail"] == "仅管理员可管理通知"


# ------------------------------------------------------------------ 9b admin 全链路
def test_notification_routes_admin_happy_path(c):
    from LLM.store import db as _db
    _db.upsert_profile("elder_1", name="李爷爷", bed="101")
    nid = notify.ingest("car", "offline", uid="elder_1", body="失联 3 分钟")["id"]

    c.post("/api/session/login", json={"password": "111111"}, headers=A)
    body = c.get("/api/notifications?state=unread", headers=A).json()
    assert body["ok"] is True and body["counts"]["unread"] == 1
    item = body["items"][0]
    assert item["id"] == nid
    assert item["type"] == "offline" and item["uid_name"] == "李爷爷 · 101"

    assert c.post(f"/api/notifications/{nid}/ack", json={"by": "nurse"},
                  headers=A).json() == {"ok": True, "id": nid}
    assert _db.get_notification(nid)["ack_by"] == "nurse"
    assert c.post(f"/api/notifications/{nid}/ack", headers=A).json()["ok"] is False

    assert notify.ingest("car", "sos")["id"]
    assert c.post("/api/notifications/ack-all", headers=A).json() == {"ok": True, "acked": 1}

    ids = [i["id"] for i in c.get("/api/notifications?limit=10", headers=A).json()["items"]]
    for i in ids:
        assert c.delete(f"/api/notifications/{i}", headers=A).json() == {"ok": True}
    assert c.get("/api/notifications", headers=A).json()["items"] == []


# ------------------------------------------------------------------ 10 /api/alarm 防回归
def test_alarm_route_unchanged(c, monkeypatch):
    """本任务不接 `/api/alarm`（T2 才接）：它必须仍返回 {"ok":True} 且仍广播 alarm 事件。"""
    from LLM.core import bus
    evts = []
    monkeypatch.setattr(bus, "publish", lambda t, **p: evts.append({"type": t, **p}))

    r = c.post("/api/alarm", json={"type": "sos", "uid": "elder_1", "message": "救命"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert [e["type"] for e in evts] == ["alarm"]
    assert evts[0]["alarm_type"] == "sos"        # 仍用 alarm_type，未被 type 覆盖
    assert "notification" not in [e["type"] for e in evts]   # 本任务不往告警口接线


# ------------------------------------------------------------------ 11 广播形态
def test_ingest_broadcast_uses_kind_not_type_and_carries_uid_name(monkeypatch):
    old = _init_tmp_db()
    from LLM.core import bus
    evts = []
    monkeypatch.setattr(bus, "publish", lambda t, **p: evts.append({"type": t, **p}))
    try:
        db.upsert_profile("elder_1", name="张建国", bed="301床")
        notify.ingest("car", "offline", uid="elder_1", body="失联")

        assert len(evts) == 1
        ev = evts[0]
        assert ev["type"] == "notification"      # 事件类型没被业务类型覆盖
        assert ev["kind"] == "offline"           # 一级键必须是 kind
        assert ev["kind"] != ev["type"]          # payload 里没有同名 type 把事件类型顶掉
        assert ev["level"] == "warning" and ev["source"] == "car" and ev["count"] == 1
        # 规格 §4.5：payload 必须带 uid_name，前端收到实时事件不必再拉档案
        assert ev["uid_name"] == "张建国 · 301床"
        assert set(ev) == {"type", "id", "level", "source", "kind", "uid", "uid_name",
                           "title", "body", "count", "last_at"}
    finally:
        _cleanup_tmp_db()
        db.DB_PATH = old


# ------------------------------------------------------------------ 11b uid_name 边界
def test_uid_name_falls_back_to_empty_or_single_part():
    old = _init_tmp_db()
    try:
        db.upsert_profile("elder_name_only", name="李爷爷", bed="")
        db.upsert_profile("elder_bed_only", name="", bed="102床")

        assert notify.uid_name("elder_name_only") == "李爷爷"
        assert notify.uid_name("elder_bed_only") == "102床"
        assert notify.uid_name("elder_missing") == ""     # 无档案
        assert notify.uid_name("") == ""                  # 无 uid
    finally:
        _cleanup_tmp_db()
        db.DB_PATH = old



