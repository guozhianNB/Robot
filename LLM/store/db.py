# -*- coding: utf-8 -*-
r"""
SQLite 数据层：档案 / 记忆 / 提醒 / 工具日志 / 对话历史 / 设置 / 摘要。

约定：
  - 单文件 brain.db，WAL 模式，允许并发读。
  - 写操作统一走 _lock（进程内互斥），避免线程/协程竞态。
  - 所有函数同步实现，供协程侧用 asyncio.to_thread 调用（IO 密集不占请求链路）。
"""
import json
import sqlite3
import threading
from datetime import datetime

from ..conf import DB_PATH, HISTORY_WINDOW

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
  uid TEXT PRIMARY KEY,
  name TEXT DEFAULT '', nickname TEXT DEFAULT '', bed TEXT DEFAULT '', age INTEGER DEFAULT 0,
  gender TEXT DEFAULT '', birthday TEXT DEFAULT '',
  profile_json TEXT DEFAULT '{}',      -- {"病史": [...], "用药": [{"name","dose","time"}]}
  style TEXT DEFAULT '',
  preferences_json TEXT DEFAULT '{}',  -- {"称呼": "...", "话题": [...]}
  notes TEXT DEFAULT '',
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uid TEXT, type TEXT, content TEXT,
  status TEXT DEFAULT 'pending',       -- confirmed | pending
  ts TEXT, ttl_days INTEGER, expires_at TEXT,
  source TEXT DEFAULT '', created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uid TEXT, kind TEXT, title TEXT, content TEXT,
  trigger_type TEXT, trigger_time TEXT, trigger_date TEXT DEFAULT '',
  status TEXT DEFAULT 'pending',
  last_trigger_date TEXT DEFAULT '', triggered_at TEXT DEFAULT '',
  missed_count INTEGER DEFAULT 0, confirm_timeout_min INTEGER DEFAULT 30,
  created_by TEXT DEFAULT 'nurse', created_at TEXT, confirmed_at TEXT DEFAULT '', updated_at TEXT
);
CREATE TABLE IF NOT EXISTS tool_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, uid TEXT, tool TEXT, args_json TEXT DEFAULT '{}',
  result_snippet TEXT DEFAULT '', status TEXT DEFAULT 'ok', latency_ms INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chat_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uid TEXT, role TEXT, content TEXT, ts TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS summaries (uid TEXT PRIMARY KEY, summary TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS portraits (uid TEXT PRIMARY KEY, content TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS core_memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uid TEXT, type TEXT, content TEXT,
  confidence REAL DEFAULT 0.5, importance INTEGER DEFAULT 0,
  source TEXT DEFAULT '', ts TEXT, updated_at TEXT,
  authority TEXT DEFAULT 'llm', pinned INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rag_memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uid TEXT, chroma_id TEXT, type TEXT, content TEXT,
  importance INTEGER DEFAULT 0, source TEXT DEFAULT '', ts TEXT
);
CREATE TABLE IF NOT EXISTS external_refs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  external_id TEXT, uid TEXT, kind TEXT DEFAULT '', created_at TEXT,
  UNIQUE(uid, external_id)
);
CREATE TABLE IF NOT EXISTS delete_operations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uid TEXT, target_table TEXT, target_id INTEGER,
  snapshot_json TEXT DEFAULT '{}', reason TEXT DEFAULT '', by TEXT DEFAULT '',
  created_at TEXT, restored_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS expressions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uid TEXT DEFAULT '', situation TEXT DEFAULT '', style TEXT DEFAULT '',
  count INTEGER DEFAULT 1, checked INTEGER DEFAULT 0,   -- 0 待审 1 已审可用
  authority TEXT DEFAULT 'llm',   -- llm(自动学) | nurse(人工维护)
  source TEXT DEFAULT '', ts TEXT, updated_at TEXT
);
-- ===== 地图标记的**只读索引缓存**（规格 §4.2 红线）=====
-- 唯一真相是地图文件夹里的 <图名>.tags.json；这三张表可随时清空、由 maptags.sync_map() 重建。
-- 业务代码**不允许**直接 INSERT/UPDATE/DELETE 下面三张表 —— 唯一写入口是 maptags.sync_map()。
CREATE TABLE IF NOT EXISTS map_tags_manifest (
  map_name TEXT PRIMARY KEY,
  file_mtime REAL DEFAULT 0,            -- tags.json 的 mtime
  file_size INTEGER DEFAULT 0,          -- tags.json 的 size
  sha1 TEXT DEFAULT '',                 -- 内容指纹（mtime/size 相同但内容变了也能发现）
  resolution REAL,                      -- 指纹：抄自 tags.json，用于与 yaml 实际值比对
  origin_json TEXT DEFAULT '',          -- 指纹
  synced_at TEXT, warnings_json TEXT DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS destinations (
  -- ⚠️ 主键必须是 (map_name, uid)：**同一个 uid（d1）在三张图里各有一份**，因为 uid 只在
  -- 单张图的 tags.json 内稳定。用 uid 单主键会让 my_map2 的 d1 覆盖 my_map 的 d1。
  uid TEXT NOT NULL,
  map_name TEXT NOT NULL,               -- 绑图（三张图坐标系不通用）
  name TEXT NOT NULL,
  aliases TEXT DEFAULT '',              -- 逗号分隔（冗余自 JSON 数组，便于 LIKE 查询）
  x REAL DEFAULT 0, y REAL DEFAULT 0,   -- 米，map 坐标系
  yaw_deg REAL DEFAULT 0,               -- 到达朝向（度，逆时针为正）
  risk TEXT DEFAULT 'low',              -- low | high
  elder_allowed INTEGER DEFAULT 1,      -- 第二期「说去哪」的唯一闸门
  note TEXT DEFAULT '',
  learned_by TEXT DEFAULT '',           -- editor | learn_button | manual
  created_at TEXT, updated_at TEXT,
  PRIMARY KEY (map_name, uid),
  UNIQUE(map_name, name)
);
CREATE TABLE IF NOT EXISTS zones (
  uid TEXT NOT NULL,
  map_name TEXT NOT NULL,
  name TEXT NOT NULL,
  kind TEXT DEFAULT 'room',             -- room | ward | bed | other
  shape TEXT DEFAULT 'polygon',         -- polygon | rect
  polygon_json TEXT DEFAULT '[]',       -- [[x,y], ...] 米坐标（世界系，非像素）
  parent TEXT DEFAULT '',               -- 上级区域的 uid（同一张图内）
  note TEXT DEFAULT '',
  created_at TEXT, updated_at TEXT,
  PRIMARY KEY (map_name, uid),
  UNIQUE(map_name, name)
);
-- ===== 通知中心（护士台数据底座，模块 11）=====
-- 任何模块发现异常都往 POST /api/notifications 投递（免鉴权）。同 (source,type,uid,正文) 在
-- NOTIFY_DEDUP_S 窗口内命中的未处理通知做合并（count+1、last_at 刷新，保留最早原文、级别只升不降）。
CREATE TABLE IF NOT EXISTS notifications (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  level      TEXT NOT NULL DEFAULT 'info',
  source     TEXT NOT NULL DEFAULT '',
  type       TEXT NOT NULL,
  uid        TEXT DEFAULT '',
  title      TEXT DEFAULT '',
  body       TEXT DEFAULT '',
  ref        TEXT DEFAULT '',
  count      INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  last_at    TEXT NOT NULL,
  ack_at     TEXT DEFAULT '',
  ack_by     TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_notifications_created ON notifications(created_at DESC);
"""


def _conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_columns(conn, table, columns):
    """对已存在的表补缺失列（CREATE TABLE IF NOT EXISTS 不会改已有表）。"""
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for col, ddl in columns.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db():
    with _lock:
        conn = _conn()
        try:
            conn.executescript(SCHEMA)
            _ensure_columns(conn, "profiles", {
                "gender": "gender TEXT DEFAULT ''",
                "birthday": "birthday TEXT DEFAULT ''",
                # 分层用户体系（规格 §9）：kind=elder|ward；ward_id=老人所属病房 uid；
                # ward_map/ward_zone=病房关联的「地图名 + 区域 uid」（几何真相在 <图名>.tags.json）
                "kind": "kind TEXT DEFAULT 'elder'",
                "ward_id": "ward_id TEXT DEFAULT ''",
                "ward_map": "ward_map TEXT DEFAULT ''",
                "ward_zone": "ward_zone TEXT DEFAULT ''",
            })
            # 软删 / 幂等列（跨表统一补齐，兼容旧库）
            for table in ("memories", "core_memories", "rag_memories", "expressions"):
                _ensure_columns(conn, table, {
                    "is_deleted": "is_deleted INTEGER DEFAULT 0",
                    "deleted_at": "deleted_at TEXT DEFAULT ''",
                })
            _ensure_columns(conn, "rag_memories", {"external_id": "external_id TEXT DEFAULT ''"})
            # P0c 账本定稿：authority=llm(模型归纳) | nurse(护士定稿) | claim(档案账本)
            _ensure_columns(conn, "core_memories", {"authority": "authority TEXT DEFAULT 'llm'"})
            # P3 保护/强化：core_memories pinned=护士永久保护(不被自动清理/画像覆盖)
            _ensure_columns(conn, "core_memories", {"pinned": "pinned INTEGER DEFAULT 0"})
            conn.commit()
            _migrate_map_tags_cache(conn)
        finally:
            conn.close()


def _migrate_map_tags_cache(conn) -> None:
    """把地图标记缓存表迁到「主键 = (map_name, uid)」的形态（幂等，每次启动跑）。

    为什么必须迁：**uid 只在单张图的 ``tags.json`` 内稳定**，三张图各有自己的 ``d1``。
    早期版本把 ``uid`` 当单列主键，于是 ``my_map`` 复制成 ``my_map2`` 后再刷缓存就会
    ``UNIQUE constraint failed: destinations.uid``（实测踩过）。

    ``CREATE TABLE IF NOT EXISTS`` 不会改已存在的表，所以这里显式"重建 + 尽量搬数据"。
    表里只是索引缓存，坏了大不了重建，因此搬运失败/有重也能安全降级。
    """
    for table in ("destinations", "zones"):
        try:
            info = conn.execute(f"PRAGMA table_info({table})").fetchall()
            if not info:
                continue
            pk = [r["name"] for r in sorted((r for r in info if r["pk"]), key=lambda r: r["pk"])]
            if pk == ["map_name", "uid"]:
                continue                       # 已是新形态
            cols = [r["name"] for r in info]
            old = f"{table}__old"
            conn.execute(f"ALTER TABLE {table} RENAME TO {old}")
            conn.executescript(SCHEMA)         # 以新定义重建（IF NOT EXISTS 现在会真的建）
            # 旧表里 (map_name, uid) 可能重复（uid 单主键造成的跨图冲突）→ 每组只搬一行
            keep = "MAX(updated_at)" if "updated_at" in cols else "MIN(rowid)"
            conn.execute(
                f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) "
                f"SELECT {', '.join(cols)} FROM {old} "
                f"WHERE rowid IN (SELECT rowid FROM {old} GROUP BY map_name, uid HAVING {keep})")
            conn.execute(f"DROP TABLE {old}")
            conn.commit()
        except Exception as e:      # noqa: BLE001  迁移失败不许拖垮启动（缓存而已）
            try:
                conn.rollback()
            except Exception:       # noqa: BLE001
                pass
            print(f"[WARN] 地图标记缓存表 {table} 迁移失败（不影响启动）：{e}")


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- profiles
def get_profile(uid: str) -> dict | None:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM profiles WHERE uid=?", (uid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["profile"] = json.loads(d.pop("profile_json") or "{}")
        d["preferences"] = json.loads(d.pop("preferences_json") or "{}")
        return d
    finally:
        conn.close()


def list_profiles(kind: str = "") -> list[dict]:
    conn = _conn()
    try:
        sql = "SELECT * FROM profiles"
        args: tuple = ()
        if kind:
            sql += " WHERE kind=?"
            args = (kind,)
        rows = conn.execute(sql + " ORDER BY uid", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["profile"] = json.loads(d.pop("profile_json") or "{}")
            d["preferences"] = json.loads(d.pop("preferences_json") or "{}")
            out.append(d)
        return out
    finally:
        conn.close()


def get_profile_kind(uid: str) -> str:
    """角色判定的**唯一权威依据**（R1）：admin 不写进 profiles → 返回 ""。

    注意：调用方（session.derive_role）必须把 "" 当"未知"并按集体层兜底（R2），
    绝不按 uid 前缀猜。
    """
    if not uid:
        return ""
    conn = _conn()
    try:
        row = conn.execute("SELECT kind FROM profiles WHERE uid=?", (uid,)).fetchone()
        return (row["kind"] or "elder") if row else ""
    finally:
        conn.close()


def list_wards() -> list[dict]:
    """全部病房用户（kind='ward'）。"""
    return list_profiles(kind="ward")


def upsert_ward(uid: str, name: str = "", ward_map: str = "", ward_zone: str = "") -> dict:
    """新建/更新一条病房用户。

    **只由本函数与 set_ward_zone 写 kind/ward_map/ward_zone**：upsert_profile 不碰这三列，
    否则管理台编辑老人档案时会静默清掉病房关联（见 git f6f6e54）。
    **三个字段一律"传空串 = 保持原值"，名字也一样**——`upsert_profile` 的 ON CONFLICT 是
    `name=excluded.name`，直接把空串喂进去会把已有名字抹成空（静默数据丢失），
    所以名字只在"行不存在"或"传了非空名字"时才写。
    """
    exists = bool(get_profile(uid))
    if not exists:
        upsert_profile(uid, name=name)      # 建行（行不存在时名字允许为空）
    elif name:
        with _lock:
            conn = _conn()
            try:
                conn.execute("UPDATE profiles SET name=?, updated_at=? WHERE uid=?",
                             (name, now_iso(), uid))
                conn.commit()
            finally:
                conn.close()
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                "UPDATE profiles SET kind='ward', "
                "ward_map=CASE WHEN ?<>'' THEN ? ELSE ward_map END, "
                "ward_zone=CASE WHEN ?<>'' THEN ? ELSE ward_zone END, "
                "updated_at=? WHERE uid=?",
                (ward_map, ward_map, ward_zone, ward_zone, now_iso(), uid))
            conn.commit()
        finally:
            conn.close()
    return get_profile(uid) or {}


def set_ward_zone(uid: str, map_name: str, zone_uid: str) -> int:
    """把病房关联到「某张图上的某个区域 uid」（几何真相在 <图名>.tags.json，这里只存引用）。

    **返回受影响行数**：uid 不存在时命中 0 行，调用方（REST）据此报 `ok:false`，
    不再把"静默 no-op"当成成功（既有调用点忽略返回值即可）。
    """
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "UPDATE profiles SET ward_map=?, ward_zone=?, updated_at=? WHERE uid=?",
                (map_name or "", zone_uid or "", now_iso(), uid))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def set_profile_ward(uid: str, ward_id: str) -> int:
    """老人归入病房（写 profiles.ward_id；空串 = 移出病房）。返回受影响行数（0 = uid 不存在）。"""
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute("UPDATE profiles SET ward_id=?, updated_at=? WHERE uid=?",
                               (ward_id or "", now_iso(), uid))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def delete_ward(uid: str) -> bool:
    """删除病房档案，并在同一事务内解除所有老人的病房归属。

    地图区域的真相在 ``<map>.tags.json``，这里只删除 profiles 中的病房及其引用，
    不删除地图区域。uid 不存在或不是病房时返回 False。
    """
    with _lock:
        conn = _conn()
        try:
            row = conn.execute("SELECT kind FROM profiles WHERE uid=?", (uid,)).fetchone()
            if not row or row["kind"] != "ward":
                return False
            ts = now_iso()
            conn.execute("UPDATE profiles SET ward_id='', updated_at=? WHERE ward_id=?", (ts, uid))
            conn.execute("DELETE FROM profiles WHERE uid=? AND kind='ward'", (uid,))
            conn.commit()
            return True
        finally:
            conn.close()


def upsert_profile(uid: str, name="", nickname="", bed="", age=0,
                   profile=None, style="", preferences=None, notes="",
                   gender="", birthday="") -> dict:
    profile = profile or {}
    preferences = preferences or {}
    ts = now_iso()
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                """INSERT INTO profiles (uid,name,nickname,bed,age,gender,birthday,profile_json,style,preferences_json,notes,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(uid) DO UPDATE SET
                     name=excluded.name, nickname=excluded.nickname, bed=excluded.bed, age=excluded.age,
                     gender=excluded.gender, birthday=excluded.birthday,
                     profile_json=excluded.profile_json, style=excluded.style,
                     preferences_json=excluded.preferences_json, notes=excluded.notes, updated_at=excluded.updated_at""",
                (uid, name, nickname, bed, age, gender, birthday,
                 json.dumps(profile, ensure_ascii=False),
                 style,
                 json.dumps(preferences, ensure_ascii=False),
                 notes, ts, ts),
            )
            conn.commit()
        finally:
            conn.close()
    return get_profile(uid)


# ---------------------------------------------------------------- memories
def add_memory(uid, mtype, content, status="pending", ttl_days=None,
               source="", ts=None) -> int:
    ts = ts or now_iso()
    expires = None
    if ttl_days:
        from datetime import timedelta
        expires = (datetime.strptime(ts, "%Y-%m-%d %H:%M:%S") + timedelta(days=ttl_days)).strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "INSERT INTO memories (uid,type,content,status,ts,ttl_days,expires_at,source,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (uid, mtype, content, status, ts, ttl_days, expires, source, ts, ts),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def import_memories(uid: str, text: str, by: str = "nurse",
                    split: str = "paragraph") -> dict:
    """批量导入中心：粘贴文本按段落/行切分逐条入 memories(pending)，护士审核确认。
    对标 MaiBot 导入中心；医疗字段仍是待审核状态由护士把关，导入本身不绕过红线。"""
    text = (text or "").strip()
    if not text:
        return {"ok": True, "imported": 0, "skipped": 0}
    if split == "line":
        chunks = [ln.strip() for ln in text.splitlines() if ln.strip()]
    else:  # paragraph：按空行分隔
        import re as _re
        chunks = [c.strip() for c in _re.split(r"\n\s*\n", text) if c.strip()]
    # 过长的段落再按 120 字内按句切，避免单条过大
    final_chunks: list[str] = []
    for c in chunks:
        if len(c) <= 160:
            final_chunks.append(c)
        else:
            import re as _re
            sents = _re.split(r"(?<=[。！？；])", c)
            buf = ""
            for s in sents:
                if not s.strip():
                    continue
                if len(buf) + len(s) > 160 and buf:
                    final_chunks.append(buf.strip())
                    buf = s
                else:
                    buf += s
            if buf.strip():
                final_chunks.append(buf.strip())
    imported = 0
    skipped = 0
    ts = now_iso()
    with _lock:
        conn = _conn()
        try:
            for c in final_chunks:
                if len(c) > 500:
                    c = c[:500]
                if not c:
                    continue
                conn.execute(
                    "INSERT INTO memories (uid,type,content,status,ts,source,created_at,updated_at) "
                    "VALUES (?,?,?,'pending',?,'import:manual',?,?)",
                    (uid, "fact", c, ts, ts, ts))
                imported += 1
            conn.commit()
        finally:
            conn.close()
    from ..core import log as audit
    audit.log("memory_change", action="import", uid=uid, count=imported, by=by)
    return {"ok": True, "imported": imported, "skipped": skipped}


def list_memories(uid: str = None, status: str = None) -> list[dict]:
    sql = "SELECT * FROM memories WHERE is_deleted=0"
    args = []
    if uid:
        sql += " AND uid=?"
        args.append(uid)
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY id DESC"
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def get_memory(mid: int) -> dict | None:
    conn = _conn()
    try:
        r = conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def set_memory_status(mid: int, status: str) -> None:
    with _lock:
        conn = _conn()
        try:
            conn.execute("UPDATE memories SET status=?, updated_at=? WHERE id=?", (status, now_iso(), mid))
            conn.commit()
        finally:
            conn.close()


def delete_memory(mid: int, uid: str = "", reason: str = "", by: str = "") -> int:
    """删除 memories 行 → 软删（置 is_deleted + delete_operations 快照），可回收站恢复。返回 op_id。"""
    return soft_delete("memories", mid, uid=uid, reason=reason or "manual_delete", by=by)


def delete_memory_hard(mid: int) -> None:
    """物理删除 memories 行（TTL 到期等生命周期清理用，不进回收站）。"""
    with _lock:
        conn = _conn()
        try:
            conn.execute("DELETE FROM memories WHERE id=?", (mid,))
            conn.commit()
        finally:
            conn.close()


def cleanup_expired_memories() -> int:
    """删除 TTL 到期的事件记忆（事件带时效，过期自动清除）。"""
    now = now_iso()
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at < ? AND status='confirmed'",
                (now,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


# ---------------------------------------------------------------- core_memories
def add_core_memory(uid, mtype, content, confidence=0.5, importance=0, source="", ts=None,
                    authority=None) -> int:
    ts = ts or now_iso()
    if authority is None:
        # 来源推断：人工/护士/迁移修正 → 定稿；模型归纳/整理 → llm(uncertain)
        authority = "nurse" if (source or "").startswith(("manual", "correct:", "nurse")) else "llm"
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "INSERT INTO core_memories (uid,type,content,confidence,importance,source,ts,updated_at,authority) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (uid, mtype, content, confidence, importance, source, ts, ts, authority))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def get_core_memory(mid: int) -> dict | None:
    conn = _conn()
    try:
        r = conn.execute("SELECT * FROM core_memories WHERE id=?", (mid,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def list_core_memories(uid, limit=None) -> list[dict]:
    sql = "SELECT * FROM core_memories WHERE uid=? AND is_deleted=0 ORDER BY importance DESC, id DESC"
    args = [uid]
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def update_core_memory(mid, content=None, importance=None, confidence=None) -> None:
    fields = {"updated_at": now_iso()}
    if content is not None:
        fields["content"] = content
    if importance is not None:
        fields["importance"] = importance
    if confidence is not None:
        fields["confidence"] = confidence
    keys = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [mid]
    with _lock:
        conn = _conn()
        try:
            conn.execute(f"UPDATE core_memories SET {keys} WHERE id=?", vals)
            conn.commit()
        finally:
            conn.close()


def delete_core_memory(mid, uid="", reason="", by="") -> int:
    """删除 core_memories 行 → 软删。注意：persona 画像"删旧写新"属于内部覆盖，
    语义上不应进回收站（旧画像直接作废），调用处可继续物理删——见 purge 语义。"""
    return soft_delete("core_memories", mid, uid=uid, reason=reason or "manual_delete", by=by)


def delete_core_memory_hard(mid) -> None:
    """物理删除 core_memories 行（内部画像覆盖等场景用，不进回收站）。"""
    with _lock:
        conn = _conn()
        try:
            conn.execute("DELETE FROM core_memories WHERE id=?", (mid,))
            conn.commit()
        finally:
            conn.close()


def set_core_authority(mid: int, authority: str) -> None:
    """定稿/降级核心记忆来源：nurse=护士确认定稿(可信), llm=AI 归纳(uncertain,仅参考)。"""
    with _lock:
        conn = _conn()
        try:
            conn.execute("UPDATE core_memories SET authority=?, updated_at=? WHERE id=?",
                         (authority, now_iso(), mid))
            conn.commit()
        finally:
            conn.close()


def set_core_pinned(mid: int, pinned: bool) -> None:
    """护士保护核心记忆(pinned)：不被自动清理、不被画像整体覆盖（对标 MaiBot protect/pinned）。"""
    with _lock:
        conn = _conn()
        try:
            conn.execute("UPDATE core_memories SET pinned=?, updated_at=? WHERE id=?",
                         (1 if pinned else 0, now_iso(), mid))
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------- rag_memories（镜像表）
def add_rag_memory(uid, mtype, content, chroma_id, importance=0, source="", ts=None,
                   external_id="") -> int:
    ts = ts or now_iso()
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "INSERT INTO rag_memories (uid,chroma_id,type,content,importance,source,ts,external_id) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (uid, chroma_id, mtype, content, importance, source, ts, external_id))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def get_rag_by_external(uid: str, external_id: str) -> dict | None:
    """按 uid+external_id 查已写入的 RAG 行（幂等命中判断）。"""
    if not external_id:
        return None
    conn = _conn()
    try:
        r = conn.execute(
            "SELECT * FROM rag_memories WHERE uid=? AND external_id=? AND is_deleted=0",
            (uid, external_id)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def find_memories_by_content(uid: str, content: str, tables=("core_memories", "rag_memories"),
                             fuzzy: bool = True) -> dict[str, list[dict]]:
    """按内容找指定表的行（精确或包含匹配，供纠错/批量作废定位源条目）。"""
    out: dict[str, list[dict]] = {}
    conn = _conn()
    try:
        for t in tables:
            if t not in ("core_memories", "rag_memories", "memories"):
                continue
            if fuzzy:
                pat = content[:30].strip() if content else ""
                if not pat:
                    out[t] = []
                    continue
                rows = conn.execute(
                    f"SELECT * FROM {t} WHERE uid=? AND is_deleted=0 AND content LIKE ? "
                    "ORDER BY id DESC LIMIT 20", (uid, f"%{pat}%")).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM {t} WHERE uid=? AND is_deleted=0 AND content=? "
                    "ORDER BY id DESC LIMIT 20", (uid, content)).fetchall()
            out[t] = [dict(r) for r in rows]
        return out
    finally:
        conn.close()


def list_rag_memories(uid, limit=None) -> list[dict]:
    sql = "SELECT * FROM rag_memories WHERE uid=? AND is_deleted=0 ORDER BY id DESC"
    args = [uid]
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def delete_rag_memory(row_id: int, uid: str = "", reason: str = "", by: str = "") -> int:
    """软删 rag_memories 行（镜像表），返回 op_id。向量清理由调用方 ragstore.delete_by_chroma_id。"""
    return soft_delete("rag_memories", row_id, uid=uid, reason=reason or "manual_delete", by=by)


def get_rag_memory(row_id: int) -> dict | None:
    conn = _conn()
    try:
        r = conn.execute("SELECT * FROM rag_memories WHERE id=?", (row_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def set_rag_chroma_id(row_id: int, chroma_id: str) -> None:
    with _lock:
        conn = _conn()
        try:
            conn.execute("UPDATE rag_memories SET chroma_id=? WHERE id=?", (chroma_id, row_id))
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------- external refs（写入幂等）
def claim_external(uid: str, external_id: str, kind: str = "") -> bool:
    """幂等注册外部业务键：首次返回 True，已存在返回 False（同业务只写一次）。"""
    if not external_id:
        return True
    ts = now_iso()
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO external_refs (external_id,uid,kind,created_at) VALUES (?,?,?,?)",
                (external_id, uid, kind, ts))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def list_external_refs(uid: str = "") -> list[dict]:
    sql = "SELECT * FROM external_refs"
    args = []
    if uid:
        sql += " WHERE uid=?"
        args.append(uid)
    sql += " ORDER BY created_at DESC LIMIT 200"
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------- 软删 + 回收站（delete_operations 快照）
def soft_delete(table: str, row_id: int, uid: str = "", reason: str = "", by: str = "") -> int:
    """软删任意记忆表行：置 is_deleted=1 + 全量快照入 delete_operations。表名白名单防注入。"""
    table = table if table in ("memories", "core_memories", "rag_memories", "expressions") else "memories"
    ts = now_iso()
    with _lock:
        conn = _conn()
        try:
            row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
            if not row or row["is_deleted"]:
                return 0
            snapshot = json.dumps(dict(row), ensure_ascii=False)
            conn.execute(f"UPDATE {table} SET is_deleted=1, deleted_at=? WHERE id=? AND is_deleted=0",
                         (ts, row_id))
            op_id = conn.execute(
                "INSERT INTO delete_operations (uid,target_table,target_id,snapshot_json,reason,by,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (uid or row["uid"], table, row_id, snapshot, reason, by, ts)).lastrowid
            conn.commit()
            return op_id or 0
        finally:
            conn.close()


def restore_operation(op_id: int) -> bool:
    """按 delete_operations 快照恢复行（清除软删标记）；RAG 行另需重建向量（调用方处理）。"""
    with _lock:
        conn = _conn()
        try:
            op = conn.execute("SELECT * FROM delete_operations WHERE id=?", (op_id,)).fetchone()
            if not op or op["restored_at"]:
                return False
            table, row_id = op["target_table"], op["target_id"]
            snap = json.loads(op["snapshot_json"] or "{}")
            ts = now_iso()
            row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
            if row:  # 行还在 → 仅清软删标记
                conn.execute(f"UPDATE {table} SET is_deleted=0, deleted_at='' WHERE id=?", (row_id,))
            else:    # 行已被物理清理 → 按快照重建
                cols = [c for c in snap.keys() if c != "id"]
                placeholders = ", ".join("?" for _ in cols)
                colnames = ", ".join(cols)
                conn.execute(
                    f"INSERT INTO {table} ({colnames}) VALUES ({placeholders})",
                    [snap[c] for c in cols])
            conn.execute("UPDATE delete_operations SET restored_at=? WHERE id=?", (ts, op_id))
            conn.commit()
            return True
        finally:
            conn.close()


def list_delete_operations(uid: str = "", include_restored: bool = False) -> list[dict]:
    sql = "SELECT * FROM delete_operations WHERE 1=1"
    args = []
    if uid:
        sql += " AND uid=?"
        args.append(uid)
    if not include_restored:
        sql += " AND restored_at=''"
    sql += " ORDER BY id DESC LIMIT 200"
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def purge_soft_deleted(days: float = 30.0) -> int:
    """物理清理软删超过宽限期的行（回收站深清），幂等可反复调用。"""
    import time as _t
    cutoff = _t.time() - days * 86400
    ts_cut = datetime.fromtimestamp(cutoff).strftime("%Y-%m-%d %H:%M:%S")
    total = 0
    with _lock:
        conn = _conn()
        try:
            for table in ("memories", "core_memories", "rag_memories"):
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE is_deleted=1 AND deleted_at!='' AND deleted_at < ?",
                    (ts_cut,))
                total += cur.rowcount
            conn.commit()
            return total
        finally:
            conn.close()


# ---------------------------------------------------------------- expressions（表达习惯语录库）
def upsert_expression(uid: str, situation: str, style: str, authority: str = "llm",
                      source: str = "") -> int:
    """记录一条「情景→说法」。同 uid+situation+style 合并累加 count（对标 MaiBot Expression merge）。"""
    ts = now_iso()
    with _lock:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT id FROM expressions WHERE uid=? AND situation=? AND style=? AND is_deleted=0",
                (uid, situation, style)).fetchone()
            if row:
                conn.execute("UPDATE expressions SET count=count+1, updated_at=? WHERE id=?",
                             (ts, row["id"]))
                conn.commit()
                return row["id"]
            cur = conn.execute(
                "INSERT INTO expressions (uid,situation,style,count,checked,authority,source,ts,updated_at) "
                "VALUES (?,?,?,1,0,?,?,?,?)",
                (uid, situation, style, authority, source, ts, ts))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def list_expressions(uid: str = "", checked_only: bool = False, limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM expressions WHERE is_deleted=0"
    args = []
    if uid:
        sql += " AND uid=?"
        args.append(uid)
    if checked_only:
        sql += " AND checked=1"
    sql += " ORDER BY count DESC, id DESC LIMIT ?"
    args.append(limit)
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def set_expression_checked(eid: int, checked: bool) -> None:
    with _lock:
        conn = _conn()
        try:
            conn.execute("UPDATE expressions SET checked=?, updated_at=? WHERE id=?",
                         (1 if checked else 0, now_iso(), eid))
            conn.commit()
        finally:
            conn.close()


def soft_delete_expression(eid: int, uid: str = "") -> None:
    with _lock:
        conn = _conn()
        try:
            conn.execute("UPDATE expressions SET is_deleted=1, deleted_at=? WHERE id=?",
                         (now_iso(), eid))
            conn.commit()
        finally:
            conn.close()


def pick_expressions(uid: str, limit: int = 3) -> list[dict]:
    """对话注入用：取该 uid 已审核的高频表达（checked=1）。"""
    return list_expressions(uid=uid, checked_only=True, limit=limit)


# ---------------------------------------------------------------- reminders
def add_reminder(uid, kind, title, content, trigger_type, trigger_time,
                 trigger_date="", confirm_timeout_min=30, created_by="nurse") -> int:
    ts = now_iso()
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                """INSERT INTO reminders (uid,kind,title,content,trigger_type,trigger_time,trigger_date,status,confirm_timeout_min,created_by,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (uid, kind, title, content, trigger_type, trigger_time, trigger_date,
                 "pending", confirm_timeout_min, created_by, ts, ts),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def list_reminders(uid: str = None) -> list[dict]:
    sql = "SELECT * FROM reminders"
    args = []
    if uid:
        sql += " WHERE uid=?"
        args.append(uid)
    sql += " ORDER BY id DESC"
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def get_reminder(rid: int) -> dict | None:
    conn = _conn()
    try:
        r = conn.execute("SELECT * FROM reminders WHERE id=?", (rid,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def update_reminder(rid: int, **fields) -> None:
    fields["updated_at"] = now_iso()
    keys = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [rid]
    with _lock:
        conn = _conn()
        try:
            conn.execute(f"UPDATE reminders SET {keys} WHERE id=?", vals)
            conn.commit()
        finally:
            conn.close()


def delete_reminder(rid: int) -> None:
    with _lock:
        conn = _conn()
        try:
            conn.execute("DELETE FROM reminders WHERE id=?", (rid,))
            conn.commit()
        finally:
            conn.close()


def upsert_medication_reminder(uid, med_name, med_dose, time_str, confirm_timeout_min=30):
    """按 老人uid+药名+时间 去重，把档案里的用药同步成每日提醒。"""
    title = f"服药提醒：{med_name}"
    content = f"{med_name}（{med_dose}）到了服药时间"
    conn = _conn()
    try:
        row = conn.execute(
            """SELECT id FROM reminders WHERE uid=? AND kind='medication' AND title=? AND trigger_time=?""",
            (uid, title, time_str)).fetchone()
    finally:
        conn.close()
    if row:
        return row["id"]
    return add_reminder(uid, "medication", title, content, "daily", time_str,
                        confirm_timeout_min=confirm_timeout_min, created_by="system")


# ---------------------------------------------------------------- tool log
def log_tool(uid, tool, args, result_snippet, status="ok", latency_ms=0) -> None:
    ts = now_iso()
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO tool_log (ts,uid,tool,args_json,result_snippet,status,latency_ms) VALUES (?,?,?,?,?,?,?)",
                (ts, uid, tool, json.dumps(args, ensure_ascii=False), (result_snippet or "")[:2000], status, latency_ms),
            )
            conn.commit()
        finally:
            conn.close()


def list_tool_log(uid: str = None, limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM tool_log"
    args = []
    if uid:
        sql += " WHERE uid=?"
        args.append(uid)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    conn = _conn()
    try:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        for r in rows:
            r["args"] = json.loads(r.pop("args_json") or "{}")
        return rows
    finally:
        conn.close()


# ---------------------------------------------------------------- chat history
def append_history(uid, role, content) -> None:
    with _lock:
        conn = _conn()
        try:
            conn.execute("INSERT INTO chat_history (uid,role,content,ts) VALUES (?,?,?,?)",
                         (uid, role, content, now_iso()))
            conn.commit()
        finally:
            conn.close()


def load_history(uid, limit=HISTORY_WINDOW) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT role, content FROM chat_history WHERE uid=? ORDER BY id DESC LIMIT ?",
            (uid, limit)).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    finally:
        conn.close()


def load_history_full(uid, limit=200) -> list[dict]:
    """带时间戳的完整历史（前端回读渲染用）。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT role, content, ts FROM chat_history WHERE uid=? ORDER BY id ASC LIMIT ?",
            (uid, limit)).fetchall()
        return [{"role": r["role"], "content": r["content"], "ts": r["ts"]} for r in rows]
    finally:
        conn.close()


def clear_history(uid) -> int:
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute("DELETE FROM chat_history WHERE uid=?", (uid,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def history_count(uid) -> int:
    conn = _conn()
    try:
        return conn.execute("SELECT COUNT(*) c FROM chat_history WHERE uid=?", (uid,)).fetchone()["c"]
    finally:
        conn.close()


def trim_history(uid, keep=HISTORY_WINDOW) -> None:
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                """DELETE FROM chat_history WHERE uid=? AND id NOT IN
                   (SELECT id FROM chat_history WHERE uid=? ORDER BY id DESC LIMIT ?)""",
                (uid, uid, keep))
            conn.commit()
        finally:
            conn.close()


def oldest_history(uid, count) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT role, content FROM chat_history WHERE uid=? ORDER BY id ASC LIMIT ?",
            (uid, count)).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------- summaries
def get_summary(uid) -> str:
    conn = _conn()
    try:
        r = conn.execute("SELECT summary FROM summaries WHERE uid=?", (uid,)).fetchone()
        return r["summary"] if r else ""
    finally:
        conn.close()


def set_summary(uid, summary) -> None:
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO summaries (uid,summary,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(uid) DO UPDATE SET summary=excluded.summary, updated_at=excluded.updated_at",
                (uid, summary, now_iso()))
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------- portraits（老人画像）
def get_portrait(uid) -> str:
    conn = _conn()
    try:
        r = conn.execute("SELECT content FROM portraits WHERE uid=?", (uid,)).fetchone()
        return r["content"] if r else ""
    finally:
        conn.close()


def set_portrait(uid, content) -> None:
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO portraits (uid,content,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(uid) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at",
                (uid, content, now_iso()))
            conn.commit()
        finally:
            conn.close()


def update_memory_content(mid: int, content: str) -> None:
    """合并记忆：把补充细节写回已有条目。"""
    with _lock:
        conn = _conn()
        try:
            conn.execute("UPDATE memories SET content=?, updated_at=? WHERE id=?",
                         (content, now_iso(), mid))
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------- settings
def get_settings() -> dict:
    from ..conf import DEFAULT_SETTINGS
    from ..agent.tools import TOOL_DEFAULTS
    out = dict(DEFAULT_SETTINGS)
    out.update(TOOL_DEFAULTS)   # per-tool 开关默认值（已保存的随后覆盖）
    conn = _conn()
    try:
        for r in conn.execute("SELECT key,value FROM settings").fetchall():
            if r["key"].startswith("admin_password_"):
                # 口令族 raw key 不参与设置项合并：它们只能经 get_admin_auth()/
                # verify_admin_password() 访问，绝不能随 GET /api/settings 外泄给前端。
                continue
            v = r["value"]
            if isinstance(out.get(r["key"]), bool):
                v = v.lower() in ("1", "true", "yes")
            elif isinstance(out.get(r["key"]), int):
                try:
                    v = int(v)
                except ValueError:
                    v = out[r["key"]]
            elif isinstance(out.get(r["key"]), float):
                try:
                    v = float(v)
                except ValueError:
                    v = out[r["key"]]
            out[r["key"]] = v
        return out
    finally:
        conn.close()


def set_settings(patch: dict) -> dict:
    from ..conf import DEFAULT_SETTINGS
    from ..agent.tools import TOOL_DEFAULTS
    cur = get_settings()
    allowed = set(DEFAULT_SETTINGS) | set(TOOL_DEFAULTS)
    cur.update({k: v for k, v in patch.items() if k in allowed})
    with _lock:
        conn = _conn()
        try:
            for k, v in cur.items():
                conn.execute(
                    "INSERT INTO settings (key,value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (k, str(v)))
            conn.commit()
        finally:
            conn.close()
    return cur


# ---------------------------------------------------------------- 管理员口令（规格 D12/D13）
# 为什么不用 set_settings：它只接受 DEFAULT_SETTINGS|TOOL_DEFAULTS 白名单里的 key，
# 而口令必须有自己的 key（且绝不落明文、绝不进前端设置页）。
def _get_setting_raw(key: str, default: str = "") -> str:
    conn = _conn()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def _set_setting_raw(key: str, value: str) -> None:
    with _lock:
        conn = _conn()
        try:
            conn.execute("INSERT INTO settings (key,value) VALUES (?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            conn.commit()
        finally:
            conn.close()


def _hash_pw(pw: str, salt: str) -> str:
    import hashlib
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()


def get_admin_auth() -> dict:
    """口令门状态 + 盐/哈希（哈希为空 = 还没设过口令）。"""
    return {
        "required": _get_setting_raw("admin_auth_required", "1").lower() in ("1", "true", "yes"),
        "hash": _get_setting_raw("admin_password_hash"),
        "salt": _get_setting_raw("admin_password_salt"),
    }


def set_admin_password(pw: str) -> None:
    """设/改口令：随机盐 + PBKDF2-SHA256 20 万轮，**不落明文**。"""
    import os as _os
    salt = _os.urandom(16).hex()
    _set_setting_raw("admin_password_salt", salt)
    _set_setting_raw("admin_password_hash", _hash_pw(pw, salt))


def verify_admin_password(pw: str) -> bool:
    import hmac
    a = get_admin_auth()
    if not a["hash"] or not a["salt"]:
        return False
    try:
        return hmac.compare_digest(_hash_pw(pw, a["salt"]), a["hash"])
    except ValueError:
        # 盐被写坏（非十六进制）→ 一律拒绝。绝不让它变成 500（登录端点必须只回"口令错误"）。
        return False


def set_admin_auth_required(required: bool) -> None:
    """开关口令门（D13）。关掉之后任何人点「管理层」都能进，UI 必须显示警示。"""
    _set_setting_raw("admin_auth_required", "1" if required else "0")


# ---------------------------------------------------------------- 地图标记索引缓存
# ⚠️ 红线（规格 §4 第 2 条）：本组函数**只服务于单向同步（文件 → SQLite）**。
# 业务代码不允许绕过 maptags.sync_map() 直接改 destinations/zones —— 那会重演"两套真相"事故。
def get_map_tags_manifest(map_name: str) -> dict | None:
    """读某图的缓存清单（含文件 mtime/size/sha1 与指纹）。"""
    conn = _conn()
    try:
        r = conn.execute("SELECT * FROM map_tags_manifest WHERE map_name=?",
                         (map_name,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["warnings"] = json.loads(d.pop("warnings_json") or "[]")
        try:
            d["origin"] = json.loads(d.pop("origin_json") or "null")
        except ValueError:
            d["origin"] = None
        return d
    finally:
        conn.close()


def replace_map_tags(map_name: str, manifest: dict, destinations: list[dict],
                     zones: list[dict]) -> None:
    """**一个事务内**整图重建缓存：DELETE 本图所有行 + 批量 INSERT + upsert 清单。

    这是 maptags.sync_map() 的落库出口；失败则整图回滚，绝不留下半套缓存。
    """
    with _lock:
        conn = _conn()
        try:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM destinations WHERE map_name=?", (map_name,))
            conn.execute("DELETE FROM zones WHERE map_name=?", (map_name,))
            for d in destinations:
                conn.execute(
                    "INSERT INTO destinations (uid,map_name,name,aliases,x,y,yaw_deg,risk,"
                    "elder_allowed,note,learned_by,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (d.get("uid", ""), map_name, d.get("name", ""), d.get("aliases", ""),
                     float(d.get("x") or 0), float(d.get("y") or 0),
                     # ★ yaw_deg 必须保留 None：JSON 里"没有该字段"= 不限制朝向，
                     #   写成 0 会让前端显示"0°"并画出一条朝正 x 的箭头（会误导操作）。
                     None if d.get("yaw_deg") is None else float(d["yaw_deg"]),
                     d.get("risk", "low"), int(d.get("elder_allowed", 1) or 0),
                     d.get("note", ""), d.get("learned_by", ""),
                     d.get("created_at", ""), d.get("updated_at", "")))
            for z in zones:
                conn.execute(
                    "INSERT INTO zones (uid,map_name,name,kind,shape,polygon_json,parent,note,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (z.get("uid", ""), map_name, z.get("name", ""), z.get("kind", "room"),
                     z.get("shape", "polygon"), z.get("polygon_json", "[]"),
                     z.get("parent", ""), z.get("note", ""),
                     z.get("created_at", ""), z.get("updated_at", "")))
            conn.execute(
                "INSERT INTO map_tags_manifest (map_name,file_mtime,file_size,sha1,resolution,"
                "origin_json,synced_at,warnings_json) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(map_name) DO UPDATE SET file_mtime=excluded.file_mtime,"
                "file_size=excluded.file_size,sha1=excluded.sha1,resolution=excluded.resolution,"
                "origin_json=excluded.origin_json,synced_at=excluded.synced_at,"
                "warnings_json=excluded.warnings_json",
                (map_name, float(manifest.get("file_mtime") or 0),
                 int(manifest.get("file_size") or 0), manifest.get("sha1", ""),
                 manifest.get("resolution"),
                 json.dumps(manifest.get("origin"), ensure_ascii=False),
                 now_iso(), json.dumps(manifest.get("warnings") or [], ensure_ascii=False)))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def drop_map_tags(map_name: str) -> None:
    """清空某图的缓存（geo 文件被删时用；缓存丢了不影响任何数据）。"""
    with _lock:
        conn = _conn()
        try:
            conn.execute("DELETE FROM destinations WHERE map_name=?", (map_name,))
            conn.execute("DELETE FROM zones WHERE map_name=?", (map_name,))
            conn.execute("DELETE FROM map_tags_manifest WHERE map_name=?", (map_name,))
            conn.commit()
        finally:
            conn.close()


def clear_all_map_tags() -> int:
    """清空全部标记缓存并返回被清行数 —— 验收「红线 3：缓存可丢弃可重建」用。"""
    with _lock:
        conn = _conn()
        try:
            n = 0
            for t in ("destinations", "zones", "map_tags_manifest"):
                n += conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
                conn.execute(f"DELETE FROM {t}")
            conn.commit()
            return n
        finally:
            conn.close()


def list_destinations(map_name: str = "", uid: str = "") -> list[dict]:
    """查缓存里的地点（只读；权威结论仍以 tags.json 为准，调用方应先 sync）。"""
    sql = "SELECT * FROM destinations"
    where, args = [], []
    if map_name:
        where.append("map_name=?")
        args.append(map_name)
    if uid:
        where.append("uid=?")
        args.append(uid)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY name"
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def get_destination(uid: str) -> dict | None:
    conn = _conn()
    try:
        r = conn.execute("SELECT * FROM destinations WHERE uid=?", (uid,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def list_zones(map_name: str = "", uid: str = "", kind: str = "") -> list[dict]:
    sql = "SELECT * FROM zones"
    where, args = [], []
    if map_name:
        where.append("map_name=?")
        args.append(map_name)
    if uid:
        where.append("uid=?")
        args.append(uid)
    if kind:
        where.append("kind=?")
        args.append(kind)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY name"
    conn = _conn()
    try:
        out = []
        for r in conn.execute(sql, args).fetchall():
            d = dict(r)
            try:
                d["polygon"] = json.loads(d.pop("polygon_json") or "[]")
            except ValueError:
                d["polygon"] = []
            out.append(d)
        return out
    finally:
        conn.close()


def get_zone(uid: str, map_name: str = "") -> dict | None:
    """按 uid（+ 可选地图名）取区域行。

    **强烈建议带上 map_name**：uid（`z1`）只在单张图内稳定，三张图各有自己的 `z1`，
    只按 uid 查会跨图串（复合主键是 `(map_name, uid)`）。
    """
    rows = list_zones(map_name=map_name, uid=uid)
    return rows[0] if rows else None


def count_map_tags(map_name: str) -> dict:
    """某图的地点/区域数量（列表页用；只读缓存）。"""
    conn = _conn()
    try:
        d = conn.execute("SELECT COUNT(*) AS c FROM destinations WHERE map_name=?",
                         (map_name,)).fetchone()["c"]
        z = conn.execute("SELECT COUNT(*) AS c FROM zones WHERE map_name=?",
                         (map_name,)).fetchone()["c"]
        return {"destinations": d, "zones": z}
    finally:
        conn.close()


# ---------------------------------------------------------------- notifications（通知中心）
# 排序口径统一：**未处理优先，再按 created_at 倒序**（同秒内用 id 兜底，保证稳定分页）。
_NOTIFY_ORDER = "ORDER BY (ack_at='') DESC, created_at DESC, id DESC"
_NOTIFY_LIST_MAX = 200          # list_notifications 的 limit 硬上限


def add_notification(level, source, ntype, uid="", title="", body="", ref="", ts="") -> int:
    """新增一条通知，返回 id。`ts` 空则用服务器时间（created_at 与 last_at 同值）。"""
    ts = ts or now_iso()
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                """INSERT INTO notifications
                   (level,source,type,uid,title,body,ref,count,created_at,last_at,ack_at,ack_by)
                   VALUES (?,?,?,?,?,?,?,1,?,?,'','')""",
                (level or "info", source or "", ntype, uid or "", title or "",
                 body or "", ref or "", ts, ts),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def get_notification(nid: int) -> dict | None:
    conn = _conn()
    try:
        r = conn.execute("SELECT * FROM notifications WHERE id=?", (nid,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def find_unacked_notification(source: str, ntype: str, uid: str, since_iso: str,
                              body: str | None = None) -> dict | None:
    """查去重合并目标：同 (source,type,uid,**正文**) 且**未处理**、`last_at >= since_iso` 的最新一条。

    只认未处理的（已 ack 的通知不许被新事件并入 —— 那会把护士刚处理完的条目又翻成未读）。

    **正文是合并键的一部分**（2026-09-18 加，规格
    `docs/superpowers/specs/2026-09-18-llm-notify-nurse-mcp-design.md` M1）：只按
    (source,type,uid) 合并时，同一分钟内「张爷爷想喝水」与「李奶奶摔倒了」会并成一条，而合并
    只 `count+1`、**保留最早那条的正文与级别** ⇒ 护士台列表最终只剩「想喝水」、critical 计数也
    不涨，紧急事件被静默降级。正文参与合并键后，合并 = 真正的"同一件事重复上报"。
    `body=None` 保留旧口径（只给迁移/测试用，业务调用一律传正文）。
    """
    sql = ("""SELECT * FROM notifications
              WHERE source=? AND type=? AND uid=? AND ack_at='' AND last_at>=? """)
    args: list = [source or "", ntype, uid or "", since_iso]
    if body is not None:
        sql += "AND body=? "
        args.append(body or "")
    sql += f"{_NOTIFY_ORDER} LIMIT 1"
    conn = _conn()
    try:
        r = conn.execute(sql, tuple(args)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def bump_notification(nid: int, ts: str, level: str = "") -> int:
    """合并：count+1、last_at=ts、必要时提级别（**只升不降**）；**不动 body/title**（保留最早原文）。

    `level` 传空串 = 不改级别；非空则覆盖。**由调用方算出"两者中更紧急的那个"**：级别词表住在
    agent 层（`notify.LEVELS`），store 层不认识它，故不在这里做比较。
    升级而非"保留最早级别"的原因同上：同正文的同一件事由 info 改报 critical（"老人摔倒了"先当
    一般通知、后确认是跌倒）时，降级会把红卡与蜂鸣一起抹掉。

    **`AND ack_at=''` 是竞态守卫**：`find_unacked_notification` 与本事不在同一临界区，
    护士可能正好在这两步之间 ack 掉该行。此时 rowcount=0，调用方（`notify.ingest`）
    **必须回落到新增一行** —— 否则新上报会被并进"已处理"行，既不出未读卡、不响蜂鸣、
    角标也不变，等于静默丢失。
    """
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "UPDATE notifications SET count=count+1, last_at=?, "
                "level=CASE WHEN ?='' THEN level ELSE ? END "
                "WHERE id=? AND ack_at=''",
                (ts, level or "", level or "", nid))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def list_notifications(state: str = "all", limit: int = 50, before_id: int = 0) -> list[dict]:
    """通知列表。`state="unread"` 只回未处理；`before_id` 分页（id < before_id）。"""
    sql = "SELECT * FROM notifications"
    where, args = [], []
    if state == "unread":
        where.append("ack_at=''")
    if before_id:
        where.append("id<?")
        args.append(before_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    limit = max(0, min(int(limit or 0), _NOTIFY_LIST_MAX))
    sql += f" {_NOTIFY_ORDER} LIMIT ?"
    args.append(limit)
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def notification_counts() -> dict:
    """角标用计数：`unread`=未处理总数，`critical`=未处理里的 critical 条数。"""
    conn = _conn()
    try:
        unread = conn.execute("SELECT COUNT(*) AS c FROM notifications WHERE ack_at=''").fetchone()["c"]
        crit = conn.execute(
            "SELECT COUNT(*) AS c FROM notifications WHERE ack_at='' AND level='critical'"
        ).fetchone()["c"]
        return {"unread": unread, "critical": crit}
    finally:
        conn.close()


def ack_notification(nid: int, by: str = "admin") -> int:
    """确认一条通知，返回受影响行数（0 = 不存在或已确认过）。"""
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "UPDATE notifications SET ack_at=?, ack_by=? WHERE id=? AND ack_at=''",
                (now_iso(), by or "admin", nid))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def ack_all_notifications(by: str = "admin") -> int:
    """确认全部未处理通知，返回条数。"""
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute("UPDATE notifications SET ack_at=?, ack_by=? WHERE ack_at=''",
                               (now_iso(), by or "admin"))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def delete_notification(nid: int) -> int:
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute("DELETE FROM notifications WHERE id=?", (nid,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def prune_notifications(before_iso: str) -> int:
    """清老通知：**只删已处理（ack_at != ''）且 last_at < before_iso 的行**，返回删除行数。

    未处理的老通知必须留着 —— 没被护士看过的告警不许因为"放太久"自己消失。
    """
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "DELETE FROM notifications WHERE ack_at!='' AND last_at<?", (before_iso,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
