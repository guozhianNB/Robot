# -*- coding: utf-8 -*-
import numpy as np
from LLM.voice import speaker as spk_mod
from LLM.voice import config


def _wav():
    return np.ones(config.SAMPLE_RATE, dtype=np.float32) * 0.1   # 1 秒 16k 语音


def _vec(first=1.0):
    v = np.zeros(8, dtype=np.float32)
    v[0] = first
    return v / (np.linalg.norm(v) + 1e-6)


def _recognizer(tmp_path, monkeypatch):
    r = spk_mod.SpeakerRecognizer(profile_dir=str(tmp_path))
    monkeypatch.setattr(r, "embed", lambda wav: _vec(1.0))
    return r


def test_enroll_new_writes_count1(tmp_path, monkeypatch):
    r = _recognizer(tmp_path, monkeypatch)
    r.enroll("elder_x", [_wav()])
    d = np.load(tmp_path / "elder_x.npz")
    assert int(d["count"]) == 1
    assert "elder_x" in r._profiles


def test_enroll_append_merges(tmp_path, monkeypatch):
    r = _recognizer(tmp_path, monkeypatch)
    r.enroll("elder_x", [_wav()])            # append=False，count=1
    r.enroll("elder_x", [_wav()], append=True)
    d = np.load(tmp_path / "elder_x.npz")
    assert int(d["count"]) == 2
    assert r.sample_count("elder_x") == 2


def test_enroll_append_without_existing_is_new(tmp_path, monkeypatch):
    r = _recognizer(tmp_path, monkeypatch)
    r.enroll("elder_x", [_wav()], append=True)   # 无旧档案，等效新建
    assert r.sample_count("elder_x") == 1


def test_enroll_override_resets_count(tmp_path, monkeypatch):
    r = _recognizer(tmp_path, monkeypatch)
    r.enroll("elder_x", [_wav()])
    r.enroll("elder_x", [_wav()], append=True)
    r.enroll("elder_x", [_wav()])                # 再次覆盖 → count 归 1
    assert r.sample_count("elder_x") == 1


def test_enroll_embedding_merges(tmp_path, monkeypatch):
    r = _recognizer(tmp_path, monkeypatch)
    r.enroll_embedding("elder_x", _vec(1.0))
    r.enroll_embedding("elder_x", _vec(2.0), append=True)
    assert r.sample_count("elder_x") == 2
    d = np.load(tmp_path / "elder_x.npz")
    expect = (_vec(1.0) * 1 + _vec(2.0)) / 2
    expect = expect / (np.linalg.norm(expect) + 1e-6)
    assert np.allclose(d["emb"], expect, atol=1e-5)


def test_delete_removes_file_and_profile(tmp_path, monkeypatch):
    r = _recognizer(tmp_path, monkeypatch)
    r.enroll("elder_x", [_wav()])
    r.delete("elder_x")
    assert not (tmp_path / "elder_x.npz").exists()
    assert "elder_x" not in r._profiles
    assert r.sample_count("elder_x") == 0


def test_legacy_npz_without_count_counts_as_1(tmp_path):
    v = _vec(1.0)
    np.savez(tmp_path / "elder_old.npz", emb=v)   # 旧格式：无 count
    r = spk_mod.SpeakerRecognizer(profile_dir=str(tmp_path))
    assert r.sample_count("elder_old") == 1
    assert "elder_old" in r._profiles
