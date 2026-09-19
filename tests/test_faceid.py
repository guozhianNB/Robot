# -*- coding: utf-8 -*-
r"""人脸识别模型测试（`vision/faceid.py`，ArcFace）。

分两层：
  * **纯逻辑**（对齐 / 预处理 / 归一化 / 余弦）：不需要模型，用合成图断言几何与
    **通道口径**（BGR→RGB 这种错不会报错、只会"识别变差"，必须用测试盯住）；
  * **真模型**（有权重才跑）：同一张脸的不同裁剪应高度相似、不同人应明显更低 ——
    这是"识别到底有没有在工作"的最小可信证据。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vision import faceid as F  # noqa: E402

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")

TESTDATA = Path(__file__).resolve().parent.parent / "vision" / "testdata"
_HAS_MODEL = Path(F.model_path()).is_file()


# ---------------------------------------------------------------------------
# 对齐
# ---------------------------------------------------------------------------
def test_align_face_returns_square_of_requested_size():
    img = np.zeros((200, 300, 3), np.uint8)
    out = F.align_face(img, [100, 80, 160, 140], size=112, margin=0.25)
    assert out.shape == (112, 112, 3)


def test_align_face_expands_box_by_margin():
    """外扩比例要对：框占裁剪后画面的比例应约为 1/(1+2m)。"""
    img = np.zeros((400, 400, 3), np.uint8)
    img[150:250, 150:250] = 255                      # 100x100 的"脸"
    for margin in (0.0, 0.25, 0.5):
        out = F.align_face(img, [150, 150, 250, 250], size=200, margin=margin)
        white = float((out.mean(axis=2) > 127).mean())
        expected = (1.0 / (1.0 + 2 * margin)) ** 2
        assert abs(white - expected) < 0.05, (margin, white, expected)


def test_align_face_replicates_edges_near_border():
    """贴边的框用边缘复制补边，不能补出黑边（黑边归一化后是强负值，会伤识别）。"""
    img = np.full((100, 100, 3), 200, np.uint8)
    out = F.align_face(img, [0, 0, 40, 40], size=112, margin=0.25)
    assert out.min() > 100                            # 边缘复制：不该出现黑


def test_align_face_rejects_tiny_or_out_of_bounds_box():
    img = np.zeros((50, 50, 3), np.uint8)
    with pytest.raises(ValueError):
        F.align_face(img, [10, 10, 11, 11], size=112)
    with pytest.raises(ValueError):
        F.align_face(img, [500, 500, 520, 520], size=112)


# ---------------------------------------------------------------------------
# 预处理：形状 / 取值范围 / **通道顺序**
# ---------------------------------------------------------------------------
def test_preprocess_shape_and_range():
    crop = np.full((112, 112, 3), 128, np.uint8)
    blob = F.preprocess(crop)
    assert blob.shape == (1, 3, 112, 112) and blob.dtype == np.float32
    assert blob.min() >= -1.0 and blob.max() <= 1.0
    assert abs(float(blob.mean())) < 0.02             # 128 ≈ 中性灰 → 归一化后接近 0


def test_preprocess_uses_rgb_order():
    """纯红 BGR=(0,0,255)：转成 RGB 后通道 0 应最大、通道 2 应最小。

    顺序错了（直接送 BGR）不会报错，只会让识别率悄悄下降 —— 所以这条必须在。
    注意归一化是 (x-127.5)/127.5：原始 0 → -1.0，原始 255 → +1.0。
    """
    crop = np.zeros((112, 112, 3), np.uint8)
    crop[:, :, 2] = 255                              # BGR 的 R 通道
    blob = F.preprocess(crop)
    assert blob[0, 0].mean() == pytest.approx(1.0, abs=1e-3)    # R（索引 0）
    assert blob[0, 1].mean() == pytest.approx(-1.0, abs=1e-3)   # G
    assert blob[0, 2].mean() == pytest.approx(-1.0, abs=1e-3)   # B（索引 2）


def test_preprocess_resizes_when_needed():
    blob = F.preprocess(np.zeros((50, 80, 3), np.uint8))
    assert blob.shape == (1, 3, 112, 112)


# ---------------------------------------------------------------------------
# 归一化与余弦
# ---------------------------------------------------------------------------
def test_normalize_unit_length_and_zero_vector():
    v = F.normalize(np.array([3.0, 4.0], np.float32))
    assert float(np.linalg.norm(v)) == pytest.approx(1.0)
    z = F.normalize(np.zeros(4, np.float32))
    assert float(np.linalg.norm(z)) == 0.0            # 零向量不炸（不除零）


def test_cosine_known_angles():
    assert F.cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert F.cosine([1, 0], [-1, 0]) == pytest.approx(-1.0)
    assert F.cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    # 未归一化的输入也应得到同样的余弦（内部会归一化）
    assert F.cosine([3, 0], [10, 0]) == pytest.approx(1.0)


def test_cosine_rejects_dim_mismatch():
    with pytest.raises(ValueError):
        F.cosine([1, 0, 0], [1, 0])


# ---------------------------------------------------------------------------
# 降级与配置
# ---------------------------------------------------------------------------
def test_model_path_env_override(monkeypatch, tmp_path):
    p = str(tmp_path / "custom.onnx")
    monkeypatch.setenv(F.MODEL_ENV, p)
    assert F.model_path() == p


def test_embedder_raises_friendly_error_when_model_missing(monkeypatch, tmp_path):
    monkeypatch.setenv(F.MODEL_ENV, str(tmp_path / "nope.onnx"))
    with pytest.raises(F.FaceUnavailable, match="模型不存在"):
        F.FaceEmbedder()


def test_fetch_model_rejects_unknown_variant():
    with pytest.raises(ValueError):
        F.fetch_model(variant="nonsense")


def test_variants_declared_with_sizes_and_notes():
    """两个变体都要登记（默认轻量版：R50 本机实测 324ms/张，mbf 仅 27ms）。"""
    assert set(F.VARIANTS) == {"r50", "mbf"}
    assert F.DEFAULT_VARIANT in F.VARIANTS
    assert F.variant_path("r50").endswith("arcface_r50.onnx")
    assert F.variant_path("mbf").endswith("arcface_mbf.onnx")


def test_import_chain_safe_without_onnxruntime():
    """红线：可选依赖不进顶层硬 import —— 缺 onnxruntime 也要能 import 本模块。"""
    import importlib
    mod = importlib.import_module("vision.faceid")
    assert hasattr(mod, "FaceEmbedder")


# ---------------------------------------------------------------------------
# 真模型（有权重才跑）
# ---------------------------------------------------------------------------
pytestmark = []


@pytest.mark.skipif(not _HAS_MODEL, reason="本地无 ArcFace 权重（python -m vision.faceid --download）")
def test_real_model_embedding_is_normalized_512d():
    img = cv2.imread(str(TESTDATA / "portrait_lena.jpg"))
    if img is None:
        pytest.skip("缺测试图 portrait_lena.jpg")
    emb = F.FaceEmbedder()
    v = emb.embed_face(img, [212, 187, 357, 389])      # 已知的人脸框
    assert v.shape == (512,)
    assert float(np.linalg.norm(v)) == pytest.approx(1.0, abs=1e-3)
    assert emb.last_ms and emb.last_ms > 0
    assert emb.dim == 512


@pytest.mark.skipif(not _HAS_MODEL, reason="本地无 ArcFace 权重")
def test_real_model_same_face_crops_are_similar():
    """同一张脸的不同裁剪（外扩比例不同）应高度相似 —— 阈值下限的实测锚点。"""
    img = cv2.imread(str(TESTDATA / "portrait_lena.jpg"))
    if img is None:
        pytest.skip("缺测试图 portrait_lena.jpg")
    box = [212, 187, 357, 389]
    emb = F.FaceEmbedder()
    a = emb.embed_face(img, box, margin=0.15)
    b = emb.embed_face(img, box, margin=0.35)
    cos = F.cosine(a, b)
    assert cos > 0.5, "同一张脸的不同裁剪只有 %.3f，偏低" % cos


@pytest.mark.skipif(not _HAS_MODEL, reason="本地无 ArcFace 权重")
def test_real_model_different_people_score_lower_than_same_person():
    """集体照里两个人之间的相似度，必须明显低于"同一人的不同裁剪"。"""
    from vision.face import FaceDetector
    img = cv2.imread(str(TESTDATA / "group_solvay.jpg"))
    if img is None:
        pytest.skip("缺测试图 group_solvay.jpg")
    det = FaceDetector(conf=0.3)
    faces = det.detect(img)
    if len(faces) < 2:
        pytest.skip("这张图里检到的人脸不足 2 张")
    faces.sort(key=lambda f: (f["box"][2] - f["box"][0]) * (f["box"][3] - f["box"][1]),
               reverse=True)
    emb = F.FaceEmbedder()
    person_a, person_b = faces[0]["box"], faces[1]["box"]
    same = F.cosine(emb.embed_face(img, person_a, margin=0.15),
                    emb.embed_face(img, person_a, margin=0.35))
    diff = F.cosine(emb.embed_face(img, person_a), emb.embed_face(img, person_b))
    assert same > diff, "同一人 %.3f 竟不高于不同人 %.3f" % (same, diff)
    assert same - diff > 0.1, "区分度太小：同人 %.3f / 异人 %.3f" % (same, diff)
