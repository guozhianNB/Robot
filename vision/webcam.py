# -*- coding: utf-8 -*-
r"""Windows / USB 摄像头后端（OpenCV）。

为什么需要它
------------
板卡用 MIPI（`camera_server.py::CameraBackend`），但 **LLM 后端默认跑在 PC 上**
（见 AGENTS.md），PC 上没有 `hobot_vio`，于是想在 PC 上接真实摄像头调试就只能
`--mock`（假画面）。本模块补上第三个来源：**用 OpenCV 读 Windows/USB 摄像头**。

与板卡后端的关键差别（为什么这个能进程内跑）
-------------------------------------------
板卡的 `libsrcampy.get_img()` 阻塞时长时间占住 GIL，把同进程的分发线程饿死，
所以那边必须隔离到子进程；`cv2.VideoCapture.read()` 在 C 层读帧，通常释放 GIL，
因此本后端**直接进程内运行**（与 MockBackend 同形态），显著更简单。

设计红线
--------
`cv2` **绝不在模块顶层导入**（项目「系统稳健性」红线）：本模块被
`camera_server` 导入，而 `webbridge`→`LLM.server` 的路由链也在用 vision 包，
顶层硬 import 会让没有 opencv 的机器直接起不来后端。因此 cv2 只在
`WebcamBackend.open()` / `list_cameras()` 内部惰性引入。

统一产出 NV12
-------------
协议与下游（`camera_client`、`webbridge` 的软件 JPEG 编码器）全部按 **NV12**
处理。cv2 给的是 BGR，所以这里归一化成 NV12 —— 这样下游一行都不用改。
"""

import sys
import time

# 默认设备号与探测上限
DEFAULT_DEVICE = 0
DEFAULT_PROBE_MAX_INDEX = 6


def _import_cv2():
    """惰性引入 cv2；缺失时给出可照抄的修复命令。"""
    try:
        import cv2
    except ImportError as e:                      # noqa: BLE001
        raise RuntimeError(
            "未安装 opencv-python，无法使用 Windows/USB 摄像头。"
            "请执行：pip install opencv-python（原因：%s）" % e) from e
    return cv2


def _silence_cv2_log(cv2):
    """关掉 cv2 打开不存在设备时的噪音日志（探测设备号时很吵）。"""
    try:
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
    except Exception:                             # noqa: BLE001
        pass


def _even(v):
    """向下取到偶数（NV12 要求宽高为偶数）。"""
    return v - (v % 2)


def _open_capture(cv2, device):
    """打开设备号；打不开返回 None。

    Windows 优先用 DSHOW 后端：MSMF 打开慢、且对不存在的设备会卡住几秒。
    """
    cap = None
    if sys.platform == "win32":
        try:
            cap = cv2.VideoCapture(device, cv2.CAP_DSHOW)
        except Exception:                         # noqa: BLE001
            cap = None
        if cap is None or not cap.isOpened():
            if cap is not None:
                cap.release()
            cap = cv2.VideoCapture(device)
    else:
        cap = cv2.VideoCapture(device)
    if not cap.isOpened():
        cap.release()
        return None
    return cap


def bgr_to_nv12(frame, width, height):
    """BGR 帧 -> NV12 字节（Y 平面 + 交织 UV 平面）。

    ``frame``：numpy 数组，形状 ``(height, width, 3)``，通道序为 BGR
    （cv2 的默认输出）。返回 ``width*height*3//2`` 字节。

    色彩规格：BT.601 **limited range**（与 ``Frame.bgr()`` 用的
    ``cv2.COLOR_YUV2BGR_NV12`` 一致）。若改成 full range，往返转换会整体
    偏色——所以这里必须与解码侧口径配套，别单独改。

    色度按 **2x2 平均**下采样（不是取左上角像素），否则画面会出现块状色噪。
    """
    import numpy as np

    if width % 2 or height % 2:
        raise ValueError("NV12 要求宽高为偶数，得到 %dx%d" % (width, height))
    if getattr(frame, "shape", None) != (height, width, 3):
        raise ValueError("帧形状应为 (%d, %d, 3)，得到 %r"
                         % (height, width, getattr(frame, "shape", None)))

    # 用 float32 做中间计算：uint8 会溢出/截断，导致偏色
    f = frame.astype(np.float32)
    b, g, r = f[:, :, 0], f[:, :, 1], f[:, :, 2]
    y = 0.257 * r + 0.504 * g + 0.098 * b + 16.0
    u = -0.148 * r - 0.291 * g + 0.439 * b + 128.0
    v = 0.439 * r - 0.368 * g - 0.071 * b + 128.0

    y_plane = np.clip(y, 0, 255).astype(np.uint8)

    # 2x2 平均 -> (h/2, w/2)
    u2 = u.reshape(height // 2, 2, width // 2, 2).mean(axis=(1, 3))
    v2 = v.reshape(height // 2, 2, width // 2, 2).mean(axis=(1, 3))
    u2 = np.clip(u2, 0, 255).astype(np.uint8)
    v2 = np.clip(v2, 0, 255).astype(np.uint8)

    # NV12：UV 交织，每个 2x2 块依次 U、V（注意不是 I420 的"整块 U 再整块 V"）
    uv = np.empty((height // 2, width), dtype=np.uint8)
    uv[:, 0::2] = u2
    uv[:, 1::2] = v2

    return y_plane.tobytes() + uv.tobytes()


class WebcamBackend:
    """OpenCV 摄像头后端（进程内）。

    实现与 ``CameraBackend`` / ``MockBackend`` 相同的接口契约：
    ``open()`` / ``next_frame(timeout)`` / ``close()``。
    """

    def __init__(self, device=DEFAULT_DEVICE, fps=30, channels=None,
                 max_read_retries=5):
        self._device = int(device)
        self._fps = fps
        self._channels = list(channels or [(640, 480)])
        self._max_read_retries = max_read_retries
        self._cv2 = None
        self._cap = None
        self._period = 1.0 / fps if fps and fps > 0 else 0.0
        self._next_t = 0.0
        self._fid = 0
        # 实际生效的通道尺寸（open() 后可能被设备能力收窄），供 server 取用
        self.channels = list(self._channels)

    # -- 生命周期 --
    def open(self):
        """打开设备。幂等；失败抛 RuntimeError（消息面向人）。"""
        if self._cap is not None:
            return self
        cv2 = _import_cv2()                       # 缺失 -> RuntimeError
        _silence_cv2_log(cv2)
        cap = _open_capture(cv2, self._device)
        if cap is None:
            raise RuntimeError(
                "摄像头设备 %d 打不开：可能没插摄像头、被其它程序占用，"
                "或设备号不对（用 --list-cameras 查可用设备号）" % self._device)
        try:
            # 按"最大的一路"去要分辨率；设备可能只给较小的
            want_w, want_h = max(self._channels, key=lambda c: c[0] * c[1])
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, want_w)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, want_h)
            ok, frame = self._read_with_retry(cap)
            if not ok or frame is None:
                raise RuntimeError(
                    "摄像头设备 %d 打开成功但读不到画面"
                    "（驱动异常或设备被独占）" % self._device)
            real_h, real_w = frame.shape[0], frame.shape[1]
            real_w, real_h = _even(real_w), _even(real_h)
            if real_w < 8 or real_h < 8:
                raise RuntimeError("摄像头分辨率异常：%dx%d" % (real_w, real_h))
        except Exception:
            cap.release()
            raise
        self._cv2 = cv2
        self._cap = cap
        # 设备真实尺寸记录了，用来把请求尺寸收窄到设备能力之内
        self._real = (real_w, real_h)
        self.channels = [self._effective(*c) for c in self._channels]
        return self

    def _effective(self, w, h):
        """把请求尺寸收窄到设备真实能力内（并保证偶数）。

        设备只支持 640x480 时，请求 1920x1080 应产出 640x480 —— 否则 NV12
        长度与帧头声明的尺寸不符，下游必错位（2026-09-14 修过同类 bug）。
        缩小请求是允许的（下采样）。
        """
        rw, rh = self._real
        if w > rw or h > rh:
            w, h = min(w, rw), min(h, rh)
        return (_even(w), _even(h))

    def _read_with_retry(self, cap):
        for _ in range(max(1, self._max_read_retries)):
            ok, frame = cap.read()
            if ok and frame is not None:
                return True, frame
            time.sleep(0.02)
        return False, None

    def next_frame(self, timeout=0.5):
        """取一轮帧：``{通道号: (fid, ts_us, NV12 字节)}``。

        超时返回 None；读失败抛 RuntimeError（交由 CameraServer 报错退出）。
        """
        if self._cap is None:
            raise RuntimeError("摄像头未打开")
        cv2 = self._cv2

        # 按 fps 节流（与 MockBackend 同口径）
        if self._period > 0:
            now = time.monotonic()
            if now < self._next_t:
                time.sleep(min(self._next_t - now, timeout))
            self._next_t = max(self._next_t + self._period, now)

        ok, frame = self._read_with_retry(self._cap)
        if not ok or frame is None:
            raise RuntimeError(
                "摄像头设备 %d 连续读帧失败（设备可能已拔出/被占用）"
                % self._device)

        self._fid += 1
        fid = self._fid
        ts = time.time_ns() // 1000

        rh, rw = frame.shape[0], frame.shape[1]
        boxes = {}
        for idx, (w, h) in enumerate(self.channels):
            if (w, h) == (rw, rh):
                one = frame
            else:
                one = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
            boxes[idx + 1] = (fid, ts, bgr_to_nv12(one, w, h))
        return boxes

    def close(self):
        """释放设备；幂等。"""
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:                     # noqa: BLE001
                pass
            self._cap = None


def list_cameras(max_index=DEFAULT_PROBE_MAX_INDEX):
    """探测哪些设备号**真能读出帧**，返回可读设备号列表。

    为什么需要：注册表 / 设备管理器会把"曾经装过的"设备也列出来
    （历史 ST-Link、旧摄像头），据此判断"有没有摄像头"会误判。直接试读一帧
    才是可靠判据。
    """
    cv2 = _import_cv2()
    _silence_cv2_log(cv2)
    found = []
    for idx in range(int(max_index)):
        cap = _open_capture(cv2, idx)
        if cap is None:
            continue
        try:
            ok, frame = cap.read()
            if ok and frame is not None:
                found.append(idx)
        except Exception:                         # noqa: BLE001
            pass
        finally:
            cap.release()
    return found
