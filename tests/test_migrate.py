# -*- coding: utf-8 -*-
"""db v3 表 CRUD 测试。"""
from LLM import db


def test_core_memories_crud(tmp_path, isolated_paths):
    db.init_db()
    mid = db.add_core_memory("elder_001", "preference", "喜欢听京剧", importance=4)
    rows = db.list_core_memories("elder_001")
    assert any(r["id"] == mid and r["content"] == "喜欢听京剧" for r in rows)


def test_rag_memories_crud(tmp_path, isolated_paths):
    db.init_db()
    rid = db.add_rag_memory("elder_001", "episodic", "上周感冒已好转", chroma_id="c1")
    rows = db.list_rag_memories("elder_001")
    assert any(r["chroma_id"] == "c1" for r in rows)


def test_profile_has_identity_columns(tmp_path, isolated_paths):
    db.init_db()
    db.upsert_profile("elder_001", name="张建国", gender="男", birthday="1948-03-02")
    p = db.get_profile("elder_001")
    assert p["gender"] == "男"
    assert p["birthday"] == "1948-03-02"


def test_core_memory_update_delete(tmp_path, isolated_paths):
    db.init_db()
    mid = db.add_core_memory("elder_001", "preference", "喜欢听京剧", importance=4)
    db.update_core_memory(mid, content="改后内容", importance=5)
    rows = db.list_core_memories("elder_001")
    assert any(r["id"] == mid and r["content"] == "改后内容" and r["importance"] == 5 for r in rows)
    db.delete_core_memory(mid)
    assert all(r["id"] != mid for r in db.list_core_memories("elder_001"))
