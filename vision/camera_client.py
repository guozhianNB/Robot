# -*- coding: utf-8 -*-
r"""MIPI 摄像头共享服务 —— 客户端库。

多个程序通过本库向 camera_server.py（唯一持有摄像头的守护进程）请求
帧，从而避免互相抢占摄像头导致冲突。

快速上手
--------
    from vision.camera_client import CameraClient

    with CameraClient() as cam:                    # 默认连 127.0.0.1:9540
        info = cam.info()                          # 服务状态
        f = cam.get_frame(channel=1)               # 最新一帧 NV12
        img_bgr = f.bgr()                          # 转 BGR（numpy/cv2）
        f.save("/tmp/frame.yuv")

        # 订阅连续帧流
        for frame in cam.frames(channel=1):
            print(frame.frame_id)

线程说明：同一个 CameraClient 实例的连接不保证线程安全；多线程各建
一个实例即可（连接开销很小）。
"""

import json
import socket
import struct
from dataclasses import dataclass

from . import protocol as P

ERR_PREFIX = P.ERR_PREFIX


class CameraServerError(Exception):
    """服务端返回的错误或协议错误。"""


class CameraNotRunning(CameraServerError, ConnectionError):
    """连不上服务（服务未启动或地址不对）。"""


@dataclass
class Frame:
    """一帧图像及其元信息。

    data 为原始编码（fmt="NV12" 时是 NV12 字节，fmt="JPEG" 时是 JPEG）。
    """

    frame_id: int
    ts_us: int
    channel: int
    width: int
    height: int
    fmt: str
    data: bytes

    # -- 原始数据 --
    def nv12_array(self):
        """NV12 帧转为 uint8 numpy 数组（需 numpy，惰性导入）。"""
        import numpy as np
        return np.frombuffer(self.data, dtype=np.uint8)

    def save(self, path):
        """把原始字节写到文件（NV12 存 .yuv，JPEG 存 .jpg）。"""
        with open(path, "wb") as f:
            f.write(self.data)

    # -- 颜色转换（需 opencv-python，惰性导入） --
    def bgr(self):
        """NV12 -> BGR ndarray（OpenCV 通道序）。"""
        import cv2
        arr = self.nv12_array().reshape(self.height * 3 // 2, self.width)
        return cv2.cvtColor(arr, cv2.COLOR_YUV2BGR_NV12)

    def rgb(self):
        """NV12 -> RGB ndarray。"""
        import cv2
        return cv2.cvtColor(self.bgr(), cv2.COLOR_BGR2RGB)

    def __repr__(self):  # pragma: no cover
        return ("Frame(id=%d ch=%d %dx%d %s %dB)"
                % (self.frame_id, self.channel, self.width, self.height,
                   self.fmt, len(self.data)))


class CameraClient:
    """摄像头共享服务客户端。"""

    def __init__(self, host=P.DEFAULT_HOST, port=P.DEFAULT_PORT,
                 connect_timeout=5.0, io_timeout=10.0):
        self.host = host
        self.port = port
        self._connect_timeout = connect_timeout
        self._io_timeout = io_timeout
        self._conn = None
        self._next_cursor = {}  # channel -> 上次 get_next_frame 返回的 frame_id

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------
    def _connect(self):
        if self._conn is not None:
            return
        try:
            self._conn = socket.create_connection(
                (self.host, self.port), self._connect_timeout)
            self._conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as e:
            raise CameraNotRunning(
                "无法连接摄像头服务 %s:%d（服务未启动？）: %s"
                % (self.host, self.port, e)) from e

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None

    def __enter__(self):
        self._connect()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ------------------------------------------------------------------
    # 底层收发
    # ------------------------------------------------------------------
    def _recv_exact(self, conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise CameraServerError("连接被服务端关闭（服务可能已退出）")
            buf += chunk
        return buf

    def _read_line(self, conn):
        line = b""
        while not line.endswith(b"\n"):
            chunk = self._recv_exact(conn, 1)
            line += chunk
        return line.rstrip(b"\n")

    def _check_err(self, buf):
        if buf.startswith(ERR_PREFIX):
            raise CameraServerError("服务端返回错误：%s"
                                    % buf[len(ERR_PREFIX):].decode())

    def _read_frame(self, conn):
        """读一帧响应：先读 4 字节判断是否是错误文本，再读帧头+载荷。"""
        head = self._recv_exact(conn, len(ERR_PREFIX))
        if head == ERR_PREFIX:
            rest = self._read_line(conn)
            raise CameraServerError("服务端返回错误：%s" % rest.decode())
        header = head + self._recv_exact(conn, P.FRAME_HEADER_SIZE
                                         - len(ERR_PREFIX))
        try:
            meta = P.unpack_frame_header(header)
        except ValueError as e:
            raise CameraServerError("帧头解析失败：%s" % e) from e
        payload = self._recv_exact(conn, meta["size"])
        return Frame(frame_id=meta["frame_id"], ts_us=meta["ts_us"],
                     channel=meta["channel"], width=meta["width"],
                     height=meta["height"], fmt=meta["format"].decode(),
                     data=payload)

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------
    def info(self):
        """查询服务状态，返回 dict（通道、帧计数、模式等）。"""
        self._connect()
        self._conn.sendall(P.CMD_INFO)
        line = self._read_line(self._conn)
        self._check_err(line)
        try:
            return json.loads(line.decode())
        except ValueError as e:
            raise CameraServerError("info 响应不是 JSON：%r" % line) from e

    def ping(self):
        """探测服务是否存活，返回 bool。"""
        try:
            self._connect()
            self._conn.settimeout(self._io_timeout)
            self._conn.sendall(P.CMD_PING)
            return self._read_line(self._conn) == b"PONG"
        except (OSError, CameraServerError):
            return False

    def get_frame(self, channel=1):
        """取指定通道最新一帧（NV12）。"""
        self._connect()
        if channel < 1 or channel > 255:
            raise ValueError("channel 应在 1~255")
        self._conn.settimeout(self._io_timeout)
        self._conn.sendall(P.CMD_GET + bytes([channel]))
        return self._read_frame(self._conn)

    def get_jpeg(self, channel=1):
        """取指定通道最新一帧的 JPEG 编码（服务端需 --enable-jpeg）。"""
        self._connect()
        if channel < 1 or channel > 255:
            raise ValueError("channel 应在 1~255")
        self._conn.settimeout(self._io_timeout)
        self._conn.sendall(P.CMD_JPEG + bytes([channel]))
        return self._read_frame(self._conn)

    def get_next_frame(self, channel=1, last_id=None):
        """阻塞等待并返回该通道"下一帧"（frame_id 严格递增）。

        - last_id=None：从客户端内部游标继续（首次调用等任意一帧）；
        - last_id=N：等 frame_id > N 的第一帧（可用于断线续传）。
        适合"逐帧处理"型消费者（每帧只消费一次，不重复不丢序）。
        """
        if channel < 1 or channel > 255:
            raise ValueError("channel 应在 1~255")
        if last_id is None:
            last_id = self._next_cursor.get(channel, 0)
        self._connect()
        self._conn.settimeout(None)  # 服务端会等到新帧再应答
        self._conn.sendall(P.CMD_NEXT + bytes([channel])
                           + struct.pack(">Q", last_id))
        frame = self._read_frame(self._conn)
        self._next_cursor[channel] = frame.frame_id
        return frame

    def frames(self, channel=1):
        """订阅指定通道的连续帧流，返回生成器，逐帧产出 Frame。

        使用独立连接；生成器结束或客户端 close 时自动断开。
        """
        if channel < 1 or channel > 255:
            raise ValueError("channel 应在 1~255")
        conn = socket.create_connection((self.host, self.port),
                                        self._connect_timeout)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.settimeout(None)
        try:
            conn.sendall(P.CMD_SUB + bytes([channel]))
            while True:
                yield self._read_frame(conn)
        finally:
            try:
                conn.close()
            except OSError:
                pass
