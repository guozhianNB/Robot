# -*- coding: utf-8 -*-
r"""摄像头共享服务（`vision.camera_server`）子进程的看护者。

为什么要起子进程、而不是 GUI 里直接 `cv2.VideoCapture`
----------------------------------------------------
摄像头同一时刻只能被一个进程独占。GUI 同时有两条取帧需求（屏幕上要连续预览、
后台还要喂检测），直接开两次必然抢占失败。统一走 `camera_server`（唯一持有摄像头
的守护进程、多客户端各取"最新一帧"）是仓库既有架构，`scripts/face_check.py --live`
与后端 `/api/vision/*` 都这么做。

隐私：服务是**按需起停**的 —— 进入"人脸录入/人脸检测"窗口才开，返回一级小窗就关，
不在后台常开摄像头（见 `ui.App.close_window2`）。
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent


def free_port():
    """让操作系统给一个空闲端口（与 `scripts/face_check.py` 同做法）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_port(port, timeout=25.0, proc=None, host="127.0.0.1"):
    """等端口可连；`proc` 若提前退出则立刻返回 False（而不是干等到超时）。"""
    end = time.time() + float(timeout)
    while time.time() < end:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            with socket.create_connection((host, int(port)), 0.3):
                return True
        except OSError:
            time.sleep(0.2)
    return False


class CameraService:
    """`python -m vision.camera_server …` 的起停与日志。

    `source`：`auto`（板卡优先 mipi、PC 优先 webcam）/ `webcam` / `mipi` / `mock`。
    `mock` 是**无摄像头自测**用的合成帧（画面里没有人脸，只能验通路）。
    """

    def __init__(self, source="auto", port=None, fps=15, device=None,
                 channels="1280x720,640x480", wait=25.0, log_dir=None):
        self.source = source
        self.port = int(port or free_port())
        self.fps = int(fps)
        self.device = device
        self.channels = channels
        self.wait = float(wait)
        self.log_dir = Path(log_dir or (Path(tempfile.gettempdir())
                                        / "vision_test_start"))
        self.proc = None
        self.log_path = None
        self._fh = None

    @property
    def host(self):
        return "127.0.0.1"

    # ------------------------------------------------------------------
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def cmd(self):
        """要跑的命令行（显示给用户 / 排查用）。"""
        out = [sys.executable, "-m", "vision.camera_server",
               "--source", str(self.source), "--port", str(self.port),
               "--fps", str(self.fps), "--channels", str(self.channels)]
        if self.device is not None:
            out += ["--device", str(int(self.device))]
        return out

    def start(self):
        """启动并等到端口就绪；返回 `(ok, 说明)`。

        失败时说明里带上服务自己的日志尾巴 —— 否则用户只能看到"起不来"三个字
        （摄像头没插 / 被别的程序占用 / 设备号不对，日志里写得清清楚楚）。
        """
        if self.running():
            return True, "已在运行"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.log_dir / ("camera_%d.log" % self.port)
        self._fh = open(str(self.log_path), "wb")
        env = {**os.environ, "PYTHONUTF8": "1"}
        try:
            self.proc = subprocess.Popen(self.cmd(), cwd=str(REPO),
                                         stdout=self._fh, stderr=subprocess.STDOUT,
                                         env=env)
        except Exception as e:                           # noqa: BLE001
            self._close_fh()
            return False, "启动摄像头服务失败：%r" % (e,)
        if wait_port(self.port, self.wait, self.proc, self.host):
            return True, "ok"
        tail = self.log_tail()
        self.stop()
        return False, (tail or "摄像头服务没起来（端口 %d 无响应）" % self.port)

    def log_tail(self, lines=12):
        """日志最后几行（读不到就返回空串）。"""
        if not self.log_path or not Path(self.log_path).is_file():
            return ""
        try:
            with open(str(self.log_path), "r", encoding="utf-8",
                      errors="replace") as fh:
                rows = [r.rstrip() for r in fh if r.strip()]
        except OSError:
            return ""
        return "\n".join(rows[-int(lines):])

    def _close_fh(self):
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None

    def stop(self, timeout=8.0):
        """关掉服务（幂等）。先礼貌 terminate，超时才 kill。"""
        proc = self.proc
        self.proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=timeout)
            except Exception:                            # noqa: BLE001
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                except Exception:                        # noqa: BLE001
                    pass
        self._close_fh()
