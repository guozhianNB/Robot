# -*- coding: utf-8 -*-
from LLM.voice.identity import IdentityVote, effective_uid

def test_effective_uid_uses_candidate():
    v = IdentityVote("elder_002", 0.7, "voiceprint")
    assert effective_uid(v, "elder_001") == "elder_002"

def test_effective_uid_falls_back_on_low_confidence():
    v = IdentityVote(None, 0.2, "voiceprint")
    assert effective_uid(v, "elder_001") == "elder_001"

def test_effective_uid_none_when_no_fallback():
    assert effective_uid(IdentityVote(None, 0.2, "voiceprint"), None) is None
