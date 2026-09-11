#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""帧结构验证器：抓真实字节流，用 XOR 校验判定切帧假设（H1: 帧长=3+LEN / H2: 帧长=4+LEN）。

不假设任何一方，让校验和说话。

用法: python3 tools/frame_format_check.py --secs 4
"""
import argparse
import sys
import time

HEAD = b'\xaa\x55'


def xor_all(bs):
    v = 0
    for b in bs:
        v ^= b
    return v


def try_hypothesis(data, mode, max_frames=50):
    """mode='h1': 帧=AA55|LEN|CMD|payload(LEN-1)|XOR  → 帧长 3+LEN
       mode='h2': 帧=AA55|LEN|CMD|payload(LEN-1)|XOR 中 LEN 只算 payload → 帧长 3+LEN 同上
       （两种假设的实现差异在 payload 取法，见下）"""
    ok = bad = 0
    i = 0
    frames = []
    while i + 4 <= len(data) and len(frames) < max_frames:
        if data[i:i + 2] != HEAD:
            i += 1
            continue
        ln = data[i + 2]
        if mode == 'a':                      # 帧长 = 3 + ln，XOR 覆盖到帧内倒数第 1 字节
            total = 3 + ln
        else:                                # 帧长 = 4 + ln
            total = 4 + ln
        if i + total > len(data):
            break
        frame = data[i:i + total]
        # 校验：除末字节外全部 XOR == 末字节
        if xor_all(frame[:-1]) == frame[-1]:
            ok += 1
        else:
            bad += 1
        frames.append((ln, frame[:6].hex(' '), frame[-1], xor_all(frame[:-1])))
        i += total
    return ok, bad, frames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', default='/dev/ttyACM0')
    ap.add_argument('--baud', type=int, default=115200)
    ap.add_argument('--secs', type=float, default=4.0)
    args = ap.parse_args()

    import serial
    ser = serial.Serial(args.port, args.baud, timeout=0.2)
    t0 = time.time()
    data = bytearray()
    while time.time() - t0 < args.secs:
        data += ser.read(4096)
    ser.close()
    print(f'收到 {len(data)} 字节，AA55 出现 {data.count(HEAD)} 次\n')

    for mode, name in [('a', 'H_a: 帧长 = 3 + LEN'), ('b', 'H_b: 帧长 = 4 + LEN')]:
        ok, bad, frames = try_hypothesis(bytes(data), mode)
        print(f'{name}: XOR 校验通过 {ok} 帧 / 失败 {bad} 帧')
        if frames[:3]:
            for ln, head, last, calc in frames[:3]:
                print(f'   LEN={ln:3d} 头={head} 末字节=0x{last:02x} 计算XOR=0x{calc:02x}')
        print()

    # 人工打印 LEN 与相邻 AA55 间距，进一步佐证
    pos = [i for i in range(len(data) - 1) if data[i] == 0xAA and data[i + 1] == 0x55]
    if len(pos) >= 3:
        gaps = [pos[i + 1] - pos[i] for i in range(min(5, len(pos) - 1))]
        print(f'相邻 AA55 间距: {gaps}；LEN 字段: {[data[p + 2] for p in pos[:5]]}')
        print(f'→ 帧长 {gaps[0]}，LEN={data[pos[0]+2]}，'
              f'因此 帧长 - LEN = {gaps[0] - data[pos[0]+2]}（固定开销字节数）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
