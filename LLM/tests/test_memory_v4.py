# -*- coding: utf-8 -*-
"""P0-P3 记忆增强核心链路测试（临时库隔离，不触碰真实 brain.db）。"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from LLM.store import db  # noqa: E402


@pytest.fixture()
def d():
    from LLM.store import db as _db
    tmp = tempfile.mkdtemp()
    old = _db.DB_PATH
    _db.DB_PATH = os.path.join(tmp, "t.db")
    _db.init_db()
    yield _db
    _db.DB_PATH = old


def test_external_claim_idempotent(d):
    assert d.claim_external("e1", "x1", "rag:episodic") is True
    assert d.claim_external("e1", "x1", "rag:episodic") is False
    assert d.claim_external("e2", "x1") is True  # 不同 uid 独立


def test_rag_external_column(d):
    rid = d.add_rag_memory("e1", "episodic", "hello", "cid1", external_id="x1")
    got = d.get_rag_by_external("e1", "x1")
    assert got and got["id"] == rid


def test_soft_delete_restore_cycle(d):
    mid = d.add_memory("e1", "fact", "内容甲", "confirmed")
    op = d.delete_memory(mid, uid="e1", reason="reject", by="nurse")
    assert op > 0
    assert d.list_memories(uid="e1", status="confirmed") == []  # 软删后查询不可见
    ops = d.list_delete_operations(uid="e1")
    assert len(ops) == 1 and ops[0]["target_id"] == mid
    assert d.restore_operation(op) is True
    assert len(d.list_memories(uid="e1", status="confirmed")) == 1


def test_rag_soft_delete(d):
    rid = d.add_rag_memory("e1", "semantic", "vec", "cid2")
    op = d.delete_rag_memory(rid, uid="e1")
    assert op > 0 and d.list_rag_memories("e1") == []
    assert d.restore_operation(op) is True
    assert len(d.list_rag_memories("e1")) == 1


def test_authority_inference(d):
    m1 = d.add_core_memory("e1", "fact", "A", "", "", source="llm:consolidate")
    m2 = d.add_core_memory("e1", "fact", "B", "", "", source="manual:nurse")
    assert d.get_core_memory(m1)["authority"] == "llm"
    assert d.get_core_memory(m2)["authority"] == "nurse"
    d.set_core_authority(m1, "nurse")
    assert d.get_core_memory(m1)["authority"] == "nurse"


def test_correct_from_feedback(d):
    from LLM.store import ragstore as rs
    rs._AVAILABLE = False  # 降级镜像路径，不触网
    from LLM.agent import memory as rag
    d.add_core_memory("e1", "fact", "老人说他是上海人", "", "", source="llm:consolidate")
    d.add_rag_memory("e1", "episodic", "昨天聊天老人提到自己是上海人", "cid_r1")
    res = rag.correct_from_feedback("e1", "上海人", "老人其实是南京人", by="nurse")
    assert res["ok"] and res["stats"]["core_softdel"] == 1 and res["stats"]["rag_softdel"] == 1
    core = d.list_core_memories("e1")
    assert len(core) == 1 and "南京" in core[0]["content"]
    assert core[0]["authority"] == "nurse"
    # 旧条目进回收站可回滚
    assert len(d.list_delete_operations(uid="e1")) == 2


def test_expression_merge_and_redline(d):
    from LLM.agent import memory as rag
    r1 = rag._apply_expression("e1", {"situation": "夸她穿得好看", "style": "闺女眼光就是好"})
    assert r1["action"] == "add"
    r2 = rag._apply_expression("e1", {"situation": "夸她穿得好看", "style": "闺女眼光就是好"})
    assert r2["eid"] == r1["eid"]  # merge
    rows = d.list_expressions("e1")
    assert len(rows) == 1 and rows[0]["count"] == 2 and rows[0]["checked"] == 0
    # 医疗红线
    assert rag._apply_expression("e1", {"situation": "吃药时", "style": "这药苦"})["action"] == "reject"
    # 脏话红线（多字词）
    assert rag._apply_expression("e1", {"situation": "骂人", "style": "真他妈烦"})["action"] == "reject"
    # 审核通过后可注入
    d.set_expression_checked(r1["eid"], True)
    assert len(d.pick_expressions("e1")) == 1


def test_import_memories(d):
    r = d.import_memories("e1", "第一条背景\n\n第二条知识内容\n第三条", split="paragraph")
    assert r["imported"] >= 2
    assert len(d.list_memories(uid="e1", status="pending")) >= 2


def test_core_pinned_guard(d):
    from LLM.agent import memory as rag
    mid = d.add_core_memory("e1", "fact", "护士重要备注", "", "", source="manual:nurse")
    d.set_core_pinned(mid, True)
    d.add_core_memory("e1", "persona", "旧画像", "", "", source="llm:consolidate")
    rag._upsert_portrait("e1", "新画像内容无医疗词")
    core = d.list_core_memories("e1")
    personas = [m for m in core if m["type"] == "persona"]
    assert len(personas) == 1 and "新画像" in personas[0]["content"]
    assert any(m["id"] == mid and m["pinned"] == 1 for m in core)


def test_correct_instant_signal_gate(d):
    """R1 信号词预筛：无纠正信号 → 不调 LLM 直接 no_signal 早退（省每轮调用）。"""
    from LLM.agent import memory as rag

    # 无信号词（日常闲聊）→ no_signal，不触发 LLM
    r = rag.correct_instant("e1", "今天天气不错", None, "fake")
    assert r == {"corrected": False, "reason": "no_signal"}
    # 有信号词（client=None 无法真调 LLM）→ 门放行进入 LLM 阶段并报 llm_error，证明信号门生效
    r2 = rag.correct_instant("e1", "我不是姓张，我姓王", None, "fake")
    assert r2["reason"] == "llm_error"
    # 超长句不做即时预判
    r3 = rag.correct_instant("e1", "其实我" + "聊" * 90, None, "fake")
    assert r3["reason"] == "no_signal"


def test_portrait_guard_and_nurse_override(d):
    """R2 画像防退化 + 护士手动维护优先（pinned 不被 AI 覆盖）。"""
    from LLM.agent import memory as rag
    # 首次 AI 画像
    rag._upsert_portrait("e1", "老人性格温和，喜欢京剧，说话亲切", source="llm:consolidate")
    personas = [m for m in d.list_core_memories("e1") if m["type"] == "persona"]
    assert len(personas) == 1 and personas[0]["authority"] == "llm"
    # 高度相似 → 跳过重写（防退化）
    rag._upsert_portrait("e1", "老人性格温和，喜欢京剧，说话亲切", source="llm:consolidate")
    personas = [m for m in d.list_core_memories("e1") if m["type"] == "persona"]
    assert len(personas) == 1
    # 护士手动维护 → pinned
    rag._upsert_portrait("e1", "护士备注：老人听力稍弱，说话要大声些", source="nurse:manual", by="nurse")
    personas = [m for m in d.list_core_memories("e1") if m["type"] == "persona"]
    assert len(personas) == 1 and personas[0]["pinned"] == 1 and personas[0]["authority"] == "nurse"
    # AI 再整理 → 不覆盖护士维护画像
    rag._upsert_portrait("e1", "AI 生成的另一个画像内容", source="llm:consolidate")
    personas = [m for m in d.list_core_memories("e1") if m["type"] == "persona"]
    assert len(personas) == 1 and "听力稍弱" in personas[0]["content"]
