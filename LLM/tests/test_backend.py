# -*- coding: utf-8 -*-
import LLM.voice.backend as b

def test_forced_cpu(monkeypatch):
    b.reset_backend()
    monkeypatch.setenv("VOICE_BACKEND", "cpu")
    assert b.detect_backend() == "cpu"

def test_forced_bpu(monkeypatch):
    b.reset_backend()
    monkeypatch.setenv("VOICE_BACKEND", "bpu")
    assert b.detect_backend() == "bpu"

def test_resolve_supported_bpu_when_forced(monkeypatch):
    b.reset_backend()
    monkeypatch.setenv("VOICE_BACKEND", "bpu")
    assert b.resolve_backend("speaker_eres2netv2", "auto") == "bpu"

def test_resolve_unsupported_falls_to_cpu(monkeypatch):
    b.reset_backend()
    monkeypatch.setenv("VOICE_BACKEND", "bpu")
    assert b.resolve_backend("asr_zipformer", "auto") == "cpu"

def test_resolve_bpu_denied_when_forced_cpu(monkeypatch):
    b.reset_backend()
    monkeypatch.setenv("VOICE_BACKEND", "cpu")
    assert b.resolve_backend("speaker_eres2netv2", "auto") == "cpu"
