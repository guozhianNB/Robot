# -*- coding: utf-8 -*-
r"""人脸接口层：检测器装配 + "连续 N 帧一致"稳定判定（为身份切换打底）。

分层（与 `voice_api.py` 同构）
------------------------------
    vision/face.py    纯检测：YOLOv8-face ONNX，只回答"脸在哪"
    LLM/face_api.py   本模块：可选能力装配 + 取帧 + 跨帧稳定判定 + 降级
    LLM/server.py     路由（`/api/face/*`）

为什么必须"连续 N 帧一致"，不能按单帧切换用户
--------------------------------------------
本机实测（两人同时在画面，16 帧 / imgsz=640）：

* 单人场景 15 帧里**有 1 帧漏检**；两人场景远离镜头那位分数只有 **0.46~0.53**；
* 同一位的框会随身体晃动漂移；画面里走过第三个人会瞬间改变"看到几张脸"。

按单帧结果切主体 = 有人路过就误切、扭头一次就丢身份。所以判定口径是：

1. **跨帧关联**：每帧的人脸框按 IoU 关联到已有轨迹（贪心匹配，1~3 人够用）；
2. **逐轨迹连续命中** `hits`：该轨迹**连续**被看到的帧数（中间丢一帧即归零）；
3. **整体一致帧数** `frames`：连续多少帧"看到的是同一组轨迹（且身份未变）"，
   轨迹集合一变就归零 —— 这就是"连续 N 帧一致"；
4. **稳定** `stable`：`frames >= FACE_STABLE_FRAMES` 且有轨迹 `hits >= 阈值`
   且其**平均置信度** `>= FACE_STABLE_CONF`（低分轨迹不计入）；
5. **可用于切换** `switchable`：还要 `count == 1`（两个人同时稳定 = 歧义，不切）
   且**身份已稳定**（`identity` 非空且 `identity_frames >= N`）。

身份这一格现在恒为 `None`
------------------------
识别（"是谁"）还没接入，所以本模块现在能给出的最强结论是
**"有一张脸稳定出现了 N 帧"**（`stable=True, reason="no_identity"`），
而 `switchable` 恒为 `False`。识别模型接上后，只要在每个 face 字典里补
`{"identity": uid}`，轨迹会自动统计 `identity_hits`，**判定逻辑一行都不用改**。
这也是本步"为身份切换打底"的含义：接口与口径先立住，识别接上去即可用。

降级（AGENTS.md「系统稳健性」）
-----------------------------
可选依赖（numpy/onnxruntime/opencv）或模型缺失 → `available()` 返回原因，
`/api/face/status` 返回 `ok:True + status:"unavailable"`，`probe()` 返回 `ok:False`。
**摄像头服务没跑只影响 probe（取不到帧），模块本身仍 available** —— 服务健康 ≠
功能可用，两者在响应里分开报（`detector` / `camera`）。
"""

import json
import math
import os
import socket
import threading
import time

from . import conf
from . import face_lib
from . import log as audit

# 模块级状态（惰性装配；任何一层失败都只降级、不抛穿）
_vision_face = None            # vision.face 模块（检测）
_vision_error = None           # 检测模块导入失败原因
_detector = None               # FaceDetector 单例（加载 ONNX 有成本）
_detector_error = None
_vision_faceid = None          # vision.faceid 模块（识别 / ArcFace）
_vision_faceid_error = None
_embedder = None               # FaceEmbedder 单例
_embedder_error = None
_lock = threading.RLock()
_last_stable = None            # 上一次稳定判定（用于只在"状态翻转"时写审计，避免刷屏）


# --------------------------------------------------------------------------
# 纯逻辑：IoU + 轨迹 + 稳定判定（不依赖 numpy/onnx，可单独单测）
# --------------------------------------------------------------------------
def _iou(a, b):
    """两个框的交并比（纯 Python，避免为了一个除法引入 numpy）。"""
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, x2 - x1), max(0.0, y2 - y1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    ar = lambda r: max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])   # noqa: E731
    union = ar(a) + ar(b) - inter
    return inter / union if union > 0 else 0.0


class _Track:
    """一条人脸轨迹（同一个人在连续帧里的框序列）。"""

    __slots__ = ("id", "box", "score", "hits", "misses", "identity", "identity_hits",
                 "identity_score", "ident_sum", "ident_n", "seen", "score_sum",
                 "first_ts", "last_ts")

    def __init__(self, tid, face, ts):
        self.id = tid
        self.box = [float(v) for v in face["box"]]
        self.score = float(face.get("score") or 0.0)
        self.score_sum = self.score
        self.hits = 1                 # 连续命中帧数
        self.misses = 0               # 连续丢失帧数（超过 max_age 就丢弃轨迹）
        self.identity = face.get("identity")     # 识别模型接上后才有值
        self.identity_hits = 1 if self.identity else 0
        # 身份相似度：与 identity_hits 同步累计（只在"同一身份连续"期间），
        # 用于**切换闸门**（认得多像），与检测分数（框得多准）分开看。
        sc = face.get("identity_score")
        self.identity_score = float(sc) if sc is not None else None
        self.ident_sum = float(sc) if (self.identity and sc is not None) else 0.0
        self.ident_n = 1 if (self.identity and sc is not None) else 0
        self.seen = True
        self.first_ts = ts
        self.last_ts = ts

    @property
    def avg_score(self):
        return self.score_sum / self.hits if self.hits else 0.0

    @property
    def avg_identity_score(self):
        """当前身份连续段内的平均相似度；一次都没给过分数则为 None（视为不设闸门）。"""
        return self.ident_sum / self.ident_n if self.ident_n else None

    def assoc(self, box, iou_thr, max_jump):
        """本帧这个框还算不算"同一个人"？返回匹配分（越大越像），不算则 None。

        两道判据，先严后宽：

        1. **IoU ≥ iou_thr** —— 连续视频帧的正常情况，最可靠；
        2. **中心位移 ≤ max_jump × 框短边，且尺寸比在 [0.5, 2]** —— **稀疏轮询的兜底**。

        为什么必须有第 2 条（2026-09-18 真机实测教训）：后端是"轮询一帧算一帧"
        （~0.8 s/轮），人在 0.8 秒里自然会挪动，**IoU 会频繁掉到 0.3 以下** → 轨迹被
        当成新目标 → "连续 N 帧"反复归零、`switchable` 刚亮就灭。IoU 对位移与缩放
        极其敏感，中心位移+尺寸比才吃得下这种自然晃动（而"换了个人"通常中心很远或
        尺寸差很多，仍会被判成新轨迹）。
        """
        iou = _iou(box, self.box)
        if iou >= iou_thr:
            return 1.0 + iou
        w1, h1 = self.box[2] - self.box[0], self.box[3] - self.box[1]
        w2, h2 = box[2] - box[0], box[3] - box[1]
        if min(w1, h1, w2, h2) <= 0:
            return None
        cx1, cy1 = (self.box[0] + self.box[2]) / 2.0, (self.box[1] + self.box[3]) / 2.0
        cx2, cy2 = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
        dist = math.hypot(cx2 - cx1, cy2 - cy1)
        s1, s2 = min(w1, h1), min(w2, h2)          # 用短边，框被画面裁掉一边时也稳
        size = (s1 + s2) / 2.0
        ratio = s2 / s1
        if dist <= max_jump * size and 0.5 <= ratio <= 2.0:
            # 位移越近分越高，但始终低于 IoU 命中（保证 IoU 配对优先）
            return 0.5 * (1.0 - dist / (max_jump * size))
        return None

    def hit(self, face, ts):
        """本帧又看到它：框/分数更新，连续命中 +1；身份变了则身份计数重新起算。"""
        self.box = [float(v) for v in face["box"]]
        self.score = float(face.get("score") or 0.0)
        self.score_sum += self.score
        self.hits += 1
        self.misses = 0
        self.seen = True
        self.last_ts = ts
        ident = face.get("identity")
        sc = face.get("identity_score")
        if ident != self.identity:
            self.identity = ident
            self.identity_hits = 1 if ident else 0
            self.ident_sum = float(sc) if (ident and sc is not None) else 0.0
            self.ident_n = 1 if (ident and sc is not None) else 0
        elif ident:
            self.identity_hits += 1
            if sc is not None:
                self.ident_sum += float(sc)
                self.ident_n += 1
        if sc is not None:
            self.identity_score = float(sc)

    def miss(self):
        """本帧没看到：连续命中归零（"连续 N 帧"必须真的连续），但轨迹先留着等重连。"""
        self.misses += 1
        self.seen = False
        self.hits = 0
        self.score_sum = 0.0
        self.identity_hits = 0
        self.ident_sum = 0.0
        self.ident_n = 0

    def snapshot(self):
        return {
            "id": self.id, "box": [round(v) for v in self.box],
            "score": round(self.score, 3), "avg_score": round(self.avg_score, 3),
            "hits": self.hits, "misses": self.misses,
            "identity": self.identity, "identity_hits": self.identity_hits,
            "identity_score": (round(self.avg_identity_score, 4)
                               if self.avg_identity_score is not None else None),
            "seen": self.seen,
        }


class StabilityTracker:
    """跨帧稳定判定器：喂每帧的人脸列表，吐出"能不能用于切换"的结论。

    线程安全（路由可能在线程池里调）。参数默认取 `conf.FACE_*`，测试可显式传入。
    """

    def __init__(self, stable_frames=None, conf_thr=None, iou_thr=None, max_age=None,
                 identity_conf=None, max_jump=None):
        self.stable_frames = int(stable_frames if stable_frames is not None
                                 else conf.FACE_STABLE_FRAMES)
        # 两道门槛分得很清：
        #   conf_thr      = **检测框置信度**门槛（"框得多准"）—— 只管"算不算稳定出现"
        #   identity_conf = **身份相似度**门槛（"认得多像"）—— 只管"能不能据此切换主体"
        # 混成一个门槛会导致：弱光下检测分偏低（真机实测 0.44~0.53）时，即使身份相似度
        # 高达 0.95 也永远不给切换（2026-09-18 真机踩到）。
        self.conf_thr = float(conf_thr if conf_thr is not None else conf.FACE_STABLE_CONF)
        self.identity_conf = float(identity_conf if identity_conf is not None
                                   else conf.FACE_IDENTITY_CONF)
        self.iou_thr = float(iou_thr if iou_thr is not None else conf.FACE_TRACK_IOU)
        # 跨帧关联的兜底：中心位移容差（单位=框短边）。稀疏轮询下 IoU 会失真，见 _Track.assoc
        self.max_jump = float(max_jump if max_jump is not None
                              else conf.FACE_TRACK_MAX_JUMP)
        self.max_age = int(max_age if max_age is not None else conf.FACE_TRACK_MAX_AGE)
        self._lock = threading.RLock()
        self._tracks = []
        self._next_id = 1
        self._frames = 0            # 连续一致帧数
        self._signature = ()        # 上一帧"看到的轨迹+身份"指纹
        self._last = self._verdict()
        self._last_ts = None

    # -- 主入口 --
    def update(self, faces, ts=None):
        """喂一帧的人脸列表（`[{"box":[x1,y1,x2,y2],"score":s,"identity":uid?}, …]`）。

        返回本次判定；同一份判定也可用 `state()` 随时读。
        """
        ts = time.time() if ts is None else ts
        faces = [f for f in (faces or []) if f and f.get("box")]
        with self._lock:
            for t in self._tracks:
                t.seen = False

            # 1) 贪心关联：匹配分大的先配对（IoU 命中优先，中心位移兜底；1~3 人场景够用）
            pairs = []
            for fi, f in enumerate(faces):
                for t in self._tracks:
                    s = t.assoc(f["box"], self.iou_thr, self.max_jump)
                    if s is not None:
                        pairs.append((s, fi, t))
            pairs.sort(key=lambda p: -p[0])
            used_f, used_t = set(), set()
            for _i, fi, t in pairs:
                if fi in used_f or t.id in used_t:
                    continue
                used_f.add(fi)
                used_t.add(t.id)
                t.hit(faces[fi], ts)

            # 2) 没配上的轨迹记丢失；没配上的检测结果开新轨迹
            for t in self._tracks:
                if t.id not in used_t:
                    t.miss()
            for fi, f in enumerate(faces):
                if fi not in used_f:
                    self._tracks.append(_Track(self._next_id, f, ts))
                    self._next_id += 1

            # 3) 轨迹老化（连续丢失超过 max_age 才丢，短暂遮挡不丢身份）
            self._tracks = [t for t in self._tracks if t.misses <= self.max_age]

            # 4) 连续一致帧数：本帧"看到的轨迹"指纹与上帧相同则累加，否则重新起算。
            #    指纹**只含轨迹 id，不含身份** —— 这是两件事：
            #      · `frames` = 人脸（这一组轨迹）连续出现了几帧 → 检测层面的稳定；
            #      · `identity_hits` = 同一身份连续出现几帧 → 识别层面的稳定（按轨迹各自计）。
            #    若把身份并进指纹，"识别第一次给出结果"或"某帧没给出"都会把检测稳定
            #    也清零，那是错的（脸一直都在，变的是我们对它是谁的判断）。
            seen = [t for t in self._tracks if t.seen]
            sig = tuple(sorted(t.id for t in seen))
            if not sig:
                self._frames = 0
            elif sig == self._signature:
                self._frames += 1
            else:
                self._frames = 1
            self._signature = sig
            self._last_ts = ts
            self._last = self._verdict()
        return self._last

    def state(self):
        """当前判定（不喂新帧）。"""
        with self._lock:
            return dict(self._last)

    def reset(self):
        """清空全部轨迹与计数（换场景/测试用）。"""
        with self._lock:
            self._tracks = []
            self._next_id = 1
            self._frames = 0
            self._signature = ()
            self._last_ts = None
            self._last = self._verdict()

    # -- 判定 --
    def _verdict(self):
        seen = [t for t in self._tracks if t.seen]
        stable = [t for t in seen
                  if t.hits >= self.stable_frames and t.avg_score >= self.conf_thr]
        count = len(stable)
        one = stable[0] if count == 1 else None

        ident = one.identity if one else None
        ident_frames = one.identity_hits if one else 0
        ident_score = one.avg_identity_score if one else None
        # 身份闸门：认出来 + 连续够 N 帧 + **平均相似度达标**。
        # 分数为 None（调用方没给相似度，如单测替身）时视为不设该闸门，保持兼容。
        ident_ok = bool(ident) and ident_frames >= self.stable_frames and (
            ident_score is None or ident_score >= self.identity_conf)

        if not seen:
            reason = "no_face"
        elif not stable:
            top = max(seen, key=lambda t: t.hits)
            reason = ("low_score" if (top.hits >= self.stable_frames
                                     and top.avg_score < self.conf_thr)
                      else "not_enough_frames")
        elif count > 1:
            reason = "multiple_faces"
        elif ident is None:
            reason = "no_identity"        # 识别未接入：能判"有人脸稳定"，不能判"是谁"
        elif ident_frames < self.stable_frames:
            reason = "identity_unstable"  # 身份刚给出/中间断过，还没连续够 N 帧
        elif ident_score is not None and ident_score < self.identity_conf:
            reason = "identity_low_score"  # 认出来了，但"认得多像"不够（独立闸门）
        elif self._frames < self.stable_frames:
            # 轨迹集合刚变过（出现新目标 / 短暂丢失后重连）→ 连续一致帧数重新起算。
            # 真机实测：这种状态下曾经的 reason 会显示 "ok" 而 stable/switchable 却是 False，
            # 排查时极具误导性；这里显式给出 frames_reset。
            reason = "frames_reset"
        else:
            reason = "ok"

        stable_ok = bool(stable) and self._frames >= self.stable_frames
        return {
            "stable": stable_ok,
            # 可用于切换主体的唯一判据：稳定 + 只有一个人 + 身份连续且够像
            "switchable": bool(stable_ok and count == 1 and ident_ok),
            "frames": self._frames,
            "required": self.stable_frames,
            "count": count,
            "ambiguous": count != 1,
            "identity": ident,
            "identity_frames": ident_frames,
            "identity_score": (round(ident_score, 4) if ident_score is not None else None),
            "identity_conf": self.identity_conf,
            "score": round(one.avg_score, 3) if one else None,
            "box": [round(v, 1) for v in one.box] if one else None,
            "reason": reason,
            "tracks": [t.snapshot() for t in self._tracks],
        }


# 进程级判定器（路由共用一份状态；测试可另行构造实例）
tracker = StabilityTracker()


# --------------------------------------------------------------------------
# 装配与降级
# --------------------------------------------------------------------------
def _face_module():
    """惰性导入 `vision.face`（它自己不硬 import 可选依赖，导入永远安全）。"""
    global _vision_face, _vision_error
    if _vision_face is not None or _vision_error:
        return _vision_face, _vision_error
    with _lock:
        if _vision_face is None and not _vision_error:
            try:
                from vision import face as vision_face
                _vision_face = vision_face
            except Exception as e:                       # noqa: BLE001
                _vision_error = "导入 vision.face 失败：%s" % e
    return _vision_face, _vision_error


def available():
    """`(bool, 原因)`：检测依赖与模型是否齐备（不含摄像头可达性）。"""
    mod, err = _face_module()
    if mod is None:
        return False, err
    return mod.available()


def _get_detector():
    """检测器单例；失败抛异常（调用方负责降级）。"""
    global _detector, _detector_error
    mod, err = _face_module()
    if mod is None:
        raise RuntimeError(err)
    with _lock:
        if _detector is None:
            if _detector_error:
                raise RuntimeError(_detector_error)
            try:
                _detector = mod.FaceDetector(conf=conf.FACE_DETECT_CONF,
                                             imgsz=conf.FACE_DETECT_IMGSZ)
            except Exception as e:                       # noqa: BLE001
                _detector_error = str(e)
                raise
    return _detector


def _grab_frame(channel=None):
    """从摄像头共享服务取一帧 BGR（**测试替换点**：单测 monkeypatch 本函数）。"""
    from vision.camera_client import CameraClient
    ch = int(channel or conf.FACE_CAMERA_CHANNEL)
    with CameraClient(host=conf.VISION_HOST, port=conf.VISION_PORT,
                      wait_timeout=5.0) as cam:
        return cam.get_frame(channel=ch).bgr()


def _camera_reachable(timeout=0.5):
    """摄像头服务是否在听（只做一次 TCP 连接，不取帧）。"""
    try:
        with socket.create_connection((conf.VISION_HOST, conf.VISION_PORT), timeout):
            return True, "ok"
    except OSError as e:
        return False, "摄像头服务 %s:%d 连不上（%s）" % (conf.VISION_HOST,
                                                    conf.VISION_PORT, e)


# --------------------------------------------------------------------------
# 识别（ArcFace）+ 样本库
# --------------------------------------------------------------------------
def _faceid_module():
    """惰性导入 `vision.faceid`（同样不硬 import 可选依赖，导入永远安全）。"""
    global _vision_faceid, _vision_faceid_error
    if _vision_faceid is not None or _vision_faceid_error:
        return _vision_faceid, _vision_faceid_error
    with _lock:
        if _vision_faceid is None and not _vision_faceid_error:
            try:
                from vision import faceid as vision_faceid
                _vision_faceid = vision_faceid
            except Exception as e:                       # noqa: BLE001
                _vision_faceid_error = "导入 vision.faceid 失败：%s" % e
    return _vision_faceid, _vision_faceid_error


def identify_available():
    """`(bool, 原因)`：识别（"是谁"）是否就绪。

    与**检测**独立降级：没识别模型时仍能检测、仍能判"有人脸稳定出现"，
    只是 `identity` 为空、`switchable` 为假。
    """
    mod, err = _faceid_module()
    if mod is None:
        return False, err
    return mod.available()


def _get_embedder():
    """识别器单例（加载 ONNX 不便宜，别每帧新建）。"""
    global _embedder, _embedder_error
    mod, err = _faceid_module()
    if mod is None:
        raise RuntimeError(err)
    with _lock:
        if _embedder is None:
            if _embedder_error:
                raise RuntimeError(_embedder_error)
            try:
                _embedder = mod.FaceEmbedder(
                    model=mod.model_path(conf.FACE_EMBED_VARIANT))
            except Exception as e:                       # noqa: BLE001
                _embedder_error = str(e)
                raise
    return _embedder


def _annotate_identities(faces, bgr):
    """给每个检测到的人脸补 `identity`（是谁）+ 相似度，供稳定判定使用。

    识别不可用/库为空时**原样返回**（不抛）：这时 `identity` 为空，
    判定会给出 `no_identity` —— "知道有人脸稳、不知道是谁"，这是正确结论而非错误。
    返回 `(faces, info)`。
    """
    ok, reason = identify_available()
    if not faces:
        return faces, {"identified": False, "reason": "no_face", "embed_ms": None}
    if not ok:
        return faces, {"identified": False, "reason": reason, "embed_ms": None}
    try:
        emb = _get_embedder()
    except Exception as e:                               # noqa: BLE001
        return faces, {"identified": False, "reason": str(e)[:160], "embed_ms": None}
    t0 = time.perf_counter()
    for f in faces:
        try:
            vec = emb.embed_face(bgr, f["box"], margin=conf.FACE_ALIGN_MARGIN)
            m = face_lib.match(vec)                      # 1:N：阈值 + 与第二名的差距
            f["identity"] = m["uid"] if m["matched"] else None
            f["identity_score"] = m["score"]
            f["identity_reason"] = m["reason"]
            f["identity_margin"] = m["margin"]
        except Exception as e:                           # noqa: BLE001
            f["identity"] = None
            f["identity_reason"] = "embed_error:%s" % str(e)[:60]
    return faces, {"identified": True, "model": os.path.basename(emb.model_path),
                   "embed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
                   "library_uids": len(face_lib.list_uids())}


def enroll(uid, frames=5, channel=None, save_photo=True):
    """注册/追加人脸样本：抓 `frames` 帧 → 每帧取**最大的人脸** → 提指纹入库。

    与声纹注册同思路（多张样本、比对时取平均）。只收"能提出指纹"的帧；
    一张都没成就返回 `ok:False` 并说明原因（没脸 / 取帧失败 / 模型不可用）。

    隐私：只存**对齐后的小脸图**（112×112 级别）与指纹，不落整幅画面。
    """
    ok, reason = available()
    if not ok:
        return {"ok": False, "status": "unavailable", "error": reason}
    ok_id, reason_id = identify_available()
    if not ok_id:
        return {"ok": False, "status": "unavailable", "error": reason_id}
    if not str(uid or "").strip():
        return {"ok": False, "status": "bad_request", "error": "uid 不能为空"}
    n = max(1, int(frames or 5))
    try:
        det = _get_detector()
        emb = _get_embedder()
        idmod, idreason = _faceid_module()
        if idmod is None:
            return {"ok": False, "error": idreason}
    except Exception as e:                               # noqa: BLE001
        return {"ok": False, "status": "unavailable", "error": "加载模型失败：%s" % e}

    added, scores, photos, misses = 0, [], 0, []
    for _ in range(n):
        try:
            bgr = _grab_frame(channel)
        except Exception as e:                           # noqa: BLE001
            misses.append("取帧失败：%s" % str(e)[:80])
            continue
        faces = det.detect(bgr)
        if not faces:
            misses.append("这一帧没检到人脸")
            continue
        # 取面积最大的一张脸：注册时人就该站镜头前，多张脸时取最近的
        f = max(faces, key=lambda x: (x["box"][2] - x["box"][0])
                * (x["box"][3] - x["box"][1]))
        try:
            crop = idmod.align_face(bgr, f["box"], margin=conf.FACE_ALIGN_MARGIN)
            vec = emb.embed(crop)
            res = face_lib.add_sample(uid, vec, photo_bgr=crop, save_photo=save_photo,
                                      model=os.path.basename(emb.model_path))
            added += 1
            scores.append(round(float(f["score"]), 3))
            if res.get("photo"):
                photos += 1
        except Exception as e:                           # noqa: BLE001
            misses.append("入库失败：%s" % str(e)[:80])

    audit.log("face_enroll", uid=uid, added=added, requested=n,
              model=os.path.basename(getattr(emb, "model_path", "") or ""))
    if not added:
        return {"ok": False, "status": "no_face", "uid": uid, "added": 0, "requested": n,
                "error": "没采到可用人脸样本（%s）" % ("；".join(misses[:3]) or "原因未知")}
    return {"ok": True, "uid": uid, "added": added, "requested": n,
            "samples_total": len(face_lib.list_samples(uid)), "photos_saved": photos,
            "detect_scores": scores, "misses": misses[:3], "library": face_lib.stats()}


def delete_person(uid):
    """删除某个人的全部人脸样本（照片 + 指纹）。"""
    try:
        res = face_lib.delete_uid(uid)
    except Exception as e:                               # noqa: BLE001
        return {"ok": False, "error": str(e)}
    audit.log("face_delete", uid=uid, removed=res.get("removed"))
    return {"ok": True, **res, "library": face_lib.stats()}


def library():
    """样本库概况（`/api/face/people` 用）。"""
    try:
        return {"ok": True, **face_lib.stats()}
    except Exception as e:                               # noqa: BLE001
        return {"ok": False, "error": str(e)}


# --------------------------------------------------------------------------
# 从**图片**入库 / 识别（不依赖摄像头）
#
# 为什么要有这一组：注册不止"对着摄像头拍"一种来源 —— 护士用手机拍的、浏览器上传的、
# 以及**旧项目留下的数据集文件夹**（`<人名>/*.jpg`）都该能进库。参考实现见用户旧项目
# `face_rec.py`（OpenCV LBPH 路线：数据集 → 训练 → 存 .yml）。我们不需要"训练"这一步，
# 所以加人只是往库里写一张 npz，**第二次检测立刻就能认出来**（无需重训、重启也在）。
# --------------------------------------------------------------------------
def _decode_image_b64(data, what="image"):
    """base64 图片 → BGR ndarray（用 base64 而不是 multipart，零新增依赖）。"""
    import base64
    if not data or not isinstance(data, str):
        raise ValueError("%s 为空" % what)
    raw = data.split(",", 1)[1] if data.startswith("data:") else data   # 容忍 data URL
    try:
        buf = base64.b64decode(raw)
    except Exception as e:                               # noqa: BLE001
        raise ValueError("%s 不是合法 base64：%s" % (what, e))
    if not buf:
        raise ValueError("%s 解码后为空" % what)
    try:
        import cv2
    except ImportError as e:                             # noqa: BLE001
        raise RuntimeError("缺少依赖：opencv-python（%s）" % e)
    import numpy as np
    img = cv2.imdecode(np.frombuffer(buf, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("%s 无法解码（格式不支持或数据损坏）" % what)
    return img


def _largest_face(faces):
    """取面积最大的人脸（离镜头最近 = 本人；注册与单图识别都用它）。"""
    if not faces:
        return None
    return max(faces, key=lambda f: (f["box"][2] - f["box"][0]) * (f["box"][3] - f["box"][1]))


def _face_payload(f):
    """人脸字典 → 可 JSON 化的响应项（含身份与两个判定数字）。"""
    return {"box": [round(v, 1) for v in f["box"]], "score": round(f["score"], 3),
            "identity": f.get("identity"), "identity_score": f.get("identity_score"),
            "identity_margin": f.get("identity_margin"),
            "identity_reason": f.get("identity_reason")}


def enroll_photo(uid, bgr, save_photo=True):
    """从**一张静态图片**入库（取画面里最大的人脸）："拍一次照就记住"的图片路径。

    与摄像头版 `enroll()` 的区别：不抓帧、不做连续判定，一次一张图；可传手机/浏览器
    拍的照片，也可批量导入旧数据集（见 `enroll_dir`）。

    `bgr` 传 ndarray 或 **base64 字符串**都行（路由直接用后者，省掉 multipart 依赖）。
    """
    if isinstance(bgr, str):
        try:
            bgr = _decode_image_b64(bgr)
        except ValueError as e:
            return {"ok": False, "status": "bad_request", "error": str(e)}
        except RuntimeError as e:
            return {"ok": False, "status": "unavailable", "error": str(e)}
    ok, reason = available()
    if not ok:
        return {"ok": False, "status": "unavailable", "error": reason}
    ok_id, reason_id = identify_available()
    if not ok_id:
        return {"ok": False, "status": "unavailable", "error": reason_id}
    if not str(uid or "").strip():
        return {"ok": False, "status": "bad_request", "error": "uid 不能为空"}
    try:
        det = _get_detector()
        emb = _get_embedder()
        idmod, idreason = _faceid_module()
        if idmod is None:
            return {"ok": False, "status": "unavailable", "error": idreason}
    except Exception as e:                               # noqa: BLE001
        return {"ok": False, "status": "unavailable", "error": "加载模型失败：%s" % e}

    faces = det.detect(bgr)
    f = _largest_face(faces)
    if f is None:
        return {"ok": False, "status": "no_face", "uid": uid, "added": 0,
                "error": "这张图里没检到人脸"}
    try:
        crop = idmod.align_face(bgr, f["box"], margin=conf.FACE_ALIGN_MARGIN)
        vec = emb.embed(crop)
        res = face_lib.add_sample(uid, vec, photo_bgr=crop, save_photo=save_photo,
                                  model=os.path.basename(emb.model_path))
    except Exception as e:                               # noqa: BLE001
        return {"ok": False, "status": "error", "uid": uid, "added": 0,
                "error": "入库失败：%s" % str(e)[:160]}

    audit.log("face_enroll", uid=uid, added=1, source="photo",
              model=os.path.basename(emb.model_path))
    return {"ok": True, "uid": uid, "added": 1,
            "samples_total": len(face_lib.list_samples(uid)),
            "photos_saved": 1 if res.get("photo") else 0,
            "detect_scores": [round(float(f["score"]), 3)],
            "library": face_lib.stats()}


def enroll_dir(base, uid=None, save_photo=True, limit_per_person=None):
    """批量导入目录（兼容旧项目的"每人一个文件夹"布局）。

    两种用法：
      * `enroll_dir(dir, uid="elder_001")` —— `dir` 里的图片都算这个人的；
      * `enroll_dir(dir)` —— `dir/<人名>/` 各自成为一个 uid（即 `dataset/<name>/*.jpg`）。

    返回 `{ok, people: {uid: 成功张数}, failed: [...], total_added, library}`。
    """
    import glob
    exts = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff")
    base = str(base or "")
    if not base or not os.path.isdir(base):
        return {"ok": False, "status": "bad_request", "error": "目录不存在：%s" % base}

    def images_in(d):
        out = []
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if os.path.isfile(p) and name.lower().endswith(exts):
                out.append(p)
        return out

    jobs = []
    if uid:
        jobs.append((uid, images_in(base)))
    else:
        for name in sorted(os.listdir(base)):
            d = os.path.join(base, name)
            if os.path.isdir(d):
                jobs.append((name, images_in(d)))
    if not jobs:
        return {"ok": False, "status": "bad_request",
                "error": "目录里没有图片（也没有子目录）：%s" % base}

    try:
        import cv2
    except ImportError as e:                             # noqa: BLE001
        return {"ok": False, "status": "unavailable", "error": "缺少依赖：opencv-python（%s）" % e}

    people, failed, total = {}, [], 0
    for person, paths in jobs:
        if limit_per_person:
            paths = paths[:int(limit_per_person)]
        added = 0
        for p in paths:
            img = cv2.imread(p)
            if img is None:
                failed.append({"uid": person, "file": p, "why": "读不到图片"})
                continue
            res = enroll_photo(person, img, save_photo=save_photo)
            if res.get("ok"):
                added += 1
            else:
                failed.append({"uid": person, "file": os.path.basename(p),
                               "why": res.get("error")})
        if added:
            people[person] = added
            total += added
    return {"ok": bool(total), "people": people, "total_added": total,
            "failed": failed[:20], "failed_count": len(failed),
            "library": face_lib.stats(),
            **({} if total else {"status": "no_face",
                                 "error": "没有导入任何样本（%d 张全部失败）" % len(failed)})}


def identify_photo(bgr, topk=1):
    """静态图片识别：这张图里有谁（**不**推进"连续 N 帧一致"判定，纯识别）。

    用途：注册后回看照片、护士确认"这是哪位老人"、离线自查与批量核验。
    `bgr` 传 ndarray 或 **base64 字符串**都行。
    """
    if isinstance(bgr, str):
        try:
            bgr = _decode_image_b64(bgr)
        except ValueError as e:
            return {"ok": False, "status": "bad_request", "error": str(e)}
        except RuntimeError as e:
            return {"ok": False, "status": "unavailable", "error": str(e)}
    ok, reason = available()
    if not ok:
        return {"ok": False, "status": "unavailable", "error": reason}
    try:
        det = _get_detector()
        bgr = bgr if bgr is not None else None
    except Exception as e:                               # noqa: BLE001
        return {"ok": False, "status": "unavailable", "error": "加载检测器失败：%s" % e}
    faces = det.detect(bgr)
    detected = len(faces)                                # 实际检出数（未截断）
    if not faces:
        return {"ok": False, "status": "no_face", "faces": [], "detected": 0,
                "error": "这张图里没检到人脸"}
    faces, ident = _annotate_identities(faces, bgr)
    faces.sort(key=lambda f: f["score"], reverse=True)
    if topk:
        faces = faces[:int(topk)]
    return {"ok": True, "status": "running", "faces": [_face_payload(f) for f in faces],
            "count": len(faces), "detected": detected, "identity": ident,
            "library": face_lib.stats()}


# --------------------------------------------------------------------------
# 命令行（离线可用，不启后端）：批量导入旧数据集 / 自查 / 看库
# --------------------------------------------------------------------------
def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(
        prog="python -m LLM.face_api",
        description="人脸样本库与识别（离线可用；注册/识别都不需要启后端）")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("status", help="打印模块与样本库状态")
    sub.add_parser("people", help="列出已注册的人与样本数")
    d = sub.add_parser("delete", help="删除某人的全部人脸样本")
    d.add_argument("uid")
    ep = sub.add_parser("enroll-photo", help="用图片文件注册（可多张）")
    ep.add_argument("uid")
    ep.add_argument("images", nargs="+")
    ed = sub.add_parser("enroll-dir", help="批量导入目录（<目录>/<人名>/*.jpg）")
    ed.add_argument("dir")
    ed.add_argument("--uid", default=None, help="把目录里所有图都算这个人的")
    ed.add_argument("--limit", type=int, default=None, help="每人最多导入几张")
    idp = sub.add_parser("identify", help="识别图片里的人是谁")
    idp.add_argument("images", nargs="+")
    args = p.parse_args(argv)

    if not args.cmd:
        p.print_help()
        return 0

    if args.cmd == "status":
        st = status()
        print(json.dumps({k: st[k] for k in ("ok", "status", "detector", "embedder",
                                             "library", "thresholds") if k in st},
                         ensure_ascii=False, indent=2, default=str))
        return 0 if st.get("ready") else 1

    if args.cmd == "people":
        print(json.dumps(library(), ensure_ascii=False, indent=2, default=str))
        return 0

    if args.cmd == "delete":
        res = delete_person(args.uid)
        print(json.dumps(res, ensure_ascii=False, default=str))
        return 0 if res.get("ok") else 1

    import cv2                                           # 以下命令都要读图
    if args.cmd == "enroll-photo":
        rc = 0
        for path in args.images:
            img = cv2.imread(path)
            if img is None:
                print("读不到图片：%s" % path)
                rc = 2
                continue
            res = enroll_photo(args.uid, img)
            print("%-40s ok=%s added=%s samples=%s %s"
                  % (os.path.basename(path), res.get("ok"), res.get("added"),
                     res.get("samples_total"), res.get("error") or ""))
            rc = rc or (0 if res.get("ok") else 1)
        return rc

    if args.cmd == "enroll-dir":
        res = enroll_dir(args.dir, uid=args.uid, limit_per_person=args.limit)
        print(json.dumps({k: res.get(k) for k in ("ok", "people", "total_added",
                                                 "failed_count", "error")},
                         ensure_ascii=False, indent=2, default=str))
        return 0 if res.get("ok") else 1

    if args.cmd == "identify":
        rc = 0
        for path in args.images:
            img = cv2.imread(path)
            if img is None:
                print("读不到图片：%s" % path)
                rc = 2
                continue
            res = identify_photo(img)
            if not res.get("ok"):
                print("%-40s %s" % (os.path.basename(path), res.get("error")))
                rc = 1
                continue
            names = ", ".join("%s(%.3f)" % (f["identity"] or "未知", f["identity_score"])
                              for f in res["faces"])
            print("%-40s 检出 %d 张脸，返回 %d 张 → %s"
                  % (os.path.basename(path), res.get("detected", res["count"]),
                     res["count"], names))
        return rc
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())


# --------------------------------------------------------------------------
# 对外接口（路由直接用）
# --------------------------------------------------------------------------
def status():
    """`/api/face/status` 的载荷：查询类，永远 ok=True（服务健康 ≠ 功能可用）。"""
    ok, reason = available()
    id_ok, id_reason = identify_available()
    reachable, cam_reason = _camera_reachable()
    try:
        lib_stats = face_lib.stats()
    except Exception as e:                               # noqa: BLE001
        lib_stats = {"error": str(e)[:160]}
    out = {
        "ok": True,
        "status": "running" if ok else "unavailable",
        "ready": bool(ok),
        # 检测器（"能否检测"）与摄像头（"能否取帧"）分开报，排查时才分得清
        "detector": {"available": bool(ok), "loaded": _detector is not None,
                     "imgsz": conf.FACE_DETECT_IMGSZ, "conf": conf.FACE_DETECT_CONF},
        "camera": {"host": conf.VISION_HOST, "port": conf.VISION_PORT,
                   "reachable": bool(reachable), "reason": None if reachable else cam_reason},
        # 识别（"是谁"）独立于检测报：缺识别模型 → 仍能检测、仍能判稳定，只是不能判身份
        "embedder": {"available": bool(id_ok), "loaded": _embedder is not None,
                     "variant": conf.FACE_EMBED_VARIANT, "dim": conf.FACE_EMBED_DIM,
                     "align_margin": conf.FACE_ALIGN_MARGIN,
                     "reason": None if id_ok else id_reason},
        "library": lib_stats,
        "thresholds": {
            "stable_frames": tracker.stable_frames,
            "stable_conf": tracker.conf_thr,
            "identity_conf": tracker.identity_conf,
            "track_iou": tracker.iou_thr,
            "track_max_jump": tracker.max_jump,
            "track_max_age": tracker.max_age,
            "camera_channel": conf.FACE_CAMERA_CHANNEL,
            "match_threshold": conf.FACE_MATCH_THRESHOLD,
            "match_min_margin": conf.FACE_MATCH_MIN_MARGIN,
        },
        "state": tracker.state(),
        "note": ("检测 + 识别（ArcFace）均已接入；verdict.switchable 需"
                 "「唯一人脸 + 同一身份连续 N 帧」同时满足"),
    }
    if not ok:
        out["reason"] = reason
    mod, _ = _face_module()
    if mod is not None:
        out["detector"]["model"] = os.path.basename(mod.model_path())
    idmod, _ = _faceid_module()
    if idmod is not None:
        out["embedder"]["model"] = os.path.basename(
            idmod.model_path(conf.FACE_EMBED_VARIANT))
    return out


def analyze(bgr, identify=True, track=True):
    """对**已有的一帧 BGR** 做检测（+可选识别）。返回 `{faces, identity, detect_ms, verdict}`。

    与 `probe()` 的分工：`probe()` 自己抓帧（后端轮询用），本函数吃现成帧。
    GUI 的预览循环已经抓过一帧，再让 `probe()` 抓一次会白做一次取帧、而且画出来的框
    与屏幕上的画面差一帧（人一动就"框追不上脸"）。

    `identify=False` 只跑检测、不跑 ArcFace —— 单纯问"镜头前有没有脸"的场合快得多
    （检测约 200ms、识别再加 27ms/脸，且识别还要读样本库）。
    `track=False` 不推进"连续 N 帧一致"判定（纯识别，不参与主体切换）。

    可能抛异常（模型不可用/推理失败）；调用方负责降级。
    """
    det = _get_detector()
    faces = det.detect(bgr)
    if identify:
        faces, ident_info = _annotate_identities(faces, bgr)   # 补 identity（是谁）
    else:
        ident_info = {"identified": False, "reason": "detect_only", "embed_ms": None}
    return {"faces": faces, "identity": ident_info,
            "detect_ms": round(float(det.last_ms or 0.0), 1),
            "verdict": tracker.update(faces) if track else None}


def probe(channel=None):
    """抓一帧 → 检测 → 喂给稳定判定。返回 `{ok, faces, verdict, frame, …}`。

    取帧/检测失败时返回 `ok:False`（与 `/api/vision/snapshot` 一致：动作类接口不可用
    就是不可用，别用 ok:True 糊过去）。**只有它会把画面交给模型**，不落盘、不外传。
    """
    global _last_stable
    ok, reason = available()
    if not ok:
        audit.log("face_probe", action="unavailable", reason=reason)
        return {"ok": False, "status": "unavailable", "error": reason}

    t0 = time.perf_counter()
    try:
        _get_detector()
        bgr = _grab_frame(channel)
    except Exception as e:                               # noqa: BLE001
        audit.log("face_probe", action="error", error=str(e)[:200])
        return {"ok": False, "status": "error", "error": "取帧/加载检测器失败：%s" % e}

    try:
        res = analyze(bgr)
    except Exception as e:                               # noqa: BLE001
        audit.log("face_probe", action="error", error=str(e)[:200])
        return {"ok": False, "status": "error", "error": "检测/识别失败：%s" % e}
    faces, ident_info, verdict = res["faces"], res["identity"], res["verdict"]
    if verdict["stable"] != _last_stable:                # 只在状态翻转时写审计，避免刷屏
        audit.log("face_state", stable=verdict["stable"], count=verdict["count"],
                  frames=verdict["frames"], reason=verdict["reason"])
        _last_stable = verdict["stable"]
    h, w = bgr.shape[:2]
    return {
        "ok": True, "status": "running",
        "frame": {"width": int(w), "height": int(h)},
        "faces": [{"box": [round(v, 1) for v in f["box"]],
                   "score": round(f["score"], 3),
                   "identity": f.get("identity"),
                   "identity_score": f.get("identity_score"),
                   "identity_margin": f.get("identity_margin"),
                   "identity_reason": f.get("identity_reason")} for f in faces],
        "identity": ident_info,
        "detect_ms": res["detect_ms"],
        "total_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        "verdict": verdict,
    }


def reset():
    """重置判定状态（测试/换场景用）。"""
    global _last_stable
    tracker.reset()
    _last_stable = None
