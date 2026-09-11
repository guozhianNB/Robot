#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线单测：用板卡实测的原始字节验证切帧逻辑（不连硬件）。

跑法: python3 ros2_car/tools/test_frame_parse.py
"""
import struct
import sys

HEAD = b'\xaa\x55'
CMD_STATUS = 0x82

# 板卡 2026-09-11 实测：连续 3 帧 STATUS，帧长 31B（AA 55 | len=26 | 0x82 | 26B payload）
REAL = bytes.fromhex(
    'aa 55 1a 82 c0 00 00 00 00 00 00 00 00 f2 59 ff ff 7a c4 00 00 96 58 ff ff 7e ae 00 00'
    'aa 55 1a 82 c1 00 00 00 00 00 00 00 00 f2 59 ff ff 7a c4 00 00 96 58 ff ff 7e ae 00 00'
    'aa 55 1a 82 c2 00 00 00 00 00 00 00 00 f2 59 ff ff 7a c4 00 00 96 58 ff ff 7e ae 00 00'
)


def parse_frames(buf: bytearray):
    """实测帧格式（板卡 2026-09-11）：AA 55 | LEN | CMD | PAYLOAD(LEN-1 字节)，共 3+LEN 字节。

    LEN 计的是「CMD + PAYLOAD」字节数；STATUS 的 LEN=26 → CMD 1B + payload 25B
    （seq 1 + rpm 4×int16 8 + enc 4×int32 16 = 25）。
    """
    out = []
    while True:
        i = buf.find(HEAD)
        if i < 0:
            del buf[:]
            break
        if i > 0:
            del buf[:i]
        if len(buf) < 3:
            break
        ln = buf[2]
        total = 3 + ln
        if len(buf) < total:
            break
        del buf[:total]
        out.append(ln)
    return out


def parse_frames_cmd(buf: bytearray):
    """返回 (cmd, payload) 列表。"""
    out = []
    while True:
        i = buf.find(HEAD)
        if i < 0:
            del buf[:]
            break
        if i > 0:
            del buf[:i]
        if len(buf) < 3:
            break
        ln = buf[2]
        total = 3 + ln
        if len(buf) < total:
            break
        body = bytes(buf[3:3 + ln])          # CMD + payload
        out.append((body[0], body[1:]))
        del buf[:total]
    return out


def main():
    ok = True
    buf = bytearray(REAL)
    frames = parse_frames_cmd(buf)
    print(f'切出 {len(frames)} 帧（应为 3），残留 {len(buf)} 字节（应为 0）')
    if len(frames) != 3 or buf:
        ok = False
    for n, (cmd, payload) in enumerate(frames):
        seq = payload[0]
        rpm = struct.unpack_from('<4h', payload, 1)
        enc = struct.unpack_from('<4i', payload, 9)
        flags = payload[24]                  # 25B payload 的最后一字节
        print(f'  帧{n}: cmd=0x{cmd:02x} seq={seq} rpm={rpm} enc={enc} flags=0x{flags:02x}')
        if cmd != CMD_STATUS or len(payload) != 25:
            ok = False

    # 乱流测试：前面塞垃圾字节，应能重同步
    buf2 = bytearray(b'\x00\xff\x13') + REAL
    f2 = parse_frames_cmd(buf2)
    print(f'\n前导垃圾后切出 {len(f2)} 帧（应为 3），残留 {len(buf2)}（应为 0）')
    ok = ok and len(f2) == 3 and not buf2

    # 半帧测试：给 46 字节 = 1 整帧(30) + 16 残字节
    buf3 = bytearray(REAL[:46])
    f3 = parse_frames_cmd(buf3)
    print(f'半帧输入切出 {len(f3)} 帧（应为 1），残留 {len(buf3)} 字节（应为 16）')
    ok = ok and len(f3) == 1 and len(buf3) == 16

    print('\n[PASS] 切帧逻辑正确' if ok else '\n[FAIL] 切帧逻辑有问题')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
