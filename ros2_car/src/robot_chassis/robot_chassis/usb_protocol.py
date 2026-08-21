# -*- coding: utf-8 -*-
"""STM32 USB CDC 车控协议编解码。

依据 docs/目标文档及说明/USB车控接口.md v1.0。

帧格式（定长二进制，小端）::

    字节 0     1       2      3      4 .. 4+len-1   4+len
   [0xAA]   [0x55]  [ len ] [ cmd ] [ payload… ]   [ xor ]

- len: payload 字节数（<=32）
- xor: 除末字节外**全部**字节（含帧头/len/cmd/payload）异或
- 下行: 0x01 STOP / 0x03 SET_CAR_VEL / 0x04 TUNE_PID / 0x05 GET_STATUS
- 上行: 0x81 ACK / 0x82 STATUS(26B payload)
"""

import struct

FRAME_HEADER = b"\xAA\x55"
MAX_PAYLOAD = 32

# 下行命令（板卡 → STM32）
CMD_STOP = 0x01
CMD_SET_CAR_VEL = 0x03
CMD_TUNE_PID = 0x04
CMD_GET_STATUS = 0x05
# 上行命令（STM32 → 板卡）
CMD_ACK = 0x81
CMD_STATUS = 0x82

STATUS_PAYLOAD_LEN = 26


def build_frame(cmd: int, payload: bytes = b"") -> bytes:
    """按协议组帧，返回完整字节串。"""
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload 超过 {MAX_PAYLOAD} 字节: {len(payload)}")
    body = bytes([len(payload), cmd]) + payload
    frame = FRAME_HEADER + body
    xor = 0
    for b in frame:
        xor ^= b
    return frame + bytes([xor])


def build_set_car_vel(vx_mm: int, vy_mm: int, wz_tenth_deg: int) -> bytes:
    """整车速度帧。

    :param vx_mm: 前进速度，mm/s（正=前进）
    :param vy_mm: 左移速度，mm/s（正=向左）
    :param wz_tenth_deg: 旋转角速度，0.1°/s（正=左转）
    """
    return build_frame(CMD_SET_CAR_VEL, struct.pack("<hhh", vx_mm, vy_mm, wz_tenth_deg))


def build_stop() -> bytes:
    """STOP 帧：立即四轮制动。"""
    return build_frame(CMD_STOP)


def build_get_status() -> bytes:
    """按需查询一帧 STATUS。"""
    return build_frame(CMD_GET_STATUS)


class FrameParser:
    """增量解析串口字节流，逐帧吐出 (cmd, payload)。坏帧自动重新同步。"""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data: bytes) -> None:
        self._buf.extend(data)

    def next_frame(self):
        """取出下一帧；数据不足时返回 None。"""
        while len(self._buf) >= 4:
            idx = self._buf.find(FRAME_HEADER)
            if idx < 0:
                self._buf.clear()
                return None
            if idx > 0:
                del self._buf[:idx]
            if len(self._buf) < 4:
                return None
            plen = self._buf[2]
            total = 4 + plen + 1
            if len(self._buf) < total:
                return None
            frame = bytes(self._buf[:total])
            del self._buf[:total]
            xor = 0
            for b in frame[:-1]:
                xor ^= b
            if xor != frame[-1]:
                continue  # 校验失败：丢弃本帧继续同步
            return frame[3], frame[4 : 4 + plen]
        return None


def decode_status(payload: bytes):
    """解析 STATUS(0x82) 的 26 字节 payload。

    :return: (seq, rpm, enc, flags)
        rpm = (rpm_LF, rpm_RF, rpm_LR, rpm_RR) 四轮实际转速 (RPM)
        enc = (enc_LF, enc_RF, enc_LR, enc_RR) 编码器累计计数（带符号）
    """
    if len(payload) != STATUS_PAYLOAD_LEN:
        raise ValueError(f"STATUS payload 长度错误: {len(payload)}，应为 {STATUS_PAYLOAD_LEN}")
    seq = payload[0]
    rpm = struct.unpack_from("<4h", payload, 1)
    enc = struct.unpack_from("<4i", payload, 9)
    flags = payload[25]
    return seq, rpm, enc, flags
