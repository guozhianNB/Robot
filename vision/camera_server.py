# -*- coding: utf-8 -*-
r"""MIPI 摄像头共享服务端（守护进程）。

背景
----
RDK X5 的 MIPI 摄像头（libsrcampy / VIO 通道）同一时刻只能被**一个进程**
独占打开。多个程序（目标检测、拍照、推流、LLM 视觉……）如果各自直接调
`libsrcampy.Camera` 会互相冲突、报错。本程序作为唯一持有摄像头的守护进
程，把每一路通道的"最新一帧"缓存到内存，并通过 TCP 协议向任意多个客户
端分发：

    G <ch>   取该通道最新一帧（NV12）
    N <ch>   阻塞等待比 last_id 更新的一帧
    J <ch>   取该通道最新一帧的 JPEG 编码（可选，需硬件编码器）
    S <ch>   订阅该通道连续帧流（服务端持续推送最新帧）
    I        查询服务状态（JSON）
    P        心跳

进程模型
--------
真实摄像头模式下，**采集运行在独立子进程**（multiprocessing）中：
- 实测 libsrcampy 的 get_img() 阻塞等待帧时会长时间占住进程 GIL，导致
  同进程内的客户端分发线程被饿死（PING 都要 ~130ms）。
- 子进程独占摄像头 + 持有自己的 GIL；主进程只负责把子进程经单槽队列
  推来的最新帧分发给客户端，响应延迟回到亚毫秒级。
- 子进程 daemon=True，随主进程退出而结束；主进程退出时也会显式通知。

启动（在仓库根目录 Robot/ 下执行）：

    # 真实摄像头
    python3 -m vision.camera_server --channels 1920x1080,512x512 --fps 30

    # 无摄像头时的自测模式（生成合成帧，协议完全一致）
    python3 -m vision.camera_server --mock

协议定义见 vision/protocol.py，客户端库见 vision/camera_client.py。
"""

import argparse
import json
import multiprocessing as mp
import os
import queue as _queue
import signal
import socket
import socketserver
import struct
import sys
import threading
import time

try:
    from hobot_vio import libsrcampy as srcampy
except ImportError:  # 允许在无 hobot_vio 的机器上以 --mock 运行
    srcampy = None

from . import protocol as P

SERVICE_NAME = "vision-camera-server"
ERR_PREFIX = P.ERR_PREFIX

# 帧长时间不更新的看门狗阈值（真实摄像头 get_img 阻塞时由子进程内部兜底）
WATCHDOG_IDLE_SECS = 10.0


def align16(v):
    """向下对齐到 16（VPS 输出分辨率要求 16 对齐）。"""
    return v - (v % 16)


# ---------------------------------------------------------------------------
# 取帧后端
# ---------------------------------------------------------------------------
def _child_capture_main(q, stop_evt, pipe_id, mode, fps, channels):
    """真实摄像头采集子进程入口。

    独占打开摄像头并循环 get_img，把每一轮的 {通道: (fid, ts, NV12字节)}
    经单槽队列推给主进程（满了丢旧帧、保最新）。get_img 阻塞时占住的是
    本进程的 GIL，不影响主进程给客户端分发帧。
    """
    import signal as _signal
    _signal.signal(_signal.SIGINT, _signal.SIG_IGN)
    _signal.signal(_signal.SIGTERM, _signal.SIG_IGN)

    def _push(item):
        try:
            q.put_nowait(item)
        except _queue.Full:
            try:
                q.get_nowait()
            except _queue.Empty:
                pass
            try:
                q.put_nowait(item)
            except Exception:
                pass

    try:
        if srcampy is None:
            raise RuntimeError("hobot_vio 未安装，无法打开真实摄像头")
        cam = srcampy.Camera()
        widths = [w for w, _ in channels]
        heights = [h for _, h in channels]
        # 单通道按官方 dump 示例传标量，多通道按 web 示例传列表
        if len(channels) == 1:
            ret = cam.open_cam(pipe_id, mode, fps, widths[0], heights[0])
        else:
            ret = cam.open_cam(pipe_id, mode, fps, widths, heights)
        if ret != 0:
            raise RuntimeError(
                "open_cam 失败(ret=%d)。可能原因：摄像头未接好/被其他进程占"
                "用/传感器检测失败，详见上方日志。" % ret)
    except Exception as e:
        try:
            q.put(("error", str(e)))
        except Exception:
            pass
        return

    try:
        q.put(("ready", None))
    except Exception:
        pass

    fid = 0
    try:
        while not stop_evt.is_set():
            fid += 1
            ts = time.time_ns() // 1000
            frames = {}
            for idx, (w, h) in enumerate(channels):
                img = cam.get_img(idx + 1, w, h)
                if img is None:
                    raise RuntimeError("get_img(%d) 返回 None" % (idx + 1))
                frames[idx + 1] = (fid, ts, bytes(img))
            _push(("frame", frames))
    except Exception as e:
        try:
            q.put(("error", str(e)))
        except Exception:
            pass
    finally:
        try:
            cam.close_cam()
        except Exception:
            pass


class CameraBackend:
    """真实摄像头后端：采集在子进程，主进程只读队列。

    get_frame/get_img 的 GIL 问题由进程隔离解决：子进程独占摄像头并持有
    自己的 GIL，主进程的 GIL 完全用于客户端分发。
    """

    def __init__(self, pipe_id, fps, channels, video_idx=-1, mode=-1):
        # channels: list[(w, h)]，通道号从 1 开始
        self._channels = channels
        self._pipe_id = pipe_id
        self._video_idx = video_idx
        self._mode = mode
        self._fps = fps
        self._q = mp.Queue(maxsize=1)
        self._stop_evt = mp.Event()
        self._proc = None

    def open(self):
        """启动子进程并等待其成功打开摄像头（或返回错误）。"""
        self._proc = mp.Process(
            target=_child_capture_main,
            args=(self._q, self._stop_evt, self._pipe_id, self._mode,
                  self._fps, self._channels),
            name="cam-capture", daemon=True)
        self._proc.start()
        try:
            kind, payload = self._q.get(timeout=15)
        except _queue.Empty:
            raise RuntimeError("摄像头子进程 15s 内未就绪，请检查摄像头/驱动")
        if kind == "error":
            raise RuntimeError(payload)
        return self

    def next_frame(self, timeout=0.5):
        """取子进程推来的最新一轮帧。

        返回 {通道: (fid, ts, NV12字节)}；超时返回 None；后端出错抛
        RuntimeError（消息来自子进程）。
        """
        try:
            kind, payload = self._q.get(timeout=timeout)
        except _queue.Empty:
            return None
        except (EOFError, OSError) as e:
            raise RuntimeError("摄像头子进程已退出：%s" % e)
        if kind == "error":
            raise RuntimeError(payload)
        return payload

    def close(self):
        self._stop_evt.set()
        if self._proc is not None:
            self._proc.join(timeout=3)
            if self._proc.is_alive():
                self._proc.terminate()
                self._proc.join(timeout=2)
            self._proc = None


class MockBackend:
    """模拟后端：生成合成 NV12 帧，便于无摄像头时开发/联调/自测。

    帧内容：横向渐变 + 随 frame_id 移动的竖条（x = (fid*8) % w），
    左上角 8x8 区域写 frame_id 低 8 位，便于程序化校验。
    """

    def __init__(self, fps, channels):
        import numpy as np
        self._np = np
        self._channels = channels
        self._period = 1.0 / fps if fps and fps > 0 else 0.0
        self._next_t = 0.0
        self._fid = 0

    def open(self):
        return self

    def next_frame(self, timeout=0.5):
        if self._period > 0:                       # 按 fps 节流
            now = time.monotonic()
            if now < self._next_t:
                time.sleep(min(self._next_t - now, timeout))
            self._next_t = max(self._next_t + self._period, now)
        self._fid += 1
        fid = self._fid
        ts = time.time_ns() // 1000
        frames = {}
        for idx, (w, h) in enumerate(self._channels):
            frames[idx + 1] = (fid, ts, self._render(idx, w, h, fid))
        return frames

    def _render(self, channel_idx, w, h, frame_id):
        np = self._np
        y = np.empty((h, w), dtype=np.uint8)
        y[:] = np.linspace(0, 255, w, dtype=np.uint8)[None, :]
        x = (frame_id * 8) % w
        y[:, x:min(x + 48, w)] = 255                      # 移动竖条
        y[:8, :8] = frame_id & 0xFF                       # 帧号标记
        uv = np.full((h // 2, w), 128, dtype=np.uint8)
        return np.concatenate([y.reshape(-1), uv.reshape(-1)]).tobytes()

    def close(self):
        pass


# ---------------------------------------------------------------------------
# 共享服务
# ---------------------------------------------------------------------------
class CameraServer:
    """取帧 + 分发一体：采集线程缓存最新帧，TCP 线程按命令分发。"""

    def __init__(self, backend, channels, fps, jpeg=False,
                 bind_host=P.DEFAULT_HOST, bind_port=P.DEFAULT_PORT):
        self._backend = backend
        self._channels = channels            # list[(w, h)]，通道号 1 起
        self._fps = fps
        self._jpeg_enabled = jpeg
        self._jpeg_lock = threading.Lock()
        self._encoder = None

        self._latest = {}                    # channel -> bytes
        self._meta = {}                      # channel -> (frame_id, ts_us)
        self._count = {}                     # channel -> 成功帧数
        self._err_count = {}                 # channel -> 连续错误数
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._started = time.time()

        for ch in range(1, len(channels) + 1):
            self._latest[ch] = None
            self._meta[ch] = (0, 0)
            self._count[ch] = 0
            self._err_count[ch] = 0

        self._tcp = socketserver.ThreadingTCPServer(
            (bind_host, bind_port), self._Handler, bind_and_activate=False)
        self._tcp.daemon_threads = True
        self._tcp.allow_reuse_address = True
        self._tcp.server = self  # 处理器通过 .server 访问服务实例

    # -- 帧头/命令字转发给处理器 --
    class _Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.server.server._serve_client(self.request)

    # ------------------------------------------------------------------
    # 采集
    # ------------------------------------------------------------------
    def _capture_loop(self):
        """主进程采集线程：从后端取最新帧（真实=子进程队列，mock=本地生成）。"""
        while not self._stop.is_set():
            try:
                frames = self._backend.next_frame(timeout=0.5)
            except RuntimeError as e:
                print("[camera-server] 摄像头后端错误：%s" % e, file=sys.stderr)
                self._stop.set()
                os._exit(1)
            if frames is None:
                continue
            with self._cond:
                for ch, (fid, ts, data) in frames.items():
                    self._latest[ch] = data
                    self._meta[ch] = (fid, ts)
                    self._count[ch] += 1
                    self._err_count[ch] = 0
                self._cond.notify_all()

    def _watchdog_loop(self):
        """get_img 可能无限阻塞（传感器掉线），用看门狗兜底退出。"""
        while not self._stop.is_set():
            time.sleep(1.0)
            stale = True
            for ch in self._latest:
                if self._latest[ch] is not None:
                    stale = False
                    break
            if stale and time.time() - self._started > WATCHDOG_IDLE_SECS:
                print("[camera-server] 看门狗：超过 %.0fs 未取到任何帧，"
                      "判定摄像头异常，退出。" % WATCHDOG_IDLE_SECS,
                      file=sys.stderr)
                self._stop.set()
                os._exit(1)

    # ------------------------------------------------------------------
    # 对外：启动/停止
    # ------------------------------------------------------------------
    def start(self):
        self._backend.open()
        if self._jpeg_enabled:
            self._init_encoder()
        self._tcp.server_bind()
        self._tcp.server_activate()
        threading.Thread(target=self._capture_loop, name="capture",
                         daemon=True).start()
        threading.Thread(target=self._watchdog_loop, name="watchdog",
                         daemon=True).start()
        print("[camera-server] %s 已启动：%s:%d  fps=%d 通道=%s 模式=%s"
              % (SERVICE_NAME,
                 self._tcp.server_address[0], self._tcp.server_address[1],
                 self._fps,
                 ",".join("%dx%d" % c for c in self._channels),
                 "mock" if isinstance(self._backend, MockBackend) else "real"))
        print("[camera-server] 客户端用法：from vision.camera_client import "
              "CameraClient")
        self._tcp.serve_forever(poll_interval=0.5)

    def stop(self):
        self._stop.set()
        try:
            self._tcp.shutdown()
        except Exception:
            pass
        try:
            self._tcp.server_close()
        except Exception:
            pass
        self._backend.close()

    def _init_encoder(self):
        try:
            w, h = self._channels[0]
            # JPU 编码要求宽高 16 对齐：若通道本身未对齐，内部对齐后编码
            w, h = align16(w), align16(h)
            enc = srcampy.Encoder()
            ret = enc.encode(0, 3, w, h)  # type 3 = JPEG
            if ret != 0:
                raise RuntimeError("encode init ret=%d" % ret)
            self._encoder = enc
            print("[camera-server] JPEG 编码器已启用（%dx%d）" % (w, h))
        except Exception as e:
            print("[camera-server] 警告：JPEG 编码器初始化失败，仅提供原始"
                  "NV12 帧：%s" % e, file=sys.stderr)
            self._encoder = None

    # ------------------------------------------------------------------
    # 协议处理
    # ------------------------------------------------------------------
    def _recv_exact(self, conn, n, timeout=None):
        buf = b""
        if timeout is not None:
            conn.settimeout(timeout)
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("peer closed")
            buf += chunk
        return buf

    def _send_all(self, conn, data):
        conn.sendall(data)

    def _send_err(self, conn, msg):
        """发送一行错误文本（客户端按 'ERR ' 前缀识别，\n 结尾）。"""
        self._send_all(conn, ERR_PREFIX + msg.encode() + b"\n")

    def _send_frame(self, conn, channel, want_jpeg):
        with self._cond:
            data = self._latest.get(channel)
            fid, ts = self._meta.get(channel, (0, 0))
        if data is None:
            self._send_err(conn, "no frame yet for channel %d" % channel)
            return
        if want_jpeg:
            if self._encoder is None:
                self._send_err(conn, "jpeg disabled (start with --enable-jpeg)")
                return
            with self._jpeg_lock:
                self._encoder.encode_file(data)
                jpg = self._encoder.get_img()
            if not jpg:
                self._send_err(conn, "jpeg encode failed")
                return
            payload, fmt, cmd = bytes(jpg), P.FMT_JPEG, P.CMD_JPEG_FRAME
        else:
            payload, fmt, cmd = data, P.FMT_NV12, P.CMD_FRAME
        w, h = self._channels[channel - 1]
        header = P.pack_frame_header(cmd, channel, fmt, w, h, fid, ts,
                                     len(payload))
        self._send_all(conn, header + payload)

    def _wait_next_frame(self, channel, last_id):
        """阻塞等待 channel 通道出现 frame_id > last_id 的新帧。"""
        with self._cond:
            while (not self._stop.is_set()
                   and self._meta.get(channel, (0, 0))[0] <= last_id):
                self._cond.wait(timeout=1.0)
        return not self._stop.is_set()

    def _stream_frames(self, conn, channel):
        last = 0
        conn.settimeout(None)
        try:
            while not self._stop.is_set():
                with self._cond:
                    self._cond.wait_for(
                        lambda: self._meta.get(channel, (0, 0))[0] != last
                        or self._stop.is_set(), timeout=1.0)
                    data = self._latest.get(channel)
                    fid, ts = self._meta.get(channel, (0, 0))
                if data is None or fid == last:
                    continue
                last = fid
                w, h = self._channels[channel - 1]
                header = P.pack_frame_header(P.CMD_FRAME, channel, P.FMT_NV12,
                                             w, h, fid, ts, len(data))
                try:
                    self._send_all(conn, header + data)
                except (ConnectionError, OSError):
                    return
        finally:
            pass

    def _serve_client(self, conn):
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            while not self._stop.is_set():
                cmd = self._recv_exact(conn, 1, timeout=30)
                if cmd == P.CMD_INFO:
                    self._send_all(conn,
                                   (json.dumps(self.info(),
                                               ensure_ascii=False) + "\n")
                                   .encode())
                elif cmd == P.CMD_GET:
                    ch = self._recv_exact(conn, 1)[0]
                    if ch < 1 or ch > len(self._channels):
                        self._send_err(conn, "bad channel")
                        continue
                    self._send_frame(conn, ch, want_jpeg=False)
                elif cmd == P.CMD_JPEG:
                    ch = self._recv_exact(conn, 1)[0]
                    if ch < 1 or ch > len(self._channels):
                        self._send_err(conn, "bad channel")
                        continue
                    self._send_frame(conn, ch, want_jpeg=True)
                elif cmd == P.CMD_NEXT:
                    body = self._recv_exact(conn, 1 + 8)
                    ch = body[0]
                    last_id = struct.unpack(">Q", body[1:9])[0]
                    if ch < 1 or ch > len(self._channels):
                        self._send_err(conn, "bad channel")
                        continue
                    if not self._wait_next_frame(ch, last_id):
                        return
                    self._send_frame(conn, ch, want_jpeg=False)
                elif cmd == P.CMD_SUB:
                    ch = self._recv_exact(conn, 1)[0]
                    if ch < 1 or ch > len(self._channels):
                        self._send_err(conn, "bad channel")
                        continue
                    self._stream_frames(conn, ch)
                    return
                elif cmd == P.CMD_PING:
                    self._send_all(conn, b"PONG\n")
                else:
                    self._send_err(conn, "unknown command")
        except (ConnectionError, OSError, socket.timeout):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def info(self):
        chans = {}
        for ch in range(1, len(self._channels) + 1):
            w, h = self._channels[ch - 1]
            fid, ts = self._meta[ch]
            chans[str(ch)] = {
                "width": w, "height": h, "format": "NV12",
                "frame_id": fid, "ts_us": ts,
                "count": self._count[ch], "err_count": self._err_count[ch],
            }
        return {
            "ok": True,
            "service": SERVICE_NAME,
            "protocol_version": P.PROTOCOL_VERSION,
            "mode": "mock" if isinstance(self._backend, MockBackend) else "real",
            "fps": self._fps,
            "jpeg": self._encoder is not None,
            "channels": chans,
            "uptime_s": round(time.time() - self._started, 2),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                        time.localtime(self._started)),
        }


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def parse_channels(spec):
    """把 "1920x1080,512x512" 解析成 [(1920,1080),(512,512)]，通道号 1 起。

    说明：VSE 支持 1920x1080 等非 16 对齐分辨率（官方 cdev/yolo 示例均
    直接用 1080）；16 对齐只是 JPU 编码（--enable-jpeg）的要求，服务端
    会在编码时内部对齐，因此这里不强制改分辨率。
    """
    channels = []
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            w, h = tok.lower().split("x")
            w, h = int(w), int(h)
        except ValueError:
            raise argparse.ArgumentTypeError("通道格式应为 WxH，例如 1920x1080")
        if w <= 0 or h <= 0 or w % 2 != 0 or h % 2 != 0:
            raise argparse.ArgumentTypeError(
                "通道分辨率须为正的偶数宽高（NV12 要求），得到 %dx%d" % (w, h))
        channels.append((w, h))
    if not channels:
        raise argparse.ArgumentTypeError("至少需要一个通道")
    return channels


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="MIPI 摄像头共享服务：一个进程持有摄像头，多程序取帧",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--bind", default=P.DEFAULT_HOST,
                        help="监听地址（默认本机；跨机共享可设 0.0.0.0）")
    parser.add_argument("--port", type=int, default=P.DEFAULT_PORT,
                        help="监听端口")
    parser.add_argument("--fps", type=int, default=30, help="采集帧率")
    parser.add_argument("--channels", type=parse_channels,
                        default="1920x1080,512x512",
                        help="输出通道分辨率（逗号分隔 WxH），通道号从 1 开始")
    parser.add_argument("--enable-jpeg", action="store_true",
                        help="启用硬件 JPEG 编码（J 命令；依赖 JPU）")
    parser.add_argument("--mock", action="store_true",
                        help="无摄像头自测模式：生成合成帧")
    parser.add_argument("--status", action="store_true",
                        help="不启动服务，仅查询运行中的服务状态")
    args = parser.parse_args(argv)

    if args.status:
        from .camera_client import CameraClient
        try:
            with CameraClient(args.bind, args.port, connect_timeout=3) as c:
                print(json.dumps(c.info(), ensure_ascii=False, indent=2))
        except OSError as e:
            print("无法连接 %s:%d：%s（服务未运行？）"
                  % (args.bind, args.port, e), file=sys.stderr)
            return 1
        return 0

    channels = args.channels
    if args.mock:
        backend = MockBackend(args.fps, channels)
    else:
        backend = CameraBackend(pipe_id=0, fps=args.fps, channels=channels)

    server = CameraServer(backend, channels, args.fps, jpeg=args.enable_jpeg,
                          bind_host=args.bind, bind_port=args.port)

    def _shutdown(signum, _frame):
        print("\n[camera-server] 收到信号 %d，正在退出……" % signum)
        server.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # serve_forever 放工作线程：shutdown() 需从另一线程调用，避免死锁
    errors = []

    def _serve():
        try:
            server.start()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
            server._stop.set()

    threading.Thread(target=_serve, name="serve", daemon=True).start()
    try:
        while not server._stop.is_set():
            time.sleep(0.5)
        if errors:
            raise errors[0]
    except OSError as e:
        print("[camera-server] 启动失败：%s\n"
              "  - 端口被占用？检查是否已有实例在运行（--status 可查询）"
              % e, file=sys.stderr)
        return 2
    except RuntimeError as e:
        print("[camera-server] 摄像头打开失败：%s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
