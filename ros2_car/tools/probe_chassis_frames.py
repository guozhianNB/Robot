#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""裸串口探针：读 STM32 USB CDC 上行帧，统计各命令号与 0x83(IMU) 频率。

用途（台架/板卡验收第一步，见 docs/superpowers/plans/2026-09-10-imu-stm32-integration.md）:
    - 只有 0x82(STATUS) → 固件还是旧版（没烧新固件）
    - 有 0x82 且出现 0x83(IMU) → 固件已烧 + IMU 已接且在出帧
    - 有 0x82 但无 0x83 → 固件是新的但 IMU 没接/没数据（新鲜度门控在不发）

注意：独占串口，运行前先停掉 chassis_driver（否则抢不到口 / 数据被读走）。

用法:
    python3 tools/probe_chassis_frames.py [秒数] [串口]
    python3 tools/probe_chassis_frames.py 5 /dev/ttyACM0
"""
import collections
import sys
import time

try:
    import serial
except ImportError:
    print("缺少 pyserial：pip3 install pyserial")
    sys.exit(1)

HDR1, HDR2 = 0xAA, 0x55
NAMES = {0x81: 'ACK', 0x82: 'STATUS', 0x83: 'IMU'}


def main():
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    port = sys.argv[2] if len(sys.argv) > 2 else '/dev/ttyACM0'

    ser = serial.Serial(port, 115200, timeout=0.1)
    print(f"读 {port} @115200，{seconds:g}s …")

    counts = collections.Counter()
    bad = 0
    buf = bytearray()
    t0 = time.time()

    while time.time() - t0 < seconds:
        buf.extend(ser.read(512))
        i = 0
        while i + 4 <= len(buf):
            if buf[i] != HDR1 or buf[i + 1] != HDR2:
                i += 1
                continue
            plen = buf[i + 2]
            total = 4 + plen + 1
            if i + total > len(buf):
                break
            xor = 0
            for b in buf[i:i + total - 1]:
                xor ^= b
            if xor == buf[i + total - 1]:
                counts[buf[i + 3]] += 1
            else:
                bad += 1
            i += total
        del buf[:i]

    ser.close()
    elapsed = time.time() - t0

    print("命令号统计:")
    for cmd, n in sorted(counts.items()):
        print("  0x%02X %-7s %5d 帧  (%.1f Hz)" % (cmd, NAMES.get(cmd, '?'), n, n / elapsed))
    if bad:
        print(f"  校验失败丢弃: {bad} 帧")

    if 0x82 not in counts:
        print("✗ 一帧 STATUS 都没收到：检查 USB / 波特率 / 串口是否被别的进程占用")
    elif 0x83 not in counts:
        print("✗ 无 0x83(IMU)：固件可能还是旧版，或 IMU 未接/无数据（新鲜度门控不发）")
    else:
        print("✓ 收到 0x83(IMU) 帧，固件已烧且 IMU 在出数据")


if __name__ == '__main__':
    main()
