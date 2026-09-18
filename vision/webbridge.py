# -*- coding: utf-8 -*-
r"""摄像头 HTTP 桥 —— 让上位机（PC）浏览器直接看板卡画面。

为什么单独一层
--------------
`camera_server.py` 是**裸 TCP + 自定义二进制协议**（效率高，适合同机推理
进程），浏览器说不了这套协议。本模块把它桥成标准 HTTP：

    GET /api/vision/status          服务状态（JSON，含通道/帧计数/模式）
    GET /api/vision/snapshot?ch=1   单帧 JPEG 快照
    GET /api/vision/stream?ch=1     MJPEG 连续流（<img src> 直接能看）

设计取向（与项目其余部分一致）：
- **上位机可达**：后端跑在 PC 上，浏览器打开 `http://127.0.0.1:8000/...`
  即可看板卡画面，不必登录板卡、不必到现场。
- **降级运行**：摄像头服务没起 / 依赖缺失时，查询类接口返回
  `status: "unavailable"` 且 `ok: True`（服务健康 ≠ 功能可用），
  写操作返回 `ok: False`。绝不因摄像头问题拖垮后端启动。
- **零新增第三方依赖**：JPEG 优先走服务端硬件编码（`J` 命令）；若未启用，
  退回**纯 stdlib 手写基线 JPEG 编码器**，保证任何环境都有画面可看。
- 帧的获取走 `vision.camera_client`，因此天然共享"摄像头唯一持有者"，
  不会与目标检测/拍照等其他消费者抢摄像头。
"""

import json as _json
import socket
import struct
import threading
import time

from . import camera_client as cc
from . import protocol as P

try:
    from LLM.core import log as audit      # 项目审计日志（可选）
except ImportError:                       # vision/ 独立使用时静默降级
    class _NullAudit:
        @staticmethod
        def log(*_a, **_kw):
            pass
    audit = _NullAudit()

try:
    from LLM import conf as _conf         # 可选：拿 VISION_HOST/VISION_PORT
except ImportError:
    _conf = None


def default_host():
    """摄像头服务地址：优先 LLM/conf 的 VISION_HOST，否则本机。

    「后端在 PC、摄像头在板卡」时把 VISION_HOST 指向板卡即可（见 conf.py 注释）。
    """
    return (getattr(_conf, "VISION_HOST", None) or cc.P.DEFAULT_HOST)


def default_port():
    """摄像头服务端口：优先 LLM/conf 的 VISION_PORT，否则协议默认 9540。"""
    return int(getattr(_conf, "VISION_PORT", None) or cc.P.DEFAULT_PORT)


# 服务状态缓存（避免每次请求都连一次；状态查询很轻，缓存 1s 足够）
_STATUS_TTL = 1.0
_status_cache = {"t": 0.0, "value": None}
_status_lock = threading.Lock()

# 单帧抓取超时（秒）：服务在但一直没帧时不能把 HTTP 请求挂死
_SNAPSHOT_TIMEOUT = 5.0
_CONNECT_TIMEOUT = 2.0


def _client(port=None, host=None):
    return cc.CameraClient(host or default_host(),
                           port or default_port(),
                           connect_timeout=_CONNECT_TIMEOUT,
                           io_timeout=_SNAPSHOT_TIMEOUT,
                           wait_timeout=_SNAPSHOT_TIMEOUT)


def status(port=None, host=None, force=False):
    """查询摄像头服务状态（带短路缓存，连接失败不抛异常）。"""
    with _status_lock:
        now = time.time()
        if (not force and _status_cache["value"] is not None
                and now - _status_cache["t"] < _STATUS_TTL):
            return _status_cache["value"]
    # 把解析出的地址带在返回里：排查"连的是哪台"时不用猜配置
    addr = {"host": host or default_host(), "port": int(port or default_port())}
    try:
        with _client(port, host) as c:
            info = c.info()
        value = {"ok": True, "status": "running", "target": addr, "info": info}
    except cc.CameraNotRunning as e:
        value = {"ok": True, "status": "unavailable", "target": addr,
                 "reason": "摄像头服务未运行：%s" % e,
                 "hint": "本机起服务：python3 -m vision.camera_server "
                         "（板卡上用 --source mipi，PC 上用 --source webcam）；"
                         "若摄像头在另一台机器，改 conf.VISION_HOST / VISION_PORT"}
    except (cc.CameraServerError, OSError) as e:
        value = {"ok": True, "status": "error", "target": addr, "reason": str(e)}
    with _status_lock:
        _status_cache["t"] = time.time()
        _status_cache["value"] = value
    return value


def reset_cache():
    """清状态缓存（测试/手动刷新用）。"""
    with _status_lock:
        _status_cache["t"] = 0.0
        _status_cache["value"] = None


# ---------------------------------------------------------------------------
# JPEG 获取：优先硬件编码，退回 stdlib 软件编码
# ---------------------------------------------------------------------------
def _hardware_jpeg(client, channel):
    """走服务端 `J` 命令（需 --enable-jpeg）。不可用返回 None。"""
    try:
        frame = client.get_jpeg(channel=channel)
    except cc.CameraServerError as e:
        # "jpeg disabled" / "jpeg encode failed" 都走软件兜底
        if "jpeg" in str(e).lower():
            return None
        raise
    return frame.data


def get_jpeg(channel=1, port=None, host=None, quality=80):
    """取一帧 JPEG。返回 (jpeg_bytes, width, height, source)。

    source: "hardware"（服务端编码）/ "software"（本地 stdlib 编码）。
    失败抛 CameraNotRunning / CameraServerError（由路由层翻译成 JSON）。
    """
    with _client(port, host) as client:
        data = _hardware_jpeg(client, channel)
        if data is not None:
            # 硬件路径的宽高以客户端解出的帧头为准（已 16 对齐）
            return data, None, None, "hardware"
        frame = client.get_frame(channel=channel)
        w, h = frame.width, frame.height
    jpg = nv12_to_jpeg(frame.data, w, h, quality=quality)
    return jpg, w, h, "software"


# ---------------------------------------------------------------------------
# 纯 stdlib 基线 JPEG 编码器（NV12 -> JPEG，不做色度抽样优化）
# ---------------------------------------------------------------------------
# 说明：这不是为了取代硬件编码，而是在"未开 --enable-jpeg / JPU 不可用"时
# 仍能出图（上位机调试、现场应急）。为控制体积与 CPU，默认先做整数倍下采样。

_ZIGZAG = [
    0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63,
]

# 标准亮度/色度量化表
_QY = [
    16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99,
]
_QC = [
    17, 18, 24, 47, 99, 99, 99, 99, 18, 21, 26, 66, 99, 99, 99, 99,
    24, 26, 56, 99, 99, 99, 99, 99, 47, 66, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99,
]

# 标准 Huffman 表（JPEG Annex K）。注意：BITS 按 1..16 位长索引，
# 故每个列表首元素补 0 占位（JPEG 文件里写的 16 字节同样从位长 1 开始）。
_DC_LUM_BITS = [0, 0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0]
_DC_LUM_VAL = list(range(12))
_DC_CHR_BITS = [0, 0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0]
_DC_CHR_VAL = list(range(12))
_AC_LUM_BITS = [0, 0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7d]
_AC_LUM_VAL = [
    0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06,
    0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xa1, 0x08,
    0x23, 0x42, 0xb1, 0xc1, 0x15, 0x52, 0xd1, 0xf0, 0x24, 0x33, 0x62, 0x72,
    0x82, 0x09, 0x0a, 0x16, 0x17, 0x18, 0x19, 0x1a, 0x25, 0x26, 0x27, 0x28,
    0x29, 0x2a, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3a, 0x43, 0x44, 0x45,
    0x46, 0x47, 0x48, 0x49, 0x4a, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
    0x5a, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6a, 0x73, 0x74, 0x75,
    0x76, 0x77, 0x78, 0x79, 0x7a, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
    0x8a, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9a, 0xa2, 0xa3,
    0xa4, 0xa5, 0xa6, 0xa7, 0xa8, 0xa9, 0xaa, 0xb2, 0xb3, 0xb4, 0xb5, 0xb6,
    0xb7, 0xb8, 0xb9, 0xba, 0xc2, 0xc3, 0xc4, 0xc5, 0xc6, 0xc7, 0xc8, 0xc9,
    0xca, 0xd2, 0xd3, 0xd4, 0xd5, 0xd6, 0xd7, 0xd8, 0xd9, 0xda, 0xe1, 0xe2,
    0xe3, 0xe4, 0xe5, 0xe6, 0xe7, 0xe8, 0xe9, 0xea, 0xf1, 0xf2, 0xf3, 0xf4,
    0xf5, 0xf6, 0xf7, 0xf8, 0xf9, 0xfa,
]
_AC_CHR_BITS = [0, 0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 0x77]
_AC_CHR_VAL = [
    0x00, 0x01, 0x02, 0x03, 0x11, 0x04, 0x05, 0x21, 0x31, 0x06, 0x12, 0x41,
    0x51, 0x07, 0x61, 0x71, 0x13, 0x22, 0x32, 0x81, 0x08, 0x14, 0x42, 0x91,
    0xa1, 0xb1, 0xc1, 0x09, 0x23, 0x33, 0x52, 0xf0, 0x15, 0x62, 0x72, 0xd1,
    0x0a, 0x16, 0x24, 0x34, 0xe1, 0x25, 0xf1, 0x17, 0x18, 0x19, 0x1a, 0x26,
    0x27, 0x28, 0x29, 0x2a, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3a, 0x43, 0x44,
    0x45, 0x46, 0x47, 0x48, 0x49, 0x4a, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58,
    0x59, 0x5a, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6a, 0x73, 0x74,
    0x75, 0x76, 0x77, 0x78, 0x79, 0x7a, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87,
    0x88, 0x89, 0x8a, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9a,
    0xa2, 0xa3, 0xa4, 0xa5, 0xa6, 0xa7, 0xa8, 0xa9, 0xaa, 0xb2, 0xb3, 0xb4,
    0xb5, 0xb6, 0xb7, 0xb8, 0xb9, 0xba, 0xc2, 0xc3, 0xc4, 0xc5, 0xc6, 0xc7,
    0xc8, 0xc9, 0xca, 0xd2, 0xd3, 0xd4, 0xd5, 0xd6, 0xd7, 0xd8, 0xd9, 0xda,
    0xe2, 0xe3, 0xe4, 0xe5, 0xe6, 0xe7, 0xe8, 0xe9, 0xea, 0xf2, 0xf3, 0xf4,
    0xf5, 0xf6, 0xf7, 0xf8, 0xf9, 0xfa,
]


def _build_huffman(bits, vals):
    """由 BITS/VALS 构造 {symbol: (code, length)}。"""
    table, code, k = {}, 0, 0
    for length in range(1, 17):
        for _ in range(bits[length]):
            table[vals[k]] = (code, length)
            code += 1
            k += 1
        code <<= 1
    return table


_HUFF_DC_LUM = _build_huffman(_DC_LUM_BITS, _DC_LUM_VAL)
_HUFF_DC_CHR = _build_huffman(_DC_CHR_BITS, _DC_CHR_VAL)
_HUFF_AC_LUM = _build_huffman(_AC_LUM_BITS, _AC_LUM_VAL)
_HUFF_AC_CHR = _build_huffman(_AC_CHR_BITS, _AC_CHR_VAL)

_COS = [[__import__("math").cos((2 * x + 1) * u * 3.141592653589793 / 16.0)
         for x in range(8)] for u in range(8)]


def _fdct(block):
    """8x8 二维 DCT-II（分离式，成本 O(8^3)）。"""
    tmp = [[0.0] * 8 for _ in range(8)]
    for y in range(8):
        row = block[y]
        for u in range(8):
            cu = _COS[u]
            s = 0.0
            for x in range(8):
                s += row[x] * cu[x]
            tmp[y][u] = s
    out = [0] * 64
    for u in range(8):
        cu = _COS[u]
        for v in range(8):
            s = 0.0
            cv = _COS[v]
            for y in range(8):
                s += tmp[y][u] * cv[y]
            out[v * 8 + u] = s
    return out


class _BitWriter:
    """JPEG 位流写入器（处理 0xFF 后的字节填充）。

    实现要点：``acc`` 只保留**不足 8 位**的未满字节，因此必须先把不足
    8 位的余量补到高位再拼新码字——否则长码字（如 16 位的 AC 码）会让
    acc 无限膨胀，写出的比特流与熵编码器预期不符（实测表现为"结构合法
    但解码报 broken data stream"）。
    """

    def __init__(self):
        self.buf = bytearray()
        self.acc = 0
        self.n = 0            # acc 中有效位数，恒 < 8

    def write(self, code, length):
        if length <= 0:
            return
        code &= (1 << length) - 1
        # 把新码字拼到已有余量之后
        self.acc = (self.acc << length) | code
        self.n += length
        while self.n >= 8:
            self.n -= 8
            b = (self.acc >> self.n) & 0xFF
            self.buf.append(b)
            if b == 0xFF:                 # 0xFF 后必须填充 0x00
                self.buf.append(0x00)
        # 只保留未满的那 n 位，防止 acc 无界增长
        self.acc &= (1 << self.n) - 1 if self.n else 0

    def flush(self):
        """补 1 位结束（不足 8 位时用 1 填满）。"""
        if self.n:
            self.write((1 << (8 - self.n)) - 1, 8 - self.n)


def _category(value):
    """返回 (幅值类别, 编码用位)。"""
    if value == 0:
        return 0, 0
    a = abs(value)
    size = a.bit_length()
    bits = value if value > 0 else value + (1 << size) - 1
    return size, bits & ((1 << size) - 1)


def _encode_block(bw, coeffs, qtable, prev_dc, dc_huff, ac_huff):
    """量化 + 熵编码一个 8x8 块，返回新的 DC 预测值。"""
    q = [0] * 64
    for i in range(64):
        z = _ZIGZAG[i]
        v = int(round(coeffs[z] / qtable[z] / 8.0))
        q[i] = max(-1023, min(1023, v))
    diff = q[0] - prev_dc
    size, bits = _category(diff)
    code, length = dc_huff[size]
    bw.write(code, length)
    if size:
        bw.write(bits, size)
    run = 0
    for i in range(1, 64):
        if q[i] == 0:
            run += 1
            continue
        while run > 15:
            code, length = ac_huff[0xF0]
            bw.write(code, length)
            run -= 16
        size, bits = _category(q[i])
        code, length = ac_huff[(run << 4) | size]
        bw.write(code, length)
        bw.write(bits, size)
        run = 0
    if run:
        code, length = ac_huff[0x00]
        bw.write(code, length)
    return q[0]


def _clamp(v):
    return 0 if v < 0 else (255 if v > 255 else int(v))


def nv12_to_jpeg(data, width, height, quality=80, max_width=640):
    """NV12 字节 -> JPEG 字节（纯 stdlib，无第三方依赖）。

    max_width：为控制体积/CPU，超过则做整数倍最近邻下采样（默认 640）。
    quality：1~100，越低量化越狠、体积越小。
    """
    if len(data) < width * height * 3 // 2:
        raise ValueError("NV12 数据长度不足：%d < %d"
                         % (len(data), width * height * 3 // 2))
    step = 1
    while width // (step * 2) >= max_width and step < 16:
        step *= 2
    ow, oh = width // step, height // step
    ow -= ow % 8
    oh -= oh % 8
    if ow < 8 or oh < 8:
        ow = max(8, ow - ow % 8)
        oh = max(8, oh - oh % 8)

    q = max(1, min(100, int(quality)))
    scale = 5000 // q if q < 50 else 200 - q * 2
    qy = [(v * scale + 50) // 100 for v in _QY]
    qc = [(v * scale + 50) // 100 for v in _QC]
    qy = [max(1, min(255, v)) for v in qy]
    qc = [max(1, min(255, v)) for v in qc]

    y_plane = data[:width * height]
    uv_base = width * height
    # NV12 的 UV 为交织平面，每 2x2 亮度块共用一个 (U, V) 对
    uv_plane = data[uv_base:uv_base + width * height // 2]

    out = bytearray(b"\xff\xd8")
    # --- SOI + APP0 ---
    out += b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00" \
        + struct.pack(">HH", 1, 1) + b"\x00\x00"
    # --- DQT ---
    # 注意：DQT 里量化表必须按 **zigzag 顺序**存（JPEG 规范），而我们内部
    # 的量化表是自然（行优先）顺序，所以这里要重排；否则解码器会用错位的
    # 量化表反量化，整个图像崩坏。
    # 另外表 ID 必须区分：亮度=0，色度=1（高 4 位是精度，这里 8bit -> 0）。
    for table_id, table in ((0, qy), (1, qc)):
        zig = bytes(table[_ZIGZAG[i]] for i in range(64))
        out += b"\xff\xdb" + struct.pack(">H", 67) + bytes([table_id]) + zig
    # --- SOF0 ---
    # 段体：精度(1) + 高(2) + 宽(2) + 分量数(1)，随后每个分量 3 字节
    # (id, 采样因子, 量化表ID)。三个分量都 1x1 抽样 -> 0x11。
    out += b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, oh, ow, 3) \
        + bytes([1, 0x11, 0]) + bytes([2, 0x11, 1]) + bytes([3, 0x11, 1])
    # --- DHT ---
    for cls_idx, bits, vals in (
            (0x00, _DC_LUM_BITS, _DC_LUM_VAL), (0x10, _AC_LUM_BITS, _AC_LUM_VAL),
            (0x01, _DC_CHR_BITS, _DC_CHR_VAL), (0x11, _AC_CHR_BITS, _AC_CHR_VAL)):
        # bits[0] 是索引占位，不能写进文件；文件里正好 16 个位长计数
        body = bytes(bits[1:17]) + bytes(vals)
        out += b"\xff\xc4" + struct.pack(">H", 2 + 1 + len(body)) \
            + bytes([cls_idx]) + body
    # --- SOS ---
    out += b"\xff\xda" + struct.pack(">H", 12) + b"\x03" \
        + b"\x01\x00\x02\x11\x03\x11" + b"\x00\x3f\x00"

    bw = _BitWriter()
    prev_y = prev_cb = prev_cr = 0
    for by in range(0, oh, 8):
        for bx in range(0, ow, 8):
            blk_y = [[0.0] * 8 for _ in range(8)]
            blk_cb = [[0.0] * 8 for _ in range(8)]
            blk_cr = [[0.0] * 8 for _ in range(8)]
            for yy in range(8):
                # 输出坐标 -> 源坐标（最近邻下采样，必须乘 step）
                sy = min((by + yy) * step, height - 1)
                base = sy * width
                uvrow = uv_base + (sy // 2) * width
                for xx in range(8):
                    sx = min((bx + xx) * step, width - 1)
                    blk_y[yy][xx] = float(y_plane[base + sx]) - 128.0
                    # NV12 UV 平面为 (U,V) 交织，每 2x2 亮度块共用一对
                    uv_off = uvrow + (sx // 2) * 2
                    if uv_off + 1 < len(data):
                        blk_cb[yy][xx] = float(data[uv_off]) - 128.0
                        blk_cr[yy][xx] = float(data[uv_off + 1]) - 128.0
            prev_y = _encode_block(bw, _fdct(blk_y), qy, prev_y,
                                   _HUFF_DC_LUM, _HUFF_AC_LUM)
            prev_cb = _encode_block(bw, _fdct(blk_cb), qc, prev_cb,
                                    _HUFF_DC_CHR, _HUFF_AC_CHR)
            prev_cr = _encode_block(bw, _fdct(blk_cr), qc, prev_cr,
                                    _HUFF_DC_CHR, _HUFF_AC_CHR)
    bw.flush()
    out += bw.buf
    out += b"\xff\xd9"
    return bytes(out)


# ---------------------------------------------------------------------------
# MJPEG 流
# ---------------------------------------------------------------------------
def mjpeg_stream(channel=1, port=None, host=None, fps=10, quality=80,
                 max_frames=None):
    """生成 multipart/x-mixed-replace 的 MJPEG 分片。

    每片形如：``--frame\\r\\nContent-Type: image/jpeg\\r\\nContent-Length: N\\r\\n\\r\\n<jpeg>``
    浏览器 ``<img src="/api/vision/stream">`` 即可连续显示。
    服务不可用时**结束流**（而非无限空转），前端据此显示占位。
    """
    boundary = b"--frame\r\n"
    interval = 1.0 / fps if fps and fps > 0 else 0.1
    produced = 0
    while max_frames is None or produced < max_frames:
        t0 = time.monotonic()
        try:
            jpg, _w, _h, _src = get_jpeg(channel=channel, port=port, host=host,
                                         quality=quality)
        except (cc.CameraNotRunning, cc.CameraServerError, OSError):
            audit.log("vision_stream_end", channel=channel,
                      reason="camera unavailable")
            return
        yield (boundary + b"Content-Type: image/jpeg\r\n"
               + ("Content-Length: %d\r\n\r\n" % len(jpg)).encode()
               + jpg + b"\r\n")
        produced += 1
        dt = time.monotonic() - t0
        if dt < interval:
            time.sleep(interval - dt)
