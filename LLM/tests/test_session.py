# -*- coding: utf-8 -*-
from LLM.voice.session import Session, State

def test_wake_from_idle():
    s = Session(handsfree_sec=30)
    assert s.state == State.IDLE
    assert s.wake() == State.LISTENING

def test_wake_ignored_outside_idle():
    s = Session()
    s.wake()
    s.start_speaking()
    assert s.wake() == State.SPEAKING

def test_speak_requires_listening():
    s = Session()
    assert s.start_speaking() is False
    s.wake()
    assert s.start_speaking() is True
    assert s.state == State.SPEAKING

def test_finish_speaking_returns_to_listening():
    s = Session(); s.wake(); s.start_speaking()
    assert s.finish_speaking() is True
    assert s.state == State.LISTENING

def test_barge_in_from_speaking():
    s = Session(); s.wake(); s.start_speaking()
    assert s.barge_in() is True
    assert s.state == State.LISTENING

def test_timeout_returns_to_idle():
    clock = [0.0]
    s = Session(handsfree_sec=30, clock=lambda: clock[0])
    s.wake()
    clock[0] = 31.0
    assert s.expire() == State.IDLE

def test_speech_resets_timeout():
    clock = [0.0]
    s = Session(handsfree_sec=30, clock=lambda: clock[0])
    s.wake()
    clock[0] = 20.0
    s.note_speech()
    clock[0] = 40.0          # 距上次 speech 20s
    assert s.expire() == State.LISTENING
    clock[0] = 51.0          # 距上次 speech 31s
    assert s.expire() == State.IDLE
