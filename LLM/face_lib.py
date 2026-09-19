# -*- coding: utf-8 -*-
r"""人脸样本库：谁注册过、他的"人脸指纹"是什么（存盘 + 1:N 比对）。

与声纹平行（`LLM/data/speakers/<uid>.npz` ↔ `LLM/data/faces/<uid>/…`）
------------------------------------------------------------------
目录结构（见 `docs/temp/face-recognition-notes.md` §2）：

    LLM/data/faces/<uid>/<yyyyMMdd_HHMMSS_ffffff>.jpg   # 原始照片（保留原图，指纹可离线重算）
    LLM/data/faces/<uid>/<同名>.npz                     # 该照片的 512 维指纹（ArcFace）

* 列人不建表：扫目录即可（与 `voice_api.list_speakers()` 同思路）；
* **一个 uid 多张样本**：注册/追加时拍 2~3 张不同角度光照，比对时取**平均指纹**
  （与声纹"多段平均"一致）；
* `LLM/data/*` 已被 `.gitignore` 排除，人脸照片不会入库。

红线
----
* 人脸是敏感生物特征：**只在本机处理与存储，不上云**；增删由接口层写审计
  （`log("face_enroll"/"face_delete")`）；
* 指纹是能用于比对的凭证（存在"指纹反演"攻击），与照片同等对待，不外传。

降级
----
numpy 缺失或目录不可写 → 抛 `FaceLibError`（接口层转成 `ok:False` + 原因）。
本模块**只做文件读写与向量比对**，不碰摄像头、不碰模型推理。
"""

import os
import shutil
import threading
import time

try:
    import numpy as np
except ImportError:                                  # noqa: BLE001
    np = None

from . import conf

_LOCK = threading.RLock()


class FaceLibError(RuntimeError):
    """样本库操作失败（依赖缺失 / 目录不可写 / 数据损坏）。消息面向人。"""


def faces_dir():
    """样本库根目录（`conf.FACE_DIR`）。"""
    return str(conf.FACE_DIR)


def _uid_dir(uid):
    """某个人的样本目录；uid 做白名单校验，避免拼出目录穿越路径。"""
    name = str(uid or "").strip()
    if not name or name in (".", "..") or os.sep in name or "/" in name or "\\" in name:
        raise FaceLibError("非法的 uid：%r" % uid)
    return os.path.join(faces_dir(), name)


def list_uids():
    """已注册的 uid（按名字排序；只列有样本的目录）。"""
    root = faces_dir()
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if os.path.isdir(p) and list_samples(name):
            out.append(name)
    return out


def list_samples(uid):
    """某人的样本文件名列表（只认 `.npz`，即"提过指纹"的样本）。"""
    d = _uid_dir(uid)
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.endswith(".npz"))


def sample_photo_path(uid, stem):
    """某样本对应的照片路径（可能不存在——只存指纹也是允许的）。"""
    return os.path.join(_uid_dir(uid), stem + ".jpg")


def embeddings(uid):
    """某人的全部指纹，返回 `(N, 512)` 的 float32 数组（无样本则 N=0）。

    损坏/维度不符的 npz 直接跳过（单张坏样本不该毁掉整库），并计入 `load_errors()`。
    """
    if np is None:
        raise FaceLibError("缺少依赖：numpy")
    vecs = []
    d = _uid_dir(uid)
    for name in list_samples(uid):
        try:
            with np.load(os.path.join(d, name)) as z:
                v = np.asarray(z["embedding"], dtype=np.float32).reshape(-1)
            if v.size != conf.FACE_EMBED_DIM:
                _note_error(uid, name, "维度 %d != %d" % (v.size, conf.FACE_EMBED_DIM))
                continue
            n = float(np.linalg.norm(v))
            vecs.append(v / n if n > 0 else v)
        except Exception as e:                       # noqa: BLE001
            _note_error(uid, name, repr(e)[:120])
    return np.stack(vecs) if vecs else np.zeros((0, conf.FACE_EMBED_DIM), np.float32)


_ERRORS = []


def _note_error(uid, name, why):
    _ERRORS.append({"uid": uid, "sample": name, "why": why})


def load_errors():
    """本次进程里跳过的坏样本（供状态接口展示）。"""
    return list(_ERRORS)


def mean_embedding(uid):
    """某人的代表指纹 = 各样本**归一化后求平均再归一化**（多角度更鲁棒）。

    返回 `None` 表示这个人还没有可用样本。
    """
    arr = embeddings(uid)
    if arr.shape[0] == 0:
        return None
    m = arr.mean(axis=0)
    n = float(np.linalg.norm(m))
    return m / n if n > 0 else m


def index():
    """全库索引 `{uid: 代表指纹}`（比对时用它做 1:N）。"""
    out = {}
    for uid in list_uids():
        v = mean_embedding(uid)
        if v is not None:
            out[uid] = v
    return out


def score_all(vec):
    """所有已注册人与 `vec` 的余弦相似度，按分数降序：`[(uid, score), …]`。"""
    if np is None:
        raise FaceLibError("缺少依赖：numpy")
    v = np.asarray(vec, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    if n > 0:
        v = v / n
    scored = [(uid, float(np.dot(v, emb))) for uid, emb in index().items()]
    scored.sort(key=lambda x: -x[1])
    return scored


def match(vec, threshold=None, min_margin=None):
    """1:N 比对：`vec` 最像谁？

    返回字典：`{"matched", "uid", "score", "second_uid", "second_score", "margin",
    "threshold", "reason"}`。

    两道闸门（缺一不可）：
      1. **绝对阈值** `FACE_MATCH_THRESHOLD`：最像的那个也得够像；
      2. **与第二名的差距** `FACE_MATCH_MIN_MARGIN`：养老场景里"两位老人长得像/同一人
         不同角度"都可能让前两名咬得很近，差距太小就宁可不认（宁问勿猜，与声纹一致）。
    """
    thr = conf.FACE_MATCH_THRESHOLD if threshold is None else float(threshold)
    margin_thr = conf.FACE_MATCH_MIN_MARGIN if min_margin is None else float(min_margin)
    scored = score_all(vec)
    if not scored:
        return {"matched": False, "uid": None, "score": None, "second_uid": None,
                "second_score": None, "margin": None, "threshold": thr,
                "reason": "empty_library"}
    uid, score = scored[0]
    second_uid, second = (scored[1] if len(scored) > 1 else (None, None))
    margin = score - second if second is not None else None
    if score < thr:
        reason = "below_threshold"
    elif margin is not None and margin < margin_thr:
        reason = "ambiguous"
    else:
        reason = "ok"
    return {"matched": reason == "ok", "uid": uid if reason == "ok" else None,
            "score": round(score, 4),
            "second_uid": second_uid, "second_score": round(second, 4) if second is not None else None,
            "margin": round(margin, 4) if margin is not None else None,
            "threshold": thr, "reason": reason}


def add_sample(uid, embedding, photo_bgr=None, save_photo=True, model=None):
    """写入一张样本（可选同时存原图）。返回 `{"sample": 文件名, "photo": 路径或 None}`。

    `photo_bgr` 为 None 或 `save_photo=False` 时只存指纹（例如注册时不想留照片）。
    """
    if np is None:
        raise FaceLibError("缺少依赖：numpy")
    if len(list_uids()) >= conf.FACE_LIB_MAX_UIDS and uid not in list_uids():
        raise FaceLibError("样本库已达人数上限 %d（改 conf.FACE_LIB_MAX_UIDS 可放宽）"
                           % conf.FACE_LIB_MAX_UIDS)
    if len(list_samples(uid)) >= conf.FACE_LIB_MAX_SAMPLES_PER_UID:
        raise FaceLibError("%s 的样本已达上限 %d 张（先删掉旧样本或调大 conf 上限）"
                           % (uid, conf.FACE_LIB_MAX_SAMPLES_PER_UID))
    d = _uid_dir(uid)
    os.makedirs(d, exist_ok=True)
    stem = time.strftime("%Y%m%d_%H%M%S") + "_%06d" % int(time.time() * 1e6 % 1e6)
    vec = np.asarray(embedding, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(vec))
    vec = vec / n if n > 0 else vec
    npz_path = os.path.join(d, stem + ".npz")
    with _LOCK:
        np.savez(npz_path, embedding=vec, model=str(model or ""),
                 created_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        photo_path = None
        if photo_bgr is not None and save_photo:
            try:
                import cv2
                photo_path = os.path.join(d, stem + ".jpg")
                cv2.imwrite(photo_path, photo_bgr)
            except Exception as e:                   # noqa: BLE001  存图失败不算致命
                _note_error(uid, stem + ".jpg", "写图失败：%r" % (e,))
                photo_path = None
    return {"sample": stem + ".npz", "photo": photo_path}


def delete_uid(uid):
    """删除某个人的全部样本（照片 + 指纹）。返回删除的文件数与目录路径。"""
    d = _uid_dir(uid)
    if not os.path.isdir(d):
        return {"uid": uid, "removed": 0, "dir": d}
    with _LOCK:
        n = len([f for f in os.listdir(d)])
        shutil.rmtree(d)
    return {"uid": uid, "removed": n, "dir": d}


def stats():
    """样本库概况（供状态接口）。"""
    uids = list_uids()
    per = {uid: len(list_samples(uid)) for uid in uids}
    return {"dir": faces_dir(), "uids": len(uids), "samples": sum(per.values()),
            "per_uid": per,
            # 样本太少的人（识别不稳）；注册引导据此提示"再拍两张"
            "min_samples": conf.FACE_MIN_SAMPLES_PER_UID,
            "under_sampled": sorted(u for u, n in per.items()
                                    if n < conf.FACE_MIN_SAMPLES_PER_UID),
            "threshold": conf.FACE_MATCH_THRESHOLD,
            "min_margin": conf.FACE_MATCH_MIN_MARGIN,
            "load_errors": load_errors()}
