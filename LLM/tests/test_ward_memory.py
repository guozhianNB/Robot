# -*- coding: utf-8 -*-
"""集体层对话不沉淀成任何老人的记忆（规格 §5.3）。"""
import os
import tempfile

import pytest

from LLM import db
from LLM import memory


@pytest.fixture()
def d():
    tmp = tempfile.mkdtemp()
    old = db.DB_PATH
    db.DB_PATH = os.path.join(tmp, "t.db")
    db.init_db()
    yield db
    db.DB_PATH = old


def test_ward_turn_is_never_buffered(d):
    memory._pending_turns.pop("ward_101", None)
    memory.note_turn("ward_101", "小车", "明天九点体检", None, "", {}, role="ward")
    assert memory._pending_turns.get("ward_101") is None      # 连缓冲都不进
    assert db.list_memories("ward_101") == []


def test_elder_turn_is_still_buffered(d):
    memory._pending_turns.pop("elder_101_1", None)
    memory.note_turn("elder_101_1", "我", "我不爱吃甜的", None, "",
                     {"memory_consolidation_enabled": False}, role="elder")
    assert memory._pending_turns.get("elder_101_1")           # 老人层照旧进缓冲


def test_default_role_keeps_old_behavior(d):
    """不传 role 的老调用点（如旧测试/其他调用方）行为不变。"""
    memory._pending_turns.pop("elder_002", None)
    memory.note_turn("elder_002", "我", "话", None, "",
                     {"memory_consolidation_enabled": False})
    assert memory._pending_turns.get("elder_002")


def test_unknown_role_is_not_settled(d):
    """未知角色取值按 fail-closed 处理：不沉淀（R2）。"""
    memory._pending_turns.pop("u_unknown", None)
    memory.note_turn("u_unknown", "我", "话", None, "", {}, role="??")
    assert memory._pending_turns.get("u_unknown") is None
    for bad in ("WARD", " ward ", ""):        # 大小写 / 空格 / 空值一并 fail-closed
        memory.note_turn("u_unknown", "我", "话", None, "", {}, role=bad)
        assert memory._pending_turns.get("u_unknown") is None, bad
