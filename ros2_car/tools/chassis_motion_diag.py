#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""决定性诊断：一边让底盘转，一边读原始 STATUS 帧，对比 RPM/编码器是否变化。

⚠️ 串口独占：先 ~/tools/nav_screen.sh kill base
⚠️ 会真让电机转：确认四轮落地、四周空旷、有人在旁。

用法:
    python3 tools/chassis_motion_diag.py --secs 10 --move vx:0.15:4
    python3 tools/chassis_motion_diag.py --secs 10 --move wz:0.4:4

输出：运动前 5 帧 / 运动中 5 帧 / 停车后 5 帧的 rpm 与 enc，并给出结论。
"""
import argparse
import struct
import sys
import threading
import time

HEAD = b'\xaa\x55'
CMD_STATUS = 0x82
CMD_SET_CAR_VEL = 0x03


def build_frame(cmd, payload=b''):
    body = bytes([cmd]) + payload
    csum = 0
    for b in body:
        csum ^= b
    return HEAD + bytes([len(body)]) + body + bytes([csum])


def set_car_vel(vx, vy, wz):
    return build_frame(CMD_SET_CAR_VEL,
                       struct.pack('<hhh', int(vx * 1000), int(vy * 1000), int(wz * 10)))


def parse(buf: bytearray):
    """实测帧格式：AA 55 | LEN | CMD | PAYLOAD(LEN-1)，帧长 = 3 + LEN（无校验字节）。"""
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
        if len(buf) < 3 + ln:
            break
        body = bytes(buf[3:3 + ln])
        out.append((body[0], body[1:]))
        del buf[:3 + ln]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', default='/dev/ttyACM0')
    ap.add_argument('--baud', type=int, default=115200)
    ap.add_argument('--secs', type=float, default=10.0)
    ap.add_argument('--move', default='vx:0.15:4')
    args = ap.parse_args()

    import serial
    ser = serial.Serial(args.port, args.baud, timeout=0.2)
    print(f'[ok] 打开 {args.port}')

    axis, val, dur = args.move.split(':')
    val, dur = float(val), float(dur)
    vx = val if axis == 'vx' else 0.0
    vy = val if axis == 'vy' else 0.0
    wz = val if axis == 'wz' else 0.0

    phases = {'运动前': [], '运动中': [], '停车后': []}
    phase = ['运动前']
    stop = threading.Event()

    def mover():
        time.sleep(3.0)
        print(f'>>> 下发 {axis}={val} 持续 {dur}s', flush=True)
        phase[0] = '运动中'
        t0 = time.time()
        while time.time() - t0 < dur and not stop.is_set():
            ser.write(set_car_vel(vx, vy, wz))
            time.sleep(0.1)
        for _ in range(6):
            ser.write(set_car_vel(0, 0, 0))
            time.sleep(0.1)
        phase[0] = '停车后'
        print('>>> 零速已下发', flush=True)

    threading.Thread(target=mover, daemon=True).start()

    buf = bytearray()
    t0 = time.time()
    while time.time() - t0 < args.secs:
        buf += ser.read(4096)
        for cmd, p in parse(buf):
            if cmd == CMD_STATUS and len(p) >= 25:
                phases[phase[0]].append(
                    (p[0], struct.unpack_from('<4h', p, 1), struct.unpack_from('<4i', p, 9)))
    stop.set()
    ser.close()

    for name, rows in phases.items():
        print(f'\n===== {name}（{len(rows)} 帧）=====')
        for seq, rpm, enc in rows[:5]:
            print(f'  seq={seq:3d} rpm={rpm} enc={enc}')
    if len(phases['运动中']) >= 2:
        first, last = phases['运动中'][0], phases['运动中'][-1]
        d_rpm = [abs(a) for a in last[1]]
        d_enc = [last[2][i] - first[2][i] for i in range(4)]
        print(f'\n运动中：RPM 峰值 {max(d_rpm)}；编码器位移 Δ={d_enc}')
        if max(d_rpm) > 5 or any(abs(x) > 10 for x in d_enc):
            print('[结论] 底盘在回传真实转速/编码器 → 底盘链路正常。')
            return 0
        print('[结论] 运动中 RPM 仍为 0、编码器不位移 → 固件没有回传真实编码器数据'
              '（或编码器/电机没实际转）。')
        return 2
    return 1


if __name__ == '__main__':
    sys.exit(main())
