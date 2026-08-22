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
