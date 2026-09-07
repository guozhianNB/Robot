# -*- coding: utf-8 -*-
"""VoiceWorker 事件广播测试：wake / recognized / chat_partial / chat_new / speaking / idle。

任务5 流式问答语义（改动签名与行为均以本文件为准）：
- VoiceWorker(stream_fn, post_turn_fn=None, publish_fn=None)；stream_fn(uid, text)
  返回 chat_stream 事件迭代器（不再是返回整段文本的 chat_fn）；
- _consume_reply 同步消费事件流：content → chat_partial 逐字广播 + 分句缓冲，
  完整句切出后按句合成入队（仅 speak 模式）；speaking 在首句触发 start_speaking 时广播；
- chat_new 由 _answer（应答线程）在流收尾后发布，_consume_reply 自身不发；
- 主循环播报结束判定 = sink.is_done() and not self._answering and finish_speaking()。
"""
import numpy as np

from LLM.voice import worker as worker_mod
from LLM.voice import session as session_mod


def _make_worker(stream_fn=None, post_turn_fn=None):
    events = []

    def pub(ev, **payload):
        events.append((ev, payload))

    w = worker_mod.VoiceWorker(
        stream_fn=stream_fn or (lambda uid, text: iter([])),
        post_turn_fn=post_turn_fn or (lambda uid, user, assistant: None),
        publish_fn=pub,
    )
    return w, events


def _silence_audit(monkeypatch):
    monkeypatch.setattr(worker_mod.audit, "log", lambda event, **kw: None)


def _install_stubs(w):
    """注入可测假件：会话/引擎/声卡/分句器全部 stub。"""
    w.session = session_mod.Session()
    w.session.wake()
    w._local_tts = w.tts = type("Tts", (), {
        "provider": "local",
        "synthesize_chunks": lambda self, t: (np.zeros(160, dtype=np.float32) for _ in (1,)),
    })()
    w.sink = type("Sink", (), {"enqueue": lambda self, s: None,
                               "stop": lambda self: None,
                               "is_done": lambda self: True,
                               "end_of_stream": lambda self: None})()


def test_wake_publish(monkeypatch):
    _silence_audit(monkeypatch)
    w, events = _make_worker()
    w.session = session_mod.Session()
    w.src = type("Src", (), {"read": lambda self: b"\x00" * 320})()
    w.vad = type("Vad", (), {"accept": lambda self, c: None})()
    w.kws = type("Kws", (), {"accept": lambda self, c: "小机器人"})()
    w._step({})
    assert ("voice_state", {"state": "listening"}) in events


def test_consume_reply_streams_partial_and_chat_new(monkeypatch):
    """content 逐段广播 chat_partial；整句切出后触发 speaking；chat_new 由 _answer 层发。"""
    _silence_audit(monkeypatch)
    events = []

    def pub(ev, **payload):
        events.append((ev, payload))

    def stream_fn(uid, text):
        return iter([
            {"type": "content", "content": "好的，"},
            {"type": "content", "content": "我记住了。"},
            {"type": "done", "assistant": "好的，我记住了。"},
        ])

    w = worker_mod.VoiceWorker(stream_fn=stream_fn,
                               post_turn_fn=lambda uid, u, a: None,
                               publish_fn=pub)
    _install_stubs(w)
    w.current_uid = "elder_002"
    assistant = w._consume_reply("elder_002", "今天吃药了吗", {"tts_enabled": True})
    assert assistant == "好的，我记住了。"
    partials = [p for ev, p in events if ev == "chat_partial"]
    assert "".join(p["delta"] for p in partials) == "好的，我记住了。"
    # chat_new 由 _answer（应答线程）层发布，_consume_reply 自身不发
    assert ("chat_new", {"uid": "elder_002", "user": "今天吃药了吗",
                         "assistant": "好的，我记住了。"}) not in events
    # 首句完整切出并成功触发 start_speaking 后才广播 speaking（出现在全部 partial 之后）
    spk = [(i, p) for i, (ev, p) in enumerate(events)
           if ev == "voice_state" and p["state"] == "speaking"]
    assert spk, "分句整句应触发 speaking"
    assert spk[0][0] >= len(partials), "speaking 不得早于全部 content 逐字上屏"
    assert w.session.state == session_mod.State.SPEAKING


def test_speech_publishes_recognized(monkeypatch):
    """语音识别整句 → recognized + user_changed（不做 LLM 段同步断言）。"""
    _silence_audit(monkeypatch)
    events = []

    def pub(ev, **payload):
        events.append((ev, payload))

    w = worker_mod.VoiceWorker(stream_fn=lambda uid, t: iter([]),
                               publish_fn=pub)
    w.session = session_mod.Session()
    w.asr = type("Asr", (), {"transcribe": lambda self, seg: "我今天有点头晕"})()
    w.fusion = type(
        "Fusion", (),
        {"resolve": lambda self, seg: type("Vote", (), {"candidate_uid": "elder_002", "confidence": 0.9})()},
    )()
    _install_stubs(w)
    # _handle_speech(seg, text, settings)：text = ASR 整句最终文本（worker 3 参签名）
    w._handle_speech("seg", "我今天有点头晕", {"asr_enabled": True, "tts_enabled": True})
    # recognized / user_changed 同步广播；chat_new 由应答线程异步发（此处不等待线程）
    assert ("voice_state",
            {"state": "recognized", "uid": "elder_002", "text": "我今天有点头晕"}) in events
    assert ("user_changed",
            {"uid": "elder_002", "locked": False, "source": "voiceprint"}) in events


def test_speaking_done_publishes_listening(monkeypatch):
    """播报完成（is_done 且应答线程已结束）→ 回 LISTENING（前端"正在听…"）。"""
    _silence_audit(monkeypatch)
    w, events = _make_worker()
    w.session = session_mod.Session()
    w.session.wake()
    w.session.start_speaking()
    w.src = type("Src", (), {"read": lambda self: b"\x00" * 320})()
    w.vad = type("Vad", (), {"accept": lambda self, c: None,
                             "is_speech_now": lambda self: False})()
    w.sink = type("Sink", (), {"is_done": lambda self: True,
                               "end_of_stream": lambda self: None})()
    w._answering = False
    w._step({})
    assert ("voice_state", {"state": "listening"}) in events
    assert w.session.state == session_mod.State.LISTENING


def test_answering_true_keeps_speaking(monkeypatch):
    """队列瞬时空但应答线程仍活跃 → 不得误判播报结束。"""
    _silence_audit(monkeypatch)
    w, events = _make_worker()
    w.session = session_mod.Session()
    w.session.wake()
    w.session.start_speaking()
    w.src = type("Src", (), {"read": lambda self: b"\x00" * 320})()
    w.vad = type("Vad", (), {"accept": lambda self, c: None,
                             "is_speech_now": lambda self: False})()
    w.sink = type("Sink", (), {"is_done": lambda self: True,
                               "end_of_stream": lambda self: None})()
    w._answering = True
    w._step({})
    assert w.session.state == session_mod.State.SPEAKING


def test_listen_timeout_publishes_idle(monkeypatch):
    """30s 免唤醒窗口超时 → 回待机（IDLE）。"""
    _silence_audit(monkeypatch)
    w, events = _make_worker()
    w.session = session_mod.Session()
    w.session.wake()
    w.src = type("Src", (), {"read": lambda self: b"\x00" * 320})()
    w.vad = type("Vad", (), {"accept": lambda self, c: None,
                             "pop_speech": lambda self: None,
                             "is_speech_now": lambda self: False})()
    w.sink = type("Sink", (), {"is_done": lambda self: False,
                               "end_of_stream": lambda self: None})()
    fake_clock = [100.0]

    class FakeClock:
        @staticmethod
        def __call__():
            return fake_clock[0]

    w.session._clock = FakeClock()
    w.session._last_activity = 50.0
    w._step({})
    assert w.session.state == session_mod.State.IDLE
    assert ("voice_state", {"state": "idle"}) in events


def test_publish_failure_is_silent(monkeypatch):
    """publish_fn 抛异常必须被吞掉，不影响语音主循环。"""
    _silence_audit(monkeypatch)
    events = [{"type": "content", "content": "好的"},
              {"type": "done", "assistant": "好的"}]
    w = worker_mod.VoiceWorker(
        stream_fn=lambda uid, t: iter(events),
        post_turn_fn=lambda uid, u, a: None,
        publish_fn=lambda ev, **kw: (_ for _ in ()).throw(RuntimeError("bus down")),
    )
    _install_stubs(w)
    w.asr = type("Asr", (), {"transcribe": lambda self, seg: "测试"})()
    w.fusion = type("Fusion", (),
                    {"resolve": lambda self, seg: type("Vote", (), {"candidate_uid": None, "confidence": 0.1})()})()
    # content 广播（chat_partial/speaking）全部走 publish → 抛异常 → 应被 _publish 吞掉
    assert w._consume_reply("elder_001", "测试", {"tts_enabled": True}) == "好的"
