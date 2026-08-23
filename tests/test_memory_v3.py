# -*- coding: utf-8 -*-
"""memory v3 写回分流 + 红线测试（不依赖真实 LLM，直接调 _apply_v3）。"""
from LLM import db, memory


def test_semantic_goes_to_rag(monkeypatch, isolated_paths):
    db.init_db()
    added = {}
    monkeypatch.setattr(memory.ragstore, "add",
                        lambda uid, t, c, **kw: added.setdefault("rag", []).append(c))
    r = memory._apply_v3("elder_001", {"type": "semantic", "content": "喜欢京剧", "importance": 2})
    assert r["route"] == "rag"
    assert added["rag"] == ["喜欢京剧"]


def test_high_importance_core_goes_to_core(monkeypatch, isolated_paths):
    db.init_db()
    r = memory._apply_v3("elder_001", {"type": "preference", "content": "喜欢听京剧", "importance": 4})
    assert r["route"] == "core"
    assert any(m["content"] == "喜欢听京剧" for m in db.list_core_memories("elder_001"))


def test_low_importance_core_downgrades_to_rag(monkeypatch, isolated_paths):
    db.init_db()
    added = []
    monkeypatch.setattr(memory.ragstore, "add", lambda uid, t, c, **kw: added.append(c))
    r = memory._apply_v3("elder_001", {"type": "preference", "content": "爱吃甜的", "importance": 1})
    assert r["route"] == "rag"
    assert added == ["爱吃甜的"]


def test_medical_rejected(monkeypatch, isolated_paths):
    db.init_db()
    r = memory._apply_v3("elder_001", {"type": "fact", "content": "每天吃两片降压药", "importance": 5})
    assert r["route"] == "reject"


def test_identity_rejected(monkeypatch, isolated_paths):
    db.init_db()
    r = memory._apply_v3("elder_001", {"type": "fact", "content": "他的姓名是王五", "importance": 5})
    assert r["route"] == "reject"
