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


def _mk_client(judge_reply):
    class C:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    class R:
                        choices = [type("Ch", (), {"message": type("M", (), {"content": judge_reply})()})()]
                    return R()
    return C()


def test_instant_correct_updates_core(tmp_path, isolated_paths, monkeypatch):
    from LLM import db, memory
    db.init_db()
    mid = db.add_core_memory("elder_001", "fact", "老人姓张", importance=4)
    monkeypatch.setattr("LLM.chat.llm_json",
                        lambda c, m, p: {"correct": True, "mid": mid, "new_content": "老人姓王"})
    r = memory.correct_instant("elder_001", "我其实不姓张，我姓王", _mk_client("{}"), "test-model")
    assert r["corrected"] is True
    assert db.get_core_memory(mid)["content"] == "老人姓王"


def test_instant_correct_blocks_identity(tmp_path, isolated_paths, monkeypatch):
    from LLM import db, memory
    db.init_db()
    mid = db.add_core_memory("elder_001", "fact", "老人喜欢戏曲", importance=4)
    monkeypatch.setattr("LLM.chat.llm_json",
                        lambda c, m, p: {"correct": True, "mid": mid, "new_content": "姓名是李四"})
    r = memory.correct_instant("elder_001", "我其实姓李", _mk_client("{}"), "test-model")
    assert r["corrected"] is False
    assert db.get_core_memory(mid)["content"] == "老人喜欢戏曲"


def test_instant_correct_blocks_cross_user(tmp_path, isolated_paths, monkeypatch):
    from LLM import db, memory
    db.init_db()
    mid = db.add_core_memory("elder_001", "fact", "老人姓张", importance=4)
    monkeypatch.setattr("LLM.chat.llm_json",
                        lambda c, m, p: {"correct": True, "mid": mid, "new_content": "老人姓王"})
    r = memory.correct_instant("elder_002", "我其实姓王", None, "test-model")
    assert r["corrected"] is False
    assert db.get_core_memory(mid)["content"] == "老人姓张"


def test_recall_v3_returns_core_and_rag(tmp_path, isolated_paths, monkeypatch):
    from LLM import db, memory
    db.init_db()
    db.add_core_memory("elder_001", "preference", "喜欢听京剧", importance=5)
    monkeypatch.setattr(memory.ragstore, "query",
                        lambda uid, q, top_k: [{"content": "上周感冒已好转", "meta": {}}])
    monkeypatch.setattr(memory.graph, "one_hop", lambda eid: [])
    r = memory.recall_v3("elder_001", "想听戏")
    assert "喜欢听京剧" in r["context"]
    assert "感冒" in r["context"]


CONSOLIDATE_JSON = '''
{"entries": [
  {"type":"episodic","content":"上周感冒已好转","importance":2},
  {"type":"preference","content":"喜欢听京剧","importance":4}
 ],
 "relations": [{"src":"张建国","stype":"person","rel":"likes","dst":"京剧","dtype":"topic"}],
 "digest":"聊了身体恢复和京剧爱好",
 "portrait":"喜欢京剧，身体在恢复"}
'''


def test_consolidate_v3_routes_and_graph(tmp_path, isolated_paths, monkeypatch):
    from LLM import db, memory
    db.init_db()
    monkeypatch.setattr("LLM.chat.llm_json", lambda c, m, p: __import__("json").loads(CONSOLIDATE_JSON))
    monkeypatch.setattr(memory, "_take_pending", lambda uid: [{"role": "user", "content": "我好了，喜欢京剧"}])
    monkeypatch.setattr(memory, "_dedup_check", lambda uid, c, **kw: None)
    monkeypatch.setattr(memory.graph, "upsert_entity", lambda *a: None)
    monkeypatch.setattr(memory.graph, "upsert_relation", lambda *a: None)
    monkeypatch.setattr(memory.ragstore, "add", lambda uid, t, c, **kw: None)
    r = memory.consolidate("elder_001", None, "test-model")
    assert r["ok"] is True
    assert any(m["content"] == "喜欢听京剧" for m in db.list_core_memories("elder_001"))


def test_apply_v3_correct_updates_old(tmp_path, isolated_paths, monkeypatch):
    from LLM import db, memory
    db.init_db()
    mid = db.add_core_memory("elder_001", "fact", "老人姓张", importance=4)
    r = memory._apply_v3("elder_001", {"action": "correct", "correct_id": mid, "content": "老人姓王"})
    assert r["route"] == "correct"
    assert db.get_core_memory(mid)["content"] == "老人姓王"


def test_apply_v3_correct_blocks_identity(tmp_path, isolated_paths, monkeypatch):
    from LLM import db, memory
    db.init_db()
    mid = db.add_core_memory("elder_001", "fact", "老人喜欢戏曲", importance=4)
    r = memory._apply_v3("elder_001", {"action": "correct", "correct_id": mid, "content": "姓名是李四"})
    assert r["route"] == "reject"
    assert db.get_core_memory(mid)["content"] == "老人喜欢戏曲"
