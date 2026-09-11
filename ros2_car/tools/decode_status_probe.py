#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断"底盘 odom 位姿冻结"：直读 /dev/ttyACM0 的 0x82 STATUS，用官方口径解码
四轮 RPM 与四轮编码器累计计数，并可选择同时下发一段 cmd_vel，观察编码器是否在变。

⚠️ 串口独占：必须先停 chassis_driver：~/tools/nav_screen.sh kill base
⚠️ 会真让电机转：确认四轮落地、四周空旷、有人在旁。

用法（板卡，已 source 环境）:
    python3 tools/decode_status_probe.py --secs 6                     # 只读，不动
    python3 tools/decode_status_probe.py --secs 10 --move vx:0.15:3   # 读 6s 后发 3s 前进

输出判定：
  - RPM 一直 0 且 enc 不变  → 编码器/固件没回传（odom 必然冻结）
  - RPM 非 0 / enc 在变     → 底盘在回传，冻结在 ROS 侧（驱动/话题）
"""
import argparse
import os
import struct
import sys
import threading
import time

HEAD = b'\xaa\x55'
CMD_STATUS = 0x82
CMD_SET_CAR_VEL = 0x03


def build_frame(cmd: int, payload: bytes = b'') -> bytes:
    body = bytes([cmd]) + payload
    csum = 0
    for b in body:
        csum ^= b
    return HEAD + bytes([len(body)]) + body + bytes([csum])


def set_car_vel(vx: float, vy: float, wz: float) -> bytes:
    return build_frame(CMD_SET_CAR_VEL, struct.pack(
        '<hhh', int(vx * 1000), int(vy * 1000), int(wz * 10)))


def parse_frames(buf: bytearray):
    """从字节流里切帧。关键：先按 AA55 重同步再判长度，长度不够时保留头部等待更多字节。

    上一版 bug：长度不够时直接 break，把已到达的 AA55 留在了 buf 里；下一轮 find 又会
    找到同一个 AA55 → 永远原地打转；且流中间开始时前导字节不丢会让帧永久错位。
    """
    out = []
    while True:
        i = buf.find(HEAD)
        if i < 0:
            # 没有帧头：丢弃全部（但保留最后 1 字节，AA 可能被切在包尾）
            del buf[:max(0, len(buf) - 1)]
            break
        if i > 0:
            del buf[:i]                       # 丢弃帧头之前的垃圾字节
        if len(buf) < 4:
            break                             # 帧头已对齐，等更多字节
        ln = buf[2]
        # LEN 字段 = payload 长度；帧体 = CMD(1B) + payload(ln B)，共 ln+1 字节
        if len(buf) < 2 + ln + 2:
            break                             # 帧头已对齐，等更多字节
        out.append(bytes(buf[2:2 + ln + 1]))
        del buf[:2 + ln + 2]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', default='/dev/ttyACM0')
    ap.add_argument('--baud', type=int, default=115200)
    ap.add_argument('--secs', type=float, default=6.0)
    ap.add_argument('--move', default='', help='如 vx:0.15:3 → 前进 0.15m/s 持续 3s（中途下发）')
    args = ap.parse_args()

    import serial
    ser = serial.Serial(args.port, args.baud, timeout=0.05)

    move_args = None
    if args.move:
        parts = args.move.split(':')
        move_args = (parts[0], float(parts[1]), float(parts[2]))

    stop_flag = threading.Event()

    def mover():
        time.sleep(max(1.0, args.secs * 0.35))
        axis, val, dur = move_args
        vx = val if axis == 'vx' else 0.0
        vy = val if axis == 'vy' else 0.0
        wz = val if axis == 'wz' else 0.0
        print(f'>>> 下发 {axis}={val} 持续 {dur}s', flush=True)
        t0 = time.time()
        while time.time() - t0 < dur and not stop_flag.is_set():
            ser.write(set_car_vel(vx, vy, wz))
            time.sleep(0.1)
        for _ in range(5):
            ser.write(set_car_vel(0.0, 0.0, 0.0))
            time.sleep(0.1)
        print('>>> 已下发零速', flush=True)

    if move_args:
        threading.Thread(target=mover, daemon=True).start()

    buf = bytearray()
    rpms, encs, seqs = [], [], []
    t0 = time.time()
    while time.time() - t0 < args.secs:
        buf += ser.read(512)
        for f in parse_frames(buf):
            if not f or f[0] != CMD_STATUS:
                continue
            p = f[1:]
            if len(p) != 26:
                continue
            seqs.append(p[0])
            rpms.append(struct.unpack_from('<4h', p, 1))
            encs.append(struct.unpack_from('<4i', p, 9))
    stop_flag.set()
    time.sleep(0.3)
    ser.close()

    print(f'STATUS 帧 {len(seqs)} 帧（{len(seqs)/args.secs:.1f} Hz）')
    if not seqs:
        print('[FAIL] 没收到 0x82：固件没发或口不对')
        return 1

    print(f'{ "wheel":>7s} {"rpm_min":>8s} {"rpm_max":>8s} {"enc_首":>12s} {"enc_末":>12s} {"enc_Δ":>10s}')
    ok = False
    for i, name in enumerate(['LF', 'RF', 'LR', 'RR']):
        r = [x[i] for x in rpms]
        e = [x[i] for x in encs]
        d = e[-1] - e[0]
        if d != 0:
            ok = True
        print(f'{name:>7s} {min(r):8d} {max(r):8d} {e[0]:12d} {e[-1]:12d} {d:10d}')

    print()
    if ok:
        print('[结论] 编码器计数在变化 → 底盘回传正常，"odom 冻结"发生在 ROS 侧（驱动/话题/集成）。')
        return 0
    print('[结论] 编码器计数全程不变 → 固件/编码器没回传真实计数值，odom 必然冻结。')
    return 1


if __name__ == '__main__':
    sys.exit(main())
