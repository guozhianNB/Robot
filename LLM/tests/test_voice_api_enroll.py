# -*- coding: utf-8 -*-
import numpy as np
from LLM import voice_api
from LLM.voice import config


class FakeRecognizer:
    def __init__(self):
        self.calls = []
        self.deleted = None
    def embed(self, wav):
        return np.ones(8, dtype=np.float32) * 0.1
    def enroll_embedding(self, uid, emb, append=False):
        self.calls.append((uid, append))
    def sample_count(self, uid):
        return 7
    def delete(self, uid):
        self.deleted = uid
    def list_profiles(self):
        return []


class FakeAudioSource:
    def __init__(self):
        self.n = 0
    def start(self):
        pass
    def read(self):
        self.n += 1
        return None if self.n > 6 else np.zeros(1600, dtype=np.float32)
    def stop(self):
        pass


class FakeVAD:
    def __init__(self):
        self._done = False
    def accept(self, samples):
        pass
    def flush(self):
        pass
    def pop_speech(self):
        if not self._done:
            self._done = True
            return np.ones(config.SAMPLE_RATE, dtype=np.float32) * 0.1
        return None


def _patch_available(monkeypatch):
    monkeypatch.setattr(voice_api, "_VOICE_AVAILABLE", True)
    monkeypatch.setattr(voice_api, "_worker", None)
    monkeypatch.setattr(voice_api, "_recognizer", FakeRecognizer())
    monkeypatch.setattr(voice_api.audio_mod, "AudioSource", FakeAudioSource)
    monkeypatch.setattr(voice_api.vad_mod, "VAD", FakeVAD)


def test_record_ok(monkeypatch):
    _patch_available(monkeypatch)
    monkeypatch.setattr(voice_api, "_pending", {})
    res = voice_api.record_speaker(seconds=2)
    assert res["ok"] is True and res["recording_id"] and res["segments"] == 1
    assert res["recording_id"] in voice_api._pending
    assert "wav" in voice_api._pending[res["recording_id"]]


def test_commit_ok(monkeypatch):
    _patch_available(monkeypatch)
    monkeypatch.setattr(voice_api, "_pending", {})
    res = voice_api.record_speaker(seconds=2)
    rid = res["recording_id"]
    out = voice_api.commit_speaker(rid, "elder_x", append=True)
    assert out["ok"] is True and out["uid"] == "elder_x" and out["samples"] == 7
    assert voice_api._recognizer.calls == [("elder_x", True)]
    assert rid not in voice_api._pending            # 入档后暂存清除


def test_commit_unknown_recording(monkeypatch):
    _patch_available(monkeypatch)
    out = voice_api.commit_speaker("nope", "elder_x", append=True)
    assert out["ok"] is False and "过期" in out["error"]


def test_discard_idempotent(monkeypatch):
    _patch_available(monkeypatch)
    monkeypatch.setattr(voice_api, "_pending", {})
    res = voice_api.record_speaker(seconds=2)
    rid = res["recording_id"]
    assert voice_api.discard_recording(rid)["ok"] is True
    assert voice_api.discard_recording(rid)["ok"] is True   # 幂等
    assert rid not in voice_api._pending


def test_get_recording_audio(monkeypatch):
    _patch_available(monkeypatch)
    monkeypatch.setattr(voice_api, "_pending", {})
    rid = voice_api.record_speaker(seconds=2)["recording_id"]
    data, ctype = voice_api.get_recording_audio(rid)
    assert data and ctype == "audio/wav" and data[:4] == b"RIFF"
    assert voice_api.get_recording_audio("nope") is None


def test_delete_speaker(monkeypatch):
    _patch_available(monkeypatch)
    out = voice_api.delete_speaker("elder_x")
    assert out["ok"] is True and voice_api._recognizer.deleted == "elder_x"


def test_degraded_paths_return_ok_false(monkeypatch):
    monkeypatch.setattr(voice_api, "_VOICE_AVAILABLE", False)
    assert voice_api.record_speaker(seconds=2)["ok"] is False
    assert voice_api.commit_speaker("x", "u")["ok"] is False
    assert voice_api.delete_speaker("u")["ok"] is False


def test_pending_ttl_cleanup(monkeypatch):
    _patch_available(monkeypatch)
    monkeypatch.setattr(voice_api, "_pending", {})
    monkeypatch.setattr(voice_api, "VOICE_PENDING_TTL_S", 0)
    rid1 = voice_api.record_speaker(seconds=2)["recording_id"]
    rid2 = voice_api.record_speaker(seconds=2)["recording_id"]
    assert rid1 not in voice_api._pending   # 第二次录制时旧暂存被清
    assert rid2 in voice_api._pending


def test_list_speaker_details(monkeypatch):
    _patch_available(monkeypatch)
    monkeypatch.setattr(voice_api._recognizer, "list_profiles", lambda: ["elder_a"])
    monkeypatch.setattr(voice_api._recognizer, "sample_count", lambda uid: 3)
    assert voice_api.list_speaker_details() == {"elder_a": {"samples": 3}}
