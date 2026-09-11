#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯 raw 帧切分（不做任何协议假设），把每帧手工打印出来，供人工核对偏移。

用法: python3 tools/raw_frames.py --secs 4
"""
import argparse
import sys
import time

HEAD = b'\xaa\x55'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', default='/dev/ttyACM0')
    ap.add_argument('--baud', type=int, default=115200)
    ap.add_argument('--secs', type=float, default=4.0)
    ap.add_argument('--max', type=int, default=4)
    args = ap.parse_args()

    import serial
    ser = serial.Serial(args.port, args.baud, timeout=0.2)
    t0 = time.time()
    data = bytearray()
    while time.time() - t0 < args.secs:
        data += ser.read(4096)
    ser.close()
    print(f'收到 {len(data)} 字节')

    # 手工定位所有 AA55，打印 "len 字段 / 帧头后若干原始字节"
    positions = [i for i in range(len(data) - 1) if data[i] == 0xAA and data[i + 1] == 0x55]
    print(f'AA55 出现 {len(positions)} 次，前几个偏移: {positions[:6]}')
    for n, i in enumerate(positions[:args.max]):
        ln = data[i + 2] if i + 2 < len(data) else None
        raw = bytes(data[i:i + 34])
        print(f'--- 帧 {n} @偏移{i}: len字段={ln}（0x{ln:02x}）---')
        print('   原始 34B:', raw.hex(' '))
        print('   假设 帧体=CMD(1)+payload(len):',
              bytes(data[i + 3:i + 3 + (ln or 0)]).hex(' '))
        print('   假设 帧体=CMD(1)+payload(len-1):',
              bytes(data[i + 3:i + 2 + (ln or 1)]).hex(' '))
    if len(positions) >= 2:
        gap = positions[1] - positions[0]
        print(f'相邻 AA55 间隔 = {gap} 字节（=帧长）')
        print(f'→ 因此 帧长 = 2(头) + 1(len字段) + {gap-3}(体)；'
              f'若 len字段={data[positions[0]+2]} 则 体=len+? ')
    return 0


if __name__ == '__main__':
    sys.exit(main())
