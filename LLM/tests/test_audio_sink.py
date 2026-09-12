# -*- coding: utf-8 -*-
"""AudioSink 队列播放测试（fake sounddevice，无真实声卡依赖）。"""
import threading
import time

import numpy as np

from LLM.voice import audio as audio_mod


class FakeStream:
    def __init__(self):
        self.writes = []          # list[np.ndarray]（实际送入 write 的块）
        self.closed = False

    def start(self):
        pass

    def write(self, x):
        self.writes.append(np.asarray(x).copy())
        time.sleep(0.001)         # 让写线程有调度窗口

    def abort(self):
        self.closed = True

    def stop(self):
        pass

    def close(self):
        self.closed = True


class FakeSd:
    def __init__(self):
        self.streams = []

    def OutputStream(self, samplerate, channels, dtype):
        s = FakeStream()
        self.streams.append(s)
        return s


def _patch_sd(monkeypatch):
    fake = FakeSd()
    monkeypatch.setattr(audio_mod, "sd", fake)
    monkeypatch.setattr(audio_mod, "_SD_OK", True)
    return fake


def _wait_done(sink, timeout=2.0):
    t0 = time.time()
    while not sink.is_done() and time.time() - t0 < timeout:
        time.sleep(0.01)
    return sink.is_done()


def test_enqueue_plays_segments_in_order(monkeypatch):
    fake = _patch_sd(monkeypatch)
    sink = audio_mod.AudioSink()
    a = np.zeros(3200, dtype=np.float32)
    b = np.ones(3200, dtype=np.float32)
    sink.enqueue(a)
    sink.enqueue(b)
    sink.end_of_stream()
    assert _wait_done(sink)
    assert len(fake.streams) == 1
    got = np.concatenate([w for w in fake.streams[0].writes]) if fake.streams[0].writes else np.zeros(0)
    assert len(got) == 6400
    assert got[3200] == 1.0 and got[0] == 0.0     # 先 a 后 b，顺序播放


def test_stop_clears_queue_and_closes(monkeypatch):
    fake = _patch_sd(monkeypatch)
    sink = audio_mod.AudioSink()
    sink.enqueue(np.zeros(320000, dtype=np.float32))   # 超长段确保没播完
    time.sleep(0.05)
    sink.stop()
    assert _wait_done(sink)
    assert all(s.closed for s in fake.streams)


def test_play_compat(monkeypatch):
    fake = _patch_sd(monkeypatch)
    sink = audio_mod.AudioSink()
    sink.play(np.zeros(1600, dtype=np.float32), 16000)
    sink.end_of_stream()
    assert _wait_done(sink)
    assert fake.streams and fake.streams[0].writes
