#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IMU 只读探针：读 STM32 USB CDC 的 0x83(IMU) 帧，解码姿态值并判定"IMU 是否可用"。

只读、不发任何下行命令（STATUS/IMU 都是固件自动上报），因此可安全运行；
但串口是独占的，运行前请先停掉 chassis_driver：`~/tools/nav_screen.sh kill base`。

判定逻辑（对应 docs/superpowers/specs/2026-09-10-imu-stm32-integration-design.md §5）:
    - 一帧 0x82 都没有        → USB/线/波特率问题，或串口被别的进程占了
    - 有 0x82 但无 0x83       → 固件是旧版（没烧 0x83），或 IMU 未接/无数据（新鲜度门控不发）
    - 有 0x83 但四元组恒定    → imu.c 没解析到真帧（TX/RX 接反、模块不主动上报、功能码对不上）
    - 有 0x83 且值随转动变化  → IMU 可用；再看 yaw_rate 符号是否 CCW 为正

用法:
    python3 tools/imu_probe.py                      # 默认 /dev/ttyACM0，采 8s
    python3 tools/imu_probe.py --secs 12
    python3 tools/imu_probe.py --echo                # 逐帧打印
    python3 tools/imu_probe.py --selftest            # 无硬件自检（合成帧过一遍解析）
"""
import argparse
import struct
import sys
import time

HDR1, HDR2 = 0xAA, 0x55
NAMES = {0x81: 'ACK', 0x82: 'STATUS', 0x83: 'IMU'}
CMD_STATUS = 0x82
CMD_IMU = 0x83


def build_frame(cmd, payload=b''):
    """与固件 usb_proto.c 同构：AA 55 | len | cmd | payload | xor。"""
    body = bytes([len(payload), cmd]) + payload
    frame = bytes([HDR1, HDR2]) + body
    xor = 0
    for b in frame:
        xor ^= b
    return frame + bytes([xor])


def build_imu_payload(yaw_deg, rate_dps, roll_deg, pitch_deg):
    return struct.pack('<4h', int(round(yaw_deg * 100)), int(round(rate_dps * 10)),
                       int(round(roll_deg * 100)), int(round(pitch_deg * 100)))


def decode_imu(payload):
    """与 usb_protocol.decode_imu 同构：yaw 0.01° / rate 0.1°/s / roll,pitch 0.01°。"""
    yaw, rate, roll, pitch = struct.unpack_from('<4h', payload, 0)
    return yaw / 100.0, rate / 10.0, roll / 100.0, pitch / 100.0


def parse_chunk(buf, counts, samples, bad, echo, t_now):
    """增量解析 buf，返回消费到的位置（缓冲区尾部保留不完整帧）。"""
    i = 0
    while i + 4 <= len(buf):
        if buf[i] != HDR1 or buf[i + 1] != HDR2:
            i += 1
            continue
        plen = buf[i + 2]
        total = 4 + plen + 1
        if i + total > len(buf):
            break
        frame = bytes(buf[i:i + total])
        xor = 0
        for b in frame[:-1]:
            xor ^= b
        if xor == frame[-1]:
            cmd = frame[3]
            counts[cmd] = counts.get(cmd, 0) + 1
            if cmd == CMD_IMU and plen == 8:
                yaw, rate, roll, pitch = decode_imu(frame[4:12])
                samples.append((t_now, yaw, rate, roll, pitch))
                if echo:
                    print("  t=%6.2fs yaw=%8.2f° rate=%7.2f°/s roll=%7.2f° pitch=%7.2f°"
                          % (t_now, yaw, rate, roll, pitch))
        else:
            bad[0] += 1
        i += total
    return i


def report(counts, samples, bad, elapsed):
    print("\n命令号统计:")
    for cmd in sorted(counts):
        print("  0x%02X %-7s %5d 帧  (%.1f Hz)"
              % (cmd, NAMES.get(cmd, '?'), counts[cmd], counts[cmd] / elapsed))
    if bad[0]:
        print("  校验失败丢弃: %d 帧" % bad[0])

    print("\n0x83(IMU) 解码:")
    if not samples:
        print("  （无样本）")
    else:
        ys = [s[1] for s in samples]
        rs = [s[2] for s in samples]
        print("  样本 %d 帧，%.2f Hz" % (len(samples), len(samples) / elapsed))
        print("  yaw      : 首 %8.2f°  末 %8.2f°  范围 [%.2f, %.2f]"
              % (ys[0], ys[-1], min(ys), max(ys)))
        print("  yaw_rate : 范围 [%.2f, %.2f] °/s  (静止时应≈0)" % (min(rs), max(rs)))
        print("  末帧     : yaw=%.2f° rate=%.2f°/s roll=%.2f° pitch=%.2f°"
              % (samples[-1][1], samples[-1][2], samples[-1][3], samples[-1][4]))

    print("\n结论:")
    if CMD_STATUS not in counts and CMD_IMU not in counts:
        print("  ✗ 一帧上行帧都没收到：USB 线/口没通、固件没跑，或串口被别的进程占用")
        print("    （先 ls /dev/ttyACM* 看设备在不在；没有就是 USB 侧的问题，不是 IMU 的问题）")
    elif CMD_IMU not in counts:
        print("  ✗ 有 STATUS 但没有 0x83：固件可能是旧版（无 IMU 帧），")
        print("    或 IMU 没在出数据（0x83 有新鲜度门控——imu.c 解析不到新帧就不发，不会发零值）")
        print("    → 查接线：STM32 PB10(TX)→模块 RX、PB11(RX)→模块 TX、共地、模块供电")
        print("    → 查 imu.c 的波特率(115200)/功能码与模块是否一致（模块帧头应为 7E 23）")
    elif len(samples) >= 2:
        frozen = len({(s[1], s[2], s[3], s[4]) for s in samples}) == 1
        if frozen:
            print("  ⚠ 有 0x83 但四元组全程恒定：imu.c 收到的不是真数据（模块只答不问/接线反/功能码对不上）")
        else:
            print("  ✓ IMU 有数据且在变：帧率 %.1f Hz（固件设计 20Hz）" % (len(samples) / elapsed))
            print("    → 标定：手托车体逆时针转，yaw_rate 与 yaw 应增大（CCW 为正）；")
            print("      不对就改 chassis_params.yaml 的 imu_sign_wz / imu_sign_yaw")
    else:
        print("  ⚠ 0x83 样本太少，延长 --secs 再看")


def run_selftest():
    """无硬件自检：合成 0x82×3 + 0x83×3（含半帧截断）过一遍解析路径。"""
    stream = b''
    for _ in range(3):
        stream += build_frame(CMD_STATUS, b'\x00' * 26)
    for k, (y, r, ro, p) in enumerate([(10.0, 0.0, 0.5, -0.5), (10.5, 5.0, 0.5, -0.5),
                                       (11.0, 5.2, 0.6, -0.4)]):
        stream += build_frame(CMD_IMU, build_imu_payload(y, r, ro, p))
    stream += b'\xaa\x55\x08\x83'          # 故意留半帧，不能崩

    counts, samples, bad = {}, [], [0]
    buf = bytearray(stream)
    consumed = parse_chunk(buf, counts, samples, bad, echo=False, t_now=0.0)
    del buf[:consumed]
    assert counts.get(CMD_STATUS) == 3, counts
    assert counts.get(CMD_IMU) == 3, counts
    assert bad[0] == 0, bad
    assert len(buf) == 4, bytes(buf)
    assert samples[0][1] == 10.0 and abs(samples[1][2] - 5.0) < 1e-6, samples

    # 长度不符的 0x83 不能崩、也不能被当成 IMU 样本
    counts2, samples2, bad2 = {}, [], [0]
    parse_chunk(bytearray(build_frame(CMD_IMU, b'\x00' * 7)), counts2, samples2, bad2,
                echo=False, t_now=0.0)
    assert samples2 == [], samples2

    print("selftest: 切帧/校验/半帧截断/长度守卫 全部通过")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', default='/dev/ttyACM0')
    ap.add_argument('--secs', type=float, default=8.0)
    ap.add_argument('--baud', type=int, default=115200)
    ap.add_argument('--echo', action='store_true', help='逐帧打印解码结果')
    ap.add_argument('--selftest', action='store_true', help='无硬件自检')
    args = ap.parse_args()

    if args.selftest:
        run_selftest()
        return

    try:
        import serial
    except ImportError:
        print("缺少 pyserial：pip3 install pyserial")
        sys.exit(1)

    try:
        ser = serial.Serial(args.port, args.baud, timeout=0.1)
    except Exception as exc:                                    # noqa: BLE001
        print("✗ 打不开 %s：%s" % (args.port, exc))
        print("  ① 串口在不在：ls /dev/ttyACM*（没有 = STM32 的 USB 没被板卡识别）")
        print("  ② 是不是被占了：pgrep -af chassis_driver")
        sys.exit(1)

    print("读 %s @%d，%gs …（只读，不发下行帧）" % (args.port, args.baud, args.secs))
    counts, samples, bad = {}, [], [0]
    buf = bytearray()
    t0 = time.time()

    while time.time() - t0 < args.secs:
        buf.extend(ser.read(512))
        del buf[:parse_chunk(buf, counts, samples, bad, args.echo, time.time() - t0)]

    ser.close()
    report(counts, samples, bad, time.time() - t0)


if __name__ == '__main__':
    main()
