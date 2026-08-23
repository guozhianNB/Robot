# -*- coding: utf-8 -*-
from fastapi.testclient import TestClient

from LLM import server, voice_api


def _patch(monkeypatch):
    monkeypatch.setattr(voice_api, "record_speaker",
                        lambda seconds=15: {"ok": True, "recording_id": "rec123", "segments": 2})
    monkeypatch.setattr(voice_api, "commit_speaker",
                        lambda rid, uid, append=True: {"ok": True, "uid": uid, "samples": 3})
    monkeypatch.setattr(voice_api, "discard_recording", lambda rid: {"ok": True})
    monkeypatch.setattr(voice_api, "delete_speaker",
                        lambda uid: {"ok": True, "uid": uid})
    monkeypatch.setattr(voice_api, "get_recording_audio",
                        lambda rid: (b"RIFF\x00\x00\x00\x00WAVE", "audio/wav") if rid == "rec123" else None)
    monkeypatch.setattr(voice_api, "list_speakers", lambda: ["elder_a"])
    monkeypatch.setattr(voice_api, "list_speaker_details",
                        lambda: {"elder_a": {"samples": 2}})


def test_voice_record_route(monkeypatch):
    _patch(monkeypatch)
    c = TestClient(server.app)   # 不进 with：不触发 lifespan，避免副作用
    r = c.post("/api/voice/record", json={"seconds": 15})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["recording_id"] == "rec123"


def test_voice_enroll_route_new(monkeypatch):
    _patch(monkeypatch)
    c = TestClient(server.app)
    r = c.post("/api/voice/enroll", json={"uid": "elder_x", "recording_id": "rec123", "append": True})
    assert r.status_code == 200 and r.json()["samples"] == 3


def test_voice_enroll_route_legacy(monkeypatch):
    # 无 recording_id → 回退旧 enroll_speaker
    monkeypatch.setattr(voice_api, "enroll_speaker",
                        lambda uid, seconds=15: {"ok": True, "uid": uid, "segments": 1})
    c = TestClient(server.app)
    r = c.post("/api/voice/enroll", json={"uid": "elder_x", "seconds": 15})
    assert r.status_code == 200 and r.json()["ok"]


def test_voice_discard_route(monkeypatch):
    _patch(monkeypatch)
    c = TestClient(server.app)
    r = c.delete("/api/voice/record/rec123")
    assert r.status_code == 200 and r.json()["ok"]


def test_voice_delete_speaker_route(monkeypatch):
    _patch(monkeypatch)
    c = TestClient(server.app)
    r = c.delete("/api/voice/speakers/elder_x")
    assert r.status_code == 200 and r.json()["ok"]


def test_voice_audio_route(monkeypatch):
    _patch(monkeypatch)
    c = TestClient(server.app)
    r = c.get("/api/voice/record/rec123/audio")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    r2 = c.get("/api/voice/record/missing/audio")
    assert r2.status_code == 404


def test_voice_speakers_route_details(monkeypatch):
    _patch(monkeypatch)
    c = TestClient(server.app)
    r = c.get("/api/voice/speakers")
    j = r.json()
    assert j["speakers"] == ["elder_a"] and j["details"] == {"elder_a": {"samples": 2}}


def test_face_status_route():
    c = TestClient(server.app)
    r = c.get("/api/face/status")
    j = r.json()
    assert r.status_code == 200 and j["ok"] is True and j["status"] == "unavailable"
