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

from .conf import DB_PATH, HISTORY_WINDOW

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
CREATE TABLE IF NOT EXISTS zones (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  map_name TEXT NOT NULL,               -- 地点绑定的地图名（三张图坐标系不通用，必须绑）
  name TEXT NOT NULL,                   -- 标准名，如 "101"
  kind TEXT DEFAULT 'room',             -- room | ward | bed | other
  shape TEXT DEFAULT 'polygon',         -- polygon | rect
  polygon_json TEXT DEFAULT '[]',       -- [[x,y], ...] 米坐标（世界系，非像素）
  parent_id INTEGER DEFAULT 0,          -- 上级区域（病房→床位）；0=无
  note TEXT DEFAULT '',
  created_at TEXT, updated_at TEXT,
  UNIQUE(map_name, name)
);
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
                # 分层用户体系：kind=elder|ward；ward_id=老人所属病房；zone_id=关联的 zones.id（0=未关联）
                "kind": "kind TEXT DEFAULT 'elder'",
                "ward_id": "ward_id TEXT DEFAULT ''",
                "zone_id": "zone_id INTEGER DEFAULT 0",
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
            conn.commit()
        finally:
            conn.close()


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


def list_profiles(kind: str | None = None) -> list[dict]:
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
    """唯一权威的角色判定依据（R1）：admin 不在 profiles 里 → 返回 ''。"""
    conn = _conn()
    try:
        row = conn.execute("SELECT kind FROM profiles WHERE uid=?", (uid,)).fetchone()
        return (row["kind"] or "elder") if row else ""
    finally:
        conn.close()


def upsert_profile(uid: str, name="", nickname="", bed="", age=0,
                   profile=None, style="", preferences=None, notes="",
                   gender="", birthday="", kind="elder", zone_id=0) -> dict:
    profile = profile or {}
    preferences = preferences or {}
    ts = now_iso()
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                """INSERT INTO profiles (uid,name,nickname,bed,age,gender,birthday,profile_json,style,preferences_json,notes,kind,zone_id,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(uid) DO UPDATE SET
                     name=excluded.name, nickname=excluded.nickname, bed=excluded.bed, age=excluded.age,
                     gender=excluded.gender, birthday=excluded.birthday,
                     profile_json=excluded.profile_json, style=excluded.style,
                     preferences_json=excluded.preferences_json, notes=excluded.notes,
                     kind=COALESCE(profiles.kind, excluded.kind),
                     zone_id=excluded.zone_id,
                     updated_at=excluded.updated_at""",
                (uid, name, nickname, bed, age, gender, birthday,
                 json.dumps(profile, ensure_ascii=False),
                 style,
                 json.dumps(preferences, ensure_ascii=False),
                 notes, kind, int(zone_id or 0), ts, ts),
            )
            conn.commit()
        finally:
            conn.close()
    return get_profile(uid)


def upsert_ward(uid: str, name: str = "", zone_id: int = 0) -> dict:
    """新建/更新病房 profile；病房几何不在这里存，只存关联的 zones.id（唯一真相在 zones）。"""
    upsert_profile(uid, name=name, kind="ward", zone_id=int(zone_id or 0))
    return get_profile(uid) or {}


def set_profile_ward(uid: str, ward_id: str) -> None:
    with _lock:
        conn = _conn()
        try:
            conn.execute("UPDATE profiles SET ward_id=?, updated_at=? WHERE uid=?",
                         (ward_id, now_iso(), uid))
            conn.commit()
        finally:
            conn.close()


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
    from . import log as audit
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


# ---- 病房区域最小集（zones 表 = 唯一真相；与一期《地图编辑器》任务 1 同一份实现）----
# 依赖说明：一期任务 1 也会实现这些函数，先落地者建表、后落地者复用
# （CREATE TABLE IF NOT EXISTS 幂等）；与 locator.py / mapserver.py 同一约定。
# 降级（D17 fail-safe）：表不存在 / 无 kind='ward' 区域 / zone_id=0 → 一律返回空，绝不抛异常。

def add_zone(map_name: str, name: str, kind: str = "room", shape: str = "polygon",
             polygon_json: list | None = None, parent_id: int = 0, note: str = "") -> int:
    """写入一条区域记录并返回其 id；同 (map_name, name) 已存在则覆盖几何（幂等，适配 UNIQUE）。"""
    with _lock:
        conn = _conn()
        try:
            ts = now_iso()
            cur = conn.execute(
                "INSERT INTO zones (map_name,name,kind,shape,polygon_json,parent_id,note,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(map_name, name) DO UPDATE SET kind=excluded.kind, "
                "shape=excluded.shape, polygon_json=excluded.polygon_json, "
                "parent_id=excluded.parent_id, note=excluded.note, updated_at=excluded.updated_at",
                (map_name, name, kind, shape,
                 json.dumps(polygon_json or [], ensure_ascii=False),
                 int(parent_id or 0), note or "", ts, ts))
            conn.commit()
            if cur.lastrowid:
                return int(cur.lastrowid)
            row = conn.execute("SELECT id FROM zones WHERE map_name=? AND name=?",
                               (map_name, name)).fetchone()
            return int(row["id"]) if row else 0
        finally:
            conn.close()


def get_zone(zone_id: int) -> dict | None:
    """按主键查 zones 行（唯一真相）；不存在或表未建 → None。"""
    try:
        conn = _conn()
        try:
            row = conn.execute("SELECT * FROM zones WHERE id=?", (int(zone_id or 0),)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception:                        # 表不存在等 → 降级
        return None


def list_zones(map_name: str | None = None, kind: str | None = None) -> list[dict]:
    """按地图名 / 类型列出区域；表未建 → []（降级）。"""
    conds, args = [], []
    if map_name:
        conds.append("map_name=?")
        args.append(map_name)
    if kind:
        conds.append("kind=?")
        args.append(kind)
    sql = "SELECT * FROM zones"
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY id"
    try:
        conn = _conn()
        try:
            return [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]
        finally:
            conn.close()
    except Exception:                        # 表不存在等 → 降级
        return []


def _point_in_polygon(x: float, y: float, poly: list) -> bool:
    """射线法：点是否在多边形内（poly=[[x,y], ...] 米坐标）。"""
    inside, n = False, len(poly)
    for i in range(n):
        x1, y1 = float(poly[i][0]), float(poly[i][1])
        x2, y2 = float(poly[(i + 1) % n][0]), float(poly[(i + 1) % n][1])
        if (y1 > y) != (y2 > y):
            if x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                inside = not inside
    return inside


def zone_contains_point(map_name: str, x: float, y: float, kind: str | None = None) -> list[dict]:
    """点落在哪些区域里：polygon 用射线法；rect 用 polygon_json 的**外接矩形**。

    降级（D17）：表不存在 / 该地图没有区域 / polygon_json 为空 → []，
    调用方据此保持手动选病房，绝不误判、绝不阻塞对话。
    """
    out = []
    for z in list_zones(map_name=map_name, kind=kind):
        try:
            poly = json.loads(z.get("polygon_json") or "[]")
        except (TypeError, ValueError):
            poly = []
        if not poly:
            continue
        try:
            if str(z.get("shape") or "polygon") == "rect":
                xs = [float(p[0]) for p in poly]
                ys = [float(p[1]) for p in poly]
                if min(xs) <= x <= max(xs) and min(ys) <= y <= max(ys):
                    out.append(z)
            elif _point_in_polygon(x, y, poly):
                out.append(z)
        except (TypeError, ValueError, KeyError, IndexError):
            continue        # 几何数据损坏/坐标非数值 → 视为不在区域内（D17 fail-safe，绝不阻塞对话）
    return out


def get_ward_zone(ward_uid: str) -> dict | None:
    """取该病房 profile 的 zone_id，再查 zones 表（病房几何的唯一真相在 zones）。

    zone_id=0（未关联）或 zones 行缺失 → None，调用方按"拿不到病房区域"降级处理。
    """
    p = get_profile(ward_uid) or {}
    zid = int(p.get("zone_id") or 0)
    return get_zone(zid) if zid else None


def list_wards_with_zone() -> list[dict]:
    """列出**已关联区域**的病房：{"uid","name","zone_id","zone"}；未关联的病房不返回。"""
    out = []
    for w in list_profiles(kind="ward"):
        zid = int(w.get("zone_id") or 0)
        zone = get_zone(zid) if zid else None
        if zone:
            out.append({"uid": w["uid"], "name": w.get("name", ""), "zone_id": zid, "zone": zone})
    return out


# ---------------------------------------------------------------- settings
def get_settings() -> dict:
    from .conf import DEFAULT_SETTINGS
    from .tools import TOOL_DEFAULTS
    out = dict(DEFAULT_SETTINGS)
    out.update(TOOL_DEFAULTS)   # per-tool 开关默认值（已保存的随后覆盖）
    conn = _conn()
    try:
        for r in conn.execute("SELECT key,value FROM settings").fetchall():
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
    from .conf import DEFAULT_SETTINGS
    from .tools import TOOL_DEFAULTS
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


# ---- 管理员口令（PBKDF2，绝不落明文）----
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
    return {
        "required": _get_setting_raw("admin_auth_required", "1").lower() in ("1", "true", "yes"),
        "hash": _get_setting_raw("admin_password_hash"),
        "salt": _get_setting_raw("admin_password_salt"),
    }


def set_admin_password(pw: str) -> None:
    import os
    salt = os.urandom(16).hex()
    _set_setting_raw("admin_password_salt", salt)
    _set_setting_raw("admin_password_hash", _hash_pw(pw, salt))


def verify_admin_password(pw: str) -> bool:
    a = get_admin_auth()
    if not a["hash"] or not a["salt"]:
        return False
    return _hash_pw(pw, a["salt"]) == a["hash"]


def set_admin_auth_required(required: bool) -> None:
    _set_setting_raw("admin_auth_required", "1" if required else "0")
