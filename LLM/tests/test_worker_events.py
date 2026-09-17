# -*- coding: utf-8 -*-
"""VoiceWorker 事件广播测试：wake / recognized / chat_partial / chat_new / speaking / idle。

任务5 流式问答语义（改动签名与行为均以本文件为准）：
- VoiceWorker(stream_fn, post_turn_fn=None, publish_fn=None)；stream_fn(uid, text)
  返回 chat_stream 事件迭代器（不再是返回整段文本的 chat_fn）；
- _consume_reply(uid, user_text, settings, turn) 同步消费事件流：content →
  chat_partial 逐字广播 + 分句缓冲，完整句切出后按句合成入队（仅 speak 模式）；
  speaking 在首句触发 start_speaking 时广播；
- chat_new 由 _answer（应答线程）在流收尾后发布，_consume_reply 自身不发；
- 轮次代数：_start_answer 每轮 self._turn += 1 抢占；被取代的旧轮在检查点
  （循环顶/入队闸 self._turn != turn）静默退出——不发 chat_new/post_turn/end_of_stream；
  消费异常由 _consume_reply 吞掉，_answer 仍以最新轮身份落已收文本 chat_new（W2）；
- 主循环播报结束判定 = sink.is_done() and not self._answering and finish_speaking()。
"""
import threading
import time

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
    w._turn += 1                                  # 模拟 _start_answer 已分配轮次
    assistant = w._consume_reply("elder_002", "今天吃药了吗",
                                 {"tts_enabled": True}, w._turn)
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


def test_reasoning_is_shown_but_never_synthesized(monkeypatch):
    """思维链只广播 chat_reasoning 上屏，**绝不进分句缓冲/TTS**（规格 D3 回归锁）。"""
    _silence_audit(monkeypatch)
    events = []
    spoken = []

    def pub(ev, **payload):
        events.append((ev, payload))

    def stream_fn(uid, text):
        return iter([
            {"type": "reasoning", "content": "老人问的是降压药，先想清楚能不能减。"},
            {"type": "content", "content": "这个要问护士。"},
            {"type": "done", "assistant": "这个要问护士。"},
        ])

    w = worker_mod.VoiceWorker(stream_fn=stream_fn,
                               post_turn_fn=lambda uid, u, a: None,
                               publish_fn=pub)
    _install_stubs(w)
    w.tts = w._local_tts = type("Tts", (), {
        "provider": "local",
        "synthesize_chunks": lambda self, t: (
            spoken.append(t) or np.zeros(160, dtype=np.float32) for _ in (1,)
        ),
    })()
    w.current_uid = "elder_002"
    w._turn += 1
    assistant = w._consume_reply("elder_002", "降压药能减半吗",
                                 {"tts_enabled": True}, w._turn)

    reasoning = "".join(p["delta"] for ev, p in events if ev == "chat_reasoning")
    assert reasoning == "老人问的是降压药，先想清楚能不能减。"
    assert [p for ev, p in events if ev == "chat_reasoning"][0]["uid"] == "elder_002"
    # 合成内容只有正文：思考过程一个字都没进去
    assert spoken == ["这个要问护士。"], f"思维链混进了 TTS：{spoken}"
    assert "先想清楚" not in "".join(spoken)
    # 落库/气泡正文也只含 content
    assert assistant == "这个要问护士。"


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
    w._turn += 1                                  # 模拟 _start_answer 已分配轮次
    assert w._consume_reply("elder_001", "测试", {"tts_enabled": True}, w._turn) == "好的"


# ---- 最终审查修复（K1 轮次代数 / W2 异常收尾）----

def _settle_answer_threads(timeout=5.0):
    """join 全部 voice-answer 应答线程（K1/W2 测试收尾：防泄漏挂起线程）。"""
    for t in list(threading.enumerate()):
        if t.name == "voice-answer" and t.is_alive():
            t.join(timeout)
            assert not t.is_alive(), "应答线程未在超时内退出"


def _wait_no_answering(w, timeout=5.0):
    deadline = time.monotonic() + timeout
    while w._answering and time.monotonic() < deadline:
        time.sleep(0.01)
    return not w._answering


def _counting_sink():
    """记录 end_of_stream 调用次数的 sink 桩（K1/W2 断言收流不变量）。"""
    eos = []
    sink = type("Sink", (), {"enqueue": lambda self, s: None,
                             "stop": lambda self: None,
                             "is_done": lambda self: True,
                             "end_of_stream": lambda self: eos.append(1)})()
    sink._eos = eos
    return sink


def test_new_speech_supersedes_old_answer(monkeypatch):
    """K1：think 期新整句启动第二轮应答 → 轮次代数取代旧轮：
    旧轮阻塞在流读上（对 _abort 脉冲不可见），放行后在其检查点发现被取代——
    不发任何 chat_partial/chat_new、不触发 end_of_stream；收流/落历史只由新轮
    收尾一次，_answering 最终复位 False，线程全部退出。"""
    _silence_audit(monkeypatch)
    events = []

    def pub(ev, **payload):
        events.append((ev, payload))

    entered = threading.Event()      # 旧轮已进入阻塞流（gate.wait 中）
    gate = threading.Event()         # 放行旧轮流

    def stream_fn(uid, text):
        if text == "第一问":
            def blocked():
                entered.set()
                gate.wait(timeout=10)
                yield {"type": "content", "content": "旧轮不应上屏"}
                yield {"type": "done", "assistant": "旧轮全文"}
            return blocked()
        return iter([{"type": "content", "content": "新轮已接管"},
                     {"type": "done", "assistant": "新轮已接管"}])

    w = worker_mod.VoiceWorker(stream_fn=stream_fn,
                               post_turn_fn=lambda uid, u, a: None,
                               publish_fn=pub)
    _install_stubs(w)
    sink = _counting_sink()
    w.sink = sink
    uid = "elder_001"
    try:
        w._start_answer(uid, "第一问", {"tts_enabled": True})
        assert entered.wait(5), "第一轮应答线程未进入阻塞流"
        # think 期新整句：新轮启动（_turn 自增接管；_answering 收尾复位在 join 后断言）
        w._start_answer(uid, "第二问", {"tts_enabled": True})
        assert w._turn == 2, "新轮应抢占轮次代数"
    finally:
        gate.set()                 # 放行旧轮（断言失败也要清理，防挂起线程）
        _settle_answer_threads()
    assert _wait_no_answering(w)
    partials = "".join(p["delta"] for ev, p in events if ev == "chat_partial")
    assert "旧轮不应上屏" not in partials, "被取代的旧轮不得再上屏文字"
    assert partials == "新轮已接管"
    news = [p for ev, p in events if ev == "chat_new"]
    assert len(news) == 1, f"只有最新轮发 chat_new：{news}"
    assert news[0] == {"uid": uid, "user": "第二问", "assistant": "新轮已接管"}
    assert len(sink._eos) == 1, "end_of_stream 只能由最新轮收尾一次（旧轮静默退出）"


def test_consume_exception_still_finalizes_chat_new(monkeypatch):
    """W2：消费异常（如设备掉线）→ _consume_reply 记审计后带已收文本返回，不抛穿；
    _answer 仍以最新轮身份收尾：chat_new 落已收文本 + post_turn + end_of_stream 一次。"""
    _silence_audit(monkeypatch)
    events = []
    posted = []

    def pub(ev, **payload):
        events.append((ev, payload))

    def stream_fn(uid, text):
        def gen():
            yield {"type": "content", "content": "前半段"}
            raise RuntimeError("设备掉线")
        return gen()

    w = worker_mod.VoiceWorker(stream_fn=stream_fn,
                               post_turn_fn=lambda uid, u, a: posted.append((uid, u, a)),
                               publish_fn=pub)
    _install_stubs(w)
    sink = _counting_sink()
    w.sink = sink
    w._start_answer("elder_001", "测试问", {"tts_enabled": True})
    _settle_answer_threads()
    assert _wait_no_answering(w)
    news = [p for ev, p in events if ev == "chat_new"]
    assert len(news) == 1
    assert news[0] == {"uid": "elder_001", "user": "测试问", "assistant": "前半段"}
    assert posted == [("elder_001", "测试问", "前半段")]
    assert len(sink._eos) == 1, "异常路径也要收流一次"


def test_text_reply_stream_speaks_sentences_without_chat_events(monkeypatch):
    """文本回复增量按完整句播报，不重复发布聊天事件，且只收流一次。"""
    _silence_audit(monkeypatch)
    w, events = _make_worker()
    spoken = []
    w.session = session_mod.Session()
    w._local_tts = w.tts = type("Tts", (), {
        "provider": "local",
        "synthesize_chunks": lambda self, text: (
            spoken.append(text) or np.zeros(160, dtype=np.float32) for _ in (1,)
        ),
    })()
    w.sink = _counting_sink()

    feed = w.begin_text_reply({"tts_enabled": True})
    assert feed is not None
    feed.feed("第一句。第二")
    feed.feed("句！尾句")
    feed.finish(flush_tail=True)
    _settle_answer_threads()

    assert spoken == ["第一句。", "第二句！", "尾句"]
    assert not [ev for ev, _ in events if ev in ("chat_partial", "chat_new")]
    assert len(w.sink._eos) == 1


def test_new_text_reply_supersedes_old_text_reply(monkeypatch):
    """新文本播报轮次接管后，旧轮后续文本不得合成或入队。"""
    _silence_audit(monkeypatch)
    w, _ = _make_worker()
    spoken = []
    enqueued = []
    eos = []
    w.session = session_mod.Session()
    w._local_tts = w.tts = type("Tts", (), {
        "provider": "local",
        "synthesize_chunks": lambda self, text: (
            spoken.append(text) or np.zeros(160, dtype=np.float32) for _ in (1,)
        ),
    })()
    w.sink = type("Sink", (), {
        "enqueue": lambda self, samples: enqueued.append(samples),
        "stop": lambda self: None,
        "is_done": lambda self: True,
        "end_of_stream": lambda self: eos.append(1),
    })()

    old_feed = w.begin_text_reply({"tts_enabled": True})
    new_feed = w.begin_text_reply({"tts_enabled": True})
    assert old_feed is not None and new_feed is not None
    old_feed.feed("旧轮不应播报。")
    old_feed.finish()
    new_feed.feed("新轮播报。")
    new_feed.finish()
    _settle_answer_threads()

    assert spoken == ["新轮播报。"]
    assert len(enqueued) == 1
    assert len(eos) == 1


def test_text_reply_handoff_keeps_new_turn_state_atomic(monkeypatch):
    """旧轮通过收尾检查后遇到新轮交接，也不得清理新轮状态或提前收流。"""
    _silence_audit(monkeypatch)
    old_consuming = threading.Event()
    allow_old_return = threading.Event()
    old_checked_turn = threading.Event()
    allow_old_cleanup = threading.Event()
    turn_two_assigned = threading.Event()
    new_consuming = threading.Event()
    allow_new_return = threading.Event()
    second_returned = threading.Event()
    eos_called = threading.Event()
    eos = []

    class ControlledTurn:
        def __init__(self, value):
            self.value = value

        def __add__(self, increment):
            result = ControlledTurn(self.value + increment)
            if result.value == 2:
                turn_two_assigned.set()
            return result

        def __eq__(self, other):
            other_value = getattr(other, "value", other)
            if self.value == 1 and allow_old_return.is_set():
                old_checked_turn.set()
                assert allow_old_cleanup.wait(5), "旧轮收尾检查未获准继续"
            return self.value == other_value

        def __ne__(self, other):
            return not self == other

    w, _ = _make_worker()
    w.session = session_mod.Session()
    w._local_tts = w.tts = type("Tts", (), {"provider": "local"})()
    w.sink = type("Sink", (), {
        "stop": lambda self: None,
        "end_of_stream": lambda self: (eos.append(1), eos_called.set()),
    })()
    w._turn = ControlledTurn(0)

    def consume(events, settings, turn, publish_text=True, wake_if_idle=False):
        if turn.value == 1:
            old_consuming.set()
            assert allow_old_return.wait(5), "旧轮消费未获准返回"
        else:
            new_consuming.set()
            assert allow_new_return.wait(5), "新轮消费未获准返回"
        return ""

    monkeypatch.setattr(w, "_consume_events", consume)
    first_feed = w.begin_text_reply({"tts_enabled": True})
    assert first_feed is not None
    assert old_consuming.wait(5), "旧轮未进入消费"
    allow_old_return.set()
    assert old_checked_turn.wait(5), "旧轮未停在轮次检查"

    second = {}

    def begin_second():
        second["feed"] = w.begin_text_reply({"tts_enabled": True})
        second_returned.set()

    starter = threading.Thread(target=begin_second, daemon=True)
    starter.start()
    # 无同步时第二轮会越过旧轮的收尾检查；有同步时会在入口等待旧轮完成临界区。
    turn_two_assigned.wait(0.2)
    allow_old_cleanup.set()
    assert second_returned.wait(5), "第二轮入口未返回"
    assert new_consuming.wait(5), "第二轮未进入消费"
    assert eos_called.wait(5), "旧轮未完成收流"
    starter.join(5)
    assert not starter.is_alive()

    snapshot = (w._turn.value, w._answering,
                w._text_reply_feed is second["feed"], len(eos))
    allow_new_return.set()
    _settle_answer_threads()

    assert snapshot == (2, True, True, 1)
    assert len(eos) == 2


def test_begin_text_reply_does_not_wait_for_sink_stop(monkeypatch):
    """文本播报入口不应让 SSE 首包同步等待旧声卡停止。"""
    _silence_audit(monkeypatch)
    stop_entered = threading.Event()
    allow_stop = threading.Event()

    class BlockingSink:
        def stop(self):
            stop_entered.set()
            allow_stop.wait(5)

        def enqueue(self, samples):
            pass

        def end_of_stream(self):
            pass

        def is_done(self):
            return True

    w, _ = _make_worker()
    w.session = session_mod.Session()
    w._local_tts = w.tts = type("Tts", (), {"provider": "local"})()
    w.sink = BlockingSink()
    started = time.monotonic()
    feed = w.begin_text_reply({"tts_enabled": True})
    elapsed = time.monotonic() - started
    assert feed is not None
    assert elapsed < 0.2
    assert stop_entered.wait(2)
    allow_stop.set()
    feed.finish()
    _settle_answer_threads()
