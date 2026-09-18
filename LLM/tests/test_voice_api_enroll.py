# -*- coding: utf-8 -*-
import numpy as np
from LLM.voice import voice_api
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


class FakeTextReplyHandle:
    def __init__(self):
        self.feed_calls = []
        self.finish_calls = []

    def feed(self, delta):
        self.feed_calls.append(delta)

    def finish(self, flush_tail=True):
        self.finish_calls.append(flush_tail)


class FakeTextReplyWorker:
    def __init__(self):
        self.settings = None
        self.handle = FakeTextReplyHandle()

    def begin_text_reply(self, settings):
        self.settings = settings
        return self.handle


def test_text_reply_api_forwards_to_running_worker(monkeypatch):
    worker = FakeTextReplyWorker()
    settings = {"voice_enabled": True, "tts_enabled": True}
    monkeypatch.setattr(voice_api, "_VOICE_AVAILABLE", True)
    monkeypatch.setattr(voice_api, "_worker", worker)
    monkeypatch.setattr(voice_api.db, "get_settings", lambda: settings)

    handle = voice_api.begin_text_reply()
    voice_api.feed_text_reply(handle, "你好")
    voice_api.end_text_reply(handle, flush_tail=False)

    assert handle is worker.handle
    assert worker.settings is settings
    assert handle.feed_calls == ["你好"]
    assert handle.finish_calls == [False]


def test_text_reply_api_returns_none_when_disabled_or_worker_missing(monkeypatch):
    monkeypatch.setattr(voice_api, "_VOICE_AVAILABLE", True)
    monkeypatch.setattr(voice_api.db, "get_settings", lambda: {
        "voice_enabled": True, "tts_enabled": False,
    })
    worker = FakeTextReplyWorker()
    monkeypatch.setattr(voice_api, "_worker", worker)

    assert voice_api.begin_text_reply() is None
    assert worker.settings is None

    monkeypatch.setattr(voice_api, "_worker", None)
    monkeypatch.setattr(voice_api.db, "get_settings", lambda: {
        "voice_enabled": True, "tts_enabled": True,
    })
    assert voice_api.begin_text_reply() is None


def test_text_reply_api_ignores_empty_handles(monkeypatch):
    voice_api.feed_text_reply(None, "忽略")
    voice_api.end_text_reply(None)


def test_text_reply_api_begin_db_failure_survives_audit_failure(monkeypatch):
    audit_calls = []

    def broken_audit(event, **fields):
        audit_calls.append((event, fields))
        raise RuntimeError("audit failed")

    monkeypatch.setattr(voice_api, "_VOICE_AVAILABLE", True)
    monkeypatch.setattr(voice_api, "_worker", FakeTextReplyWorker())
    monkeypatch.setattr(voice_api.db, "get_settings",
                        lambda: (_ for _ in ()).throw(RuntimeError("settings failed")))
    monkeypatch.setattr(voice_api.audit, "log", broken_audit)

    assert voice_api.begin_text_reply() is None
    assert audit_calls == [("voice_error", {
        "action": "text_tts_begin", "error": "settings failed",
    })]


def test_text_reply_api_worker_failure_survives_audit_failure(monkeypatch):
    audit_calls = []

    def broken_audit(event, **fields):
        audit_calls.append((event, fields))
        raise RuntimeError("audit failed")

    class FailingWorker:
        def begin_text_reply(self, settings):
            raise RuntimeError("worker failed")

    monkeypatch.setattr(voice_api, "_VOICE_AVAILABLE", True)
    monkeypatch.setattr(voice_api, "_worker", FailingWorker())
    monkeypatch.setattr(voice_api.db, "get_settings",
                        lambda: {"voice_enabled": True, "tts_enabled": True})
    monkeypatch.setattr(voice_api.audit, "log", broken_audit)

    assert voice_api.begin_text_reply() is None
    assert audit_calls == [("voice_error", {
        "action": "text_tts_begin", "error": "worker failed",
    })]


def test_text_reply_api_feed_failure_survives_audit_failure(monkeypatch):
    audit_calls = []

    def broken_audit(event, **fields):
        audit_calls.append((event, fields))
        raise RuntimeError("audit failed")

    class FailingHandle:
        def feed(self, delta):
            raise RuntimeError("feed failed")

    monkeypatch.setattr(voice_api.audit, "log", broken_audit)

    voice_api.feed_text_reply(FailingHandle(), "内容")

    assert audit_calls == [("voice_error", {
        "action": "text_tts_feed", "error": "feed failed",
    })]


def test_text_reply_api_end_failure_survives_audit_failure(monkeypatch):
    audit_calls = []

    def broken_audit(event, **fields):
        audit_calls.append((event, fields))
        raise RuntimeError("audit failed")

    class FailingHandle:
        def finish(self, flush_tail=True):
            raise RuntimeError("end failed")

    monkeypatch.setattr(voice_api.audit, "log", broken_audit)

    voice_api.end_text_reply(FailingHandle(), flush_tail=False)

    assert audit_calls == [("voice_error", {
        "action": "text_tts_end", "error": "end failed",
    })]
