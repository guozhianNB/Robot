# -*- coding: utf-8 -*-
"""Kuzu 图谱封装测试：实体/边去重 + 一跳查询 + 降级。"""
import pytest
from LLM import graph


@pytest.fixture
def g(tmp_path, monkeypatch):
    monkeypatch.setattr(graph, "_DB_PATH", str(tmp_path / "graph"))
    graph._init()
    if not graph._AVAILABLE:
        pytest.skip("kuzu 未安装")
    return graph


def test_upsert_entity_and_relation(g):
    g.upsert_entity("elder_001", "elder_001:张建国", "张建国", "person")
    g.upsert_entity("elder_001", "elder_001:京剧", "京剧", "topic")
    g.upsert_relation("elder_001", "elder_001:张建国", "elder_001:京剧", "likes")
    rels = g.one_hop("elder_001:张建国")
    assert any(r["target"] == "京剧" and r["type"] == "likes" for r in rels)


def test_relation_dedup(g):
    g.upsert_entity("elder_001", "elder_001:张建国", "张建国", "person")
    g.upsert_entity("elder_001", "elder_001:京剧", "京剧", "topic")
    g.upsert_relation("elder_001", "elder_001:张建国", "elder_001:京剧", "likes")
    g.upsert_relation("elder_001", "elder_001:张建国", "elder_001:京剧", "likes")
    rels = g.one_hop("elder_001:张建国")
    likes = [r for r in rels if r["type"] == "likes" and r["target"] == "京剧"]
    assert len(likes) == 1


def test_list_relations(g):
    g.upsert_entity("elder_001", "elder_001:张建国", "张建国", "person")
    g.upsert_entity("elder_001", "elder_001:京剧", "京剧", "topic")
    g.upsert_relation("elder_001", "elder_001:张建国", "elder_001:京剧", "likes")
    rels = g.list_relations("elder_001")
    assert any(r["src"] == "张建国" and r["type"] == "likes" and r["dst"] == "京剧" for r in rels)


def test_status_degraded_when_unavailable(monkeypatch):
    monkeypatch.setattr(graph, "_AVAILABLE", False)
    assert graph.status()["available"] is False
    assert graph.one_hop("x") == []
