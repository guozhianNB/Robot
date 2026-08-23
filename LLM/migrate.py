# -*- coding: utf-8 -*-
r"""一次性幂等迁移：旧 memories/portraits/summaries/style/preferences → v3 分层。
用 settings 里的 migrate_done 标记保证只跑一次；写入核心记忆前去重，
即使标记未落盘（中途崩溃）也不会产生重复数据。"""
from . import db
from . import ragstore
from . import log as audit


def _core_exists(uid: str, mtype: str, content: str) -> bool:
    return any(m["type"] == mtype and m["content"] == content
               for m in db.list_core_memories(uid))


def _rag_exists(uid: str, content: str) -> bool:
    return any(m["content"] == content for m in db.list_rag_memories(uid))


def run() -> dict:
    settings = db.get_settings()
    if settings.get("migrate_done"):
        return {"ok": True, "skipped": True}
    migrated = {"core": 0, "rag": 0}
    try:
        for m in db.list_memories():
            if m.get("status") != "confirmed":
                continue
            if m.get("expires_at") and m["expires_at"] < db.now_iso():
                continue
            mtype = m["type"]
            content = m["content"]
            if mtype in ("preference", "fact", "relation", "persona", "style"):
                if _core_exists(m["uid"], mtype, content):
                    continue
                db.add_core_memory(m["uid"], mtype, content, importance=3, source="migrate")
                migrated["core"] += 1
            else:  # event → episodic
                if not _rag_exists(m["uid"], content):
                    ragstore.add(m["uid"], "episodic", content, source="migrate")
                    migrated["rag"] += 1
        for uid in _all_uids():
            portrait = db.get_portrait(uid)
            if portrait and not _core_exists(uid, "persona", portrait):
                db.add_core_memory(uid, "persona", portrait, importance=5, source="migrate")
                migrated["core"] += 1
            summary = db.get_summary(uid)
            if summary and not _rag_exists(uid, summary):
                ragstore.add(uid, "episodic", summary, source="migrate")
                migrated["rag"] += 1
        db.set_settings({"migrate_done": True})
    except Exception as e:
        audit.log("memory_change", action="migrate_error", error=str(e))
        return {"ok": False, "error": str(e)}
    audit.log("memory_change", action="migrate", **migrated)
    return {"ok": True, **migrated}


def _all_uids() -> list[str]:
    return [p["uid"] for p in db.list_profiles()]
