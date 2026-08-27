# -*- coding: utf-8 -*-
"""VoiceWorker 事件广播测试：wake / recognized / chat_new / speaking / idle。"""
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
    # 测试不写运行时审计日志
    # 注：log(event, **fields) 的 event 是位置参数，lambda 需接受它
    monkeypatch.setattr(worker_mod.audit, "log", lambda event, **kw: None)


def test_wake_publish(monkeypatch):
    _silence_audit(monkeypatch)
    w, events = _make_worker()
    w.session = session_mod.Session()
    w.src = type("Src", (), {"read": lambda self: b"\x00" * 320})()
    w.vad = type("Vad", (), {"accept": lambda self, c: None})()
    w.kws = type("Kws", (), {"accept": lambda self, c: "小机器人"})()
    w._step({})
    # 唤醒后进入 LISTENING（最终审查 I-2 修复：worker 发 listening 而非 wake）
    assert ("voice_state", {"state": "listening"}) in events


def test_speech_publishes_recognized_and_chat_new(monkeypatch):
    _silence_audit(monkeypatch)
    calls = []

    def chat_fn(uid, text):
        calls.append((uid, text))
        return "好的，我记住了"

    w, events = _make_worker(chat_fn=chat_fn)
    w.session = session_mod.Session()
    w.asr = type("Asr", (), {"transcribe": lambda self, seg: "我今天有点头晕"})()
    w.fusion = type(
        "Fusion", (),
        {"resolve": lambda self, seg: type("Vote", (), {"candidate_uid": "elder_002", "confidence": 0.9})()},
    )()
    w._handle_speech("seg", {"asr_enabled": True, "tts_enabled": False})
    # 最终审查 I-1 修复：声纹切换先广播 user_changed（source=voiceprint），
    # 再广播 recognized——断言改按 in 匹配，不依赖索引顺序
    assert ("voice_state",
            {"state": "recognized", "uid": "elder_002", "text": "我今天有点头晕"}) in events
    assert ("chat_new",
            {"uid": "elder_002", "user": "我今天有点头晕", "assistant": "好的，我记住了"}) in events
    assert ("user_changed",
            {"uid": "elder_002", "locked": False, "source": "voiceprint"}) in events
    assert calls == [("elder_002", "我今天有点头晕")]


def test_speak_publishes_speaking(monkeypatch):
    _silence_audit(monkeypatch)
    w, events = _make_worker()
    w.session = session_mod.Session()
    w.session.wake()
    w.tts = type("Tts", (), {"synthesize": lambda self, t: (b"\x00\x00", 16000)})()
    w.sink = type("Sink", (), {"play": lambda self, s, sr: None})()
    w._speak("你好呀")
    assert ("voice_state", {"state": "speaking", "text": "你好呀"}) in events


def test_speaking_done_publishes_idle(monkeypatch):
    _silence_audit(monkeypatch)
    w, events = _make_worker()
    w.session = session_mod.Session()
    w.session.wake()
    w.session.start_speaking()
    w.src = type("Src", (), {"read": lambda self: b"\x00" * 320})()
    w.vad = type("Vad", (), {"accept": lambda self, c: None,
                             "is_speech_now": lambda self: False})()
    w.sink = type("Sink", (), {"is_done": lambda self: True})()
    w._step({})
    assert ("voice_state", {"state": "idle"}) in events


def test_publish_failure_is_silent(monkeypatch):
    """publish_fn 抛异常必须被吞掉，不影响语音主循环。"""
    _silence_audit(monkeypatch)
    w = worker_mod.VoiceWorker(
        chat_fn=lambda uid, text: "x",
        post_turn_fn=lambda uid, u, a: None,
        publish_fn=lambda ev, **kw: (_ for _ in ()).throw(RuntimeError("bus down")),
    )
    w.session = session_mod.Session()
    w.asr = type("Asr", (), {"transcribe": lambda self, seg: "测试"})()
    w.fusion = type("Fusion", (),
                    {"resolve": lambda self, seg: type("Vote", (), {"candidate_uid": None, "confidence": 0.1})()})()
    w._handle_speech("seg", {"asr_enabled": True, "tts_enabled": False})  # 不应抛异常
