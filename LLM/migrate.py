# -*- coding: utf-8 -*-
r"""一次性幂等迁移：旧 memories/portraits/summaries/style/preferences → v3 分层。
用 settings 里的 migrate_done 标记保证只跑一次。"""
from . import db
from . import ragstore
from . import log as audit


def run() -> dict:
    settings = db.get_settings()
    if settings.get("migrate_done"):
        return {"ok": True, "skipped": True}
    migrated = {"core": 0, "rag": 0}
    for m in db.list_memories():
        if m.get("status") != "confirmed":
            continue
        if m.get("expires_at") and m["expires_at"] < db.now_iso():
            continue
        mtype = m["type"]
        content = m["content"]
        if mtype in ("preference", "fact", "relation", "persona", "style"):
            db.add_core_memory(m["uid"], mtype, content, importance=3, source="migrate")
            migrated["core"] += 1
        else:  # event → episodic
            ragstore.add(m["uid"], "episodic", content, source="migrate")
            migrated["rag"] += 1
    for uid in _all_uids():
        portrait = db.get_portrait(uid)
        if portrait:
            db.add_core_memory(uid, "persona", portrait, importance=5, source="migrate")
            migrated["core"] += 1
        summary = db.get_summary(uid)
        if summary:
            ragstore.add(uid, "episodic", summary, source="migrate")
            migrated["rag"] += 1
    db.set_settings({"migrate_done": True})
    audit.log("memory_change", action="migrate", **migrated)
    return {"ok": True, **migrated}


def _all_uids() -> list[str]:
    return [p["uid"] for p in db.list_profiles()]
