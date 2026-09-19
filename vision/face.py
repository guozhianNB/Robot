# -*- coding: utf-8 -*-
r"""人脸检测（YOLOv8-face / ONNX Runtime）—— 只回答"人脸在哪、有几张"。

定位（先划清边界）
------------------
本模块**只做检测**：输入一帧 BGR 图，输出若干人脸框 + 置信度。
"这是谁"是识别问题，需要另加特征模型（ArcFace 等）+ 样本库比对，
见 `docs/temp/face-recognition-notes.md`。两者别混：YOLO 的类别只有一个
（face），它不区分身份。

为什么用 ONNX 而不是 PyTorch
---------------------------
* 运行期只需 `onnxruntime` + `numpy`（可选 `opencv` 做画框/读写图），
  不必引入 torch（Windows CPU 轮子 200MB+）；
* 板卡（RDK X5）最终走 BPU，ONNX 是"pt → onnx → hbm"转换链路上的中间格式，
  PC 与板卡共用同一份 ONNX，口径一致。

模型
----
* 权重：`deepghs/yolo-face` 的 `yolov8n-face`（YOLOv8n，WIDER FACE 人脸单类，
  12MB，输出 `[1, 5, 8400]` = 4 框 + 1 类置信度，无关键点）。
* 来源镜像：**hf-mirror.com**（本机 huggingface.co 直连不通；见 `fetch_model()`）。
* 默认落盘：`vision/models/yolov8n-face.onnx`（`.gitignore` 已排除，权重不入库）。
* 可用环境变量 `FACE_MODEL_PATH` 换模型（例如换成 yolov8s-face，精度更高、
  速度更慢）。

降级（AGENTS.md「系统稳健性」红线）
---------------------------------
`onnxruntime-numpy-cv2` 任一缺失，或模型文件不存在时：`available()` 返回
`(False, 原因)`，`FaceDetector` 构造抛 `FaceUnavailable`。**绝不在模块顶层硬
import 可选依赖**，`LLM.server` 的导入链与 lifespan 不受影响。
"""

import os
import sys

# --------------------------------------------------------------------------
# 可选依赖：逐个 try，缺失项全部收集（别只报第一个）
# --------------------------------------------------------------------------
_MISSING_DEPS = []
try:
    import numpy as np
except ImportError as _e:                            # noqa: BLE001
    np = None
    _MISSING_DEPS.append("numpy")

try:
    import onnxruntime as ort
except ImportError as _e:                            # noqa: BLE001
    ort = None
    _MISSING_DEPS.append("onnxruntime")

try:
    import cv2
except ImportError as _e:                            # noqa: BLE001
    cv2 = None
    _MISSING_DEPS.append("opencv-python")

# 画框/读写图才需要 cv2，纯检测（喂 ndarray）不强制；但 CLI 与示例要用
_HARD_DEPS = ("numpy", "onnxruntime")

# --------------------------------------------------------------------------
# 模型来源与默认路径
# --------------------------------------------------------------------------
MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
DEFAULT_MODEL_NAME = "yolov8n-face.onnx"
DEFAULT_MODEL_PATH = os.path.join(MODEL_DIR, DEFAULT_MODEL_NAME)
MODEL_URL = ("https://hf-mirror.com/deepghs/yolo-face/resolve/main/"
             "yolov8n-face/model.onnx")
MODEL_URL_HINT = ("huggingface.co 在本机直连不通，用 hf-mirror 镜像；"
                  "也可自行下载后设 FACE_MODEL_PATH")

# 检测默认参数（YOLOv8-face 常用口径）
DEFAULT_CONF = 0.25
DEFAULT_IOU = 0.45
DEFAULT_IMGSZ = 640
PAD_VALUE = 114                     # letterbox 补边灰度（YOLO 官方约定）


class FaceUnavailable(RuntimeError):
    """人脸检测不可用（依赖缺失 / 模型缺失）。消息面向人，可直接展示。"""


# --------------------------------------------------------------------------
# 纯函数：预处理与解码（可单测，不碰 onnx/cv2 也能跑）
# --------------------------------------------------------------------------
def letterbox(shape, imgsz=DEFAULT_IMGSZ):
    """算 letterbox 的缩放与补边量（等比缩放 + 居中补边）。

    ``shape``：原图 ``(h, w)``。返回 ``(new_w, new_h, dw, dh, r)``：

    * ``r``：缩放比（原图 → 网络输入）
    * ``dw``/``dh``：左右/上下各补多少像素（可能是小数，消费方取整）

    为什么要 letterbox：直接 resize 会拉伸人脸、破坏宽高比，检测框会系统性偏移；
    官方 YOLO 训练即用 letterbox，推理必须同口径。
    """
    h, w = int(shape[0]), int(shape[1])
    if h <= 0 or w <= 0:
        raise ValueError("原图尺寸非法：%dx%d" % (w, h))
    r = min(imgsz / w, imgsz / h)
    new_w, new_h = int(round(w * r)), int(round(h * r))
    dw = (imgsz - new_w) / 2.0
    dh = (imgsz - new_h) / 2.0
    return new_w, new_h, dw, dh, r


def decode(output, conf=DEFAULT_CONF, iou=DEFAULT_IOU, r=1.0, dw=0.0, dh=0.0,
           orig_shape=None):
    """YOLOv8 输出 → 原图坐标系的人脸框列表。

    ``output``：``(1, 5, N)`` 或 ``(5, N)`` 的原始张量（cx, cy, w, h, score，
    坐标在网络输入尺度上）。返回 ``[{"box": [x1, y1, x2, y2], "score": float}]``，
    坐标为**原图像素**（已撤掉缩放与补边）。

    步骤：置信度阈值 → cxcywh 转 xyxy → 撤 letterbox → 裁剪到图内 → NMS 去重。
    """
    if np is None:
        raise FaceUnavailable("缺少依赖：numpy")
    arr = np.asarray(output, dtype=np.float32)
    if arr.ndim == 3:
        arr = arr[0]
    if arr.ndim != 2:
        raise ValueError("输出形状应为 (5, N) 或 (1, 5, N)，得到 %r" % (arr.shape,))
    if arr.shape[0] != 5 and arr.shape[1] == 5:
        arr = arr.T                                    # 兼容 (N, 5)
    if arr.shape[0] != 5:
        raise ValueError("每列应为 5 个数（4 框 + 1 置信度），得到 %r" % (arr.shape,))

    scores = arr[4]
    keep = scores > float(conf)
    if not keep.any():
        return []
    b = arr[:4, keep].T                                # (M, 4) cx,cy,w,h
    s = scores[keep]
    boxes = np.empty_like(b)
    boxes[:, 0] = b[:, 0] - b[:, 2] / 2.0              # x1
    boxes[:, 1] = b[:, 1] - b[:, 3] / 2.0              # y1
    boxes[:, 2] = b[:, 0] + b[:, 2] / 2.0              # x2
    boxes[:, 3] = b[:, 1] + b[:, 3] / 2.0              # y2
    boxes[:, [0, 2]] -= float(dw)
    boxes[:, [1, 3]] -= float(dh)
    boxes /= float(r) if r else 1.0

    if orig_shape is not None:
        h, w = int(orig_shape[0]), int(orig_shape[1])
        boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, w)
        boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, h)

    idx = nms(boxes, s, iou)
    return [{"box": [float(v) for v in boxes[i]], "score": float(s[i])} for i in idx]


def nms(boxes, scores, iou=DEFAULT_IOU):
    """numpy 实现的 NMS（按分数降序贪心抑制），返回保留的下标列表。

    不用 ``cv2.dnn.NMSBoxes``：一来纯 numpy 便于无 cv2 环境单测，二来行为显式
    （阈值口径就是 IoU，不含别的隐含规则）。人脸场景框不多（个位数），
    复杂度无所谓。
    """
    if np is None:
        raise FaceUnavailable("缺少依赖：numpy")
    boxes = np.asarray(boxes, dtype=np.float32).reshape(-1, 4)
    scores = np.asarray(scores, dtype=np.float32).reshape(-1)
    if boxes.shape[0] == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(x2 - x1, 0) * np.maximum(y2 - y1, 0)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])
        inter = np.maximum(xx2 - xx1, 0) * np.maximum(yy2 - yy1, 0)
        union = areas[i] + areas[rest] - inter
        iou_arr = np.where(union > 0, inter / np.maximum(union, 1e-9), 0.0)
        order = rest[iou_arr <= float(iou)]
    return keep


def available():
    """``(bool, 原因)``：依赖是否齐 + 默认/环境指定的模型是否在盘上。

    依赖缺失返回 ``False`` + 逐个列出的缺失项；模型缺失返回 ``False`` + 提示如何
    获取（``fetch_model()`` 或设 ``FACE_MODEL_PATH``）。查询类接口用得上
    （``ok`` 保持 True、``status="unavailable"`` 是路由层的责任）。
    """
    if _HARD_DEPS:
        miss = [d for d in _HARD_DEPS if d in _MISSING_DEPS]
        if miss:
            return False, "缺少依赖：%s（pip install %s）" % (
                "、".join(miss), " ".join(miss))
    path = model_path()
    if not os.path.isfile(path):
        return False, "人脸检测模型不存在：%s（%s；或运行 python -m vision.face --download 获取）" % (
            path, MODEL_URL_HINT)
    return True, "ok"


def model_path():
    """生效的模型路径：``FACE_MODEL_PATH`` 优先，否则默认落盘位置。"""
    return os.environ.get("FACE_MODEL_PATH") or DEFAULT_MODEL_PATH


def fetch_model(url=MODEL_URL, path=None, timeout=180, quiet=False):
    """下载模型到 ``path``（默认位置），带进度提示。返回落盘路径。

    只做"取模型"这一件事，不替调用方决定要不要用（避免隐式联网）。
    """
    import urllib.request
    path = path or model_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "robot-vision/1.0"})
    if not quiet:
        print("[face] 下载模型 %s\n       -> %s" % (url, path))
    with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as fh:
        total = 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            fh.write(chunk)
            total += len(chunk)
    os.replace(tmp, path)                              # 原子落盘，避免半截文件
    if not quiet:
        print("[face] 完成：%.2f MB" % (total / 1e6))
    return path


# --------------------------------------------------------------------------
# 检测器
# --------------------------------------------------------------------------
class FaceDetector:
    """YOLOv8-face ONNX 检测器（进程内、CPU 或任意 onnxruntime provider）。

    用法::

        det = FaceDetector()                    # 自动读 FACE_MODEL_PATH/默认路径
        faces = det.detect(bgr)                 # [(x1,y1,x2,y2,score), ...]
        print(det.last_ms)                      # 上一次推理耗时（含前后处理）

    线程安全：onnxruntime 的 ``InferenceSession.run`` 可并发调用，但本类缓存的
    ``last_ms`` 不是；多线程各自建实例即可（与 ``CameraClient`` 同口径）。
    """

    def __init__(self, model=None, conf=DEFAULT_CONF, iou=DEFAULT_IOU,
                 imgsz=DEFAULT_IMGSZ, providers=None, intra_threads=None):
        ok, reason = available() if model is None else (True, "ok")
        path = model or model_path()
        if not os.path.isfile(path):
            raise FaceUnavailable("人脸检测模型不存在：%s（%s）" % (path, reason
                                                             if not ok else MODEL_URL_HINT))
        if ort is None or np is None:
            raise FaceUnavailable("缺少依赖：%s" % "、".join(
                d for d in _HARD_DEPS if d in _MISSING_DEPS))
        self.model_path = path
        self.conf = float(conf)
        self.iou = float(iou)
        self.imgsz = int(imgsz)
        self.last_ms = None
        opts = ort.SessionOptions()
        if intra_threads:
            opts.intra_op_num_threads = int(intra_threads)
        self._sess = ort.InferenceSession(
            path, sess_options=opts,
            providers=providers or ["CPUExecutionProvider"])
        self._input = self._sess.get_inputs()[0].name
        shp = self._sess.get_inputs()[0].shape
        # 模型若固定了输入尺寸（非动态），以模型为准，避免喂错形状
        if isinstance(shp[-1], int) and shp[-1] > 0:
            self.imgsz = int(shp[-1])

    # -- 主入口 --
    def detect(self, bgr):
        """对一帧 BGR 图检测人脸。

        返回 ``[{"box": [x1, y1, x2, y2], "score": s}, ...]``（原图坐标，浮点）。
        无脸返回 ``[]``（不是 None、不抛异常——"没人脸"是正常结果）。
        """
        if np is None:
            raise FaceUnavailable("缺少依赖：numpy")
        img = np.asarray(bgr)
        if img.ndim != 3 or img.shape[2] != 3:
            raise ValueError("需要 HxWx3 的 BGR 图，得到 %r" % (img.shape,))
        import time
        t0 = time.perf_counter()
        blob, r, dw, dh = self._preprocess(img)
        out = self._sess.run(None, {self._input: blob})[0]
        faces = decode(out, conf=self.conf, iou=self.iou, r=r, dw=dw, dh=dh,
                       orig_shape=img.shape[:2])
        faces.sort(key=lambda f: f["score"], reverse=True)
        self.last_ms = (time.perf_counter() - t0) * 1000.0
        return faces

    def _preprocess(self, img):
        """letterbox + BGR→RGB + /255 + NCHW float32。"""
        h, w = img.shape[:2]
        new_w, new_h, dw, dh, r = letterbox((h, w), self.imgsz)
        if cv2 is not None:
            resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        else:                                          # 无 cv2 时的最近邻兜底
            ys = (np.arange(new_h) * h // new_h).clip(0, h - 1)
            xs = (np.arange(new_w) * w // new_w).clip(0, w - 1)
            resized = img[ys][:, xs]
        top, left = int(round(dh - 0.1)), int(round(dw - 0.1))
        bottom = self.imgsz - new_h - top
        right = self.imgsz - new_w - left
        padded = np.full((self.imgsz, self.imgsz, 3), PAD_VALUE, dtype=np.uint8)
        padded[top:top + new_h, left:left + new_w] = resized
        rgb = padded[:, :, ::-1]                       # BGR -> RGB（模型按 RGB 训练）
        blob = rgb.transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        return np.ascontiguousarray(blob), r, dw, dh

    def close(self):
        self._sess = None

    # -- 便利方法（列表化输出，便于 JSON/接口）--
    def detect_list(self, bgr):
        """``detect()`` 的元组形式：``[(x1, y1, x2, y2, score), ...]``。"""
        return [(f["box"][0], f["box"][1], f["box"][2], f["box"][3], f["score"])
                for f in self.detect(bgr)]


def draw(img, faces, color=(0, 200, 0), thickness=2, show_score=True):
    """在图上画人脸框，返回新图（不改原图）。需要 cv2。

    人脸字典里的 `identity`（识别出来的 uid/姓名，由 `LLM/face_api.py` 填）有就一起画 ——
    "这个人是谁"是最终要给人看的信息，只画分数没法验收。
    """
    if cv2 is None:
        raise FaceUnavailable("缺少依赖：opencv-python")
    out = img.copy()
    for f in faces:
        x1, y1, x2, y2 = [int(round(v)) for v in f["box"]]
        cv2.rectangle(out, (x1, y1), (x2, y2), color, thickness)
        ident = f.get("identity")
        parts = []
        if ident:
            parts.append(str(ident))
        if show_score:
            parts.append("%.2f" % float(f.get("score") or 0.0))
        if parts:
            label = " ".join(parts)
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 1)
            cv2.rectangle(out, (x1, max(0, y1 - th - 6)), (x1 + tw + 4, y1), color, -1)
            cv2.putText(out, label, (x1 + 2, max(12, y1 - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 1, cv2.LINE_AA)
    return out


# --------------------------------------------------------------------------
# CLI：python -m vision.face 图片... [--download] [--out DIR]
# --------------------------------------------------------------------------
def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(
        prog="python -m vision.face",
        description="YOLOv8-face 人脸检测（ONNX，只用 onnxruntime）")
    p.add_argument("images", nargs="*", help="图片路径；不给则只打印状态")
    p.add_argument("--model", default=None, help="模型路径（默认 FACE_MODEL_PATH）")
    p.add_argument("--conf", type=float, default=DEFAULT_CONF,
                   help="置信度阈值（默认 %.2f）" % DEFAULT_CONF)
    p.add_argument("--iou", type=float, default=DEFAULT_IOU,
                   help="NMS IoU 阈值（默认 %.2f）" % DEFAULT_IOU)
    p.add_argument("--imgsz", type=int, default=DEFAULT_IMGSZ)
    p.add_argument("--out", default=None, help="画框结果输出目录（默认不落盘）")
    p.add_argument("--download", action="store_true", help="先下载模型再跑")
    p.add_argument("--status", action="store_true", help="只查可用性")
    args = p.parse_args(argv)

    ok, reason = available()
    print("[face] 依赖/模型状态：%s —— %s" % ("可用" if ok else "不可用", reason))
    print("[face] 模型路径：%s" % model_path())
    if args.download:
        fetch_model(path=args.model or model_path())
        ok, reason = available()
    if not args.images or args.status:
        if not args.images:
            print("[face] 用法：python -m vision.face 图片.jpg [--out 目录] [--download]")
        return 0 if ok else 1
    if not ok:
        print("[face] 无法检测：%s" % reason, file=sys.stderr)
        return 2
    if cv2 is None:
        print("[face] 需要 opencv-python 读写图片", file=sys.stderr)
        return 2

    det = FaceDetector(model=args.model, conf=args.conf, iou=args.iou,
                       imgsz=args.imgsz)
    rc = 0
    for path in args.images:
        img = cv2.imread(path)
        if img is None:
            print("[face] 读不到图片：%s" % path, file=sys.stderr)
            rc = 2
            continue
        faces = det.detect(img)
        print("[face] %s  %dx%d  检出 %d 张脸  用时 %.1f ms"
              % (os.path.basename(path), img.shape[1], img.shape[0],
                 len(faces), det.last_ms or 0.0))
        for i, f in enumerate(faces, 1):
            x1, y1, x2, y2 = f["box"]
            print("        #%d  score=%.3f  box=(%.0f, %.0f, %.0f, %.0f)  %dx%d"
                  % (i, f["score"], x1, y1, x2, y2, x2 - x1, y2 - y1))
        if args.out:
            os.makedirs(args.out, exist_ok=True)
            dst = os.path.join(args.out, "det_" + os.path.basename(path))
            cv2.imwrite(dst, draw(img, faces))
            print("        画框结果 -> %s" % dst)
    return rc


if __name__ == "__main__":
    sys.exit(main())
