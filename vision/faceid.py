# -*- coding: utf-8 -*-
r"""人脸识别模型（ArcFace）—— 把一张人脸变成"指纹向量"，用来回答"这是谁"。

与检测的分工
------------
    vision/face.py    检测：YOLO，回答"人脸在哪"（输出框）
    vision/faceid.py  识别：本模块，回答"这人是谁"（输出 512 维向量）
    LLM/face_lib.py   样本库：谁注册过、他的指纹是什么（存盘、比对）
    LLM/face_api.py   接口层：把上面三者串起来 + "连续 N 帧一致"稳定判定

ArcFace 干了什么（一句话）
------------------------
训练时给"正确答案"加一个角度惩罚（默认约 28.6°），逼网络把**同一个人的脸挤成一团、
不同人之间留出空白**。所以推理阶段只要两两算**余弦相似度**（两个向量夹角越小越像），
再比阈值就能判"是不是同一个人"。它**不做活体检测**（举张照片也能通过），
也不负责检测（那是 YOLO 的事）。

模型
----
* 标准版：`buffalo_l/w600k_r50.onnx`（ArcFace + ResNet50，174MB，精度最好）
* 轻量版：`buffalo_s/w600k_mbf.onnx`（ArcFace + MobileFaceNet，13.6MB，适合板卡/CPU）
* 输入 `112x112` RGB，归一化 `(x-127.5)/127.5`（insightface 的口径）；输出 512 维。
* 来源：**hf-mirror.com** 的 `deepghs/insightface`（huggingface.co 本机直连不通）。
* 落盘 `vision/models/`（`.gitignore` 已排除）；可用环境变量 `FACE_EMBED_MODEL` 换模型。

**没有关键点怎么办（本模块的已知妥协）**
------------------------------------
正规流程要先用人脸 5 个关键点（眼角/鼻尖/嘴角）把人脸**摆正**再送模型；而我们现在
用的检测模型（YOLOv8n-face）只输出框、**不给关键点**。所以这里退一步：按框**外扩
`FACE_ALIGN_MARGIN` 后裁成正方形**，再缩放到 112×112（相当于"不旋转地对齐"）。
正面脸影响不大，**侧脸/歪头会明显掉精度**。要根治就换带关键点的检测器
（SCRFD / RetinaFace / 带 kpt 的 yolov5-face），详见 `vision/人脸识别注意事项.md`。

降级
----
可选依赖（numpy / onnxruntime / opencv）或模型文件缺失 → `available()` 返回原因，
`FaceEmbedder` 构造抛 `FaceUnavailable`；模块顶层**不硬 import 可选依赖**。
"""

import os
import sys

_MISSING_DEPS = []
try:
    import numpy as np
except ImportError:                                  # noqa: BLE001
    np = None
    _MISSING_DEPS.append("numpy")

try:
    import onnxruntime as ort
except ImportError:                                  # noqa: BLE001
    ort = None
    _MISSING_DEPS.append("onnxruntime")

try:
    import cv2
except ImportError:                                  # noqa: BLE001
    cv2 = None
    _MISSING_DEPS.append("opencv-python")

from .face import FaceUnavailable                    # noqa: F401  复用同一个异常类型

_HARD_DEPS = ("numpy", "onnxruntime", "opencv-python")

MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
MODEL_ENV = "FACE_EMBED_MODEL"
VARIANTS = {
    "r50": ("arcface_r50.onnx", "buffalo_l/w600k_r50.onnx",
            "ArcFace + ResNet50（174MB，精度最好）"),
    "mbf": ("arcface_mbf.onnx", "buffalo_s/w600k_mbf.onnx",
            "ArcFace + MobileFaceNet（13.6MB，轻量，适合板卡）"),
}
DEFAULT_VARIANT = os.environ.get("FACE_EMBED_VARIANT", "mbf")
# 默认用**轻量版**：本机实测 R50 单张 112x112 推理 **323.6 ms**，MobileFaceNet 仅 **27.1 ms**
# （差 12 倍）。整条链还要叠加 YOLO 检测 ~190 ms，用 R50 会让"轮询一次"到 500 ms 以上。
# 要最高精度（离线批量/注册时）可换 r50：`FACE_EMBED_VARIANT=r50` 或直接用 --variant。
# **注意：两个模型的余弦分布不同，阈值必须按所用模型分别标定**（见 vision/人脸识别注意事项.md）。
BASE_URL = "https://hf-mirror.com/deepghs/insightface/resolve/main"

INPUT_SIZE = 112                 # ArcFace 标准输入边长
EMBED_DIM = 512                  # 输出向量维度
# 按框外扩比例：检测框是"贴着脸"的，而 ArcFace 的对齐模板含额头/下巴余量，故要放大
ALIGN_MARGIN = float(os.environ.get("FACE_ALIGN_MARGIN", "0.25"))
# 归一化口径（insightface 一致）：(x - 127.5) / 127.5
PIXEL_MEAN = 127.5
PIXEL_STD = 127.5


def variant_path(variant=DEFAULT_VARIANT):
    """某个变体的默认落盘路径。"""
    return os.path.join(MODEL_DIR, VARIANTS[variant][0])


def model_path(variant=None):
    """生效的模型路径：`FACE_EMBED_MODEL` 优先，否则该变体的默认位置。"""
    return os.environ.get(MODEL_ENV) or variant_path(variant or DEFAULT_VARIANT)


def available():
    """`(bool, 原因)`：依赖与模型是否齐备。"""
    miss = [d for d in _HARD_DEPS if d in _MISSING_DEPS]
    if miss:
        return False, "缺少依赖：%s（pip install %s）" % ("、".join(miss), " ".join(miss))
    path = model_path()
    if not os.path.isfile(path):
        return False, ("人脸识别模型不存在：%s（或运行 python -m vision.faceid --download "
                       "获取；也可用 %s 指定别的模型）" % (path, MODEL_ENV))
    return True, "ok"


def fetch_model(variant=DEFAULT_VARIANT, path=None, timeout=600, quiet=False):
    """下载指定变体到 `path`（默认对应默认位置），返回落盘路径。"""
    import urllib.request
    if variant not in VARIANTS:
        raise ValueError("未知变体 %r（可选 %s）" % (variant, list(VARIANTS)))
    local, remote, note = VARIANTS[variant]
    path = path or os.path.join(MODEL_DIR, local)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    url = "%s/%s" % (BASE_URL, remote)
    tmp = path + ".part"
    if not quiet:
        print("[faceid] 下载 %s\n         %s\n         -> %s" % (note, url, path))
    req = urllib.request.Request(url, headers={"User-Agent": "robot-vision/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as fh:
        total = 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            fh.write(chunk)
            total += len(chunk)
    os.replace(tmp, path)
    if not quiet:
        print("[faceid] 完成：%.2f MB" % (total / 1e6))
    return path


# --------------------------------------------------------------------------
# 纯函数：对齐、预处理、相似度（可单测，不需要模型）
# --------------------------------------------------------------------------
def align_face(bgr, box, size=INPUT_SIZE, margin=None):
    """按检测框裁出对齐人脸图（**不旋转**：外扩成正方形后缩放）。

    ``box`` = ``[x1, y1, x2, y2]``（原图像素）。返回 ``size×size×3`` 的 **BGR** 图。

    为什么外扩：检测框贴着五官，而 ArcFace 的训练模板含额头/下巴余量；不外扩会让
    人脸在 112×112 里"顶格"，识别率下降。外扩比例由 `FACE_ALIGN_MARGIN` 控制。
    """
    if np is None or cv2 is None:
        raise FaceUnavailable("缺少依赖：%s" % "、".join(
            d for d in ("numpy", "opencv-python") if d in _MISSING_DEPS))
    m = ALIGN_MARGIN if margin is None else float(margin)
    h, w = bgr.shape[:2]
    x1, y1, x2, y2 = [float(v) for v in box[:4]]
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    side = max(x2 - x1, y2 - y1) * (1.0 + 2.0 * m)          # 外扩后的正方形边长
    half = side / 2.0
    # 先按原图边界裁（超出部分后面用边缘复制补），再统一缩放到 size
    nx1, ny1 = int(round(cx - half)), int(round(cy - half))
    nx2, ny2 = int(round(cx + half)), int(round(cy + half))
    cx1, cy1 = max(0, nx1), max(0, ny1)
    cx2, cy2 = min(w, nx2), min(h, ny2)
    if cx2 - cx1 < 2 or cy2 - cy1 < 2:
        raise ValueError("人脸框越界或过小：%r（原图 %dx%d）" % (box, w, h))
    crop = bgr[cy1:cy2, cx1:cx2]
    # 补边到正方形（用边缘复制，避免黑边在归一化后变成强负值）
    pad_l, pad_t = cx1 - nx1, cy1 - ny1
    pad_r, pad_b = nx2 - cx2, ny2 - cy2
    if any(v > 0 for v in (pad_l, pad_t, pad_r, pad_b)):
        crop = cv2.copyMakeBorder(crop, max(0, pad_t), max(0, pad_b),
                                  max(0, pad_l), max(0, pad_r),
                                  cv2.BORDER_REPLICATE)
    return cv2.resize(crop, (size, size), interpolation=cv2.INTER_LINEAR)


def preprocess(crop_bgr, size=INPUT_SIZE):
    """对齐后的 BGR 图 → 模型输入张量 ``(1, 3, size, size)`` float32。

    口径与 insightface 一致：**RGB 顺序**、`(x-127.5)/127.5`、NCHW。
    通道顺序错了（BGR 直接送）精度会掉，这类错误不会报错、只会"识别结果变差"，
    故这里显式做转换并有单测盯着。
    """
    if np is None or cv2 is None:
        raise FaceUnavailable("缺少依赖：numpy / opencv-python")
    img = crop_bgr if crop_bgr.shape[:2] == (size, size) else cv2.resize(
        crop_bgr, (size, size), interpolation=cv2.INTER_LINEAR)
    rgb = img[:, :, ::-1].astype(np.float32)             # BGR -> RGB
    blob = (rgb - PIXEL_MEAN) / PIXEL_STD
    return np.ascontiguousarray(blob.transpose(2, 0, 1)[None])


def normalize(vec):
    """L2 归一化（长度变 1）；零向量原样返回。"""
    if np is None:
        raise FaceUnavailable("缺少依赖：numpy")
    v = np.asarray(vec, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def cosine(a, b):
    """余弦相似度（两个已归一化向量就是点积）。返回 -1~1。"""
    if np is None:
        raise FaceUnavailable("缺少依赖：numpy")
    x, y = normalize(a), normalize(b)
    if x.size != y.size:
        raise ValueError("维度不一致：%d vs %d" % (x.size, y.size))
    return float(np.dot(x, y))


# --------------------------------------------------------------------------
# 识别器
# --------------------------------------------------------------------------
class FaceEmbedder:
    """ArcFace ONNX 推理器（进程内，CPU 或任意 onnxruntime provider）。

    用法::

        emb = FaceEmbedder()
        v1 = emb.embed_face(bgr, box)          # 512 维，已归一化
        cosine(v1, v2)                          # 两张脸有多像
    """

    def __init__(self, model=None, providers=None, intra_threads=None):
        path = model or model_path()
        if not os.path.isfile(path):
            raise FaceUnavailable("人脸识别模型不存在：%s（python -m vision.faceid --download）"
                                  % path)
        miss = [d for d in _HARD_DEPS if d in _MISSING_DEPS]
        if miss:
            raise FaceUnavailable("缺少依赖：%s" % "、".join(miss))
        self.model_path = path
        self.last_ms = None
        opts = ort.SessionOptions()
        if intra_threads:
            opts.intra_op_num_threads = int(intra_threads)
        self._sess = ort.InferenceSession(path, sess_options=opts,
                                          providers=providers or ["CPUExecutionProvider"])
        self._input = self._sess.get_inputs()[0].name
        self.dim = int(self._sess.get_outputs()[0].shape[-1] or EMBED_DIM)

    def embed(self, crop_bgr):
        """对**一张已对齐的人脸图**提指纹（返回归一化后的 512 维向量）。"""
        import time
        t0 = time.perf_counter()
        blob = preprocess(crop_bgr)
        out = self._sess.run(None, {self._input: blob})[0]
        self.last_ms = (time.perf_counter() - t0) * 1000.0
        return normalize(out.reshape(-1))

    def embed_face(self, bgr, box, margin=None):
        """从整图 + 人脸框：裁对齐后提指纹（`margin` 可覆盖默认外扩比例）。"""
        return self.embed(align_face(bgr, box, margin=margin))

    def close(self):
        self._sess = None


# --------------------------------------------------------------------------
# CLI：python -m vision.faceid --status | --download | --compare A B
# --------------------------------------------------------------------------
def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(
        prog="python -m vision.faceid",
        description="ArcFace 人脸识别模型（112x112 -> 512 维指纹，只用 onnxruntime）")
    p.add_argument("--status", action="store_true", help="只查可用性")
    p.add_argument("--download", action="store_true", help="下载模型")
    p.add_argument("--variant", default=DEFAULT_VARIANT, choices=list(VARIANTS),
                   help="下载哪个变体（默认 r50）")
    p.add_argument("--model", default=None, help="模型路径（默认 FACE_EMBED_MODEL）")
    p.add_argument("--embed", metavar="IMG", help="对一张**人脸图**提指纹并打印摘要")
    p.add_argument("--compare", nargs=2, metavar=("A", "B"),
                   help="算两张人脸图的余弦相似度（>阈值 视为同一人）")
    args = p.parse_args(argv)

    ok, reason = available()
    print("[faceid] 状态：%s —— %s" % ("可用" if ok else "不可用", reason))
    print("[faceid] 模型：%s" % model_path())
    if args.download:
        fetch_model(variant=args.variant, path=args.model or variant_path(args.variant))
        ok, reason = available()
    if args.status or not (args.embed or args.compare):
        return 0 if ok else 1
    if not ok:
        print("[faceid] 无法识别：%s" % reason, file=sys.stderr)
        return 2

    emb = FaceEmbedder(model=args.model)
    if args.embed:
        img = cv2.imread(args.embed)
        if img is None:
            print("[faceid] 读不到图片：%s" % args.embed, file=sys.stderr)
            return 2
        v = emb.embed(img)
        print("[faceid] %s  维度=%d  模长=%.4f  前 5 维=%s  用时 %.1f ms"
              % (os.path.basename(args.embed), v.size, float(np.linalg.norm(v)),
                 np.round(v[:5], 4), emb.last_ms or 0.0))
    if args.compare:
        vs = []
        for path in args.compare:
            img = cv2.imread(path)
            if img is None:
                print("[faceid] 读不到图片：%s" % path, file=sys.stderr)
                return 2
            vs.append(emb.embed(img))
        print("[faceid] 余弦相似度 = %.4f" % cosine(vs[0], vs[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
