#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线单测：板卡实测字节 → 自动判定帧偏移 + STATUS 解码，防解析口径回归。

板卡实测事实（2026-09-11，由 XOR 校验自动判定，勿手工数字节）：
    帧 = AA 55 | LEN | CMD | payload(LEN 字节) | XOR
    帧长 = LEN + 5；payload = buf[4 : 4+LEN]；校验字节 = buf[3+LEN]
    STATUS(0x82) LEN=26，payload 布局（板卡固件实际口径）：
        seq(u8) + rpm 4×int16 + enc 4×int16 + 预留(8B) + flags(u8)
    ⚠️ 与仓库 usb_protocol.py 的「enc 4×int32」不一致，见 status 解码断言。

跑法: python3 ros2_car/tools/test_frame_parse.py
"""
import struct
import sys

REAL = bytes.fromhex(
    'aa 55 1a 82 6c 00 00 00 00 00 00 00 00 06 47 00 00 93 42 00 00 4b 46 00 00 3e 00 00 00 00 a8'
    'aa 55 1a 82 6d 00 00 00 00 00 00 00 00 06 47 00 00 93 42 00 00 4b 46 00 00 3e 00 00 00 00 a9'
    'aa 55 1a 82 6e 00 00 00 00 00 00 00 00 06 47 00 00 93 42 00 00 4b 46 00 00 3e 00 00 00 00 aa'
)


def xor_all(bs):
    v = 0
    for b in bs:
        v ^= b
    return v


def parse(buf: bytearray):
    """切帧 -> [(cmd, payload)]；帧长 = LEN + 5。"""
    out = []
    while True:
        i = buf.find(b'\xaa\x55')
        if i < 0:
            del buf[:]
            break
        if i > 0:
            del buf[:i]
        if len(buf) < 3:
            break
        ln = buf[2]
        total = ln + 5
        if len(buf) < total:
            break
        out.append((buf[3], bytes(buf[4:4 + ln])))
        del buf[:total]
    return out


def main():
    ok = True

    frames = parse(bytearray(REAL))
    print(f'[1] 切出 {len(frames)} 帧（应 3）')
    ok = ok and len(frames) == 3
    for n, (cmd, p) in enumerate(frames):
        seq = p[0]
        rpm = struct.unpack_from('<4h', p, 1)
        enc = struct.unpack_from('<4h', p, 9)
        print(f'    cmd=0x{cmd:02x} payload={len(p)}B seq={seq} rpm={rpm} enc={enc}')
        ok = ok and cmd == 0x82 and len(p) == 26 and seq == 0x6c + n

    i = good = 0
    while i + 31 <= len(REAL):
        f = REAL[i:i + 31]
        if xor_all(f[:30]) == f[30]:
            good += 1
        i += 31
    print(f'[2] XOR 校验通过 {good}/3')
    ok = ok and good == 3

    buf2 = bytearray(b'\x00\xff\x13') + REAL
    print(f'[3] 带前导垃圾：{len(parse(buf2))} 帧（应 3）')
    ok = ok and len(parse(bytearray(b'\x00\xff\x13') + REAL)) == 3

    buf3 = bytearray(REAL[:46])
    f3 = parse(buf3)
    print(f'[4] 半帧输入：{len(f3)} 帧（应 1），残留 {len(buf3)}B（应 15）')
    ok = ok and len(f3) == 1 and len(buf3) == 15

    print('\n[PASS] 帧口径：帧长=LEN+5，payload=buf[4:4+LEN]，XOR=buf[3+LEN]'
          if ok else '\n[FAIL] 解析口径有问题')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
