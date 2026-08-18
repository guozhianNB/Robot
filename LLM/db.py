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
"""


def _conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with _lock:
        conn = _conn()
        try:
            conn.executescript(SCHEMA)
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


def list_profiles() -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute("SELECT * FROM profiles ORDER BY uid").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["profile"] = json.loads(d.pop("profile_json") or "{}")
            d["preferences"] = json.loads(d.pop("preferences_json") or "{}")
            out.append(d)
        return out
    finally:
        conn.close()


def upsert_profile(uid: str, name="", nickname="", bed="", age=0,
                   profile=None, style="", preferences=None, notes="") -> dict:
    profile = profile or {}
    preferences = preferences or {}
    ts = now_iso()
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                """INSERT INTO profiles (uid,name,nickname,bed,age,profile_json,style,preferences_json,notes,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(uid) DO UPDATE SET
                     name=excluded.name, nickname=excluded.nickname, bed=excluded.bed, age=excluded.age,
                     profile_json=excluded.profile_json, style=excluded.style,
                     preferences_json=excluded.preferences_json, notes=excluded.notes, updated_at=excluded.updated_at""",
                (uid, name, nickname, bed, age,
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


def list_memories(uid: str = None, status: str = None) -> list[dict]:
    sql = "SELECT * FROM memories WHERE 1=1"
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


def delete_memory(mid: int) -> None:
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
    from .conf import DEFAULT_SETTINGS
    out = dict(DEFAULT_SETTINGS)
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
            out[r["key"]] = v
        return out
    finally:
        conn.close()


def set_settings(patch: dict) -> dict:
    from .conf import DEFAULT_SETTINGS
    cur = get_settings()
    cur.update({k: v for k, v in patch.items() if k in DEFAULT_SETTINGS})
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
