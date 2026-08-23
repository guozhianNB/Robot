# -*- coding: utf-8 -*-
import numpy as np
from LLM.voice.speaker import cosine, classify

def test_cosine_same_is_one():
    e = np.random.default_rng(0).random(192).astype(np.float32)
    assert abs(cosine(e, e) - 1.0) < 1e-4

def test_cosine_orthogonal_is_zero():
    assert abs(cosine(np.array([1, 0], dtype=np.float32),
                      np.array([0, 1], dtype=np.float32))) < 1e-6

def test_classify_picks_best_above_threshold():
    profiles = {"a": np.array([1, 0, 0], dtype=np.float32),
                "b": np.array([0, 1, 0], dtype=np.float32)}
    q = np.array([0.9, 0.2, 0.1], dtype=np.float32)
    uid, score = classify(q, profiles, threshold=0.5)
    assert uid == "a" and score > 0.5

def test_classify_rejects_below_threshold():
    profiles = {"a": np.array([1, 0], dtype=np.float32)}
    uid, score = classify(np.array([0.0, 1.0], dtype=np.float32), profiles, 0.5)
    assert uid is None

def test_classify_empty_profiles():
    uid, score = classify(np.array([1, 0], dtype=np.float32), {}, 0.5)
    assert uid is None and score == 0.0


# -*- coding: utf-8 -*-
import numpy as np
from LLM.voice.speaker import merge_profile


def _vec(first=1.0):
    v = np.zeros(8, dtype=np.float32)
    v[0] = first
    return v / (np.linalg.norm(v) + 1e-6)


def test_merge_profile_new_when_no_old():
    merged, count = merge_profile(None, 0, _vec(1.0))
    assert count == 1
    assert np.allclose(merged, _vec(1.0))


def test_merge_profile_weighted_average():
    # 旧档案 2 次样本（vec A），新样本 vec B → 均值 (2A+B)/3
    a, b = _vec(1.0), _vec(2.0)
    merged, count = merge_profile(a, 2, b)
    assert count == 3
    expect = (a * 2 + b) / 3
    expect = expect / (np.linalg.norm(expect) + 1e-6)
    assert np.allclose(merged, expect, atol=1e-5)
    assert abs(np.linalg.norm(merged) - 1.0) < 1e-5   # 归一化


def test_merge_profile_zero_old_count_treated_as_new():
    merged, count = merge_profile(_vec(1.0), 0, _vec(2.0))
    assert count == 1
    assert np.allclose(merged, _vec(2.0))
