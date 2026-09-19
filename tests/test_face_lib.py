# -*- coding: utf-8 -*-
r"""人脸样本库测试（`LLM/face_lib.py`）。

样本库是"谁注册过 + 他的指纹"的唯一真相，且**直接落盘**，所以边界要测清楚：
  * 存/列/删：目录结构、多张样本、删人要连照片一起删；
  * 代表指纹：多样本**归一化后平均再归一化**（不是直接平均）；
  * 1:N 比对两道闸门：绝对阈值 + 与第二名的差距（差太小 = 歧义，宁可不认）；
  * 脏数据韧性：坏 npz / 维度不对 → 跳过该样本而不是整库崩；
  * 安全：uid 不能拼出目录穿越路径；数量上限要拦住。

全部落在 `tmp_path`，绝不碰真实 `LLM/data/faces/`。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from LLM import conf as _conf  # noqa: E402
from LLM import face_lib  # noqa: E402

np = pytest.importorskip("numpy")


@pytest.fixture(autouse=True)
def isolated_lib(tmp_path, monkeypatch):
    """样本库指到临时目录（真实目录里有真人样本，测试绝不许碰）。"""
    monkeypatch.setattr(_conf, "FACE_DIR", tmp_path / "faces")
    yield


def vec(*idx):
    """构造一个单位向量：给定位为 1，其余 0（便于算出确定的余弦）。"""
    v = np.zeros(512, np.float32)
    for i in idx:
        v[i] = 1.0
    return v


# ---------------------------------------------------------------------------
# 存 / 列 / 删
# ---------------------------------------------------------------------------
def test_add_sample_creates_npz_and_photo():
    cv2 = pytest.importorskip("cv2")
    face = np.full((112, 112, 3), 128, np.uint8)
    res = face_lib.add_sample("elder_001", vec(0), photo_bgr=face, model="arcface_mbf.onnx")
    assert res["sample"].endswith(".npz") and res["photo"].endswith(".jpg")
    assert os.path.isfile(res["photo"])
    assert face_lib.list_uids() == ["elder_001"]
    assert len(face_lib.list_samples("elder_001")) == 1
    assert face_lib.sample_photo_path("elder_001", res["sample"][:-4]) == res["photo"]


def test_add_sample_without_photo():
    res = face_lib.add_sample("elder_001", vec(0), photo_bgr=None, save_photo=True)
    assert res["photo"] is None
    assert len(face_lib.list_samples("elder_001")) == 1


def test_multiple_samples_per_uid_are_all_listed():
    for _ in range(3):
        face_lib.add_sample("elder_001", vec(0), save_photo=False)
    assert len(face_lib.list_samples("elder_001")) == 3
    assert face_lib.embeddings("elder_001").shape == (3, 512)


def test_delete_uid_removes_everything():
    cv2 = pytest.importorskip("cv2")
    face_lib.add_sample("elder_001", vec(0), photo_bgr=np.zeros((112, 112, 3), np.uint8))
    res = face_lib.delete_uid("elder_001")
    assert res["removed"] >= 2 and face_lib.list_uids() == []
    assert not os.path.isdir(res["dir"])


def test_delete_unknown_uid_is_noop():
    res = face_lib.delete_uid("nobody")
    assert res["removed"] == 0 and face_lib.list_uids() == []


def test_stats_counts_people_and_samples():
    face_lib.add_sample("a", vec(0), save_photo=False)
    face_lib.add_sample("a", vec(1), save_photo=False)
    face_lib.add_sample("b", vec(2), save_photo=False)
    st = face_lib.stats()
    assert st["uids"] == 2 and st["samples"] == 3
    assert st["per_uid"] == {"a": 2, "b": 1}
    assert st["threshold"] == _conf.FACE_MATCH_THRESHOLD


# ---------------------------------------------------------------------------
# 代表指纹
# ---------------------------------------------------------------------------
def test_mean_embedding_of_identical_samples_is_that_vector():
    for _ in range(3):
        face_lib.add_sample("a", vec(0), save_photo=False)
    m = face_lib.mean_embedding("a")
    assert float(np.linalg.norm(m)) == pytest.approx(1.0, abs=1e-5)
    assert float(m[0]) == pytest.approx(1.0, abs=1e-5)


def test_mean_embedding_averages_then_renormalizes():
    """两个正交样本 → 平均方向是 45°，而不是"取第一个"或"不归一化"。"""
    face_lib.add_sample("a", vec(0), save_photo=False)
    face_lib.add_sample("a", vec(1), save_photo=False)
    m = face_lib.mean_embedding("a")
    assert float(m[0]) == pytest.approx(0.7071, abs=1e-3)
    assert float(m[1]) == pytest.approx(0.7071, abs=1e-3)


def test_mean_embedding_none_when_no_samples():
    assert face_lib.mean_embedding("nobody") is None
    assert face_lib.index() == {}


# ---------------------------------------------------------------------------
# 1:N 比对：两道闸门
# ---------------------------------------------------------------------------
def test_match_returns_best_uid_and_margin():
    face_lib.add_sample("elder_001", vec(0), save_photo=False)
    face_lib.add_sample("elder_002", vec(5), save_photo=False)
    m = face_lib.match(vec(0), threshold=0.4, min_margin=0.01)
    assert m["matched"] is True and m["uid"] == "elder_001"
    assert m["score"] == pytest.approx(1.0, abs=1e-5)
    assert m["second_uid"] == "elder_002" and m["second_score"] == pytest.approx(0.0, abs=1e-5)
    assert m["margin"] == pytest.approx(1.0, abs=1e-5)


def test_match_margin_is_none_with_single_identity():
    """库里只有一个人时没有"第二名的差距"可言 —— margin 必须是 None（不是 0）。"""
    face_lib.add_sample("elder_001", vec(0), save_photo=False)
    m = face_lib.match(vec(0))
    assert m["matched"] is True and m["margin"] is None and m["second_uid"] is None


def test_match_below_threshold_is_not_matched():
    face_lib.add_sample("elder_001", vec(0), save_photo=False)
    m = face_lib.match(vec(3), threshold=0.4)          # 正交 → 余弦 0
    assert m["matched"] is False and m["uid"] is None
    assert m["reason"] == "below_threshold" and m["score"] == pytest.approx(0.0, abs=1e-5)


def test_match_ambiguous_when_two_close_candidates():
    """两个人分数咬得很近 → 宁可不认（reason=ambiguous），避免在两位老人间乱切。"""
    face_lib.add_sample("a", vec(0, 1), save_photo=False)
    face_lib.add_sample("b", vec(1, 0), save_photo=False)
    m = face_lib.match(vec(1, 1), threshold=0.4, min_margin=0.1)   # 与两人各 0.707
    assert m["matched"] is False and m["reason"] == "ambiguous"
    assert m["margin"] == pytest.approx(0.0, abs=1e-5)
    # 放宽差距要求 → 认第一个（分数相同则按 uid 排序稳定）
    relaxed = face_lib.match(vec(1, 1), threshold=0.4, min_margin=0.0)
    assert relaxed["matched"] is True


def test_match_on_empty_library():
    m = face_lib.match(vec(0))
    assert m["matched"] is False and m["reason"] == "empty_library" and m["uid"] is None


def test_score_all_sorted_desc():
    face_lib.add_sample("far", vec(9), save_photo=False)
    face_lib.add_sample("near", vec(0), save_photo=False)
    scored = face_lib.score_all(vec(0))
    assert [uid for uid, _ in scored][0] == "near"


# ---------------------------------------------------------------------------
# 脏数据与安全
# ---------------------------------------------------------------------------
def test_corrupt_sample_is_skipped_not_fatal():
    face_lib.add_sample("a", vec(0), save_photo=False)
    bad = os.path.join(face_lib.faces_dir(), "a", "broken.npz")
    open(bad, "wb").write(b"not a real npz")
    arr = face_lib.embeddings("a")                     # 坏样本被跳过，好样本还在
    assert arr.shape == (1, 512)
    assert any(e["sample"] == "broken.npz" for e in face_lib.load_errors())


def test_wrong_dimension_sample_is_skipped():
    face_lib.add_sample("a", vec(0), save_photo=False)
    d = os.path.join(face_lib.faces_dir(), "a")
    np.savez(os.path.join(d, "wrongdim.npz"), embedding=np.ones(128, np.float32))
    arr = face_lib.embeddings("a")
    assert arr.shape == (1, 512)
    assert any("维度" in e["why"] for e in face_lib.load_errors())


def test_rejects_uid_with_path_traversal():
    for bad in ("..", "../evil", "a/b", "a\\b", "", "   "):
        with pytest.raises(face_lib.FaceLibError):
            face_lib.add_sample(bad, vec(0), save_photo=False)


def test_sample_cap_per_uid(monkeypatch):
    monkeypatch.setattr(_conf, "FACE_LIB_MAX_SAMPLES_PER_UID", 2)
    face_lib.add_sample("a", vec(0), save_photo=False)
    face_lib.add_sample("a", vec(1), save_photo=False)
    with pytest.raises(face_lib.FaceLibError, match="样本已达上限"):
        face_lib.add_sample("a", vec(2), save_photo=False)


def test_uid_cap(monkeypatch):
    monkeypatch.setattr(_conf, "FACE_LIB_MAX_UIDS", 1)
    face_lib.add_sample("a", vec(0), save_photo=False)
    with pytest.raises(face_lib.FaceLibError, match="人数上限"):
        face_lib.add_sample("b", vec(1), save_photo=False)
