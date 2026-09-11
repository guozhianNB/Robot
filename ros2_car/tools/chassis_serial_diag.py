#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""底盘串口诊断（自检版）：一次运行同时给出 raw 字节统计、帧解析统计、编码器/RPM 对照。

⚠️ 串口独占：先停 chassis_driver  ->  ~/tools/nav_screen.sh kill base
⚠️ --move 会真让电机转：确认四轮落地、四周空旷、有人在旁。

用法:
    python3 tools/chassis_serial_diag.py --secs 6                      # 纯只读
    python3 tools/chassis_serial_diag.py --secs 8 --move vx:0.15:3     # 中间前进 3s

判定:
    raw 有字节但解析 0 帧  -> 解析器/协议问题（脚本 bug）
    解析到帧但 enc 不变     -> 固件没回传真实编码器计数（odom 必然冻结）
    enc 在变               -> 底盘正常，"odom 冻结"在 ROS 侧
"""
import argparse
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


def set_car_vel(vx, vy, wz) -> bytes:
    return build_frame(CMD_SET_CAR_VEL,
                       struct.pack('<hhh', int(vx * 1000), int(vy * 1000), int(wz * 10)))


def parse_frames(buf: bytearray, stats):
    out = []
    while True:
        i = buf.find(HEAD)
        if i < 0:
            stats['dropped'] += len(buf)
            del buf[:]
            break
        if i > 0:
            stats['dropped'] += i
            del buf[:i]
        if len(buf) < 4:
            break
        ln = buf[2]
        # 固件口径（usb_proto.c::up_send）：LEN 计「cmd+payload」字节数，
        # 帧 = AA 55 | LEN | CMD | payload(LEN-1) | XOR，帧长 = 4 + LEN。
        # STATUS 的 LEN=26 → CMD 1B + payload 25B（seq1+rpm8+enc16+flags1=26？不：
        # 固件 payload 实为 26，LEN=len(payload)=26 仅指 payload → 帧长 3+LEN+1）
        total = 3 + ln + 1
        if len(buf) < total:
            break
        body = bytes(buf[3:3 + ln])           # CMD + payload
        out.append((body[0], body[1:]))
        stats['cmds'][body[0]] = stats['cmds'].get(body[0], 0) + 1
        del buf[:total]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', default='/dev/ttyACM0')
    ap.add_argument('--baud', type=int, default=115200)
    ap.add_argument('--secs', type=float, default=6.0)
    ap.add_argument('--move', default='')
    args = ap.parse_args()

    import serial
    ser = serial.Serial(args.port, args.baud, timeout=0.2)
    print(f'[ok] 已打开 {args.port} @ {args.baud}')

    move_args = None
    if args.move:
        p = args.move.split(':')
        move_args = (p[0], float(p[1]), float(p[2]))
        print(f'[plan] 约 {args.secs*0.35:.1f}s 后下发 {p[0]}={p[1]} 持续 {p[2]}s（随后补零速）')

    stop_flag = threading.Event()

    def mover():
        time.sleep(max(1.0, args.secs * 0.35))
        axis, val, dur = move_args
        vx = val if axis == 'vx' else 0.0
        vy = val if axis == 'vy' else 0.0
        wz = val if axis == 'wz' else 0.0
        print(f'>>> 下发 {axis}={val}', flush=True)
        t0 = time.time()
        while time.time() - t0 < dur and not stop_flag.is_set():
            ser.write(set_car_vel(vx, vy, wz))
            time.sleep(0.1)
        for _ in range(5):
            ser.write(set_car_vel(0, 0, 0))
            time.sleep(0.1)
        print('>>> 零速已下发', flush=True)

    if move_args:
        threading.Thread(target=mover, daemon=True).start()

    buf = bytearray()
    stats = {'dropped': 0, 'cmds': {}, 'bytes': 0, 'reads': 0}
    rpms, encs, seqs, flagbytes = [], [], [], []
    t0 = time.time()
    while time.time() - t0 < args.secs:
        chunk = ser.read(4096)
        stats['reads'] += 1
        stats['bytes'] += len(chunk)
        buf += chunk
        for f in parse_frames(buf, stats):
            if not f or f[0] != CMD_STATUS:
                continue
            p = f[1:]
            if len(p) != 26:
                continue
            seqs.append(p[0])
            rpms.append(struct.unpack_from('<4h', p, 1))
            encs.append(struct.unpack_from('<4i', p, 9))
            flagbytes.append(p[25])
    stop_flag.set()
    time.sleep(0.3)
    ser.close()

    print(f'\n---- 原始统计 ----')
    print(f'read() 调用 {stats["reads"]} 次，收到 {stats["bytes"]} 字节'
          f'（{stats["bytes"]/args.secs:.0f} B/s），丢弃 {stats["dropped"]} 字节')
    print('各 cmd 帧计数:', {hex(k): v for k, v in sorted(stats['cmds'].items())})
    print(f'解析出 0x82 STATUS {len(seqs)} 帧（{len(seqs)/args.secs:.1f} Hz）')

    if not seqs:
        print('[FAIL] 无 STATUS 帧：若上面字节数也为 0 → 口/权限问题；有字节但 0 帧 → 解析问题')
        if stats['bytes']:
            print('  调试：buf 残留', len(buf), '字节；最后 32 字节', bytes(buf[-32:]).hex(' '))
        return 1

    print(f'\n---- 四轮数据 ----')
    print(f'{"wheel":>6s} {"rpm_min":>8s} {"rpm_max":>8s} {"enc_首":>12s} {"enc_末":>12s} {"enc_Δ":>10s}')
    moved = False
    for i, name in enumerate(['LF', 'RF', 'LR', 'RR']):
        r = [x[i] for x in rpms]
        e = [x[i] for x in encs]
        d = e[-1] - e[0]
        moved = moved or d != 0
        print(f'{name:>6s} {min(r):8d} {max(r):8d} {e[0]:12d} {e[-1]:12d} {d:10d}')
    print(f'seq: {seqs[0]} → {seqs[-1]}（推进 {"是" if seqs[-1] != seqs[0] else "否"}）'
          f'  flags 取值 {sorted(set(flagbytes))}')

    print()
    if moved:
        print('[结论] 编码器计数在变 → 底盘回传正常；"odom 冻结"在 ROS 侧（驱动/话题）。')
        return 0
    print('[结论] 编码器计数全程不变 → 固件没回传真实计数（或编码器没转），odom 必然冻结。')
    return 2


if __name__ == '__main__':
    sys.exit(main())
