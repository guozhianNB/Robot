#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读安全探针：按 8 个 45° 扇区报告最近障碍距离（板卡上跑，用前先 source 环境）。

用法:
    python3 tools/zone_clearance.py [--secs 6] [--front-min 0.5]

注意: /scan 是 BEST_EFFORT QoS，订阅必须显式设 ReliabilityPolicy.BEST_EFFORT，
      否则静默收不到数据（默认 RELIABLE 不兼容）。
"""
import argparse
import math
import sys
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from sensor_msgs.msg import LaserScan

NAMES = ['-180~-135', '-135~-90', '-90~-45', '-45~0',
         '0~45', '45~90', '90~135', '135~180']


class Probe(Node):
    def __init__(self):
        super().__init__('zone_clearance')
        self.best = {}
        qos = QoSProfile(depth=10,
                         reliability=ReliabilityPolicy.BEST_EFFORT,
                         history=HistoryPolicy.KEEP_LAST)
        self.create_subscription(LaserScan, '/scan', self.cb, qos)

    def cb(self, m):
        for i, r in enumerate(m.ranges):
            if not math.isfinite(r) or r < 0.04:
                continue
            a = math.degrees(m.angle_min + i * m.angle_increment)
            k = int((a + 180.0) // 45.0)
            if 0 <= k < 8 and r < self.best.get(k, 99.0):
                self.best[k] = r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--secs', type=float, default=6.0)
    ap.add_argument('--front-min', type=float, default=0.5,
                    help='正前方扇区(-45~45)要求的余量，低于则退出码非 0')
    args = ap.parse_args()

    rclpy.init()
    n = Probe()
    t0 = time.time()
    while time.time() - t0 < args.secs:
        rclpy.spin_once(n, timeout_sec=0.2)

    print('扇区(角度范围，0=正前，逆时针为正) 最近障碍距离 m:')
    for k in range(8):
        v = n.best.get(k)
        print(f'  {NAMES[k]:>10s} : ' + (f'{v:.2f}' if v else 'no-return'))
    front = [n.best.get(3), n.best.get(4)]
    front = [v for v in front if v is not None]
    rclpy.shutdown()

    if not front:
        print('[FAIL] 正前方无回波（雷达被挡或朝向异常）')
        return 2
    m = min(front)
    if m < args.front_min:
        print(f'[FAIL] 正前方最近障碍 {m:.2f} m < {args.front_min} m，别前进')
        return 1
    print(f'[OK] 正前方余量 {m:.2f} m')
    return 0


if __name__ == '__main__':
    sys.exit(main())
