# -*- coding: utf-8 -*-
r"""人脸接口层测试（`LLM/face_api.py` + `/api/face/*` 路由）。

只测"判定口径"与"接口形状"，**不需要摄像头、不需要 ONNX 模型**：
  * 判定器（`StabilityTracker`）是纯逻辑：喂合成的框列表即可断言
    "连续 N 帧一致 / 中断归零 / 换人归零 / 多人歧义 / 低分不计入 / 身份稳定才可切换"；
  * 路由层用替身替换 `face_api._grab_frame` 与 `face_api._get_detector`。

真模型 + 真摄像头的活体验证在 `.ptmp_shim/live_face_test.py` / `live_face_two.py`
（需设备权限，不进单测）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from LLM import face_api, server  # noqa: E402
from LLM.face_api import StabilityTracker, _iou  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_tracker(tmp_path, monkeypatch):
    """判定器是模块级单例（路由共用），每个用例前后都清干净。

    审计也指到临时文件：`probe()` 会在判定翻转时写 `face_state` 审计，不隔离的话
    单测会往真实 `LLM/data/audit.jsonl` 塞记录（管理台的审计页会看到"假事件"）。
    """
    from LLM import log as audit
    monkeypatch.setattr(audit, "AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    face_api.reset()
    yield
    face_api.reset()


def box(x1, y1, x2, y2, score=0.9, identity=None):
    f = {"box": [x1, y1, x2, y2], "score": score}
    if identity is not None:
        f["identity"] = identity
    return f


# ---------------------------------------------------------------------------
# IoU
# ---------------------------------------------------------------------------
def test_iou_basics():
    assert _iou([0, 0, 10, 10], [0, 0, 10, 10]) == pytest.approx(1.0)
    assert _iou([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0
    assert _iou([0, 0, 10, 10], [5, 0, 15, 10]) == pytest.approx(1 / 3, abs=1e-4)


# ---------------------------------------------------------------------------
# 连续 N 帧一致
# ---------------------------------------------------------------------------
def test_stable_only_after_n_consecutive_frames():
    t = StabilityTracker(stable_frames=5, conf_thr=0.6)
    f = box(100, 100, 200, 260)
    for i in range(1, 5):
        v = t.update([f])
        assert v["frames"] == i
        assert v["stable"] is False
        assert v["reason"] == "not_enough_frames"
    v = t.update([f])
    assert v["frames"] == 5
    assert v["stable"] is True
    assert v["count"] == 1
    assert v["ambiguous"] is False
    # 识别未接入 → 能判"有人脸稳定"，但不能判"可以切换"
    assert v["switchable"] is False
    assert v["reason"] == "no_identity"
    assert v["box"] == [100.0, 100.0, 200.0, 260.0]


def test_gap_resets_consecutive_count():
    """偶发漏检（实测 15 帧里有 1 帧）必须把"连续"打断，不能靠累计蒙过去。"""
    t = StabilityTracker(stable_frames=3, conf_thr=0.6)
    f = box(100, 100, 200, 260)
    for _ in range(3):
        t.update([f])
    assert t.state()["stable"] is True
    v = t.update([])                                   # 这一帧没检到
    assert v["frames"] == 0 and v["stable"] is False and v["reason"] == "no_face"
    v = t.update([f])                                  # 重新起算
    assert v["frames"] == 1 and v["stable"] is False


def test_far_jump_starts_new_track_and_resets_frames():
    """同一帧里人换了位置（IoU 低于阈值）→ 视为新轨迹，一致帧数重新起算。"""
    t = StabilityTracker(stable_frames=3, conf_thr=0.6, iou_thr=0.3, max_age=2)
    for _ in range(3):
        t.update([box(100, 100, 200, 260)])
    assert t.state()["stable"] is True
    v = t.update([box(900, 100, 1000, 260)])           # 画面另一头
    assert v["frames"] == 1 and v["stable"] is False
    assert len(v["tracks"]) == 2                       # 旧轨迹还在（等它回来）


def test_jitter_still_same_track():
    """真人会晃：小幅抖动必须仍是同一条轨迹（否则永远攒不满连续帧）。"""
    t = StabilityTracker(stable_frames=5, conf_thr=0.6, iou_thr=0.3)
    for dx in (0, 4, 8, 12, 16):
        v = t.update([box(100 + dx, 100, 200 + dx, 260)])
    assert v["frames"] == 5 and v["stable"] is True
    assert len(v["tracks"]) == 1 and v["tracks"][0]["hits"] == 5


def test_two_faces_are_ambiguous():
    """两个人同时稳定：能判"有人脸稳定"，但**不允许切换**（歧义）。"""
    t = StabilityTracker(stable_frames=3, conf_thr=0.6)
    a, b = box(100, 100, 200, 260), box(600, 100, 700, 260)
    for _ in range(3):
        v = t.update([a, b])
    assert v["stable"] is True
    assert v["count"] == 2 and v["ambiguous"] is True
    assert v["switchable"] is False
    assert v["reason"] == "multiple_faces"


def test_low_score_track_never_stable():
    """分数不达标（实测远处那位只有 0.46~0.53）→ 命中够多也不算稳定。"""
    t = StabilityTracker(stable_frames=3, conf_thr=0.6)
    f = box(100, 100, 200, 260, score=0.40)
    for _ in range(6):
        v = t.update([f])
    assert v["stable"] is False
    assert v["reason"] == "low_score"
    assert v["tracks"][0]["hits"] == 6                 # 命中是够的，是分不够


def test_track_dropped_after_max_age():
    t = StabilityTracker(stable_frames=2, conf_thr=0.6, max_age=3)
    t.update([box(100, 100, 200, 260)])
    for _ in range(3):
        t.update([])
    assert len(t.state()["tracks"]) == 1               # 丢 3 帧内还留着
    t.update([])
    assert t.state()["tracks"] == []                   # 超过 max_age 丢弃


def test_state_read_does_not_advance_counters():
    t = StabilityTracker(stable_frames=3, conf_thr=0.6)
    t.update([box(100, 100, 200, 260)])
    for _ in range(5):
        assert t.state()["frames"] == 1


def test_reset_clears_everything():
    t = StabilityTracker(stable_frames=2, conf_thr=0.6)
    t.update([box(100, 100, 200, 260)])
    t.reset()
    v = t.state()
    assert v["frames"] == 0 and v["tracks"] == [] and v["reason"] == "no_face"


# ---------------------------------------------------------------------------
# 为身份切换打底：identity 一接上就该可用（判定逻辑不改）
# ---------------------------------------------------------------------------
def test_identity_makes_it_switchable():
    """每个 face 带上 identity 后：检测稳 + 身份连续 N 帧同一人 → switchable 变 True。"""
    t = StabilityTracker(stable_frames=3, conf_thr=0.6)
    plain, named = box(100, 100, 200, 260), box(100, 100, 200, 260, identity="elder_001")
    t.update([plain])
    t.update([plain])
    v = t.update([named])                              # 第 3 帧识别才给出身份
    assert v["stable"] is True                         # 人脸稳定出现：够了
    assert v["identity"] == "elder_001" and v["identity_frames"] == 1
    assert v["switchable"] is False                    # 身份只稳了 1 帧 → 还不能切
    assert v["reason"] == "identity_unstable"
    t.update([named])
    v = t.update([named])                              # 身份连续 3 帧
    assert v["identity_frames"] == 3
    assert v["switchable"] is True and v["reason"] == "ok"


def test_identity_change_restarts_identity_counting():
    """同一位置换成另一个人（身份 A→B）→ 身份计数重算，不给切换；检测连续不受影响。"""
    t = StabilityTracker(stable_frames=2, conf_thr=0.6)
    t.update([box(100, 100, 200, 260, identity="elder_001")])
    v = t.update([box(100, 100, 200, 260, identity="elder_002")])
    assert v["identity"] == "elder_002" and v["identity_frames"] == 1
    assert v["switchable"] is False and v["reason"] == "identity_unstable"
    assert v["frames"] == 2                            # 人脸一直在，检测层面照样连续


def test_identity_lost_resets_identity_hits():
    """识别这一帧没给出身份（None）→ 身份连续帧数归零，但仍算"有人脸稳定"。"""
    t = StabilityTracker(stable_frames=2, conf_thr=0.6)
    t.update([box(100, 100, 200, 260, identity="elder_001")])
    v = t.update([box(100, 100, 200, 260)])
    assert v["stable"] is True and v["identity"] is None
    assert v["identity_frames"] == 0 and v["switchable"] is False
    assert v["reason"] == "no_identity"


# ---------------------------------------------------------------------------
# 两道门槛分开（真机 2026-09-18 踩到后加的）：检测分管"稳不稳"，身份分管"能不能切"
# ---------------------------------------------------------------------------
def _id_box(score, ident, ident_score):
    f = box(100, 100, 200, 260, score=score)
    f["identity"] = ident
    f["identity_score"] = ident_score
    return f


def test_identity_low_score_blocks_switchable():
    """认出来了、连续也够，但"认得多像"不达标 → 不给切换（独立闸门）。"""
    t = StabilityTracker(stable_frames=3, conf_thr=0.45, identity_conf=0.55)
    for _ in range(3):
        v = t.update([_id_box(0.50, "elder_001", 0.50)])
    assert v["stable"] is True                     # 检测层：算稳定出现
    assert v["identity"] == "elder_001"
    assert v["switchable"] is False                # 但不够确定到可以切主体
    assert v["reason"] == "identity_low_score"


def test_identity_score_above_gate_allows_switchable():
    t = StabilityTracker(stable_frames=3, conf_thr=0.45, identity_conf=0.55)
    for _ in range(3):
        v = t.update([_id_box(0.50, "elder_001", 0.80)])
    assert v["switchable"] is True and v["reason"] == "ok"
    assert v["identity_score"] == pytest.approx(0.80, abs=1e-3)
    assert v["identity_conf"] == 0.55


def test_detection_gate_accepts_low_real_world_scores():
    """回归（真机实测）：弱光下检测分 0.44~0.53 也要算"稳定出现"；低于门槛仍拦。"""
    t = StabilityTracker(stable_frames=3, conf_thr=0.45)
    for _ in range(3):
        v = t.update([box(100, 100, 200, 260, score=0.47)])
    assert v["stable"] is True
    assert v["reason"] not in ("low_score", "not_enough_frames")

    t2 = StabilityTracker(stable_frames=3, conf_thr=0.45)
    for _ in range(3):
        v2 = t2.update([box(100, 100, 200, 260, score=0.40)])
    assert v2["stable"] is False and v2["reason"] == "low_score"


def test_identity_score_is_averaged_over_the_streak():
    """身份分取"当前身份连续段"的平均 —— 单帧抖动不该把闸门打穿，也不该被一帧拉高。"""
    t = StabilityTracker(stable_frames=3, conf_thr=0.45, identity_conf=0.55)
    for sc in (0.90, 0.50, 0.90):
        v = t.update([_id_box(0.80, "elder_001", sc)])
    assert v["identity_score"] == pytest.approx((0.90 + 0.50 + 0.90) / 3, abs=1e-3)
    assert v["switchable"] is True                 # 平均 0.767 ≥ 0.55

    t2 = StabilityTracker(stable_frames=3, conf_thr=0.45, identity_conf=0.55)
    for sc in (0.90, 0.30, 0.30):
        v2 = t2.update([_id_box(0.80, "elder_001", sc)])
    assert v2["switchable"] is False and v2["reason"] == "identity_low_score"


def test_identity_gate_skipped_when_no_score_provided():
    """调用方没给相似度（如单测替身）时视为不设该闸门 —— 保持向后兼容。"""
    t = StabilityTracker(stable_frames=2, conf_thr=0.45, identity_conf=0.55)
    for _ in range(2):
        v = t.update([box(100, 100, 200, 260, identity="elder_001")])
    assert v["identity_score"] is None
    assert v["switchable"] is True


# ---------------------------------------------------------------------------
# 跨帧关联的兜底（真机实测）：稀疏轮询下 IoU 会失真，靠"中心位移 + 尺寸比"续上轨迹
# ---------------------------------------------------------------------------
def test_centroid_fallback_keeps_track_when_iou_drops():
    """人挪了半张脸的距离（IoU 掉到阈值下）仍算同一条轨迹 —— 否则"连续 N 帧"永远攒不住。"""
    t = StabilityTracker(stable_frames=3, conf_thr=0.45, iou_thr=0.3, max_jump=1.0)
    for dx in (0, 80, 160):                    # 每帧中心移 80 px，框短边 100 px
        v = t.update([box(100 + dx, 100, 200 + dx, 260, score=0.8)])
        assert v["reason"] != "no_face"
    assert len(v["tracks"]) == 1, "同一人被拆成了多条轨迹"
    assert v["tracks"][0]["hits"] == 3
    assert v["frames"] == 3 and v["stable"] is True


def test_centroid_fallback_rejects_far_face():
    """位移超过容差 → 仍是新轨迹（不能因为"兜底"把远处另一个人也算成同一个）。"""
    t = StabilityTracker(stable_frames=2, conf_thr=0.45, iou_thr=0.3, max_jump=1.0)
    t.update([box(100, 100, 200, 260)])        # 框短边 100 → 容差 100 px
    v = t.update([box(400, 100, 500, 260)])    # 中心移 300 px
    assert len(v["tracks"]) == 2
    assert v["frames"] == 1 and v["stable"] is False


def test_centroid_fallback_rejects_size_mismatch():
    """中心几乎不动但尺寸差 3 倍 → 不是同一个人（挡住"远处小脸凑近大脸"这类错配）。"""
    t = StabilityTracker(stable_frames=2, conf_thr=0.45, iou_thr=0.3, max_jump=1.0)
    t.update([box(100, 100, 200, 260)])        # 短边 100
    v = t.update([box(100, 100, 400, 580)])    # 短边 300，IoU 很低
    assert len(v["tracks"]) == 2


def test_reason_is_frames_reset_when_track_set_changes():
    """回归：轨迹集合刚变过时，reason 必须说明白（旧实现会显示 "ok" 却 stable=False，误导排查）。"""
    t = StabilityTracker(stable_frames=3, conf_thr=0.45, identity_conf=0.55)
    for _ in range(3):
        t.update([_id_box(0.8, "elder_001", 0.9)])
    assert t.state()["stable"] is True
    # 画面里多出第二个人 → 轨迹集合变了 → 一致帧数重新起算
    v = t.update([_id_box(0.8, "elder_001", 0.9), box(600, 100, 700, 260, score=0.8)])
    assert v["frames"] == 1
    assert v["stable"] is False and v["switchable"] is False
    assert v["reason"] == "frames_reset"


# ---------------------------------------------------------------------------
# 路由层（替身，不碰摄像头/模型）
# ---------------------------------------------------------------------------
class _FakeFrame:
    shape = (720, 1280, 3)


class _FakeDetector:
    def __init__(self, faces):
        self._faces = faces
        self.last_ms = 42.0

    def detect(self, bgr):
        return list(self._faces)


@pytest.fixture()
def client():
    """不进 lifespan（不起 reminder/voice 线程），只测路由层。"""
    return TestClient(server.app)


def test_status_route_shape(client):
    r = client.get("/api/face/status")
    j = r.json()
    assert r.status_code == 200 and j["ok"] is True
    assert j["status"] in ("running", "unavailable")
    assert set(j["detector"]) >= {"available", "imgsz", "conf"}
    assert set(j["camera"]) >= {"host", "port", "reachable"}
    assert set(j["thresholds"]) >= {"stable_frames", "stable_conf", "track_iou"}
    assert j["state"]["switchable"] is False          # 识别未接入，永远不可切换
    if j["status"] == "unavailable":
        assert j.get("reason")


def test_state_route_does_not_grab_frame(client, monkeypatch):
    called = []
    monkeypatch.setattr(face_api, "_grab_frame", lambda *a, **k: called.append(1))
    r = client.get("/api/face/state")
    j = r.json()
    assert r.status_code == 200 and j["ok"] is True
    assert j["state"]["reason"] == "no_face"
    assert called == []                                # 纯读，不许取帧


def test_probe_503_when_detector_unavailable(client, monkeypatch):
    monkeypatch.setattr(face_api, "available",
                        lambda: (False, "缺少依赖：onnxruntime（pip install onnxruntime）"))
    r = client.post("/api/face/probe")
    j = r.json()
    assert r.status_code == 503 and j["ok"] is False
    assert "onnxruntime" in j["error"]


def test_probe_503_when_camera_unreachable(client, monkeypatch):
    def boom(*a, **k):
        raise OSError("摄像头服务未启动")
    monkeypatch.setattr(face_api, "_get_detector", lambda: _FakeDetector([]))
    monkeypatch.setattr(face_api, "_grab_frame", boom)
    r = client.post("/api/face/probe")
    j = r.json()
    assert r.status_code == 503 and j["ok"] is False
    assert "摄像头" in j["error"] or "取帧" in j["error"]


def test_probe_returns_faces_and_verdict(client, monkeypatch):
    monkeypatch.setattr(face_api, "_grab_frame", lambda channel=None: _FakeFrame())
    monkeypatch.setattr(face_api, "_get_detector",
                        lambda: _FakeDetector([box(100, 100, 200, 260, score=0.83)]))
    r = client.post("/api/face/probe")
    j = r.json()
    assert r.status_code == 200 and j["ok"] is True
    assert j["frame"] == {"width": 1280, "height": 720}
    assert len(j["faces"]) == 1 and j["faces"][0]["score"] == 0.83
    assert j["verdict"]["frames"] == 1
    assert j["verdict"]["switchable"] is False


def test_probe_becomes_stable_after_n_frames(client, monkeypatch):
    """连打 5 次（配置的连续帧数）→ 第 5 次 verdict.stable 变 True。"""
    monkeypatch.setattr(face_api, "_grab_frame", lambda channel=None: _FakeFrame())
    monkeypatch.setattr(face_api, "_get_detector",
                        lambda: _FakeDetector([box(100, 100, 200, 260, score=0.85)]))
    for i in range(1, conf_stable()):
        j = client.post("/api/face/probe").json()
        assert j["verdict"]["frames"] == i and j["verdict"]["stable"] is False
    j = client.post("/api/face/probe").json()
    assert j["verdict"]["frames"] == conf_stable()
    assert j["verdict"]["stable"] is True
    assert j["verdict"]["count"] == 1


def conf_stable():
    from LLM import conf
    return conf.FACE_STABLE_FRAMES


def test_probe_passes_channel_query(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(face_api, "_grab_frame",
                        lambda channel=None: (seen.update(ch=channel), _FakeFrame())[1])
    monkeypatch.setattr(face_api, "_get_detector", lambda: _FakeDetector([]))
    client.post("/api/face/probe?channel=2")
    assert seen["ch"] == 2


def test_import_chain_never_raises_without_detector(monkeypatch):
    """红线：检测依赖缺失时，本模块与路由仍要能正常报告，不许抛穿。"""
    monkeypatch.setattr(face_api, "_face_module", lambda: (None, "模拟缺依赖"))
    ok, reason = face_api.available()
    assert ok is False and "模拟缺依赖" in reason
    r = face_api.probe()
    assert r["ok"] is False and r["status"] == "unavailable"


# ---------------------------------------------------------------------------
# 识别链路（ArcFace + 样本库）：替身识别器 + 真样本库（落在临时目录）
# ---------------------------------------------------------------------------
np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")

from LLM import conf as _conf  # noqa: E402
from LLM import face_lib  # noqa: E402


class _VecEmbedder:
    """替身识别器：所有脸都返回同一个向量（测身份链路够用，不依赖模型）。"""

    def __init__(self, vec=None):
        self._vec = np.asarray(vec if vec is not None else np.ones(512), np.float32)
        self.model_path = "/tmp/fake_arcface.onnx"
        self.last_ms = 7.0

    def embed(self, crop_bgr):                        # enroll 用
        return self._vec / max(float(np.linalg.norm(self._vec)), 1e-9)

    def embed_face(self, bgr, box, margin=None):      # probe 用
        return self.embed(bgr)


@pytest.fixture()
def face_env(tmp_path, monkeypatch):
    """样本库落在临时目录（别动真人的 LLM/data/faces），并假定识别可用。"""
    monkeypatch.setattr(_conf, "FACE_DIR", tmp_path / "faces")
    monkeypatch.setattr(face_api, "identify_available", lambda: (True, "ok"))
    return face_lib


def _install_fakes(monkeypatch, faces, vec=None, frame=None):
    frame = frame if frame is not None else np.zeros((720, 1280, 3), np.uint8)
    monkeypatch.setattr(face_api, "_grab_frame", lambda channel=None: frame)
    monkeypatch.setattr(face_api, "_get_detector", lambda: _FakeDetector(faces))
    monkeypatch.setattr(face_api, "_get_embedder", lambda: _VecEmbedder(vec))
    return frame


def test_probe_identifies_known_face_and_becomes_switchable(client, monkeypatch, face_env):
    """**核心**：库里已注册 → 连续 N 帧识别一致 → identity 落地、switchable 变真。"""
    vec = np.ones(512, np.float32)
    face_lib.add_sample("elder_001", vec, save_photo=False)
    _install_fakes(monkeypatch, [box(100, 100, 200, 260, score=0.9)], vec=vec)

    frames = 0
    for _ in range(_conf.FACE_STABLE_FRAMES):
        j = client.post("/api/face/probe").json()
        frames += 1
        assert j["faces"][0]["identity"] == "elder_001"
    v = j["verdict"]
    assert v["frames"] == frames and v["stable"] is True
    assert v["identity"] == "elder_001" and v["identity_frames"] == frames
    assert v["switchable"] is True and v["reason"] == "ok"
    assert j["identity"]["identified"] is True
    assert j["identity"]["library_uids"] == 1


def test_probe_unknown_face_is_not_switchable(client, monkeypatch, face_env):
    """库里没有这张脸 → 不瞎认（identity 为空，不能切换）。"""
    enrolled = np.zeros(512, np.float32)
    enrolled[0] = 1.0
    face_lib.add_sample("elder_001", enrolled, save_photo=False)
    other = np.zeros(512, np.float32)
    other[1] = 1.0                                    # 与已注册向量正交 → 余弦 0
    _install_fakes(monkeypatch, [box(100, 100, 200, 260, score=0.9)], vec=other)

    for _ in range(_conf.FACE_STABLE_FRAMES):
        j = client.post("/api/face/probe").json()
    assert j["faces"][0]["identity"] is None
    assert j["faces"][0]["identity_reason"] in ("below_threshold", "ambiguous")
    assert j["verdict"]["switchable"] is False


def test_probe_with_empty_library_reports_empty_library(client, monkeypatch, face_env):
    _install_fakes(monkeypatch, [box(100, 100, 200, 260)], vec=np.ones(512, np.float32))
    j = client.post("/api/face/probe").json()
    assert j["faces"][0]["identity"] is None
    assert j["faces"][0]["identity_reason"] == "empty_library"


def test_enroll_saves_samples_then_list_and_delete(client, monkeypatch, face_env):
    _install_fakes(monkeypatch, [box(100, 100, 220, 280, score=0.9)])

    r = client.post("/api/face/enroll", json={"uid": "elder_002", "frames": 3})
    j = r.json()
    assert r.status_code == 200 and j["ok"] is True
    assert j["added"] == 3 and j["samples_total"] == 3
    assert j["photos_saved"] == 3                     # 默认存对齐后的小脸图
    assert face_lib.list_uids() == ["elder_002"]

    people = client.get("/api/face/people").json()
    assert people["ok"] is True and people["uids"] == 1
    assert people["per_uid"] == {"elder_002": 3}

    d = client.delete("/api/face/people/elder_002").json()
    assert d["ok"] is True and d["removed"] >= 3
    assert client.get("/api/face/people").json()["uids"] == 0


def test_enroll_picks_largest_face(client, monkeypatch, face_env):
    """注册时画面里有多张脸：取**最大的**那张（离镜头最近 = 本人）。"""
    faces = [box(10, 10, 60, 60), box(300, 300, 500, 560, score=0.9)]
    _install_fakes(monkeypatch, faces)
    seen = {}
    real_add = face_lib.add_sample

    def spy(uid, embedding, photo_bgr=None, **kw):
        seen["crop_shape"] = None if photo_bgr is None else photo_bgr.shape
        return real_add(uid, embedding, photo_bgr=photo_bgr, **kw)

    monkeypatch.setattr(face_api.face_lib, "add_sample", spy)
    j = client.post("/api/face/enroll", json={"uid": "elder_003", "frames": 1}).json()
    assert j["ok"] is True and j["added"] == 1
    assert seen["crop_shape"][:2] == (112, 112)       # 存的是对齐后的小脸图


def test_enroll_no_face_is_business_result_not_failure(client, monkeypatch, face_env):
    """取景里没人脸 → 200 + ok:false + status:"no_face"（前端提示"请正对镜头"）。"""
    _install_fakes(monkeypatch, [])
    r = client.post("/api/face/enroll", json={"uid": "elder_004", "frames": 2})
    j = r.json()
    assert r.status_code == 200 and j["ok"] is False
    assert j["status"] == "no_face" and j["added"] == 0


def test_enroll_503_when_identification_unavailable(client, monkeypatch, face_env):
    monkeypatch.setattr(face_api, "identify_available",
                        lambda: (False, "缺少依赖：onnxruntime"))
    r = client.post("/api/face/enroll", json={"uid": "elder_005", "frames": 1})
    assert r.status_code == 503 and "onnxruntime" in r.json()["error"]


def test_enroll_400_on_empty_uid(client, monkeypatch, face_env):
    _install_fakes(monkeypatch, [box(100, 100, 200, 260)])
    r = client.post("/api/face/enroll", json={"uid": "   ", "frames": 1})
    assert r.status_code == 400 and r.json()["status"] == "bad_request"


def test_probe_reports_identity_margin_with_two_people_in_library(client, monkeypatch, face_env):
    """库里有两个人时，probe 要给出"与第二名的差距"（阈值标定的关键数字）。

    回归：早先没把 `identity_margin` 放进响应，标定时只能看到相似度、看不到差距。
    """
    a = np.zeros(512, np.float32)
    a[0] = 1.0
    b = np.zeros(512, np.float32)
    b[1] = 1.0
    face_lib.add_sample("elder_001", a, save_photo=False)
    face_lib.add_sample("elder_002", b, save_photo=False)
    _install_fakes(monkeypatch, [box(100, 100, 200, 260)], vec=a)
    j = client.post("/api/face/probe").json()
    f0 = j["faces"][0]
    assert f0["identity"] == "elder_001"
    assert f0["identity_score"] == pytest.approx(1.0, abs=1e-3)
    assert f0["identity_margin"] == pytest.approx(1.0, abs=1e-3)   # 与 elder_002 的差距


def test_status_reports_embedder_and_library(client, face_env):
    j = client.get("/api/face/status").json()
    assert set(j["embedder"]) >= {"available", "variant", "dim", "align_margin"}
    assert j["embedder"]["dim"] == _conf.FACE_EMBED_DIM
    assert "dir" in j["library"]
    assert j["thresholds"]["match_threshold"] == _conf.FACE_MATCH_THRESHOLD
    # 两道门槛都要暴露出来（排查"为什么不切换"靠它们）
    assert j["thresholds"]["stable_conf"] == _conf.FACE_STABLE_CONF
    assert j["thresholds"]["identity_conf"] == _conf.FACE_IDENTITY_CONF


# ---------------------------------------------------------------------------
# 从图片注册 / 识别（不依赖摄像头）—— "拍一次照 → 第二次认出来"
# ---------------------------------------------------------------------------
def _png_b64(img):
    """把 ndarray 编码成 PNG 的 base64（模拟前端上传的图片）。"""
    ok, buf = cv2.imencode(".png", img)
    assert ok
    import base64
    return base64.b64encode(buf.tobytes()).decode("ascii")


@pytest.fixture()
def still_image():
    """一张"照片"：真实 ndarray（入库要真的裁脸、真的写图）。"""
    return np.full((480, 640, 3), 120, np.uint8)


def test_enroll_photo_then_identify_same_photo(client, monkeypatch, face_env, still_image):
    """**用户要的核心行为**：第一次用照片入库 → 第二次检测同一张照片 → 认出来。"""
    vec = np.ones(512, np.float32)
    _install_fakes(monkeypatch, [box(200, 150, 360, 340, score=0.88)], vec=vec)
    b64 = _png_b64(still_image)

    r = client.post("/api/face/enroll_photo",
                    json={"uid": "elder_010", "image_base64": b64})
    j = r.json()
    assert r.status_code == 200 and j["ok"] is True
    assert j["added"] == 1 and j["samples_total"] == 1
    assert j["library"]["uids"] == 1

    r2 = client.post("/api/face/identify", json={"image_base64": b64})
    j2 = r2.json()
    assert r2.status_code == 200 and j2["ok"] is True
    assert j2["count"] == 1
    f0 = j2["faces"][0]
    assert f0["identity"] == "elder_010"
    assert f0["identity_score"] == pytest.approx(1.0, abs=1e-3)
    assert f0["identity_reason"] == "ok"
    assert j2["library"]["uids"] == 1


def test_identify_photo_unknown_when_not_enrolled(client, monkeypatch, face_env, still_image):
    enrolled = np.zeros(512, np.float32)
    enrolled[0] = 1.0
    face_lib.add_sample("elder_011", enrolled, save_photo=False)
    other = np.zeros(512, np.float32)
    other[1] = 1.0                                    # 正交 → 余弦 0
    _install_fakes(monkeypatch, [box(200, 150, 360, 340)], vec=other)
    j = client.post("/api/face/identify", json={"image_base64": _png_b64(still_image)}).json()
    assert j["ok"] is True and j["faces"][0]["identity"] is None
    assert j["faces"][0]["identity_reason"] in ("below_threshold", "ambiguous")


def test_enroll_photo_rejects_bad_base64(client, face_env):
    r = client.post("/api/face/enroll_photo",
                    json={"uid": "elder_012", "image_base64": "这不是base64!!"})
    j = r.json()
    assert r.status_code == 400 and j["status"] == "bad_request"
    assert "base64" in j["error"]


def test_enroll_photo_rejects_undecodable_image(client, face_env):
    """是合法 base64 但不是图片 → 400（别把"数据坏了"当成"没人脸"）。"""
    import base64
    r = client.post("/api/face/enroll_photo",
                    json={"uid": "elder_013",
                          "image_base64": base64.b64encode(b"not an image").decode()})
    j = r.json()
    assert r.status_code == 400 and "无法解码" in j["error"]


def test_enroll_photo_no_face_is_business_result(client, monkeypatch, face_env, still_image):
    _install_fakes(monkeypatch, [])
    r = client.post("/api/face/enroll_photo",
                    json={"uid": "elder_014", "image_base64": _png_b64(still_image)})
    j = r.json()
    assert r.status_code == 200 and j["ok"] is False and j["status"] == "no_face"


def test_identify_no_face_is_business_result(client, monkeypatch, face_env, still_image):
    _install_fakes(monkeypatch, [])
    r = client.post("/api/face/identify", json={"image_base64": _png_b64(still_image)})
    j = r.json()
    assert r.status_code == 200 and j["ok"] is False and j["status"] == "no_face"


def test_identify_accepts_data_url_prefix(client, monkeypatch, face_env, still_image):
    """前端 canvas 给的是 data:image/png;base64,… —— 要能吃。"""
    vec = np.ones(512, np.float32)
    face_lib.add_sample("elder_015", vec, save_photo=False)
    _install_fakes(monkeypatch, [box(200, 150, 360, 340)], vec=vec)
    url = "data:image/png;base64," + _png_b64(still_image)
    j = client.post("/api/face/identify", json={"image_base64": url}).json()
    assert j["faces"][0]["identity"] == "elder_015"


def test_enroll_dir_imports_dataset_layout(monkeypatch, face_env, tmp_path):
    """批量导入旧项目的"每人一个文件夹"数据集布局（`dataset/<人名>/*.jpg`）。"""
    base = tmp_path / "dataset"
    for person, n in (("elder_a", 2), ("elder_b", 1)):
        d = base / person
        d.mkdir(parents=True)
        for i in range(n):
            cv2.imwrite(str(d / ("%d.jpg" % i)),
                        np.full((300, 300, 3), 100 + i * 20, np.uint8))
    _install_fakes(monkeypatch, [box(80, 60, 220, 240, score=0.8)])
    res = face_api.enroll_dir(str(base))
    assert res["ok"] is True
    assert res["people"] == {"elder_a": 2, "elder_b": 1}
    assert res["total_added"] == 3
    assert face_lib.stats()["per_uid"] == {"elder_a": 2, "elder_b": 1}


def test_enroll_dir_with_single_uid_override(monkeypatch, face_env, tmp_path):
    base = tmp_path / "photos"
    base.mkdir()
    for i in range(3):
        cv2.imwrite(str(base / ("%d.jpg" % i)), np.full((300, 300, 3), 90, np.uint8))
    _install_fakes(monkeypatch, [box(80, 60, 220, 240)])
    res = face_api.enroll_dir(str(base), uid="elder_c")
    assert res["people"] == {"elder_c": 3}


def test_enroll_dir_rejects_missing_dir(face_env, tmp_path):
    res = face_api.enroll_dir(str(tmp_path / "nope"))
    assert res["ok"] is False and res["status"] == "bad_request"


def test_library_stats_flags_under_sampled(face_env):
    face_lib.add_sample("thin", np.ones(512, np.float32), save_photo=False)
    st = face_lib.stats()
    assert st["min_samples"] == _conf.FACE_MIN_SAMPLES_PER_UID
    assert st["under_sampled"] == ["thin"]            # 只有 1 张，低于建议值 3


def test_identify_topk_limits_returned_but_reports_detected(client, monkeypatch, face_env,
                                                            still_image):
    """`topk` 只截断**返回**的脸，但"实际检出几张"必须如实报（回归：早先两者混用）。"""
    faces = [box(10, 10, 60, 60, score=0.6), box(100, 100, 200, 260, score=0.9),
             box(400, 100, 500, 260, score=0.7)]
    _install_fakes(monkeypatch, faces, vec=np.ones(512, np.float32))
    j = client.post("/api/face/identify",
                    json={"image_base64": _png_b64(still_image), "topk": 1}).json()
    assert j["detected"] == 3 and j["count"] == 1
    assert j["faces"][0]["score"] == 0.9               # 返回分数最高的那张


# ---------------------------------------------------------------------------
# 真模型端到端（有权重才跑；**不需要摄像头**）
# ---------------------------------------------------------------------------
REPO = Path(__file__).resolve().parent.parent
TESTDATA = REPO / "vision" / "testdata"


def _models_ready():
    try:
        from vision import face as vf
        from vision import faceid as vi
        return bool(vf.available()[0] and vi.available()[0])
    except Exception:                                  # noqa: BLE001
        return False


_HAS_MODELS = _models_ready()


@pytest.mark.skipif(not _HAS_MODELS, reason="本地缺检测/识别权重（见 vision/人脸识别注意事项.md）")
def test_real_photo_enroll_then_identify(face_env):
    """**用户要的核心行为（真模型版）**：照片入库 → 再识别同一张照片 → 认出这个人。"""
    lena = cv2.imread(str(TESTDATA / "portrait_lena.jpg"))
    if lena is None:
        pytest.skip("缺测试图 portrait_lena.jpg")

    r = face_api.enroll_photo("elder_real", lena)
    assert r["ok"] is True and r["added"] == 1
    assert r["library"]["per_uid"] == {"elder_real": 1}

    j = face_api.identify_photo(lena)
    assert j["ok"] is True and j["detected"] == 1
    f0 = j["faces"][0]
    assert f0["identity"] == "elder_real"
    assert f0["identity_score"] > 0.8                   # 同一张图应高度相似
    assert f0["identity_reason"] == "ok"

    graf = cv2.imread(str(TESTDATA / "noface_graf.png"))
    assert face_api.identify_photo(graf)["status"] == "no_face"   # 无脸图不瞎认


@pytest.mark.skipif(not _HAS_MODELS, reason="本地缺检测/识别权重")
def test_real_cli_recognizes_in_fresh_process(face_env):
    """**跨进程证明"记住了"**：库落在文件里，换个进程（等价重启）照样认得出。"""
    import subprocess
    path = TESTDATA / "portrait_lena.jpg"
    lena = cv2.imread(str(path))
    if lena is None:
        pytest.skip("缺测试图 portrait_lena.jpg")
    assert face_api.enroll_photo("elder_cli", lena)["ok"] is True

    env = {**os.environ, "FACE_DIR": str(_conf.FACE_DIR), "PYTHONUTF8": "1",
           "PYTHONPATH": str(REPO)}
    p = subprocess.run([sys.executable, "-m", "LLM.face_api", "identify", str(path)],
                       cwd=str(REPO), env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    out = (p.stdout or "") + (p.stderr or "")
    assert p.returncode == 0, out
    assert "elder_cli" in out, out


@pytest.mark.skipif(not _HAS_MODELS, reason="本地缺检测/识别权重")
def test_real_unenrolled_face_is_not_recognized(face_env):
    """库里没这个人 → 输出"未知"（宁可不认，不能瞎认）。"""
    solvay = cv2.imread(str(TESTDATA / "group_solvay.jpg"))
    if solvay is None:
        pytest.skip("缺测试图 group_solvay.jpg")
    face_lib.add_sample("elder_other", np.ones(512, np.float32) / np.sqrt(512),
                        save_photo=False)
    j = face_api.identify_photo(solvay, topk=1)
    assert j["ok"] is True and j["detected"] >= 10      # 合影里人很多
    assert j["faces"][0]["identity"] is None            # 都不在库里 → 全部未知
    assert j["faces"][0]["identity_score"] < _conf.FACE_MATCH_THRESHOLD
