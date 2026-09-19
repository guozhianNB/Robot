# -*- coding: utf-8 -*-
r"""后台取帧：抓帧线程 + 推理线程（**两个线程，别合成一个**）。

为什么必须分开
--------------
一开始是"一个线程：抓帧 → 检测/识别 → 发布（帧+人脸一起）"。结果是**预览被推理拖住**：
检测+识别一轮 250~700ms，意味着画面每 0.25~0.7 秒才换一张 —— 看着就是"摄像头卡顿"，
而摄像头本身其实是 15~30fps 的。分开之后：

    抓帧线程    只要帧就要（相机 15~30fps）→ 立刻发布 bgr（预览流畅）
    推理线程    拿**最新**那一帧去检测/识别（约 1~4 次/秒）→ 只发布 faces/verdict

于是"画面流畅"与"识别较慢"各归各的：框会稍微滞后于画面（这是物理限制，
推理就是慢），但**视频不再卡**。两个速率在界面上分开显示（`fps` / `analyze_fps`），
一眼就能看出是"取帧慢"还是"推理慢"。

`identify` 两档：
    True  检测 + 识别（ArcFace 比样本库）—— "这是谁"；识别约 27ms/脸，检测约 200ms
    False 只检测 —— "镜头前有没有脸"，快得多（录入窗口的"有没有脸"指示用它）
`enabled=False` 时推理线程空转，抓帧线程照常（录入时把模型让给 `face_api.enroll`，
既不抢 CPU、框也不会乱跳）。
"""

import collections
import threading
import time

from ..camera_client import CameraClient, CameraTimeout


def _rate(times):
    """时间戳 deque → 每秒次数。"""
    if len(times) < 2:
        return 0.0
    span = times[-1] - times[0]
    return round((len(times) - 1) / span, 1) if span > 0 else 0.0


class FrameWorker:
    """抓帧 + 推理；用 `snapshot()` 取最新结果（主线程渲染用）。

    对外接口：`start()` / `stop()` / `is_alive()` / `set_identify()` /
    `set_enabled()` / `snapshot()`。
    """

    def __init__(self, host, port, channel=1, identify=True):
        self.host = host
        self.port = int(port)
        self.channel = int(channel)
        self._identify = threading.Event()
        self._enabled = threading.Event()
        # 注意：**不能**叫 `self._stop` —— `threading.Thread` 内部有个私有方法
        # `_stop()`，`join()` 会调它；用同名属性会把它盖掉，join 时报
        # `TypeError: 'Event' object is not callable`（实测踩过）。
        self._stop_evt = threading.Event()
        if identify:
            self._identify.set()
        self._enabled.set()
        self._lock = threading.Lock()
        self._grab_times = collections.deque(maxlen=60)
        self._analyze_times = collections.deque(maxlen=20)
        self._frame_ver = 0                     # 抓帧线程每发布一帧 +1
        self._analyzed_ver = -1                 # 推理线程最后处理过的版本号
        self._threads = []
        self._snap = {"bgr": None, "faces": [], "identity": {}, "verdict": None,
                      "ts": 0.0, "n": 0, "fps": 0.0, "analyze_ms": None,
                      "analyze_fps": 0.0, "analyzed": 0, "error": None,
                      "detect_error": None, "state": "starting"}

    # ------------------------------------------------------------------
    # 控制（主线程调用）
    # ------------------------------------------------------------------
    def set_identify(self, flag):
        """是否跑识别（ArcFace）。"""
        (self._identify.set if flag else self._identify.clear)()

    def set_enabled(self, flag):
        """是否跑模型；关掉后只抓帧（预览继续动，框停在最后一帧）。"""
        (self._enabled.set if flag else self._enabled.clear)()

    def start(self):
        self._threads = [
            threading.Thread(target=self._grab_loop, name="vtest-grab", daemon=True),
            threading.Thread(target=self._detect_loop, name="vtest-detect", daemon=True),
        ]
        for t in self._threads:
            t.start()
        return self

    def is_alive(self):
        return any(t.is_alive() for t in self._threads)

    def stop(self, timeout=3.0):
        """请求停止并等两个线程收尾（幂等）。"""
        self._stop_evt.set()
        for t in self._threads:
            if t.is_alive():
                t.join(timeout)

    # ------------------------------------------------------------------
    def snapshot(self):
        """最新一帧与判定结果的浅拷贝（faces 列表也拷一层，避免边读边改）。"""
        with self._lock:
            s = dict(self._snap)
            s["faces"] = list(self._snap["faces"] or [])
            return s

    def _publish(self, **kv):
        with self._lock:
            self._snap.update(kv)

    # ------------------------------------------------------------------
    # 线程 A：只抓帧（保证预览流畅）
    # ------------------------------------------------------------------
    def _grab_loop(self):
        # wait_timeout=1.0：等新帧最多等 1 秒就回来看看"要不要停"。设成 None（不限时）
        # 的话，摄像头卡住时线程会一直阻塞在 socket 读上，关窗口要等到超时才收尾。
        cam = CameraClient(host=self.host, port=self.port,
                           connect_timeout=5.0, io_timeout=5.0, wait_timeout=1.0)
        self._publish(state="running")
        n = 0
        while not self._stop_evt.is_set():
            try:
                # get_next_frame：**只在真有新帧时**才返回（frame_id 严格递增）。
                # 别用 get_frame —— 它每次都给"最新那一帧"，同一帧会被反复解码
                # （实测空转到 139 次/秒，白烧 CPU、而且把真实帧率显示成假的）。
                frame = cam.get_next_frame(channel=self.channel)
            except CameraTimeout:
                continue                             # 服务还没出新帧，接着等
            except Exception as e:                   # noqa: BLE001
                # 连接断了/服务重启：丢掉连接重连，别把线程弄死
                cam.close()
                self._publish(error="取帧失败：%s" % str(e)[:160], state="retry")
                time.sleep(0.5)
                continue
            try:
                bgr = frame.bgr()
            except Exception as e:                   # noqa: BLE001
                self._publish(error="解码失败：%s" % str(e)[:160])
                time.sleep(0.2)
                continue

            n += 1
            self._grab_times.append(time.time())
            with self._lock:
                self._frame_ver += 1
                self._snap.update(bgr=bgr, ts=time.time(), n=n,
                                  fps=_rate(self._grab_times), error=None,
                                  state="running")
        cam.close()
        self._publish(state="stopped")

    # ------------------------------------------------------------------
    # 线程 B：拿最新帧推理（慢就慢，不拖累预览）
    # ------------------------------------------------------------------
    def _detect_loop(self):
        from LLM import face_api                     # 懒导入：环境变量已由入口设好
        analyzed = 0
        while not self._stop_evt.is_set():
            if not self._enabled.is_set():
                time.sleep(0.05)                     # 暂停：不跑模型，也不改 faces
                continue
            with self._lock:
                ver, bgr = self._frame_ver, self._snap["bgr"]
            if bgr is None or ver == self._analyzed_ver:
                time.sleep(0.01)                     # 没有新帧，别重复算同一张
                continue
            self._analyzed_ver = ver
            try:
                res = face_api.analyze(bgr, identify=self._identify.is_set())
            except Exception as e:                   # noqa: BLE001
                self._publish(detect_error="检测/识别失败：%s" % str(e)[:160])
                time.sleep(0.2)
                continue
            analyzed += 1
            self._analyze_times.append(time.time())
            self._publish(faces=res["faces"] or [],
                          identity=res.get("identity") or {},
                          verdict=res.get("verdict"),
                          analyze_ms=res.get("detect_ms"),
                          analyze_fps=_rate(self._analyze_times),
                          analyzed=analyzed, detect_error=None)
