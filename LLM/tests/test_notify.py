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
from LLM.agent import reminder
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


# ------------------------------------------------------------------ 4b I1 竞态守卫
def test_bump_notification_returns_zero_for_acked_row():
    """`bump` 的 UPDATE 带 `ack_at=''` 守卫：已处理行进不去，返回 0。"""
    old = _init_tmp_db()
    try:
        nid = notify.ingest("car", "offline", uid="elder_1")["id"]
        assert db.bump_notification(nid, db.now_iso()) == 1     # 未处理 → 命中
        assert notify.ack(nid) is True
        assert db.bump_notification(nid, db.now_iso()) == 0     # 已处理 → 不命中
        assert db.get_notification(nid)["count"] == 2           # 已处理行的 count 不再被改
    finally:
        _cleanup_tmp_db()
        db.DB_PATH = old


def test_ingest_adds_new_row_when_bump_loses_ack_race(monkeypatch):
    """I1 竞态：find 命中后、bump 之前那一行被 ack（bump → 0）→ **必须新增一行**。

    否则新上报会并进"已处理"行：不出未读卡、不响蜂鸣、角标不变 = 静默丢失。
    """
    old = _init_tmp_db()
    try:
        a = notify.ingest("car", "offline", uid="elder_1", body="第一次失联")
        monkeypatch.setattr(db, "bump_notification", lambda nid, ts: 0)

        b = notify.ingest("car", "offline", uid="elder_1", body="第二次失联")

        assert b["deduped"] is False and b["id"] != a["id"]
        assert len(db.list_notifications(state="all")) == 2
        assert len(db.list_notifications(state="unread")) == 2   # 新上报必须进未读
        assert db.get_notification(a["id"])["count"] == 1        # 老行没被 bump
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
    # 需求文档模块 11 明确"任何模块发现异常都往该端口 POST"。
    # （读/确认按 D11 同样免鉴权，仅删除仍是管理员专属，见下方 9 节用例。）
    r = c.post("/api/notifications", json={"type": "offline", "source": "car"}, headers=K)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert db.list_notifications()[0]["source"] == "car"


# ------------------------------------------------------------------ 9 读/确认免鉴权、删单条仍 admin（D11）
def test_notification_delete_rejects_non_admin_others_are_open(c):
    """D11 演进的身份用例：原「四端点全拒非 admin」→「读/确认三端点放行 + 删单条仍 403」。

    **契约已被规格变更**（规格 §4.3 路由表 + D11「护士台不再需要登录、也不会被弹」）：
    `GET /api/notifications`、`POST /{nid}/ack`、`POST /ack-all` 对非 admin（含未登录的 kiosk
    槽）放行；**`DELETE /{nid}` 是唯一不放宽的端点**（删记录是数据损失）。

    本用例继续锁住旧用例真正的关键断言：**删单条必须过口令，且 403 不留任何副作用（行还在）**。
    """
    nid = notify.ingest("car", "offline")["id"]

    # D11：三条读/确认端点对非 admin 放行（身份闸门已按规格移除）
    assert c.get("/api/notifications", headers=K).status_code == 200
    assert c.post(f"/api/notifications/{nid}/ack", headers=K).status_code == 200
    assert c.post("/api/notifications/ack-all", headers=K).status_code == 200

    # D11：删单条仍 fail-closed
    r = c.delete(f"/api/notifications/{nid}", headers=K)
    assert r.status_code == 403
    assert r.json()["detail"] == "仅管理员可管理通知"

    assert db.get_notification(nid) is not None      # 403 无副作用：行未被删除


def test_notification_read_and_ack_are_anonymous(c):
    """D11：三条读/确认端点**完全不带 `X-Surface`/会话**也必须成功，且语义完整。

    不带任何头是最严口径 —— 后端已不读 principal，连默认槽位都不该依赖。
    """
    first = notify.ingest("car", "offline")["id"]      # warning
    second = notify.ingest("car", "fall")["id"]        # critical

    body = c.get("/api/notifications?state=all&limit=50").json()
    assert body["ok"] is True
    assert {i["id"] for i in body["items"]} == {first, second}
    assert body["counts"] == {"unread": 2, "critical": 1}

    # 单条 ack：落库 ack_at/ack_by（审计由 notify.ack 写，见同类 notify 层用例）
    assert c.post(f"/api/notifications/{first}/ack",
                  json={"by": "nurse"}).json() == {"ok": True, "id": first}
    row = db.get_notification(first)
    assert row["ack_at"] != "" and row["ack_by"] == "nurse"

    # ack-all：把剩下的未处理全标掉，条数正确
    assert c.post("/api/notifications/ack-all",
                  json={"by": "nurse"}).json() == {"ok": True, "acked": 1}
    assert notify.counts() == {"unread": 0, "critical": 0}


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


# ==================================================================== 9c 口径锁（规格 §8）
# 规格 §8 第 6/10 条点名的断言：D4 免鉴权投递必须**写审计**；ack/ack-all 必须**落库 + 广播**。


def test_post_notifications_anonymous_writes_notify_ingest_audit(c, monkeypatch):
    """D4 口径锁：非 admin 槽投递 → 200 放行，且写 `notify_ingest`（含 source/type/level/uid/id/deduped）。"""
    logs = _capture_audit(monkeypatch)

    r = c.post("/api/notifications",
               json={"type": "offline", "source": "car", "uid": "elder_1", "message": "失联"},
               headers=K)                     # K = kiosk 槽，未登录 → 绝非 admin
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["deduped"] is False

    rows = [e for e in logs if e["event"] == "notify_ingest"]
    assert len(rows) == 1
    e = rows[0]
    assert e["source"] == "car" and e["type"] == "offline"
    assert e["level"] == "warning"            # DEFAULT_LEVEL["offline"]
    assert e["uid"] == "elder_1"
    assert e["id"] == r.json()["id"]
    assert e["deduped"] is False


def test_ack_routes_land_in_db_and_broadcast_notification_ack(c, monkeypatch):
    """规格 §8 第 6 条：单条 ack → 落库 + 广播含 id/by；ack-all → 落库 + 广播含 all=True/n。"""
    evts = _capture_bus(monkeypatch)
    nid = notify.ingest("car", "offline", uid="elder_1")["id"]
    evts.clear()                              # 丢掉 ingest 的 notification，只看 ack 广播

    c.post("/api/session/login", json={"password": "111111"}, headers=A)
    assert c.post(f"/api/notifications/{nid}/ack", json={"by": "nurse"},
                  headers=A).json() == {"ok": True, "id": nid}
    assert db.get_notification(nid)["ack_at"] != ""          # 落库
    assert db.get_notification(nid)["ack_by"] == "nurse"
    assert evts[-1] == {"type": "notification_ack", "id": nid, "by": "nurse"}

    notify.ingest("car", "fall")
    notify.ingest("car", "help")
    evts.clear()
    assert c.post("/api/notifications/ack-all", json={"by": "nurse"},
                  headers=A).json() == {"ok": True, "acked": 2}
    assert evts[-1] == {"type": "notification_ack", "all": True, "by": "nurse", "n": 2}
    assert notify.counts() == {"unread": 0, "critical": 0}   # 落库


def test_delete_route_writes_notify_delete_audit(c, monkeypatch):
    """规格 §8 第 10 条旁支：删单条 → 写 `notify_delete`（含 id）。"""
    nid = notify.ingest("car", "offline")["id"]
    c.post("/api/session/login", json={"password": "111111"}, headers=A)
    logs = _capture_audit(monkeypatch)

    assert c.delete(f"/api/notifications/{nid}", headers=A).json() == {"ok": True}

    rows = [e for e in logs if e["event"] == "notify_delete"]
    assert len(rows) == 1
    assert rows[0]["id"] == nid and rows[0]["by"] == "admin"
    assert db.get_notification(nid) is None                  # 真的删了


# ------------------------------------------------------------------ 10 /api/alarm 防回归
def test_alarm_route_unchanged(c, monkeypatch):
    """`/api/alarm` 的既有契约不变：仍返回 {"ok":True} 且仍广播 `alarm` 事件。

    T1 时该口**没接**通知中心，故原断言只允许 `alarm` 一个事件；T2 起它在广播之后
    追加落库（`notify.ingest` 自身会广播 `notification`），所以这里的负向断言换成
    「`alarm` 仍是首个事件、字段一字不改」的防回归断言 —— 见下方 T2 用例。
    """
    from LLM.core import bus
    evts = []
    monkeypatch.setattr(bus, "publish", lambda t, **p: evts.append({"type": t, **p}))

    r = c.post("/api/alarm", json={"type": "sos", "uid": "elder_1", "message": "救命"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert evts[0]["type"] == "alarm"
    assert evts[0]["alarm_type"] == "sos"        # 仍用 alarm_type，未被 type 覆盖
    assert [e["type"] for e in evts[1:]] == ["notification"]   # T2 起额外落库并广播通知


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


# ==================================================================== T2
# T2：把 `/api/alarm` 与 reminder._escalate 接到通知中心，**既有契约一个字不改**。
# 库隔离用上面的 `c` fixture / `_init_tmp_db`；bus 捕获照抄 test_alarm_route_unchanged。


def _capture_bus(monkeypatch):
    """捕获总线事件（照抄本文件既有手法：`bus.publish` 是模块属性查找，可直接打桩）。"""
    from LLM.core import bus
    evts = []
    monkeypatch.setattr(bus, "publish", lambda t, **p: evts.append({"type": t, **p}))
    return evts


def _capture_audit(monkeypatch):
    """捕获审计事件（`audit.log` 同样是模块属性查找）。"""
    from LLM.core import log as audit
    rows = []
    monkeypatch.setattr(audit, "log", lambda ev, **f: rows.append({"event": ev, **f}))
    return rows


# ------------------------------------------------------------------ T2-1 告警口落库
def test_alarm_route_ingests_kiosk_notification(c, monkeypatch):
    """POST /api/alarm → 仍广播 alarm，且新增一条 source=kiosk 的 critical 通知。"""
    evts = _capture_bus(monkeypatch)

    r = c.post("/api/alarm", json={"type": "sos", "uid": "elder_x", "message": "老人说胸口疼"})

    assert r.status_code == 200 and r.json() == {"ok": True}
    # 既有行为：alarm 事件仍是第一个、仍带 alarm_type（kiosk 的 toast 依赖它）
    assert evts[0]["type"] == "alarm"
    assert evts[0]["alarm_type"] == "sos"
    assert evts[0]["level"] == "critical"

    rows = db.list_notifications()
    assert len(rows) == 1
    n = rows[0]
    assert n["source"] == "kiosk"              # AlarmIn.source 默认值
    assert n["type"] == "sos"
    assert n["level"] == "critical"            # DEFAULT_LEVEL["sos"]
    assert "胸口疼" in n["body"]
    assert n["uid"] == "elder_x"
    assert n["title"] == "紧急呼叫"             # TITLES 兜底
    assert n["count"] == 1


# ------------------------------------------------------------------ T2-2 source 覆盖
def test_alarm_source_override_lands_in_notification(c, monkeypatch):
    """AlarmIn.source 可覆盖（向后兼容：不传仍是 kiosk）。"""
    _capture_bus(monkeypatch)

    r = c.post("/api/alarm", json={"type": "fall", "source": "vision"})

    assert r.json() == {"ok": True}
    rows = db.list_notifications()
    assert len(rows) == 1
    assert rows[0]["source"] == "vision" and rows[0]["type"] == "fall"
    assert rows[0]["level"] == "critical"      # DEFAULT_LEVEL["fall"]


# ------------------------------------------------------------------ T2-3 落库失败不许 500
def test_alarm_ingest_failure_still_returns_ok(c, monkeypatch):
    """notify.ingest 抛异常时，救命通道仍 {"ok": True}，且写 notify_ingest_failed 审计。"""
    evts = _capture_bus(monkeypatch)
    logs = _capture_audit(monkeypatch)

    def _boom(*a, **k):
        raise ValueError("type 不能为空")

    monkeypatch.setattr(notify, "ingest", _boom)

    r = c.post("/api/alarm", json={"type": "sos", "uid": "elder_x", "message": "救命"})

    assert r.status_code == 200 and r.json() == {"ok": True}
    assert [e["type"] for e in evts] == ["alarm"]   # 落库失败也**不许**吞掉 alarm 广播
    assert db.list_notifications() == []       # 没落库
    fails = [e for e in logs if e["event"] == "notify_ingest_failed"]
    assert len(fails) == 1
    assert fails[0]["path"] == "/api/alarm" and fails[0]["type"] == "sos"
    assert "type 不能为空" in fails[0]["error"]


# ------------------------------------------------------------------ T2-4 提醒升级落库
def test_escalate_ingests_reminder_unconfirmed(c, monkeypatch):
    """_escalate → 仍广播 alarm，且新增一条 reminder_unconfirmed 通知。"""
    evts = _capture_bus(monkeypatch)
    rem = {"id": 42, "uid": "elder_x", "title": "吃降压药", "content": "饭后一片"}

    reminder._escalate(rem, {})

    # 既有行为：alarm 广播字段一字不改
    assert evts[0]["type"] == "alarm"
    assert evts[0]["level"] == "warning" and evts[0]["rid"] == 42
    assert evts[0]["message"] == "提醒未确认：吃降压药"

    rows = db.list_notifications()
    assert len(rows) == 1
    n = rows[0]
    assert n["source"] == "reminder" and n["type"] == "reminder_unconfirmed"
    assert n["level"] == "warning"
    assert n["ref"] == "rid:42"
    assert n["uid"] == "elder_x"
    assert n["title"] == "提醒未确认：吃降压药"
    assert n["body"] == "饭后一片"


# ------------------------------------------------------------------ T2-5 关告警也要留痕
def test_escalate_ingests_even_when_alarm_disabled(c, monkeypatch):
    """alarm_enabled=False → 不广播 alarm，**但仍落一条通知**（通知是护士留痕，不是播报）。"""
    evts = _capture_bus(monkeypatch)
    rem = {"id": 7, "uid": "elder_y", "title": "测血糖", "content": ""}

    reminder._escalate(rem, {"alarm_enabled": False})

    assert [e["type"] for e in evts] == ["notification"]   # 没有 alarm
    rows = db.list_notifications()
    assert len(rows) == 1
    assert rows[0]["type"] == "reminder_unconfirmed"
    assert rows[0]["uid"] == "elder_y"
    assert rows[0]["body"] == ""                            # content 缺省 → 空正文


# ------------------------------------------------------------------ T2-6 两处去重
def test_escalate_dedups_same_reminder(c, monkeypatch):
    """同一提醒连续两次 _escalate → 通知只有 1 行、count == 2（沿用 T1 去重窗口）。"""
    _capture_bus(monkeypatch)
    rem = {"id": 9, "uid": "elder_z", "title": "喝水", "content": "200ml"}

    reminder._escalate(rem, {})
    reminder._escalate(rem, {})

    rows = db.list_notifications()
    assert len(rows) == 1
    assert rows[0]["count"] == 2
    assert rows[0]["body"] == "200ml"          # 保留最早原文


def test_alarm_route_dedups_repeated_sos(c, monkeypatch):
    """告警口同样走去重窗口：同一老人连续两次 SOS → 1 行、count == 2。"""
    _capture_bus(monkeypatch)
    body = {"type": "sos", "uid": "elder_x", "message": "胸口疼"}

    c.post("/api/alarm", json=body)
    c.post("/api/alarm", json=body)

    rows = db.list_notifications()
    assert len(rows) == 1
    assert rows[0]["count"] == 2
    assert rows[0]["source"] == "kiosk" and rows[0]["type"] == "sos"



