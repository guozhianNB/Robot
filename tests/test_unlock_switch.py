# -*- coding: utf-8 -*-
"""解锁后声纹自动切换回归测试（用户报告问题 2）。

场景：锁定 elder_001 → 解锁 → 识别到 elder_003 → 应切换 current_uid 并广播 user_changed。
锁定时识别到 elder_003 → 不切换（locked_ignored）。
"""
from LLM.voice import worker as worker_mod
from LLM.voice import session as session_mod


def _make_worker(chat_fn=None, post_turn_fn=None):
    events = []

    def pub(ev, **payload):
        events.append((ev, payload))

    w = worker_mod.VoiceWorker(
        chat_fn=chat_fn or (lambda uid, text: "回复：" + text),
        post_turn_fn=post_turn_fn or (lambda uid, user, assistant: None),
        publish_fn=pub,
    )
    return w, events


def _silence_audit(monkeypatch):
    monkeypatch.setattr(worker_mod.audit, "log", lambda event, **kw: None)


def _fusion_for(candidate, score):
    """构造返回指定 candidate 的 fake 融合层。"""
    return type(
        "Fusion", (),
        {"resolve": lambda self, seg: type(
            "Vote", (),
            {"candidate_uid": candidate, "confidence": score, "source": "voiceprint"})()},
    )()


def test_locked_ignores_voiceprint_switch(monkeypatch):
    """锁定时识别到锁定外用户：不切换、不广播 user_changed。"""
    _silence_audit(monkeypatch)
    w, events = _make_worker()
    w.locked_uid = "elder_001"
    w.current_uid = "elder_001"
    w.asr = type("Asr", (), {"transcribe": lambda self, seg: "你好"})()
    w.fusion = _fusion_for("elder_003", 0.9)
    w._handle_speech("seg", {"asr_enabled": True, "tts_enabled": False})
    assert w.current_uid == "elder_001"          # 锁定：不切换
    types = [e[0] for e in events]
    assert "user_changed" not in types            # 锁定：不广播切换
    ignored = [e for e in events if e[0] == "voice_spk"]
    # 审计被静音，事件列表里不应有 user_changed；voice_state recognized 的 uid 应为锁定值
    rec = [e for e in events if e[0] == "voice_state"][0]
    assert rec[1]["uid"] == "elder_001"


def test_unlocked_switches_uid_and_broadcasts(monkeypatch):
    """解锁后识别到不同用户：切换 current_uid + 广播 user_changed(voiceprint)。"""
    _silence_audit(monkeypatch)
    w, events = _make_worker()
    w.locked_uid = None                            # 解锁（关键：locked_uid 已清）
    w.current_uid = "elder_001"                    # 之前的用户
    w.asr = type("Asr", (), {"transcribe": lambda self, seg: "你好"})()
    w.fusion = _fusion_for("elder_003", 0.9)
    w._handle_speech("seg", {"asr_enabled": True, "tts_enabled": False})
    assert w.current_uid == "elder_003"            # 解锁：切换到识别结果
    uc = [e for e in events if e[0] == "user_changed"]
    assert uc, "解锁后应广播 user_changed"
    assert uc[0][1]["uid"] == "elder_003"
    assert uc[0][1]["source"] == "voiceprint"
    assert uc[0][1]["locked"] is False


def test_unlocked_low_confidence_keeps_current(monkeypatch):
    """解锁但识别分数不足（candidate=None）：沿用当前 uid（宁问勿猜）。"""
    _silence_audit(monkeypatch)
    w, events = _make_worker()
    w.locked_uid = None
    w.current_uid = "elder_001"
    w.asr = type("Asr", (), {"transcribe": lambda self, seg: "你好"})()
    w.fusion = _fusion_for(None, 0.2)              # 低置信/未识别
    w._handle_speech("seg", {"asr_enabled": True, "tts_enabled": False})
    assert w.current_uid == "elder_001"            # 宁问勿猜：沿用
    types = [e[0] for e in events]
    assert "user_changed" not in types
