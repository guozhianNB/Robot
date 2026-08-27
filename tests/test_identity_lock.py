# -*- coding: utf-8 -*-
"""effective_uid 锁定模式行为矩阵（规格 §8.2）。

- 未锁定：声纹识别高置信 → 用识别结果；低置信/未识别 → 沿用当前 uid。
- 锁定：固定返回锁定 uid，声纹识别到其他人也不切换。
"""
from LLM.voice.identity import effective_uid, IdentityVote


def _vote(uid, conf):
    return IdentityVote(candidate_uid=uid, confidence=conf, source="voiceprint")


def test_unlocked_high_confidence_uses_voiceprint():
    assert effective_uid(_vote("elder_002", 0.9), "elder_001") == "elder_002"


def test_unlocked_low_confidence_keeps_current():
    assert effective_uid(_vote(None, 0.1), "elder_001") == "elder_001"


def test_locked_returns_locked_uid_even_if_voiceprint_differs():
    assert effective_uid(_vote("elder_002", 0.9), "elder_001", "elder_003") == "elder_003"


def test_locked_with_none_current():
    assert effective_uid(_vote(None, 0.0), None, "elder_003") == "elder_003"


def test_unlock_recovers_voiceprint_priority():
    # 解锁（locked_uid=None）后恢复声纹优先
    assert effective_uid(_vote("elder_002", 0.9), "elder_001", None) == "elder_002"
