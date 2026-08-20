#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
USB 车控接口 - 地瓜派端示例驱动
================================
对应协议：docs/2.pre/USB车控接口.md

用法（地瓜派 / 任意装有 pyserial 的机器）：
    pip install pyserial
    python usb_chassis_demo.py --port /dev/ttyACM0

功能：
    - 封装帧构建/解析
    - 示例：前进、横移、旋转、停止
    - 打印 STM32 心跳 STATUS（四轮转速 + 编码器计数）

通信参数：115200 8N1，二进制帧协议。
"""

import argparse
import functools
import operator
import struct
import time

import serial

# ---- 命令号（与 usb_proto.h 保持一致） ----
CMD_STOP        = 0x01
CMD_SET_CAR_VEL = 0x03
CMD_TUNE_PID    = 0x04
CMD_GET_STATUS  = 0x05

CMD_ACK         = 0x81
CMD_STATUS      = 0x82

# ---- 应答码 ----
ACK_OK      = 0x00
ACK_BAD_CMD = 0x01
ACK_BAD_LEN = 0x02


class Chassis:
    def __init__(self, port, baud=115200):
        self.ser = serial.Serial(port, baud, timeout=0.1)

    # ---------- 帧构建 ----------
    @staticmethod
    def build_frame(cmd, payload=b""):
        head = bytes([0xAA, 0x55, len(payload), cmd])
        body = head + payload
        xor = functools.reduce(operator.xor, body, 0)
        return body + bytes([xor])

    # ---------- 下行命令 ----------
    def stop(self):
        self.ser.write(self.build_frame(CMD_STOP))

    def set_vel(self, vx, vy=0, w=0):
        """vx/vy: mm/s, w: 0.1°/s"""
        payload = struct.pack("<hhh", int(vx), int(vy), int(w))
        self.ser.write(self.build_frame(CMD_SET_CAR_VEL, payload))

    def tune_pid(self, kp=0.0, ki=0.0, kd=0.0):
        """传 0 表示不修改该项"""
        payload = struct.pack("<fff", kp, ki, kd)
        self.ser.write(self.build_frame(CMD_TUNE_PID, payload))

    def get_status(self):
        self.ser.write(self.build_frame(CMD_GET_STATUS))

    # ---------- 接收 & 解析 ----------
    def _read_frame(self):
        """读一完整帧；解析失败返回 None"""
        buf = self.ser.read(4)
        if len(buf) < 4:
            return None
        # 找帧头
        while len(buf) >= 4:
            i = buf.find(b"\xaa\x55")
            if i < 0:
                buf = buf[-1:] + self.ser.read(64)
                continue
            if i > 0:
                buf = buf[i:]
            if len(buf) < 4:
                buf += self.ser.read(4 - len(buf))
                continue
            plen, cmd = buf[2], buf[3]
            need = 4 + plen + 1
            while len(buf) < need:
                buf += self.ser.read(need - len(buf))
            f = buf[:need]
            buf = buf[need:]
            if functools.reduce(operator.xor, f[:-1], 0) == f[-1]:
                return f
        return None

    def poll(self, timeout=1.0):
        """收一帧并解析，返回 (cmd, payload) 或 None"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            f = self._read_frame()
            if f is None:
                continue
            return f[3], f[4:-1]
        return None

    @staticmethod
    def parse_status(payload):
        """STATUS 帧 → (seq, rpm[4], enc[4], flags)"""
        seq = payload[0]
        rpm = struct.unpack_from("<hhhh", payload, 1)
        enc = struct.unpack_from("<iiii", payload, 9)
        flags = payload[25]
        return seq, rpm, enc, flags


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--baud", type=int, default=115200)
    args = ap.parse_args()

    car = Chassis(args.port, args.baud)

    # 前进 200mm/s
    print("前进 200mm/s")
    car.set_vel(200, 0, 0)
    time.sleep(1.5)

    # 左移 200mm/s
    print("左移 200mm/s")
    car.set_vel(0, 200, 0)
    time.sleep(1.5)

    # 原地左转 30°/s
    print("原地左转 30°/s")
    car.set_vel(0, 0, 300)
    time.sleep(1.5)

    # 停止
    print("停止")
    car.stop()

    # 持续打印心跳 STATUS（约 100ms 一帧）
    print("接收心跳状态（Ctrl+C 退出）...")
    try:
        while True:
            r = car.poll()
            if r is None:
                continue
            cmd, payload = r
            if cmd == CMD_STATUS:
                seq, rpm, enc, flags = car.parse_status(payload)
                print(f"[STATUS seq={seq}] rpm={rpm} enc={enc} flags=0x{flags:02x}")
    except KeyboardInterrupt:
        car.stop()


if __name__ == "__main__":
    main()
