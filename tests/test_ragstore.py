# -*- coding: utf-8 -*-
"""ChromaDB 封装测试：add/query + SQLite 镜像 + 降级。"""
import pytest
from LLM import ragstore, db


@pytest.fixture
def rs(tmp_path, monkeypatch):
    monkeypatch.setattr(ragstore, "_PATH", str(tmp_path / "chroma"))
    ragstore._init()
    if not ragstore._AVAILABLE:
        pytest.skip("chromadb 未安装")
    return ragstore


def test_add_and_query(rs, isolated_paths):
    db.init_db()
    rs.add("elder_001", "episodic", "上周感冒已好转", source="llm")
    hits = rs.query("elder_001", "感冒好了吗", top_k=3)
    assert any("感冒" in h["content"] for h in hits)


def test_mirror_row_created(rs, isolated_paths):
    db.init_db()
    rs.add("elder_001", "semantic", "喜欢京剧", source="llm")
    rows = db.list_rag_memories("elder_001")
    assert any(r["content"] == "喜欢京剧" for r in rows)


def test_degraded_query_empty(monkeypatch):
    monkeypatch.setattr(ragstore, "_AVAILABLE", False)
    assert ragstore.query("elder_001", "任意", top_k=3) == []
