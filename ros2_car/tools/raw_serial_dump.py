#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小验证：/dev/ttyACM0 上到底有没有字节流（不做协议解析）。

用法: python3 tools/raw_serial_dump.py --secs 5
输出：收到字节数、前 64 字节 hex、AA55 出现次数与疑似帧长分布。
"""
import argparse
import sys
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', default='/dev/ttyACM0')
    ap.add_argument('--baud', type=int, default=115200)
    ap.add_argument('--secs', type=float, default=5.0)
    args = ap.parse_args()

    import serial
    for p in [args.port, '/dev/ttyACM0', '/dev/ttyACM1']:
        try:
            ser = serial.Serial(p, args.baud, timeout=0.2)
            print(f'[ok] 打开 {p}，等待 {args.secs}s ...')
            t0 = time.time()
            data = bytearray()
            while time.time() - t0 < args.secs:
                data += ser.read(4096)
            ser.close()
            print(f'  收到 {len(data)} 字节（{len(data)/args.secs:.0f} B/s）')
            if data:
                print('  前 64 字节:', data[:64].hex(' '))
                n = data.count(b'\xaa\x55')
                print(f'  AA55 出现 {n} 次')
                lens = {}
                i = 0
                while True:
                    i = data.find(b'\xaa\x55', i)
                    if i < 0 or i + 3 > len(data):
                        break
                    ln = data[i + 2]
                    cmd = data[i + 3] if i + 3 < len(data) else None
                    key = (hex(cmd) if cmd is not None else '?', ln)
                    lens[key] = lens.get(key, 0) + 1
                    i += 1
                print('  疑似 (cmd, len) 分布:', dict(sorted(lens.items(), key=lambda kv: -kv[1])[:8]))
            else:
                print('  [FAIL] 一个字节都没收到')
            return 0 if data else 1
        except Exception as e:                       # noqa: BLE001
            print(f'[fail] 打开 {p} 失败: {type(e).__name__}: {e}')
    return 2


if __name__ == '__main__':
    sys.exit(main())
