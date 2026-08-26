# -*- coding: utf-8 -*-
r"""摄像头共享服务 —— 客户端/服务器共享协议定义。

MIPI 摄像头同一时刻只能被一个进程独占（libsrcampy 打开后即占用 VIO
通道），因此需要"摄像头共享服务"：一个守护进程持有摄像头，其余程序
通过网络请求取帧。本模块定义客户端与服务端之间约定的命令字、二进制
帧头格式与常量，camera_server.py 与 camera_client.py 共用，避免两端
漂移。
"""

import struct

# 服务版本号
PROTOCOL_VERSION = 1
MAGIC = b"VCAM"

# 默认监听地址与端口（只在本机共享，默认 127.0.0.1；如需跨机可 --bind）
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9540

# 帧格式标识
FMT_NV12 = b"NV12"
FMT_JPEG = b"JPEG"

# 服务端错误响应前缀（文本行，\n 结尾）
ERR_PREFIX = b"ERR "

# ---- 命令字（客户端 -> 服务端，1 字节） ----
CMD_INFO = b"I"          # 查询服务信息，返回一行 JSON（\n 结尾）
CMD_GET = b"G"           # 取某通道最新一帧：G + 1 字节通道号
CMD_NEXT = b"N"          # 阻塞等待比 last_id 更新的一帧：N + 通道号 + 8 字节 last_id
CMD_SUB = b"S"           # 订阅某通道连续帧流：S + 1 字节通道号（持续推送）
CMD_JPEG = b"J"          # 取某通道最新一帧的 JPEG 编码：J + 1 字节通道号
CMD_PING = b"P"          # 心跳：返回 PONG

# ---- 帧头二进制格式（固定 40 字节，大端） ----
#  magic     4s   b"VCAM"
#  version   1B   协议版本
#  cmd       1B   帧内容类型（'F'=原始帧 / 'J'=JPEG）
#  channel   1B   通道号（1 起）
#  format    4s   b"NV12" / b"JPEG"
#  width     2B   图像宽
#  height    2B   图像高
#  frame_id  8B   帧序号（服务端单调递增）
#  ts_us     8B   采集时间戳（微秒，time.time_ns()//1000）
#  size      8B   载荷字节数
#  reserved  1B   保留
FRAME_HEADER = struct.Struct(">4sBBB4sHHQQQB")
FRAME_HEADER_SIZE = FRAME_HEADER.size  # 40

# 帧头里 cmd 字段取值
CMD_FRAME = ord("F")
CMD_JPEG_FRAME = ord("J")


def pack_frame_header(cmd, channel, fmt, width, height, frame_id, ts_us, size):
    """把帧头打包成 40 字节二进制。"""
    return FRAME_HEADER.pack(MAGIC, PROTOCOL_VERSION, cmd, channel,
                             fmt, width, height, frame_id, ts_us, size, 0)


def unpack_frame_header(buf):
    """解包 40 字节帧头，返回 dict；魔数/长度不对时抛出 ValueError。"""
    if len(buf) < FRAME_HEADER_SIZE:
        raise ValueError("frame header truncated: %d bytes" % len(buf))
    magic, version, cmd, channel, fmt, width, height, \
        frame_id, ts_us, size, _reserved = FRAME_HEADER.unpack(buf)
    if magic != MAGIC:
        raise ValueError("bad magic: %r" % magic)
    if version != PROTOCOL_VERSION:
        raise ValueError("protocol version mismatch: %d" % version)
    return {
        "cmd": cmd,
        "channel": channel,
        "format": fmt,
        "width": width,
        "height": height,
        "frame_id": frame_id,
        "ts_us": ts_us,
        "size": size,
    }


def nv12_size(width, height):
    """NV12 一帧的字节数：Y 平面 W*H + UV 交织平面 W*H/2。"""
    return width * height * 3 // 2
