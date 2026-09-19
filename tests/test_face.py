# -*- coding: utf-8 -*-
r"""人脸检测测试（vision/face.py）。

分两层，互不拖累：

* **纯逻辑层**（letterbox / decode / NMS / 降级）：不装 onnxruntime、不下载模型
  也能跑——用合成张量断言坐标映射，这类"撤 letterbox"的换算是历史 bug 高发区；
* **真模型层**：仅当 `vision/models/*.onnx` 在盘上时才跑（权重不入库，
  没权重时 skip，不用网络）。

模型文件缺失、依赖缺失时的行为也在测：`available()` 必须返回原因而不是抛栈，
`FaceDetector` 构造要抛 `FaceUnavailable`（面向人的消息）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vision import face as F  # noqa: E402

np = pytest.importorskip("numpy")


def _out(rows):
    """把 [(cx, cy, w, h, score), ...] 拼成模型形状 (1, 5, N)。"""
    arr = np.array(rows, dtype=np.float32).T[None]        # (1, 5, N)
    return arr


# ---------------------------------------------------------------------------
# letterbox：等比缩放 + 居中补边
# ---------------------------------------------------------------------------
def test_letterbox_wide_image_pads_top_bottom():
    new_w, new_h, dw, dh, r = F.letterbox((480, 640), 640)
    assert (new_w, new_h) == (640, 480)          # 宽已满，高按比例
    assert r == pytest.approx(1.0)
    assert dw == 0 and dh == pytest.approx(80.0)


def test_letterbox_tall_image_pads_left_right():
    new_w, new_h, dw, dh, r = F.letterbox((640, 480), 640)
    assert (new_w, new_h) == (480, 640)
    assert dw == pytest.approx(80.0) and dh == 0


def test_letterbox_scales_down_big_image():
    new_w, new_h, dw, dh, r = F.letterbox((3000, 2171), 640)
    assert max(new_w, new_h) == 640
    assert new_w == round(2171 * r) and new_h == round(3000 * r)
    assert r < 1


def test_letterbox_rejects_bad_shape():
    for bad in ((0, 100), (100, 0), (-5, 10)):
        with pytest.raises(ValueError):
            F.letterbox(bad, 640)


# ---------------------------------------------------------------------------
# decode：cxcywh -> xyxy -> 撤 letterbox -> 阈值 -> NMS
# ---------------------------------------------------------------------------
def test_decode_maps_boxes_back_to_original_coords():
    """回归：撤 letterbox 必须严格是 ((xyxy - pad) / r)，不能只除以 r。"""
    out = _out([(100, 100, 50, 50, 0.9)])
    got = F.decode(out, conf=0.25, iou=0.45, r=0.5, dw=10.0, dh=20.0)
    assert len(got) == 1
    # (100±25) -> (75,75,125,125) -> -pad -> (65,55,115,105) -> /0.5
    assert got[0]["box"] == pytest.approx([130.0, 110.0, 230.0, 210.0])
    assert got[0]["score"] == pytest.approx(0.9, abs=1e-6)


def test_decode_filters_below_conf():
    out = _out([(100, 100, 50, 50, 0.9), (300, 300, 40, 40, 0.2)])
    assert len(F.decode(out, conf=0.25)) == 1
    assert len(F.decode(out, conf=0.1)) == 2
    assert F.decode(_out([(1, 1, 2, 2, 0.01)]), conf=0.5) == []


def test_decode_suppresses_duplicate_boxes():
    """同一张脸的两个重叠框只留分高的那个。"""
    out = _out([(100, 100, 50, 50, 0.9), (102, 101, 50, 50, 0.8)])
    got = F.decode(out, conf=0.25, iou=0.45)
    assert len(got) == 1 and got[0]["score"] == pytest.approx(0.9)


def test_decode_keeps_distinct_boxes():
    out = _out([(100, 100, 50, 50, 0.9), (400, 400, 50, 50, 0.85)])
    assert len(F.decode(out, conf=0.25)) == 2


def test_decode_sorted_by_score_desc():
    out = _out([(100, 100, 50, 50, 0.3), (400, 400, 50, 50, 0.95)])
    got = F.decode(out, conf=0.25)
    assert [round(g["score"], 2) for g in got] == [0.95, 0.3]


def test_decode_clips_to_image_bounds():
    out = _out([(10, 10, 100, 100, 0.9)])                 # 左上角一半在图外
    got = F.decode(out, conf=0.25, r=1.0, orig_shape=(480, 640))
    x1, y1, x2, y2 = got[0]["box"]
    assert x1 >= 0 and y1 >= 0 and x2 <= 640 and y2 <= 480


def test_decode_accepts_both_output_layouts():
    """(1,5,N) 与 (5,N) / (N,5) 都要能吃（不同导出口径）。"""
    a = _out([(100, 100, 50, 50, 0.9)])
    assert len(F.decode(a, conf=0.25)) == 1
    assert len(F.decode(a[0], conf=0.25)) == 1
    assert len(F.decode(a[0].T, conf=0.25)) == 1


def test_decode_rejects_wrong_shape():
    with pytest.raises(ValueError):
        F.decode(np.zeros((1, 6, 10), np.float32), conf=0.25)


# ---------------------------------------------------------------------------
# NMS
# ---------------------------------------------------------------------------
def test_nms_suppresses_overlap_and_keeps_top():
    boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [50, 50, 60, 60]], np.float32)
    scores = np.array([0.5, 0.9, 0.6], np.float32)
    keep = F.nms(boxes, scores, iou=0.5)
    assert keep == [1, 2]                                  # 索引 0 被 1 抑制


def test_nms_empty_returns_empty():
    assert F.nms(np.zeros((0, 4), np.float32), np.zeros((0,), np.float32)) == []


def test_nms_keeps_all_when_disjoint():
    boxes = np.array([[0, 0, 5, 5], [100, 100, 105, 105]], np.float32)
    assert len(F.nms(boxes, np.array([0.5, 0.5], np.float32))) == 2


# ---------------------------------------------------------------------------
# 降级路径（AGENTS.md 系统稳健性）
# ---------------------------------------------------------------------------
def test_available_reports_missing_dependency(monkeypatch):
    monkeypatch.setattr(F, "_MISSING_DEPS", ["onnxruntime"])
    ok, reason = F.available()
    assert ok is False
    assert "onnxruntime" in reason and "pip install" in reason


def test_available_reports_missing_model(monkeypatch, tmp_path):
    monkeypatch.setattr(F, "model_path", lambda: str(tmp_path / "nope.onnx"))
    ok, reason = F.available()
    assert ok is False
    assert "模型不存在" in reason and "FACE_MODEL_PATH" in reason


def test_model_path_env_override(monkeypatch, tmp_path):
    p = str(tmp_path / "custom.onnx")
    monkeypatch.setenv("FACE_MODEL_PATH", p)
    assert F.model_path() == p


def test_detector_raises_friendly_error_when_model_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(F, "DEFAULT_MODEL_PATH", str(tmp_path / "nope.onnx"))
    monkeypatch.delenv("FACE_MODEL_PATH", raising=False)
    with pytest.raises(F.FaceUnavailable, match="模型不存在"):
        F.FaceDetector()


def test_import_chain_does_not_need_onnxruntime():
    """红线：可选依赖不进顶层硬 import —— 没有 onnxruntime 也要能 import。"""
    import importlib
    mod = importlib.import_module("vision.face")
    assert hasattr(mod, "FaceDetector")


# ---------------------------------------------------------------------------
# 真模型（有权重才跑；权重不入库，缺失时 skip）
# ---------------------------------------------------------------------------
_HAS_MODEL = os.path.isfile(F.DEFAULT_MODEL_PATH)
_HAS_RUNTIME = not [d for d in ("numpy", "onnxruntime") if d in F._MISSING_DEPS]


@pytest.mark.skipif(not (_HAS_MODEL and _HAS_RUNTIME),
                    reason="本地无 ONNX 权重或 onnxruntime（python -m vision.face --download）")
def test_real_model_detects_lena_face():
    """标准测试图 Lena：应恰好检出 1 张脸，且框落在脸部区域。"""
    cv2 = pytest.importorskip("cv2")
    base = Path(__file__).resolve().parent.parent / "vision" / "testdata" / "portrait_lena.jpg"
    if not base.is_file():
        pytest.skip("缺测试图 portrait_lena.jpg")
    img = cv2.imread(str(base))
    det = F.FaceDetector()
    faces = det.detect(img)
    assert len(faces) == 1, faces
    x1, y1, x2, y2 = faces[0]["box"]
    h, w = img.shape[:2]
    assert faces[0]["score"] > 0.5
    # 脸在图中央偏上，框不能跑到边角
    assert 0.2 * w < (x1 + x2) / 2 < 0.8 * w
    assert 0.1 * h < (y1 + y2) / 2 < 0.8 * h
    assert det.last_ms and det.last_ms > 0


@pytest.mark.skipif(not (_HAS_MODEL and _HAS_RUNTIME),
                    reason="本地无 ONNX 权重或 onnxruntime")
def test_real_model_no_face_on_texture():
    """无人脸的纹理图必须 0 检出（误检比漏检更毁体验：会乱切 uid）。"""
    cv2 = pytest.importorskip("cv2")
    base = Path(__file__).resolve().parent.parent / "vision" / "testdata" / "noface_graf.png"
    if not base.is_file():
        pytest.skip("缺测试图 noface_graf.png")
    img = cv2.imread(str(base))
    assert F.FaceDetector().detect(img) == []


@pytest.mark.skipif(not (_HAS_MODEL and _HAS_RUNTIME),
                    reason="本地无 ONNX 权重或 onnxruntime")
def test_real_model_group_photo_finds_many_faces():
    """集体照要多检出：Solvay 1927 合影是 29 人（已知真值）。"""
    cv2 = pytest.importorskip("cv2")
    base = Path(__file__).resolve().parent.parent / "vision" / "testdata" / "group_solvay.jpg"
    if not base.is_file():
        pytest.skip("缺测试图 group_solvay.jpg")
    img = cv2.imread(str(base))
    faces = F.FaceDetector(conf=0.3).detect(img)
    assert len(faces) >= 25, "只检出 %d 张，集体照应 >= 25" % len(faces)


@pytest.mark.skipif(not (_HAS_MODEL and _HAS_RUNTIME),
                    reason="本地无 ONNX 权重或 onnxruntime")
def test_real_model_rejects_bad_input():
    det = F.FaceDetector()
    with pytest.raises(ValueError):
        det.detect(np.zeros((10, 10), np.uint8))          # 不是 HxWx3


# ---------------------------------------------------------------------------
# 画框（需要 cv2）
# ---------------------------------------------------------------------------
def test_draw_marks_box_without_touching_source():
    cv2 = pytest.importorskip("cv2")
    img = np.zeros((64, 64, 3), np.uint8)
    faces = [{"box": [10, 10, 40, 40], "score": 0.9}]
    out = F.draw(img, faces)
    assert img.sum() == 0, "原图被改了"
    assert out[10, 10].tolist() != [0, 0, 0]              # 左上角画上了
    assert out.sum() > 0


def test_draw_needs_cv2(monkeypatch):
    monkeypatch.setattr(F, "cv2", None)
    with pytest.raises(F.FaceUnavailable, match="opencv"):
        F.draw(np.zeros((4, 4, 3), np.uint8), [])
