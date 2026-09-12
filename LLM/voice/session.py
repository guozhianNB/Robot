# -*- coding: utf-8 -*-
r"""会话状态机：IDLE / LISTENING / SPEAKING。纯逻辑，可注入时钟便于测试。"""
import threading
import time
from enum import Enum


class State(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    SPEAKING = "speaking"


class Session:
    def __init__(self, handsfree_sec: float = 30.0, clock=time.monotonic):
        self._lock = threading.Lock()   # 状态机方法跨线程（主循环+应答线程）串行化
        self.state = State.IDLE
        self.handsfree_sec = handsfree_sec
        self._clock = clock
        self._last_activity = 0.0

    def _touch(self):
        self._last_activity = self._clock()

    def wake(self) -> State:
        with self._lock:
            if self.state == State.IDLE:
                self.state = State.LISTENING
                self._touch()
            return self.state

    def note_speech(self) -> None:
        with self._lock:
            if self.state == State.LISTENING:
                self._touch()

    def start_speaking(self) -> bool:
        with self._lock:
            if self.state == State.LISTENING:
                self.state = State.SPEAKING
                return True
            return False

    def finish_speaking(self) -> bool:
        with self._lock:
            if self.state == State.SPEAKING:
                self.state = State.LISTENING
                self._touch()
                return True
            return False

    def barge_in(self) -> bool:
        with self._lock:
            if self.state == State.SPEAKING:
                self.state = State.LISTENING
                self._touch()
                return True
            return False

    def expire(self) -> State:
        with self._lock:
            if self.state == State.LISTENING and (self._clock() - self._last_activity) > self.handsfree_sec:
                self.state = State.IDLE
            return self.state
